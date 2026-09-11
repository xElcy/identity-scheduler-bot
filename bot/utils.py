from datetime import datetime
import re
from zoneinfo import ZoneInfo


def parse_jst(date_text: str, time_text: str, timezone: str) -> datetime:
    value = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    return value.replace(tzinfo=ZoneInfo(timezone)).astimezone(ZoneInfo("UTC"))


def format_jst(timestamp: float, timezone: str) -> str:
    value = datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).astimezone(ZoneInfo(timezone))
    weekdays = "月火水木金土日"
    return value.strftime("%Y/%m/%d") + f"（{weekdays[value.weekday()]}） " + value.strftime("%H:%M")


def parse_candidate_slots(raw: str, timezone: str) -> list[datetime]:
    local_zone = ZoneInfo(timezone)
    current_year = datetime.now(local_zone).year
    parts = [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]
    if not parts:
        raise ValueError("候補日時を1つ以上入力してください")

    formats = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m/%d %H:%M", "%m/%d %H時%M分")
    parsed: list[datetime] = []
    for part in parts:
        value = None
        for date_format in formats:
            try:
                value = datetime.strptime(part, date_format)
                if "%Y" not in date_format:
                    value = value.replace(year=current_year)
                break
            except ValueError:
                continue
        if value is None:
            raise ValueError(f"`{part}`。例：`2026-09-20 20:00`")
        parsed.append(value.replace(tzinfo=local_zone).astimezone(ZoneInfo("UTC")))

    unique = {candidate.timestamp(): candidate for candidate in parsed}
    return [unique[timestamp] for timestamp in sorted(unique)]


def parse_schedule_dates(raw: str, timezone: str) -> list[tuple[str, str]]:
    local_zone = ZoneInfo(timezone)
    current_year = datetime.now(local_zone).year
    parts = [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]
    if not parts:
        raise ValueError("日付を1つ以上入力してください")

    formats = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d")
    parsed: list[tuple[datetime, str]] = []
    for part in parts:
        cleaned = re.sub(r"\s*[（(].*?[）)]", "", part).strip()
        value = None
        for date_format in formats:
            try:
                value = datetime.strptime(cleaned, date_format)
                if "%Y" not in date_format:
                    value = value.replace(year=current_year)
                break
            except ValueError:
                continue
        if value is None:
            raise ValueError(f"日付 `{part}` を読み取れません。例：`9/20` または `2026-09-20`")
        local_value = value.replace(tzinfo=local_zone)
        weekdays = "月火水木金土日"
        label = local_value.strftime("%m/%d") + f"（{weekdays[local_value.weekday()]}）"
        parsed.append((local_value, label))

    unique = {value.strftime("%Y-%m-%d"): (value, label) for value, label in parsed}
    return [(value.strftime("%Y-%m-%d"), label) for value, label in sorted(unique.values(), key=lambda item: item[0])]


def parse_schedule_blocks(raw: str) -> list[tuple[str, str]]:
    parts = [part.strip() for part in re.split(r"[,\n]+", raw) if part.strip()]
    if not parts:
        raise ValueError("時間帯を1つ以上入力してください")
    if len(parts) > 4:
        raise ValueError("時間帯は最大4つまでです")
    return [(f"block-{index}", part[:40]) for index, part in enumerate(parts)]
