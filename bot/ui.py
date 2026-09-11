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


def _group_cells(cells: list[dict]) -> OrderedDict[str, list[dict]]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for cell in cells:
        groups.setdefault(cell["date_key"], []).append(cell)
    return groups


def schedule_page_count(db: Database, schedule_id: int) -> int:
    return max(1, (len(_group_cells(db.schedule_cells(schedule_id))) + 3) // 4)


def build_personal_embed(db: Database, schedule_id: int, user_id: int, page: int) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    groups = _group_cells(db.schedule_cells(schedule_id))
    dates = list(groups.items())
    page_count = schedule_page_count(db, schedule_id)
    page = min(max(page, 0), page_count - 1)
    current_dates = dates[page * 4:(page + 1) * 4]

    embed = discord.Embed(
        title=f"🔒 {schedule['title']}",
        description=(
            "このチャンネルはあなた専用です。\n"
            "各ボタンを押すと `— → ○ → △ → × → —` の順に切り替わります。\n"
            "○ 行ける　△ 未定　× 行けない"
        ),
        color=discord.Color.from_rgb(88, 101, 242) if schedule["is_open"] else discord.Color.greyple(),
    )
    for _, cells in current_dates:
        lines = []
        for cell in cells:
            status = db.schedule_answer(schedule_id, cell["id"], user_id)
            lines.append(f"`{cell['block_label']}`　**{STATUS_SYMBOLS[status]}**")
        embed.add_field(name=f"📅 {cells[0]['date_label']}", value="\n".join(lines), inline=True)
    embed.set_footer(text=f"ページ {page + 1}/{page_count}　|　回答受付中" if schedule["is_open"] else "回答締切")
    return embed


def build_result_embed(db: Database, schedule_id: int) -> discord.Embed:
    schedule = db.get_schedule(schedule_id)
    if schedule is None:
        return discord.Embed(title="予定表が見つかりません", color=discord.Color.red())

    embed = discord.Embed(
        title=f"📊 集計結果｜{schedule['title']}",
        description="各候補の回答数です。最も参加可能者が多い候補には ⭐ を付けています。",
        color=discord.Color.gold(),
    )
    cells = db.schedule_cells(schedule_id)
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
        current_dates = dates[self.page * 4:(self.page + 1) * 4]

        for date_index, (_, cells) in enumerate(current_dates):
            for cell in cells:
                status = db.schedule_answer(schedule_id, cell["id"], user_id)
                button = discord.ui.Button(
                    label=f"{cell['date_label']} {cell['block_label']} {STATUS_SYMBOLS[status]}",
                    style=STATUS_STYLES[status],
                    row=date_index,
                    disabled=not bool(schedule["is_open"]),
                    custom_id=f"schedule:{schedule_id}:cell:{cell['id']}:user:{user_id}",
                )
                button.callback = self._make_cell_callback(cell["id"])
                self.add_item(button)

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
            current = self.db.schedule_answer(self.schedule_id, cell_id, self.user_id)
            self.db.set_schedule_answer(self.schedule_id, cell_id, self.user_id, NEXT_STATUS[current])
            await interaction.response.edit_message(
                embed=build_personal_embed(self.db, self.schedule_id, self.user_id, self.page),
                view=ScheduleView(self.db, self.schedule_id, self.user_id, self.page),
            )

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
