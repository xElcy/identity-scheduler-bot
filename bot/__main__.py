import asyncio
import logging
import os
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from .database import Database
from .ui import (
    AdminMonitorView,
    CandidateView,
    ScheduleView,
    build_admin_monitor_embed,
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

    async def setup_hook(self) -> None:
        self.db.setup()
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
schedule_group = app_commands.Group(name="schedule", description="個人専用チャンネルで日程を調整します")


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


@schedule_group.command(name="create", description="新しい個人用日程表を作成します")
@app_commands.describe(
    title="例：9月クラン戦の都合確認",
    dates="日付をカンマ区切り。例：9/20, 9/21, 9/22",
    blocks="時間帯をカンマ区切り。例：昼 12-18時, 夜 18-24時",
    target_role="このロールを持つメンバーだけ個人鍵チャンネルを作成",
    category="個人鍵チャンネルを入れるカテゴリー。省略時は.env設定を使用",
)
async def schedule_create(
    interaction: discord.Interaction,
    title: str,
    dates: str,
    blocks: str,
    target_role: discord.Role,
    category: discord.CategoryChannel | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    if not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return

    try:
        cells = _build_schedule_cells(dates, blocks)
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
@app_commands.describe(
    title="例：9月クラン戦の都合確認",
    dates="日付をカンマ区切り。例：9/20, 9/21, 9/22",
    blocks="時間帯をカンマ区切り。例：昼 12-18時, 夜 18-24時",
)
async def schedule_edit(interaction: discord.Interaction, title: str, dates: str, blocks: str) -> None:
    guild = interaction.guild
    if guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    schedule = bot.db.active_schedule(guild.id)
    if schedule is None:
        await interaction.response.send_message("現在受付中の日程表はありません。", ephemeral=True)
        return
    try:
        cells = _build_schedule_cells(dates, blocks)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await _delete_candidate_messages(guild, schedule["id"])
    bot.db.edit_schedule(schedule["id"], title, cells)
    await _refresh_all_panels(guild, schedule["id"])
    await refresh_admin_panel(bot, bot.db, schedule["id"], guild)
    await interaction.followup.send(
        "日程表を編集しました。安全のため、これまでの回答はリセットされています。",
        ephemeral=True,
    )


@schedule_group.command(name="delete", description="受付中の日程表と個人チャンネルを削除します")
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
async def schedule_clear_voice_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    bot.db.set_voice_notify_channel(interaction.guild.id, None)
    await interaction.response.send_message(
        "VC入室通知の個別送り先を解除しました。未設定の場合は日程告知チャンネルへ送られます。",
        ephemeral=True,
    )


@schedule_group.command(name="candidates", description="回答から決定候補を管理者チャンネルに表示します")
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


@schedule_group.command(name="monitor", description="このチャンネルにリアルタイム集計パネルを設置します")
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
async def schedule_clear_voice_role(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return
    bot.db.set_voice_notify_role(interaction.guild.id, None)
    await interaction.response.send_message("VC入室通知ロールを解除しました。", ephemeral=True)


@schedule_group.command(name="setup", description="自分専用の日程表チャンネルを作成します")
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
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="🔒 クラン個人日程表BOT", color=discord.Color.blurple())
    embed.description = (
        "メンバーごとに鍵チャンネルを作り、他のメンバーには回答内容を見せずに日程を集計します。\n\n"
        "`/schedule create` — 管理者が日程表を作成\n"
        "`/schedule setup` — 自分の専用チャンネルを作成\n"
        "`/schedule result` — 管理者が集計結果を確認\n"
        "`/schedule close` — 回答を締め切る\n\n"
        "個人チャンネルのボタンを押すだけで、`— → ○ → △ → ×` と回答が切り替わります。"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _is_manager(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and (
        member.guild_permissions.manage_guild or member.guild_permissions.administrator
    )


def _is_manager_member(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator


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
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
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
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    embed = build_personal_embed(bot.db, schedule_id, member.id, 0)
    view = ScheduleView(bot.db, schedule_id, member.id, 0)
    if message is None:
        message = await channel.send(embed=embed, view=view)
    else:
        await message.edit(embed=embed, view=view)
    bot.db.save_private_profile(guild.id, member.id, channel.id, message.id)
    return channel


async def _refresh_all_panels(guild: discord.Guild, schedule_id: int) -> None:
    for profile in bot.db.private_profiles(guild.id):
        if not profile["channel_id"] or not profile["panel_message_id"]:
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


bot.tree.add_command(schedule_group)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が設定されていません。.env を作成してください。")
    asyncio.run(bot.start(TOKEN))
