import asyncio
import logging
from collections import OrderedDict

import discord

from .database import Database


STATUS_SYMBOLS = {
    "blank": "—",
    "available": "○",
    "maybe": "△",
    "unavailable": "×",
}
STATUS_STYLES = {
    "blank": discord.ButtonStyle.secondary,
    "available": discord.ButtonStyle.success,
    "maybe": discord.ButtonStyle.primary,
    "unavailable": discord.ButtonStyle.danger,
}
NEXT_STATUS = {
    "blank": "available",
    "available": "maybe",
    "maybe": "unavailable",
    "unavailable": "blank",
}

logger = logging.getLogger(__name__)
_refresh_locks: dict[tuple[int, int], asyncio.Lock] = {}


def schedule_days_per_page(db: Database, schedule_id: int) -> int:
    schedule = db.get_schedule(schedule_id)
    groups = _group_cells(db.schedule_cells(schedule_id))
    blocks = max((len(cells) for cells in groups.values()), default=1)
    return min(7, 20 // blocks) if schedule and schedule["rolling_days"] else 4


def _group_cells(cells: list[dict]) -> OrderedDict[str, list[dict]]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for cell in cells:
        groups.setdefault(cell["date_key"], []).append(cell)
    return groups


def schedule_page_count(db: Database, schedule_id: int) -> int:
    days = schedule_days_per_page(db, schedule_id)
    return max(1, (len(_group_cells(db.schedule_cells(schedule_id))) + days - 1) // days)


def build_personal_embed(db: Database, schedule_id: int, user_id: int, page: int) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    groups = _group_cells(db.schedule_cells(schedule_id))
    dates = list(groups.items())
    page_count = schedule_page_count(db, schedule_id)
    page = min(max(page, 0), page_count - 1)
    days = schedule_days_per_page(db, schedule_id)
    current_dates = dates[page * days:(page + 1) * days]

    embed = discord.Embed(
        title=f"🔒 {schedule['title']}",
        description=(
            "このチャンネルはあなた専用です。\n"
            "各ボタンを押すと `— → ○ → △ → × → —` の順に切り替わります。\n"
            "○ 行ける　△ 未定　× 行けない"
        ),
        color=discord.Color.from_rgb(88, 101, 242) if schedule["is_open"] else discord.Color.greyple(),
    )
    if schedule["rolling_days"]:
        embed.description += "\n📅 今日から7日間・毎日自動更新／入力済みの回答は保存されます。"
    for _, cells in current_dates:
        lines = []
        for cell in cells:
            status = db.schedule_answer(schedule_id, cell["id"], user_id)
            lines.append(f"`{cell['block_label']}`　**{STATUS_SYMBOLS[status]}**")
        embed.add_field(name=f"📅 {cells[0]['date_label']}", value="\n".join(lines), inline=True)
    embed.set_footer(text=f"ページ {page + 1}/{page_count}　|　回答受付中" if schedule["is_open"] else "回答締切")
    return embed


def build_result_embed(db: Database, schedule_id: int, history_date: str | None = None) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    embed = discord.Embed(
        title=f"📊 集計結果｜{schedule['title']}",
        description="各候補の回答数です。最も参加可能者が多い候補には ⭐ を付けています。",
        color=discord.Color.gold(),
    )
    cells = db.schedule_cells(schedule_id, include_history=bool(history_date))
    if history_date:
        cells = [cell for cell in cells if cell["date_key"] == history_date]
        embed.title = f"📚 回答履歴｜{history_date}"
    counts = [(cell, db.schedule_answer_counts(schedule_id, cell["id"])) for cell in cells]
    best = max((item[1]["available"] for item in counts), default=0)
    groups = _group_cells(cells)
    for date_key, date_cells in groups.items():
        lines = []
        for cell in date_cells:
            result = next(item[1] for item in counts if item[0]["id"] == cell["id"])
            star = "⭐ " if result["available"] == best and best > 0 else ""
            names = []
            for status, icon in (("available", "✅"), ("maybe", "❓"), ("unavailable", "❌")):
                users = db.schedule_answer_users(schedule_id, cell["id"], status)
                mentions = " ".join(f"<@{user_id}>" for user_id in users)
                if len(mentions) > 180:
                    mentions = mentions[:177] + "…"
                names.append(f"{icon} {result[status]}人 {mentions or '—'}")
            lines.append(
                f"{star}`{cell['block_label']}`\n" + "　".join(names)
            )
        embed.add_field(name=f"📅 {date_cells[0]['date_label']}", value="\n".join(lines), inline=False)
    embed.set_footer(text="✅ 行ける　❓ 未定　❌ 行けない")
    return embed


def candidate_cells(db: Database, schedule_id: int, limit: int = 10) -> list[tuple[dict, dict]]:
    """Return the best schedule cells, ordered for the manager decision panel."""
    ranked = [(cell, db.schedule_answer_counts(schedule_id, cell["id"])) for cell in db.schedule_cells(schedule_id)]
    ranked.sort(key=lambda item: (-item[1]["available"], -item[1]["maybe"], item[0]["date_key"], item[0]["cell_order"]))
    return ranked[:limit]


def build_candidate_embed(db: Database, schedule_id: int) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    ranked = candidate_cells(db, schedule_id)
    decided = db.decided_cell_ids(schedule_id)
    embed = discord.Embed(
        title=f"🏆 決定候補｜{schedule['title']}",
        description=(
            "回答状況から、参加可能者が多い順に候補を表示しています。\n"
            "人気順は参考情報です。最終決定は管理者が `/schedule decide` で行えます。"
        ),
        color=discord.Color.gold(),
    )
    if not ranked:
        embed.description = "候補日時がありません。日程を編集してください。"
        return embed

    lines = []
    for index, (cell, counts) in enumerate(ranked, start=1):
        lines.append(
            f"**{index}. {cell['date_label']}｜{cell['block_label']}**{' ・告知済み' if cell['id'] in decided else ''}\n"
            f"✅ {counts['available']}人　△ {counts['maybe']}人　❌ {counts['unavailable']}人"
        )
    for offset in range(0, len(lines), 5):
        embed.add_field(name="候補一覧" if offset == 0 else "候補一覧（続き）", value="\n\n".join(lines[offset:offset + 5]), inline=False)
    embed.set_footer(text="候補は上位10件まで表示 | 個人の回答内容は管理者だけが確認できます")
    return embed


def build_announcement_embed(db: Database, schedule_id: int, cell_id: int, guild: discord.Guild) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    cell = next((item for item in db.schedule_cells(schedule_id) if item["id"] == cell_id), None)
    if schedule is None or cell is None:
        return discord.Embed(title="確定日程", description="日程情報を取得できませんでした。", color=discord.Color.red())

    available = db.schedule_answer_users(schedule_id, cell_id, "available")
    maybe = db.schedule_answer_users(schedule_id, cell_id, "maybe")
    available_names = [guild.get_member(user_id).display_name for user_id in available if guild.get_member(user_id)]
    maybe_names = [guild.get_member(user_id).display_name for user_id in maybe if guild.get_member(user_id)]
    embed = discord.Embed(
        title=f"📢 日程確定｜{schedule['title']}",
        description=f"**{cell['date_label']}　{cell['block_label']}**\nこの日時で決定しました！",
        color=discord.Color.green(),
    )
    embed.add_field(name=f"参加予定（{len(available)}人）", value="、".join(available_names) or "回答者なし", inline=False)
    if maybe_names:
        embed.add_field(name=f"未定（{len(maybe)}人）", value="、".join(maybe_names), inline=False)
    embed.set_footer(text="日程調整BOT")
    return embed


def build_admin_monitor_embed(db: Database, schedule_id: int, guild: discord.Guild) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    target_role = guild.get_role(schedule["target_role_id"]) if schedule["target_role_id"] else None
    eligible_members = [
        member
        for member in guild.members
        if not member.bot and (target_role is None or target_role in member.roles)
    ]
    answered = set(db.schedule_answered_users(schedule_id))
    target_label = f"@{target_role.name}" if target_role else "対象ロール未設定"
    status_label = "回答受付中" if schedule["is_open"] else "回答締切"
    if schedule["rolling_days"]:
        status_label += "・今日から7日間／毎日自動更新"
    embed = discord.Embed(
        title=f"📊 リアルタイム集計｜{schedule['title']}",
        description=(
            f"対象：**{target_label}**　|　{status_label}\n"
            f"回答済み：**{len(answered & {member.id for member in eligible_members})}/{len(eligible_members)}人**\n"
            "個人の回答が変更されるたびに、このパネルも更新されます。"
        ),
        color=discord.Color.blurple() if schedule["is_open"] else discord.Color.greyple(),
    )
    groups = _group_cells(db.schedule_cells(schedule_id))
    total = len(eligible_members)
    for _date_key, date_cells in groups.items():
        lines = []
        for cell in date_cells:
            counts = db.schedule_answer_counts(schedule_id, cell["id"])
            answered_count = counts["available"] + counts["maybe"] + counts["unavailable"]
            unanswered = max(0, total - answered_count)
            lines.append(
                f"`{cell['block_label']}`　✅ {counts['available']}　△ {counts['maybe']}　"
                f"❌ {counts['unavailable']}　— {unanswered}"
            )
        embed.add_field(name=f"📅 {date_cells[0]['date_label']}", value="\n".join(lines), inline=False)
    embed.set_footer(text="✅ 行ける　△ 未定　❌ 行けない　— 未回答")
    return embed


async def refresh_admin_panel(client: discord.Client, db: Database, schedule_id: int, guild: discord.Guild) -> bool:
    lock = _refresh_locks.setdefault((guild.id, schedule_id), asyncio.Lock())
    async with lock:
        return await _refresh_management_panels(client, db, schedule_id, guild)


async def _refresh_management_panels(client: discord.Client, db: Database, schedule_id: int, guild: discord.Guild) -> bool:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return True
    posts = [(post["channel_id"], post["message_id"], False) for post in db.candidate_posts(schedule_id)]
    if schedule["admin_panel_channel_id"] and schedule["admin_panel_message_id"]:
        posts.append((schedule["admin_panel_channel_id"], schedule["admin_panel_message_id"], True))
    success = True
    for channel_id, message_id, monitor in posts:
        try:
            channel = guild.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=build_admin_monitor_embed(db, schedule_id, guild) if monitor else build_candidate_embed(db, schedule_id),
                view=AdminMonitorView(db, schedule_id) if monitor else CandidateView(db, schedule_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            continue
        except discord.HTTPException:
            logger.exception("Could not refresh management panel %s", message_id)
            success = False
    return success


async def announce_decision(interaction: discord.Interaction, db: Database, schedule_id: int, cell_id: int) -> bool:
    """Both the command and candidate button use the same one-time announcement."""
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return False
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule["guild_id"] != guild.id or not schedule["is_open"]:
        await interaction.response.send_message("この予定表は締め切られています。", ephemeral=True)
        return False
    if not any(cell["id"] == cell_id for cell in db.schedule_cells(schedule_id)):
        await interaction.response.send_message("表示期間外の候補です。最新のパネルから選んでください。", ephemeral=True)
        return False
    channel_id = db.announcement_channel_id(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("告知先で `/schedule set-announcement` を実行してください。", ephemeral=True)
        return False
    permissions = channel.permissions_for(guild.me) if guild.me else discord.Permissions.none()
    if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.mention_everyone)):
        await interaction.response.send_message("告知先でBOTに「チャンネルを見る・メッセージ送信・埋め込みリンク・@everyone のメンション」の権限が必要です。", ephemeral=True)
        return False
    await interaction.response.defer(ephemeral=True)
    if not db.reserve_decision(schedule_id, cell_id):
        await interaction.followup.send("この日時は告知済み、または送信確認待ちです。重複通知を防ぐため、告知先を確認してください。", ephemeral=True)
        return False
    try:
        message = await channel.send(
            content="@everyone",
            embed=build_announcement_embed(db, schedule_id, cell_id, guild),
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False),
        )
    except (discord.Forbidden, discord.NotFound):
        db.release_decision(schedule_id, cell_id)
        await interaction.followup.send("告知を送信できませんでした。BOTの権限と告知先を確認して再実行してください。", ephemeral=True)
        return False
    except (discord.HTTPException, OSError, asyncio.TimeoutError):
        logger.exception("Announcement delivery uncertain for schedule %s cell %s", schedule_id, cell_id)
        await interaction.followup.send("送信結果を確認できませんでした。重複通知を防ぐため自動再送はしていません。告知先を確認してください。", ephemeral=True)
        return False
    db.record_decision(schedule_id, cell_id, message.id)
    if not schedule["rolling_days"]:
        db.close_schedule(schedule_id)
    await interaction.followup.send(f"{channel.mention} に @everyone 付きで確定日程を告知しました。" + ("\n1週間の予定表は引き続き回答できます。" if schedule["rolling_days"] else ""), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    await refresh_admin_panel(interaction.client, db, schedule_id, guild)
    return True


class AdminMonitorView(discord.ui.View):
    def __init__(self, db: Database, schedule_id: int) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.schedule_id = schedule_id

    @discord.ui.button(
        label="集計を更新",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="schedule:admin-monitor:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_admin_monitor_embed(self.db, self.schedule_id, interaction.guild),
            view=AdminMonitorView(self.db, self.schedule_id),
        )


class CandidateView(discord.ui.View):
    def __init__(self, db: Database, schedule_id: int) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.schedule_id = schedule_id
        schedule = db.get_schedule(schedule_id)
        decided = db.decided_cell_ids(schedule_id)
        for index, (cell, _counts) in enumerate(candidate_cells(db, schedule_id)):
            button = discord.ui.Button(
                label=f"#{index + 1} {cell['date_label']} {cell['block_label']}"[:80],
                style=discord.ButtonStyle.success,
                row=index // 5,
                custom_id=f"schedule:{schedule_id}:candidate:{cell['id']}",
                disabled=not schedule or not schedule["is_open"] or cell["id"] in decided,
            )
            button.callback = self._make_candidate_callback(cell["id"])
            self.add_item(button)

    def _make_candidate_callback(self, cell_id: int):
        async def callback(interaction: discord.Interaction) -> None:
            await announce_decision(interaction, self.db, self.schedule_id, cell_id)

        return callback


class ScheduleView(discord.ui.View):
    def __init__(self, db: Database, schedule_id: int, user_id: int, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.schedule_id = schedule_id
        self.user_id = user_id
        self.page = page

        schedule = db.get_schedule(schedule_id)
        if schedule is None:
            return
        groups = _group_cells(db.schedule_cells(schedule_id))
        dates = list(groups.items())
        page_count = schedule_page_count(db, schedule_id)
        self.page = min(max(page, 0), page_count - 1)
        days = schedule_days_per_page(db, schedule_id)
        current_dates = dates[self.page * days:(self.page + 1) * days]

        button_index = 0
        for date_index, (_, cells) in enumerate(current_dates):
            for cell in cells:
                status = db.schedule_answer(schedule_id, cell["id"], user_id)
                button = discord.ui.Button(
                    label=f"{cell['date_label']} {cell['block_label']} {STATUS_SYMBOLS[status]}",
                    style=STATUS_STYLES[status],
                    row=button_index // 5 if schedule["rolling_days"] else date_index,
                    disabled=not bool(schedule["is_open"]),
                    custom_id=f"schedule:{schedule_id}:cell:{cell['id']}:user:{user_id}",
                )
                button.callback = self._make_cell_callback(cell["id"])
                self.add_item(button)
                button_index += 1

        if page_count > 1:
            previous = discord.ui.Button(
                label="◀ 前へ", style=discord.ButtonStyle.secondary, row=4,
                custom_id=f"schedule:{schedule_id}:page:{self.page}:prev:user:{user_id}",
                disabled=self.page == 0,
            )
            next_page = discord.ui.Button(
                label="次へ ▶", style=discord.ButtonStyle.secondary, row=4,
                custom_id=f"schedule:{schedule_id}:page:{self.page}:next:user:{user_id}",
                disabled=self.page >= page_count - 1,
            )
            previous.callback = self._make_page_callback(-1)
            next_page.callback = self._make_page_callback(1)
            self.add_item(previous)
            self.add_item(next_page)

    def _make_cell_callback(self, cell_id: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("この予定表は本人専用です。", ephemeral=True)
                return
            schedule = self.db.get_schedule(self.schedule_id)
            if schedule is None or not schedule["is_open"]:
                await interaction.response.send_message("この予定表は締め切られています。", ephemeral=True)
                return
            if interaction.guild is None or interaction.guild.id != schedule["guild_id"]:
                return
            if schedule["target_role_id"] and (not isinstance(interaction.user, discord.Member) or not any(role.id == schedule["target_role_id"] for role in interaction.user.roles)):
                await interaction.response.send_message("この予定表の対象ロールが必要です。", ephemeral=True)
                return
            if not any(cell["id"] == cell_id for cell in self.db.schedule_cells(self.schedule_id)):
                await interaction.response.edit_message(
                    embed=build_personal_embed(self.db, self.schedule_id, self.user_id, 0),
                    view=ScheduleView(self.db, self.schedule_id, self.user_id, 0),
                )
                return
            current = self.db.schedule_answer(self.schedule_id, cell_id, self.user_id)
            self.db.set_schedule_answer(self.schedule_id, cell_id, self.user_id, NEXT_STATUS[current])
            await interaction.response.edit_message(
                embed=build_personal_embed(self.db, self.schedule_id, self.user_id, self.page),
                view=ScheduleView(self.db, self.schedule_id, self.user_id, self.page),
            )
            if interaction.guild is not None:
                await refresh_admin_panel(interaction.client, self.db, self.schedule_id, interaction.guild)

        return callback

    def _make_page_callback(self, direction: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("この予定表は本人専用です。", ephemeral=True)
                return
            await interaction.response.edit_message(
                embed=build_personal_embed(self.db, self.schedule_id, self.user_id, self.page + direction),
                view=ScheduleView(self.db, self.schedule_id, self.user_id, self.page + direction),
            )

        return callback
