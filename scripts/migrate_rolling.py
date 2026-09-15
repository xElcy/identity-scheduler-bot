"""Enable the existing schedule, verifying that no stored answers are lost."""
import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.database import Database


def snapshot(path):
    with sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True) as con:
        result = {table: con.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                  for table in ('schedule_answers', 'private_profiles', 'guild_settings', 'voice_notify_ignored_channels')}
        result['cells'] = con.execute('SELECT id, schedule_id, date_key, block_label FROM schedule_cells ORDER BY id').fetchall()
    con.close()
    return result


def copy_database(source, target):
    if target.exists():
        raise RuntimeError('Backup target already exists')
    with sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    src.close()
    dst.close()


def migrate(path, sid, timezone, before):
    db = Database(path)
    db.setup()
    if not db.get_schedule(sid):
        raise RuntimeError('Schedule not found')
    db.enable_rolling(sid, timezone, reopen=True)
    after = snapshot(path)
    for table in ('schedule_answers', 'private_profiles', 'guild_settings', 'voice_notify_ignored_channels'):
        assert before[table] == after[table], f'{table} changed unexpectedly'
    assert set(before['cells']) <= set(after['cells']), 'Existing cell identities changed'
    with db._connect() as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    schedule = db.get_schedule(sid)
    print(json.dumps(dict(answers_preserved=len(before['schedule_answers']),
                         profiles_preserved=len(before['private_profiles']),
                         window_start=schedule['window_start'], window_end=schedule['window_end'],
                         visible_cells=len(db.schedule_cells(sid)), integrity='ok')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    parser.add_argument('--schedule-id', type=int, required=True)
    parser.add_argument('--timezone', default='Asia/Tokyo')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    before = snapshot(args.database)
    if args.apply:
        if not args.backup:
            parser.error('--apply requires --backup')
        copy_database(args.database, args.backup)
        migrate(args.database, args.schedule_id, args.timezone, before)
    else:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'dry-run.db'
            copy_database(args.database, target)
            migrate(target, args.schedule_id, args.timezone, before)


if __name__ == '__main__':
    main()
