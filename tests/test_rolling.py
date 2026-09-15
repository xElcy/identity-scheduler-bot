import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord

from bot.database import Database
from bot import ui


class FixedDateTime(datetime):
    day = date(2026, 9, 15)

    @classmethod
    def now(cls, tz=None):
        return cls(cls.day.year, cls.day.month, cls.day.day, 12, tzinfo=tz)


def cells(day, count=7, labels=("昼ラン帯", "夜ラン帯")):
    return [dict(date_key=(day + timedelta(days=i)).isoformat(),
                 date_label=(day + timedelta(days=i)).strftime('%m/%d'),
                 block_key=f'block-{j}', block_label=label, cell_order=i * len(labels) + j)
            for i in range(count) for j, label in enumerate(labels)]


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = patch('bot.database.datetime', FixedDateTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        FixedDateTime.day = date(2026, 9, 15)
        self.db = Database(Path(self.temp.name) / 'schedule.db')
        self.db.setup()
        self.sid = self.db.create_schedule(1, 2, 'クラン', cells(date(2026, 9, 14)), 7, 8)

    def test_rollover_keeps_ids_answers_and_history_after_restart(self):
        before = self.db.schedule_cells(self.sid)
        for cell in before:
            self.db.set_schedule_answer(self.sid, cell['id'], 11, 'available')
        self.db.save_private_profile(1, 11, 22, 33)
        self.db.close_schedule(self.sid)
        self.db.enable_rolling(self.sid, reopen=True)
        FixedDateTime.day = date(2026, 9, 16)
        self.db.advance_rolling(self.sid)
        reopened = Database(self.db.path)
        reopened.setup()
        current = reopened.schedule_cells(self.sid)
        self.assertEqual(len(current), 14)
        self.assertEqual(current[0]['date_key'], '2026-09-16')
        self.assertEqual(current[-1]['date_key'], '2026-09-22')
        for cell in before:
            self.assertEqual(reopened.schedule_answer(self.sid, cell['id'], 11), 'available')
        self.assertTrue(all(reopened.schedule_answer(self.sid, c['id'], 11) == 'blank' for c in current if c['date_key'] > '2026-09-20'))
        self.assertEqual(reopened.private_profile(1, 11)['channel_id'], 22)
        size = len(reopened.schedule_cells(self.sid, include_history=True))
        self.assertFalse(reopened.advance_rolling(self.sid))
        self.assertEqual(len(reopened.schedule_cells(self.sid, include_history=True)), size)

    def test_leap_day_year_change_and_offline_catchup(self):
        self.db.enable_rolling(self.sid)
        for day in (date(2027, 12, 29), date(2028, 2, 27)):
            FixedDateTime.day = day
            self.db.advance_rolling(self.sid)
            dates = sorted({c['date_key'] for c in self.db.schedule_cells(self.sid)})
            self.assertEqual(dates, [(day + timedelta(days=i)).isoformat() for i in range(7)])
        self.assertIn('2028-02-29', dates)

    def test_edit_matches_label_not_block_position(self):
        cell = self.db.schedule_cells(self.sid)[0]
        self.db.set_schedule_answer(self.sid, cell['id'], 11, 'maybe')
        self.db.edit_schedule(self.sid, '変更', cells(date(2026, 9, 14), labels=('夜ラン帯', '昼ラン帯')))
        same = next(c for c in self.db.schedule_cells(self.sid) if c['date_key'] == cell['date_key'] and c['block_label'] == cell['block_label'])
        self.assertEqual(same['id'], cell['id'])
        self.assertEqual(self.db.schedule_answer(self.sid, same['id'], 11), 'maybe')
        self.db.edit_schedule(self.sid, '変更', cells(date(2026, 9, 14), labels=('午後',)))
        self.assertEqual(self.db.schedule_answer(self.sid, cell['id'], 11), 'maybe')
        self.assertEqual(self.db.schedule_answer(self.sid, self.db.schedule_cells(self.sid)[0]['id'], 11), 'blank')

    def test_old_answers_do_not_count_as_answered_this_week(self):
        old = self.db.schedule_cells(self.sid)[0]
        self.db.set_schedule_answer(self.sid, old['id'], 11, 'available')
        self.db.enable_rolling(self.sid)
        self.assertEqual(self.db.schedule_answered_users(self.sid), [])

    def test_closed_schedule_does_not_restart_automatically(self):
        self.db.enable_rolling(self.sid)
        self.db.close_schedule(self.sid)
        FixedDateTime.day += timedelta(days=1)
        self.assertFalse(self.db.advance_rolling(self.sid))
        self.assertFalse(self.db.get_schedule(self.sid)['is_open'])


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'schedule.db')
        self.db.setup()
        self.day = datetime.now(ZoneInfo('Asia/Tokyo')).date()
        self.sid = self.db.create_schedule(1, 2, 'クラン', cells(self.day), 7, 8)
        self.db.enable_rolling(self.sid)
        self.db.set_announcement_channel(1, 23)
        self.member = MagicMock(spec=discord.Member)
        self.member.id = 11
        self.member.display_name = 'テストメンバー'
        self.member.bot = False
        self.member.roles = [SimpleNamespace(id=7)]
        self.member.guild_permissions = discord.Permissions(administrator=True)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 23
        self.channel.mention = '<#23>'
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=99))
        self.channel.permissions_for.return_value = discord.Permissions.all()
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id = 1
        self.guild.get_channel.return_value = self.channel
        self.category = MagicMock(spec=discord.CategoryChannel)
        self.category.id = 8
        self.guild.get_channel.side_effect = lambda cid: self.category if cid == 8 else self.channel
        self.guild.get_member.return_value = self.member
        self.member.guild = self.guild
        self.guild.members = [self.member]
        self.guild.me = self.member
        self.guild.get_role.return_value = SimpleNamespace(name='メンバー')
        self.interaction = SimpleNamespace(user=self.member, guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), client=MagicMock())
        ui._refresh_locks.clear()

    async def test_week_layout_and_four_block_pagination_are_valid(self):
        view = ui.ScheduleView(self.db, self.sid, 11)
        self.assertEqual(len(view.children), 14)
        self.assertEqual(len(ui.build_personal_embed(self.db, self.sid, 11, 0).fields), 7)
        self.assertEqual(ui.schedule_page_count(self.db, self.sid), 1)
        self.db.edit_schedule(self.sid, '変更', cells(self.day, labels=('朝', '昼', '夜', '深夜')))
        self.db.enable_rolling(self.sid)
        for page in range(ui.schedule_page_count(self.db, self.sid)):
            payload = ui.ScheduleView(self.db, self.sid, 11, page).to_components()
            self.assertLessEqual(len(payload), 5)
            self.assertTrue(all(len(row['components']) <= 5 for row in payload))

    async def test_old_button_never_writes_a_different_date(self):
        old = self.db.schedule_cells(self.sid)[0]
        view = ui.ScheduleView(self.db, self.sid, 11)
        self.db.edit_schedule(self.sid, '変更', cells(self.day + timedelta(days=1)))
        await view._make_cell_callback(old['id'])(self.interaction)
        self.assertEqual(self.db.schedule_answer(self.sid, old['id'], 11), 'blank')
        self.interaction.response.edit_message.assert_awaited_once()

    async def test_confirm_mentions_everyone_once_and_keeps_rolling_open(self):
        cell = self.db.schedule_cells(self.sid)[0]
        callback = ui.CandidateView(self.db, self.sid)._make_candidate_callback(cell['id'])
        await asyncio.gather(callback(self.interaction), callback(self.interaction))
        self.channel.send.assert_awaited_once()
        kwargs = self.channel.send.call_args.kwargs
        self.assertEqual(kwargs['content'], '@everyone')
        self.assertEqual(kwargs['allowed_mentions'].to_dict()['parse'], ['everyone'])
        self.assertTrue(self.db.get_schedule(self.sid)['is_open'])
        self.assertIn(cell['id'], Database(self.db.path).decided_cell_ids(self.sid))

    async def test_no_permission_or_stale_confirmation_never_sends(self):
        cell = self.db.schedule_cells(self.sid)[0]
        self.channel.permissions_for.return_value = discord.Permissions.none()
        self.assertFalse(await ui.announce_decision(self.interaction, self.db, self.sid, cell['id']))
        self.assertEqual(self.db.decided_cell_ids(self.sid), set())
        self.channel.permissions_for.return_value = discord.Permissions.all()
        self.db.close_schedule(self.sid)
        self.assertFalse(await ui.announce_decision(self.interaction, self.db, self.sid, cell['id']))
        self.channel.send.assert_not_awaited()

    async def test_manager_without_administrator_is_denied(self):
        self.member.guild_permissions = discord.Permissions(manage_guild=True)
        cell = self.db.schedule_cells(self.sid)[0]
        self.assertFalse(await ui.announce_decision(self.interaction, self.db, self.sid, cell['id']))
        self.channel.send.assert_not_awaited()

    async def test_member_add_is_targeted_and_role_checked(self):
        from bot import __main__ as main
        original = main.bot.db
        main.bot.db = self.db
        self.addCleanup(setattr, main.bot, 'db', original)
        with patch.object(main, '_ensure_private_panel', new_callable=AsyncMock, return_value=self.channel) as ensure, patch.object(main, 'refresh_admin_panel', new_callable=AsyncMock):
            await main.schedule_add_member.callback(self.interaction, self.member)
            ensure.assert_awaited_once_with(self.guild, self.member, self.sid)
            self.member.roles = []
            await main.schedule_add_member.callback(self.interaction, self.member)
            self.assertEqual(ensure.await_count, 1)

    async def test_retry_reuses_saved_room_after_panel_failure(self):
        from bot import __main__ as main
        original = main.bot.db
        main.bot.db = self.db
        main.bot.panel_locks.clear()
        self.addCleanup(setattr, main.bot, 'db', original)
        self.guild.create_text_channel = AsyncMock(return_value=self.channel)
        self.channel.set_permissions = AsyncMock()
        self.channel.category_id = 8
        self.channel.send.side_effect = [OSError('temporary failure'), SimpleNamespace(id=99)]
        with self.assertRaises(OSError):
            await main._ensure_private_panel(self.guild, self.member, self.sid)
        self.assertEqual(self.db.private_profile(1, 11)['channel_id'], 23)
        await main._ensure_private_panel(self.guild, self.member, self.sid)
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.db.private_profile(1, 11)['panel_message_id'], 99)

    async def test_command_and_button_share_announcement_behavior(self):
        from bot import __main__ as main
        original = main.bot.db
        main.bot.db = self.db
        self.addCleanup(setattr, main.bot, 'db', original)
        await main.schedule_decide.callback(self.interaction, self.day.isoformat(), '昼ラン帯')
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.call_args.kwargs['content'], '@everyone')
        self.assertTrue(self.db.get_schedule(self.sid)['is_open'])


if __name__ == '__main__':
    unittest.main()
