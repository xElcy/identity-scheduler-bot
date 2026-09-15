import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .database import Database
from .ui import (
    AdminMonitorView,
    CandidateView,
    ScheduleView,
    announce_decision,
    build_admin_monitor_embed,
    build_announcement_embed,
    build_candidate_embed,
    build_personal_embed,
    build_result_embed,
    refresh_admin_panel,
    schedule_page_count,
)
from .utils import parse_schedule_blocks, parse_schedule_dates

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("identity-private-schedule")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")
PRIVATE_CATEGORY_ID = os.getenv("PRIVATE_CATEGORY_ID")


class ScheduleBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(Path("data") / "schedule.db")
        self.views_registered = False
        self.synced_panels: set[tuple[int, int]] = set()
        self.panel_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        self.db.setup()
        self.rolling_refresh.start()
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        if self.views_registered:
            return
        self.views_registered = True
        for guild in self.guilds:
            schedule = self.db.active_schedule(guild.id)
            if schedule is None:
                continue
            for profile in self.db.private_profiles(guild.id):
                if profile["channel_id"]:
                    for page in range(schedule_page_count(self.db, schedule["id"])):
                        self.add_view(ScheduleView(self.db, schedule["id"], profile["user_id"], page))
            for post in self.db.active_candidate_posts(guild.id):
                self.add_view(CandidateView(self.db, post["schedule_id"]), message_id=post["message_id"])
            if schedule["admin_panel_message_id"]:
                self.add_view(AdminMonitorView(self.db, schedule["id"]), message_id=schedule["admin_panel_message_id"])
        logger.info("Logged in as %s", self.user)

    @tasks.loop(seconds=30)
    async def rolling_refresh(self) -> None:
        for guild in self.guilds:
            try:
                schedule = self.db.active_schedule(guild.id)
                if not schedule or not schedule["rolling_days"]:
                    continue
                self.db.advance_rolling(schedule["id"])
                schedule = self.db.get_schedule(schedule["id"])
                revision = schedule["panel_revision"]
                key = (schedule["id"], revision)
                if key in self.synced_panels and revision == schedule["panels_synced_revision"]:
                    continue
                personal_ok = await _refresh_all_panels(guild, schedule["id"])
                management_ok = await refresh_admin_panel(self, self.db, schedule["id"], guild)
                if personal_ok and management_ok:
                    self.db.mark_panels_synced(schedule["id"], revision)
                    self.synced_panels = {entry for entry in self.synced_panels if entry[0] != schedule["id"]}
                    self.synced_panels.add(key)
                    logger.info("Rolling panels synced: schedule=%s window=%s..%s", schedule["id"], schedule["window_start"], schedule["window_end"])
            except Exception:
                logger.exception("Rolling schedule refresh failed for guild %s; will retry", guild.id)

    @rolling_refresh.before_loop
    async def before_rolling_refresh(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        self.rolling_refresh.cancel()
        await super().close()

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or after.channel is None:
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return
        if self.db.is_voice_channel_ignored(member.guild.id, after.channel.id):
            return

        # Notify only when this member is the first human in a new voice session.
        # Voice channel member lists include bots, so ignore bot accounts here.
        human_members = [voice_member for voice_member in after.channel.members if not voice_member.bot]
        if len(human_members) != 1 or human_members[0].id != member.id:
            return

        role_id = self.db.voice_notify_role_id(member.guild.id)
        channel_id = self.db.voice_notify_channel_id(member.guild.id)
        if channel_id is None:
            channel_id = self.db.announcement_channel_id(member.guild.id)
        if role_id is None or channel_id is None:
            return
        role = member.guild.get_role(role_id)
        channel = member.guild.get_channel(channel_id)
        if role is None or not isinstance(channel, discord.TextChannel):
            return

        await channel.send(
            f"🔊 {member.display_name} さんが **{after.channel.name}** に入りました。 {role.mention}",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )


bot = ScheduleBot()


class AdminScheduleGroup(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and _is_manager(interaction):
            return True
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return False


schedule_group = AdminScheduleGroup(name="schedule", description="個人専用チャンネルで日程を調整します", default_permissions=discord.Permissions(administrator=True), guild_only=True)


def _build_schedule_cells(dates: str, blocks: str) -> list[dict]:
    parsed_dates = parse_schedule_dates(dates, TIMEZONE)
    parsed_blocks = parse_schedule_blocks(blocks)
    if len(parsed_dates) > 21:
        raise ValueError("日付は最大21日までにしてください。")
    return [
        {
            "date_key": date_key,
            "date_label": date_label,
            "block_key": block_key,
            "block_label": block_label,
        }
        for date_key, date_label in parsed_dates
        for block_key, block_label in parsed_blocks
    ]


def _week_cells(blocks: str) -> list[dict]:
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    return _build_schedule_cells(",".join((today + timedelta(days=i)).isoformat() for i in range(7)), blocks)


@schedule_group.command(name="create", description="新しい個人用日程表を作成します")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    title="例：9月クラン戦の都合確認",
    dates="省略すると今日から7日間を毎日自動更新。固定日程の場合だけ指定",
    blocks="時間帯をカンマ区切り。例：昼 12-18時, 夜 18-24時",
    target_role="このロールを持つメンバーだけ個人鍵チャンネルを作成",
    category="個人鍵チャンネルを入れるカテゴリー。省略時は.env設定を使用",
)
async def schedule_create(
    interaction: discord.Interaction,
    title: str,
    blocks: str,
    target_role: discord.Role,
    dates: str | None = None,
    category: discord.CategoryChannel | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    if not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return

    if bot.db.active_schedule(guild.id):
        await interaction.response.send_message("受付中の予定表があります。1人追加は `/schedule add-member`、変更は `/schedule edit` を使ってください。", ephemeral=True)
        return
    if target_role.is_default():
        await interaction.response.send_message("メンバー専用の対象ロールを指定してください。", ephemeral=True)
        return
    try:
        cells = _build_schedule_cells(dates, blocks) if dates else _week_cells(blocks)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    category_id = category.id if category else None
    if category_id is None and PRIVATE_CATEGORY_ID and PRIVATE_CATEGORY_ID.isdigit():
        category_id = int(PRIVATE_CATEGORY_ID)
    schedule_id = bot.db.create_schedule(
        guild.id,
        interaction.user.id,
        title,
        cells,
        target_role_id=target_role.id,
        category_id=category_id,
    )
    if dates is None:
        bot.db.enable_rolling(schedule_id, TIMEZONE)

    members = [member for member in guild.members if not member.bot and target_role in member.roles]

    created = 0
    failed = 0
    for member in members:
        try:
            await _ensure_private_panel(guild, member, schedule_id)
            created += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
            logger.exception("Could not create private panel for %s", member.id)

    result = f"個人用の日程表を {created}人分 作成しました。"
    if failed:
        result += f" {failed}人分は権限不足などで作成できませんでした。"
    await interaction.followup.send(result, ephemeral=True)


@schedule_group.command(name="edit", description="受付中の日程表を編集します")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    title="例：9月クラン戦の都合確認",
    dates="日付をカンマ区切り。例：9/20, 9/21, 9/22",
    blocks="時間帯をカンマ区切り。例：昼 12-18時, 夜 18-24時",
)
async def schedule_edit(interaction: discord.Interaction, title: str | None = None, dates: str | None = None, blocks: str | None = None) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    if schedule["rolling_days"] and dates is not None:
        await interaction.response.send_message("毎日自動更新中は日付の指定が不要です。タイトル・時間帯のみ変更できます。", ephemeral=True)
        return
    existing = bot.db.schedule_cells(schedule["id"])
    blocks = blocks or ",".join(dict.fromkeys(cell["block_label"] for cell in existing))
    dates = dates or ",".join(dict.fromkeys(cell["date_key"] for cell in existing))
    try:
        cells = _week_cells(blocks) if schedule["rolling_days"] else _build_schedule_cells(dates, blocks)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    bot.db.edit_schedule(schedule["id"], title or schedule["title"], cells)
    if schedule["rolling_days"]:
        bot.db.enable_rolling(schedule["id"], TIMEZONE)
    await _refresh_all_panels(guild, schedule["id"])
    await refresh_admin_panel(bot, bot.db, schedule["id"], guild)
    await interaction.followup.send(
        "日程表を編集しました。同じ日付・同じ時間帯の回答は引き継ぎ、表示から外れた回答も履歴に保存しています。",
        ephemeral=True,
    )


@schedule_group.command(name="add-member", description="指定メンバー1人だけ専用部屋と回答パネルを追加します")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="対象ロールが付いた追加メンバー")
async def schedule_add_member(interaction: discord.Interaction, member: discord.Member) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("受付中の予定表がありません。`/schedule rolling` で既存の予定表を再開できます。", ephemeral=True)
        return
    if member.bot or member.guild.id != guild.id:
        await interaction.response.send_message("このサーバーのメンバーを指定してください。", ephemeral=True)
        return
    if not schedule["target_role_id"] or not any(role.id == schedule["target_role_id"] for role in member.roles):
        await interaction.response.send_message("先にそのメンバーへ予定表の対象ロールを付けてください。", ephemeral=True)
        return
    if schedule["category_id"] and not isinstance(guild.get_channel(schedule["category_id"]), discord.CategoryChannel):
        await interaction.response.send_message("予定表のカテゴリーが見つかりません。カテゴリー設定を確認してください。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    bot.db.advance_rolling(schedule["id"])
    try:
        channel = await _ensure_private_panel(guild, member, schedule["id"])
    except discord.HTTPException:
        logger.exception("Could not add member %s", member.id)
        await interaction.followup.send("部屋・パネルを作成できませんでした。BOTのカテゴリー権限を確認して再実行してください。", ephemeral=True)
        return
    await refresh_admin_panel(bot, bot.db, schedule["id"], guild)
    await interaction.followup.send(f"{member.display_name} さんの予定表を用意しました：{channel.mention}\n既存の部屋があれば再利用します。", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@schedule_group.command(name="rolling", description="既存の回答を残して、今日から7日間の自動更新を開始・再開します")
@app_commands.default_permissions(administrator=True)
async def schedule_rolling(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id) or bot.db.latest_schedule(guild.id)
    if not schedule:
        await interaction.response.send_message("最初に `/schedule create` で予定表を作成してください。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        bot.db.enable_rolling(schedule["id"], TIMEZONE, reopen=True)
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    await _refresh_all_panels(guild, schedule["id"])
    await refresh_admin_panel(bot, bot.db, schedule["id"], guild)
    await interaction.followup.send("今日から7日間の自動更新を開始しました。回答は保存し、日付が変わると先の1日が追加されます。", ephemeral=True)


@schedule_group.command(name="history", description="過去の日付に保存された回答を管理者だけに表示します")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(date="履歴の日付。例：2026-09-14")
async def schedule_history(interaction: discord.Interaction, date: str) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(interaction.guild.id) or bot.db.latest_schedule(interaction.guild.id)
    if not schedule:
        await interaction.response.send_message("予定表がありません。", ephemeral=True)
        return
    try:
        parsed = parse_schedule_dates(date, TIMEZONE)
        if len(parsed) != 1:
            raise ValueError("日付は1日だけ指定してください。")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(embed=build_result_embed(bot.db, schedule["id"], parsed[0][0]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@schedule_group.command(name="delete", description="受付中の日程表と個人チャンネルを削除します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_delete(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if bot.db.active_schedule(interaction.guild.id) is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    await interaction.response.send_message(
        "現在の予定表と、登録されている個人用鍵チャンネルをすべて削除します。\n本当に実行しますか？",
        ephemeral=True,
        view=DeleteScheduleView(),
    )


@schedule_group.command(name="set-announcement", description="このチャンネルを確定日程の告知先にします")
@app_commands.default_permissions(manage_guild=True)
async def schedule_set_announcement(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("通常のテキストチャンネルで実行してください。", ephemeral=True)
        return
    bot.db.set_announcement_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(
        f"確定日程の告知先を {interaction.channel.mention} に設定しました。", ephemeral=True
    )


@schedule_group.command(name="set-voice-channel", description="このチャンネルをVC入室通知の送り先にします")
@app_commands.default_permissions(manage_guild=True)
async def schedule_set_voice_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("通常のテキストチャンネルで実行してください。", ephemeral=True)
        return
    bot.db.set_voice_notify_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(
        f"VC入室通知の送り先を {interaction.channel.mention} に設定しました。", ephemeral=True
    )


@schedule_group.command(name="clear-voice-channel", description="VC入室通知の送り先を解除します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_clear_voice_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    bot.db.set_voice_notify_channel(interaction.guild.id, None)
    await interaction.response.send_message(
        "VC入室通知の個別送り先を解除しました。未設定の場合は日程告知チャンネルへ送られます。",
        ephemeral=True,
    )


@schedule_group.command(name="ignore-voice-channel", description="指定したVCでは入室通知を送らないようにします")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="入室通知を鳴らさないVC")
async def schedule_ignore_voice_channel(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    bot.db.ignore_voice_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"{channel.mention} をVC入室通知の対象外にしました。",
        ephemeral=True,
    )


@schedule_group.command(name="unignore-voice-channel", description="指定したVCの入室通知を再び有効にします")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="入室通知を再び有効にするVC")
async def schedule_unignore_voice_channel(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    removed = bot.db.unignore_voice_channel(interaction.guild.id, channel.id)
    if removed:
        message = f"{channel.mention} のVC入室通知を再び有効にしました。"
    else:
        message = f"{channel.mention} は通知対象外に登録されていません。"
    await interaction.response.send_message(message, ephemeral=True)


@schedule_group.command(name="candidates", description="回答から決定候補を管理者チャンネルに表示します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_candidates(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("管理者用の通常テキストチャンネルで実行してください。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    message = await interaction.channel.send(
        embed=build_candidate_embed(bot.db, schedule["id"]),
        view=CandidateView(bot.db, schedule["id"]),
    )
    bot.db.create_candidate_post(schedule["id"], interaction.channel.id, message.id)
    await interaction.followup.send("このチャンネルに決定候補を表示しました。", ephemeral=True)


@schedule_group.command(name="decide", description="管理者が指定した日付・時間帯を最終決定して告知します")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    date="決定する日付。例：9/21 または 2026-09-21",
    block="決定する時間帯。作成時に入力した表記をそのまま指定",
)
async def schedule_decide(interaction: discord.Interaction, date: str, block: str) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    channel_id = bot.db.announcement_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "先に日程確定部屋で `/schedule set-announcement` を実行してください。",
            ephemeral=True,
        )
        return

    try:
        parsed_date = parse_schedule_dates(date, TIMEZONE)
        if len(parsed_date) != 1:
            raise ValueError("日付は1日だけ指定してください。")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    date_key = parsed_date[0][0]
    normalized_block = block.strip().casefold()
    cells = bot.db.schedule_cells(schedule["id"])
    cell = next(
        (
            item
            for item in cells
            if item["date_key"] == date_key
            and (
                item["block_label"].strip().casefold() == normalized_block
                or item["block_key"].strip().casefold() == normalized_block
            )
        ),
        None,
    )
    if cell is None:
        available_blocks = sorted({item["block_label"] for item in cells if item["date_key"] == date_key})
        block_hint = ", ".join(available_blocks) if available_blocks else "その日付は候補にありません"
        await interaction.response.send_message(
            f"指定された日付・時間帯が見つかりません。利用可能な時間帯：{block_hint}",
            ephemeral=True,
        )
        return

    if await announce_decision(interaction, bot.db, schedule["id"], cell["id"]):
        if not schedule["rolling_days"]:
            await _refresh_all_panels(guild, schedule["id"])


@schedule_group.command(name="monitor", description="このチャンネルにリアルタイム集計パネルを設置します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_monitor(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("運営用の通常テキストチャンネルで実行してください。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    message = None
    if schedule["admin_panel_message_id"]:
        panel_channel = guild.get_channel(schedule["admin_panel_channel_id"])
        if isinstance(panel_channel, discord.TextChannel):
            try:
                message = await panel_channel.fetch_message(schedule["admin_panel_message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
    if message is None or message.channel.id != interaction.channel.id:
        message = await interaction.channel.send(
            embed=build_admin_monitor_embed(bot.db, schedule["id"], guild),
            view=AdminMonitorView(bot.db, schedule["id"]),
        )
    else:
        await message.edit(
            embed=build_admin_monitor_embed(bot.db, schedule["id"], guild),
            view=AdminMonitorView(bot.db, schedule["id"]),
        )
    bot.db.set_admin_panel(schedule["id"], interaction.channel.id, message.id)
    await interaction.followup.send("リアルタイム集計パネルを設置しました。", ephemeral=True)


@schedule_group.command(name="set-voice-role", description="VC入室通知を受け取るロールを設定します")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(role="VC入室通知を受け取るロール")
async def schedule_set_voice_role(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    if role.is_default():
        await interaction.response.send_message("@everyone は通知ロールに設定できません。", ephemeral=True)
        return
    bot.db.set_voice_notify_role(interaction.guild.id, role.id)
    await interaction.response.send_message(
        f"VC入室通知の対象を {role.mention} に設定しました。\n"
        "VC通知チャンネルで、そのロールが見られる権限も確認してください。",
        ephemeral=True,
    )


@schedule_group.command(name="clear-voice-role", description="VC入室通知ロールを解除します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_clear_voice_role(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    bot.db.set_voice_notify_role(interaction.guild.id, None)
    await interaction.response.send_message("VC入室通知ロールを解除しました。", ephemeral=True)


@schedule_group.command(name="setup", description="自分専用の日程表チャンネルを作成します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_setup(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    if schedule["target_role_id"]:
        target_role = guild.get_role(schedule["target_role_id"])
        if target_role is not None and (not isinstance(interaction.user, discord.Member) or target_role not in interaction.user.roles):
            await interaction.response.send_message("この日程表の対象ロールが必要です。", ephemeral=True)
            return
    await interaction.response.defer(ephemeral=True)
    channel = await _ensure_private_panel(guild, interaction.user, schedule["id"])
    await interaction.followup.send(f"専用チャンネルを用意しました：{channel.mention}", ephemeral=True)


@schedule_group.command(name="result", description="全員分の回答結果を確認します")
@app_commands.default_permissions(manage_guild=True)
async def schedule_result(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_result_embed(bot.db, schedule["id"]), ephemeral=True)


@schedule_group.command(name="close", description="全員の日程回答を締め切ります")
@app_commands.default_permissions(manage_guild=True)
async def schedule_close(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    bot.db.close_schedule(schedule["id"])
    await _refresh_all_panels(guild, schedule["id"])
    await refresh_admin_panel(bot, bot.db, schedule["id"], guild)
    await interaction.followup.send("全員の日程回答を締め切りました。", ephemeral=True)


@bot.tree.command(name="help", description="BOTの使い方を表示します")
@app_commands.default_permissions(administrator=True)
async def help_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    embed = discord.Embed(title="🔒 クラン個人日程表BOT", color=discord.Color.blurple())
    embed.description = (
        "メンバーごとに鍵チャンネルを作り、他のメンバーには回答内容を見せずに日程を集計します。\n\n"
        "`/schedule create` — 管理者が日程表を作成\n"
        "`/schedule setup` — 自分の専用チャンネルを作成\n"
        "`/schedule add-member member:@メンバー` — 1人だけ追加\n"
        "`/schedule rolling` — 回答を残して7日間の自動更新を開始・再開\n"
        "`/schedule history date:2026-09-14` — 過去の回答を確認\n"
        "`/schedule decide` — @everyone 付きで日程確定を告知\n"
        "`/schedule result` — 管理者が集計結果を確認\n"
        "`/schedule close` — 回答を締め切る\n\n"
        "個人チャンネルのボタンを押すだけで、`— → ○ → △ → ×` と回答が切り替わります。"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _is_manager(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and (
        member.guild_permissions.administrator
    )


def _is_manager_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def _channel_slug(member: discord.Member) -> str:
    slug = re.sub(r"[^0-9A-Za-zぁ-んァ-ン一-龥_-]+", "-", member.display_name).strip("-")
    return (slug or str(member.id))[:70].lower()


class DeleteScheduleView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=90)

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not _is_manager(interaction):
            await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
            return
        await interaction.response.defer()
        deleted, failed = await _delete_active_schedule(interaction.guild)
        result = f"予定表と個人チャンネルを削除しました。削除数：{deleted}件"
        if failed:
            result += f"、権限不足などで削除できなかったチャンネル：{failed}件"
        await interaction.edit_original_response(content=result, view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="削除をキャンセルしました。", view=None)
        self.stop()


async def _delete_candidate_messages(guild: discord.Guild, schedule_id: int) -> None:
    for post in bot.db.candidate_posts(schedule_id):
        channel = guild.get_channel(post["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            message = await channel.fetch_message(post["message_id"])
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Could not delete candidate post %s", post["message_id"])


async def _delete_active_schedule(guild: discord.Guild) -> tuple[int, int]:
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        return 0, 0

    await _delete_candidate_messages(guild, schedule["id"])
    if schedule["admin_panel_channel_id"] and schedule["admin_panel_message_id"]:
        panel_channel = guild.get_channel(schedule["admin_panel_channel_id"])
        if isinstance(panel_channel, discord.TextChannel):
            try:
                panel_message = await panel_channel.fetch_message(schedule["admin_panel_message_id"])
                await panel_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Could not delete admin monitor panel %s", schedule["admin_panel_message_id"])
    deleted = 0
    failed = 0
    for profile in bot.db.private_profiles(guild.id):
        channel = guild.get_channel(profile["channel_id"]) if profile["channel_id"] else None
        if channel is None and profile["channel_id"]:
            try:
                channel = await bot.fetch_channel(profile["channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            await channel.delete(reason="日程表の一括削除")
            deleted += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            failed += 1

    bot.db.delete_schedule(schedule["id"])
    bot.db.clear_private_profiles(guild.id)
    return deleted, failed


async def _ensure_private_panel(guild: discord.Guild, member: discord.Member, schedule_id: int) -> discord.TextChannel:
    lock = bot.panel_locks.setdefault((guild.id, member.id), asyncio.Lock())
    async with lock:
        return await _ensure_private_panel_locked(guild, member, schedule_id)


async def _ensure_private_panel_locked(guild: discord.Guild, member: discord.Member, schedule_id: int) -> discord.TextChannel:
    schedule = bot.db.get_schedule(schedule_id)
    if schedule is None:
        raise RuntimeError("Schedule is not available")
    profile = bot.db.private_profile(guild.id, member.id)
    channel = None
    if profile and profile["channel_id"]:
        channel = guild.get_channel(profile["channel_id"])
        if channel is None:
            try:
                channel = await bot.fetch_channel(profile["channel_id"])
            except discord.NotFound:
                channel = None

    if not isinstance(channel, discord.TextChannel):
        bot_member = guild.me
        if bot_member is None:
            raise RuntimeError("Bot member is not available")
        manager_overwrite = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
            ),
        }
        for manager in guild.members:
            if not manager.bot and manager.id != member.id and _is_manager_member(manager):
                overwrites[manager] = manager_overwrite
        category = None
        category_id = schedule.get("category_id")
        if category_id is None and PRIVATE_CATEGORY_ID and PRIVATE_CATEGORY_ID.isdigit():
            category_id = int(PRIVATE_CATEGORY_ID)
        if category_id:
            possible_category = guild.get_channel(category_id)
            if isinstance(possible_category, discord.CategoryChannel):
                category = possible_category
        channel = await guild.create_text_channel(
            name=f"🔒・予定-{_channel_slug(member)}",
            category=category,
            overwrites=overwrites,
            topic="このチャンネルは対象本人・運営・BOTだけが見られる個人用日程表です。",
        )
        # Persist the room before sending its panel so retries cannot duplicate it.
        bot.db.save_private_profile(guild.id, member.id, channel.id, 0)
    else:
        category_id = schedule.get("category_id")
        if category_id is None and PRIVATE_CATEGORY_ID and PRIVATE_CATEGORY_ID.isdigit():
            category_id = int(PRIVATE_CATEGORY_ID)
        if category_id:
            possible_category = guild.get_channel(category_id)
            if isinstance(possible_category, discord.CategoryChannel) and channel.category_id != possible_category.id:
                await channel.edit(category=possible_category, reason="日程表カテゴリーを更新")
        manager_overwrite = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
        for manager in guild.members:
            if not manager.bot and manager.id != member.id and _is_manager_member(manager):
                await channel.set_permissions(manager, overwrite=manager_overwrite)

    message = None
    if profile and profile["panel_message_id"]:
        try:
            message = await channel.fetch_message(profile["panel_message_id"])
        except discord.NotFound:
            message = None

    embed = build_personal_embed(bot.db, schedule_id, member.id, 0)
    view = ScheduleView(bot.db, schedule_id, member.id, 0)
    if message is None:
        message = await channel.send(embed=embed, view=view)
    else:
        await message.edit(embed=embed, view=view)
    bot.db.save_private_profile(guild.id, member.id, channel.id, message.id)
    return channel


async def _refresh_all_panels(guild: discord.Guild, schedule_id: int) -> bool:
    success = True
    schedule = bot.db.get_schedule(schedule_id)
    for profile in bot.db.private_profiles(guild.id):
        if not profile["channel_id"] or not profile["panel_message_id"]:
            continue
        member = guild.get_member(profile["user_id"])
        if member is None or member.bot or (schedule["target_role_id"] and not any(role.id == schedule["target_role_id"] for role in member.roles)):
            continue
        channel = guild.get_channel(profile["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            message = await channel.fetch_message(profile["panel_message_id"])
            await message.edit(
                embed=build_personal_embed(bot.db, schedule_id, profile["user_id"], 0),
                view=ScheduleView(bot.db, schedule_id, profile["user_id"], 0),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Could not refresh private panel in %s", channel.id)
            success = False
    return success


bot.tree.add_command(schedule_group)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が設定されていません。.env を作成してください。")
    asyncio.run(bot.start(TOKEN))
