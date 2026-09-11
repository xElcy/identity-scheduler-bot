import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    def setup(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    organizer_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    start_at REAL NOT NULL,
                    remind_minutes INTEGER NOT NULL DEFAULT 60,
                    reminder_sent INTEGER NOT NULL DEFAULT 0,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    event_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('join', 'maybe', 'absent')),
                    updated_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (event_id, user_id),
                    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER,
                    organizer_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS poll_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id INTEGER NOT NULL,
                    start_at REAL NOT NULL,
                    slot_order INTEGER NOT NULL,
                    FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS poll_responses (
                    poll_id INTEGER NOT NULL,
                    slot_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('available', 'maybe', 'unavailable')),
                    updated_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (poll_id, slot_id, user_id),
                    FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE,
                    FOREIGN KEY (slot_id) REFERENCES poll_slots(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS poll_selections (
                    poll_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    slot_id INTEGER NOT NULL,
                    updated_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (poll_id, user_id),
                    FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE,
                    FOREIGN KEY (slot_id) REFERENCES poll_slots(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    organizer_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL DEFAULT (unixepoch())
                );
                CREATE TABLE IF NOT EXISTS schedule_cells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER NOT NULL,
                    date_key TEXT NOT NULL,
                    date_label TEXT NOT NULL,
                    block_key TEXT NOT NULL,
                    block_label TEXT NOT NULL,
                    cell_order INTEGER NOT NULL,
                    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS schedule_answers (
                    schedule_id INTEGER NOT NULL,
                    cell_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('blank', 'available', 'maybe', 'unavailable')),
                    updated_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (schedule_id, cell_id, user_id),
                    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE,
                    FOREIGN KEY (cell_id) REFERENCES schedule_cells(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS private_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    panel_message_id INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )

    def create_event(self, guild_id: int, channel_id: int, organizer_id: int, title: str, start_at: float, remind_minutes: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events (guild_id, channel_id, organizer_id, title, start_at, remind_minutes) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, organizer_id, title, start_at, remind_minutes),
            )
            return int(cursor.lastrowid)

    def set_message_id(self, event_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE events SET message_id = ? WHERE id = ?", (message_id, event_id))

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return dict(row) if row else None

    def active_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM events WHERE is_open = 1").fetchall()
            return [dict(row) for row in rows]

    def upcoming_events(self, guild_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE guild_id = ? AND start_at >= unixepoch() ORDER BY start_at LIMIT 20",
                (guild_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_attendance(self, event_id: int, user_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO attendance (event_id, user_id, status, updated_at)
                VALUES (?, ?, ?, unixepoch())
                ON CONFLICT(event_id, user_id) DO UPDATE SET status = excluded.status, updated_at = unixepoch()
                """,
                (event_id, user_id, status),
            )

    def attendance(self, event_id: int, status: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM attendance WHERE event_id = ? AND status = ? ORDER BY updated_at",
                (event_id, status),
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def joined_users(self, event_id: int) -> list[int]:
        return self.attendance(event_id, "join")

    def close_event(self, event_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE events SET is_open = 0 WHERE id = ?", (event_id,))

    def delete_event(self, event_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM events WHERE id = ?", (event_id,))

    def events_due_for_reminder(self, now: float) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE reminder_sent = 0 AND remind_minutes > 0
                  AND is_open = 1
                  AND start_at <= (? + remind_minutes * 60)
                  AND start_at > ?
                """,
                (now, now),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_reminder_sent(self, event_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE events SET reminder_sent = 1 WHERE id = ?", (event_id,))

    def create_poll(self, guild_id: int, channel_id: int, organizer_id: int, title: str, slots: list[float]) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO polls (guild_id, channel_id, organizer_id, title) VALUES (?, ?, ?, ?)",
                (guild_id, channel_id, organizer_id, title),
            )
            poll_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO poll_slots (poll_id, start_at, slot_order) VALUES (?, ?, ?)",
                [(poll_id, start_at, index) for index, start_at in enumerate(slots)],
            )
            return poll_id

    def set_poll_message_id(self, poll_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE polls SET message_id = ? WHERE id = ?", (message_id, poll_id))

    def get_poll(self, poll_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
            return dict(row) if row else None

    def active_polls(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM polls WHERE is_open = 1").fetchall()
            return [dict(row) for row in rows]

    def upcoming_polls(self, guild_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, MIN(s.start_at) AS first_slot
                FROM polls p JOIN poll_slots s ON s.poll_id = p.id
                WHERE p.guild_id = ? AND p.is_open = 1
                GROUP BY p.id ORDER BY first_slot LIMIT 20
                """,
                (guild_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def poll_slots(self, poll_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM poll_slots WHERE poll_id = ? ORDER BY slot_order", (poll_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def set_poll_selection(self, poll_id: int, user_id: int, slot_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_selections (poll_id, user_id, slot_id, updated_at)
                VALUES (?, ?, ?, unixepoch())
                ON CONFLICT(poll_id, user_id) DO UPDATE SET slot_id = excluded.slot_id, updated_at = unixepoch()
                """,
                (poll_id, user_id, slot_id),
            )

    def poll_selection(self, poll_id: int, user_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT slot_id FROM poll_selections WHERE poll_id = ? AND user_id = ?",
                (poll_id, user_id),
            ).fetchone()
            return int(row["slot_id"]) if row else None

    def set_poll_response(self, poll_id: int, slot_id: int, user_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_responses (poll_id, slot_id, user_id, status, updated_at)
                VALUES (?, ?, ?, ?, unixepoch())
                ON CONFLICT(poll_id, slot_id, user_id)
                DO UPDATE SET status = excluded.status, updated_at = unixepoch()
                """,
                (poll_id, slot_id, user_id, status),
            )

    def poll_response_users(self, poll_id: int, slot_id: int, status: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM poll_responses
                WHERE poll_id = ? AND slot_id = ? AND status = ? ORDER BY updated_at
                """,
                (poll_id, slot_id, status),
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def close_poll(self, poll_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE polls SET is_open = 0 WHERE id = ?", (poll_id,))

    def delete_poll(self, poll_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM polls WHERE id = ?", (poll_id,))

    def create_schedule(self, guild_id: int, organizer_id: int, title: str, cells: list[dict[str, Any]]) -> int:
        with self._connect() as connection:
            connection.execute("UPDATE schedules SET is_open = 0 WHERE guild_id = ? AND is_open = 1", (guild_id,))
            cursor = connection.execute(
                "INSERT INTO schedules (guild_id, organizer_id, title) VALUES (?, ?, ?)",
                (guild_id, organizer_id, title),
            )
            schedule_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO schedule_cells
                (schedule_id, date_key, date_label, block_key, block_label, cell_order)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (schedule_id, cell["date_key"], cell["date_label"], cell["block_key"], cell["block_label"], index)
                    for index, cell in enumerate(cells)
                ],
            )
            return schedule_id

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            return dict(row) if row else None

    def active_schedule(self, guild_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE guild_id = ? AND is_open = 1 ORDER BY id DESC LIMIT 1",
                (guild_id,),
            ).fetchone()
            return dict(row) if row else None

    def schedule_cells(self, schedule_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedule_cells WHERE schedule_id = ? ORDER BY cell_order", (schedule_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def schedule_answer(self, schedule_id: int, cell_id: int, user_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM schedule_answers WHERE schedule_id = ? AND cell_id = ? AND user_id = ?",
                (schedule_id, cell_id, user_id),
            ).fetchone()
            return str(row["status"]) if row else "blank"

    def set_schedule_answer(self, schedule_id: int, cell_id: int, user_id: int, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO schedule_answers (schedule_id, cell_id, user_id, status, updated_at)
                VALUES (?, ?, ?, ?, unixepoch())
                ON CONFLICT(schedule_id, cell_id, user_id)
                DO UPDATE SET status = excluded.status, updated_at = unixepoch()
                """,
                (schedule_id, cell_id, user_id, status),
            )

    def schedule_answer_counts(self, schedule_id: int, cell_id: int) -> dict[str, int]:
        counts = {"available": 0, "maybe": 0, "unavailable": 0}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total FROM schedule_answers
                WHERE schedule_id = ? AND cell_id = ? AND status != 'blank'
                GROUP BY status
                """,
                (schedule_id, cell_id),
            ).fetchall()
            for row in rows:
                counts[str(row["status"])] = int(row["total"])
        return counts

    def schedule_answer_users(self, schedule_id: int, cell_id: int, status: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM schedule_answers
                WHERE schedule_id = ? AND cell_id = ? AND status = ?
                ORDER BY updated_at
                """,
                (schedule_id, cell_id, status),
            ).fetchall()
            return [int(row["user_id"]) for row in rows]

    def private_profile(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM private_profiles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def save_private_profile(self, guild_id: int, user_id: int, channel_id: int, panel_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO private_profiles (guild_id, user_id, channel_id, panel_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    panel_message_id = excluded.panel_message_id
                """,
                (guild_id, user_id, channel_id, panel_message_id),
            )

    def private_profiles(self, guild_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM private_profiles WHERE guild_id = ?", (guild_id,)).fetchall()
            return [dict(row) for row in rows]

    def close_schedule(self, schedule_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE schedules SET is_open = 0 WHERE id = ?", (schedule_id,))

    def delete_schedule(self, schedule_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
