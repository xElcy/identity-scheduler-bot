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
from .ui import ScheduleView, build_personal_embed, build_result_embed, schedule_page_count
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
        logger.info("Logged in as %s", self.user)


bot = ScheduleBot()
schedule_group = app_commands.Group(name="schedule", description="個人専用チャンネルで日程を調整します")


@schedule_group.command(name="create", description="新しい個人用日程表を作成します")
@app_commands.describe(
    title="例：9月クラン戦の都合確認",
    dates="日付をカンマ区切り。例：9/20, 9/21, 9/22",
    blocks="時間帯をカンマ区切り。例：昼 12-18時, 夜 18-24時",
)
async def schedule_create(interaction: discord.Interaction, title: str, dates: str, blocks: str) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    if not _is_manager(interaction):
        await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        return

    try:
        parsed_dates = parse_schedule_dates(dates, TIMEZONE)
        parsed_blocks = parse_schedule_blocks(blocks)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    if len(parsed_dates) > 21:
        await interaction.response.send_message("日付は最大21日までにしてください。", ephemeral=True)
        return

    cells = [
        {
            "date_key": date_key,
            "date_label": date_label,
            "block_key": block_key,
            "block_label": block_label,
        }
        for date_key, date_label in parsed_dates
        for block_key, block_label in parsed_blocks
    ]
    await interaction.response.defer(ephemeral=True)
    schedule_id = bot.db.create_schedule(guild.id, interaction.user.id, title, cells)

    members = [member for member in guild.members if not member.bot]
    if not any(member.id == interaction.user.id for member in members):
        members.append(interaction.user)

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
    return isinstance(member, discord.Member) and member.guild_permissions.manage_guild


def _channel_slug(member: discord.Member) -> str:
    slug = re.sub(r"[^0-9A-Za-zぁ-んァ-ン一-龥_-]+", "-", member.display_name).strip("-")
    return (slug or str(member.id))[:70].lower()


async def _ensure_private_panel(guild: discord.Guild, member: discord.Member, schedule_id: int) -> discord.TextChannel:
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
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
            ),
        }
        category = None
        if PRIVATE_CATEGORY_ID and PRIVATE_CATEGORY_ID.isdigit():
            possible_category = guild.get_channel(int(PRIVATE_CATEGORY_ID))
            if isinstance(possible_category, discord.CategoryChannel):
                category = possible_category
        channel = await guild.create_text_channel(
            name=f"🔒・予定-{_channel_slug(member)}",
            category=category,
            overwrites=overwrites,
            topic="このチャンネルは本人とBOTだけが見られる個人用日程表です。",
        )

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
