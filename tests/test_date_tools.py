import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, date_tools as d
from assistant.backup import create_backup, restore_backup
from assistant.continuous import run_continuous
from assistant.store import Store


def add(day, n, calendar='calendar', excluded=()):
    return {'operation': 'add', 'date': day, 'days': n,
            'calendar': calendar, 'excluded_dates': list(excluded)}


def between(start, end, calendar='calendar', excluded=()):
    return {'operation': 'between', 'start': start, 'end': end,
            'calendar': calendar, 'excluded_dates': list(excluded)}


def action(args):
    return json.dumps({'tool': 'date_math', 'args': args, 'note': '核對日期'}, ensure_ascii=False)


class Engine:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []

    def answer(self, prompt, cancel=None):
        self.prompts.append(prompt)
        answer = next(self.answers)
        return answer() if callable(answer) else answer


class DateToolsTests(unittest.TestCase):
    def test_cross_year_with_explicit_holidays_and_reverse(self):
        holidays = ['2027-01-01', '2026-12-25']
        forward = d.calculate(add('2026-12-24', 5, 'weekdays', holidays))
        self.assertEqual(forward['end'], {'date': '2027-01-04', 'weekday_iso': 1, 'weekday': '星期一'})
        self.assertEqual((forward['counted_days'], forward['calendar_days']), (5, 11))
        self.assertEqual((forward['skipped_weekend_days'], forward['skipped_excluded_weekdays']), (4, 2))
        self.assertFalse(forward['start_included'])
        self.assertEqual(forward['excluded_dates'], sorted(holidays))
        reverse = d.calculate(add('2027-01-04', -5, 'weekdays', holidays))
        self.assertEqual((reverse['end']['date'], reverse['calendar_days']), ('2026-12-24', -11))
        interval = d.calculate(between('2026-12-24', '2027-01-04', 'weekdays', holidays))
        self.assertEqual(interval['counted_days'], 5)

    def test_leap_day_century_and_year_boundaries(self):
        for start, n, end in [('2028-02-28', 1, '2028-02-29'), ('2028-03-01', -1, '2028-02-29'),
                              ('2100-02-28', 1, '2100-03-01'), ('2000-02-28', 1, '2000-02-29'),
                              ('2026-12-31', 1, '2027-01-01'), ('2027-01-01', -1, '2026-12-31')]:
            with self.subTest(start=start, n=n):
                row = d.calculate(add(start, n))
                self.assertEqual(row['end']['date'], end)
                self.assertEqual(row['calendar_days'], n)
        self.assertEqual(d.calculate({'operation': 'weekday', 'date': '2000-01-01'})['weekday'], '星期六')

    def test_directional_endpoints_zero_and_weekend_exclusions(self):
        forward = d.calculate(between('2026-11-06', '2026-11-08', 'weekdays'))
        reverse = d.calculate(between('2026-11-08', '2026-11-06', 'weekdays'))
        self.assertEqual((forward['counted_days'], reverse['counted_days']), (0, -1))
        self.assertEqual(d.calculate(add('2026-11-08', 0, 'weekdays'))['end']['date'], '2026-11-08')
        self.assertFalse(d.calculate(between('2026-11-08', '2026-11-08'))['end_included'])
        row = d.calculate(add('2026-11-06', 1, 'weekdays', ['2026-11-07']))
        self.assertEqual(row['end']['date'], '2026-11-09')
        self.assertEqual((row['skipped_weekend_days'], row['skipped_excluded_weekdays']), (2, 0))

    def test_no_implicit_holidays_or_silent_calendar_substitution(self):
        self.assertEqual(d.calculate(add('2026-12-24', 1, 'weekdays'))['end']['date'], '2026-12-25')
        with self.assertRaises(ValueError):
            d.calculate(add('2026-12-24', 1, 'calendar', ['2026-12-25']))
        with self.assertRaises(ValueError):
            d.calculate(add('2026-12-24', 1, 'taiwan_holidays'))

    def test_strict_dates_types_and_shapes(self):
        invalid = ['2026-2-01', '20260201', '2026-W01-1', '2026-02-29', '0000-01-01',
                   '10000-01-01', '２０２６-01-01', '2026-01-01T00:00:00', None, 20260101]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                d.calculate({'operation': 'weekday', 'date': value})
        for value in [True, 1.0, '1', None, 3661, -3661]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                d.calculate(add('2026-01-01', value))
        for value in [None, [], {}, {'operation': []}, {'operation': 'today', 'timezone': 'Asia/Taipei'},
                      {'operation': 'shell', 'command': 'never'}, {**add('2026-01-01', 1), 'filename': 'x'}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                a.validate_action({'tool': 'date_math', 'args': value, 'note': 'invalid'})
        for excluded in [['2026-01-01'] * 2, ['2026-02-29'], [None], ['2026-01-01'] * 367, '2026-01-01']:
            with self.subTest(excluded=excluded), self.assertRaises(ValueError):
                d.calculate({**add('2026-01-01', 1, 'weekdays'), 'excluded_dates': excluded})

    def test_bounds_overflow_and_long_exclusion_span(self):
        for args in [add('9999-12-31', 1), add('0001-01-01', -1),
                     add('9999-12-31', 1, 'weekdays'), between('2000-01-01', '2020-01-01')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                d.calculate(args)
        self.assertEqual(d.calculate(add('2000-01-01', 3660))['end']['date'], '2010-01-08')
        # A whole leap year of supplied exclusions must be traversed, not silently capped.
        from datetime import date
        excluded = [(date(2028, 1, 1) + timedelta(days=i)).isoformat() for i in range(366)]
        row = d.calculate(add('2027-12-31', 1, 'weekdays', excluded))
        self.assertEqual(row['end']['date'], '2029-01-01')
        self.assertEqual(row['skipped_excluded_weekdays'], 260)

    def test_today_reports_same_instant_local_date_and_explicit_provenance(self):
        class ClockDate(datetime):
            def astimezone(self, tz=None):
                return super().astimezone(tz or timezone(timedelta(hours=8)))
        instant = ClockDate(2026, 11, 3, 16, 30, tzinfo=timezone.utc)
        with patch.object(d, 'datetime') as clock:
            clock.now.return_value = instant
            row = d.calculate({'operation': 'today'})
            clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(row['date'], '2026-11-04')
        self.assertEqual(row['weekday'], '星期三')
        self.assertEqual(row['utc_offset_seconds'], 28800)
        self.assertEqual(row['timezone_source'], 'computer_local')
        self.assertEqual(datetime.fromisoformat(row['local_datetime']), datetime.fromisoformat(row['observed_at_utc']))
        before = datetime.now(timezone.utc)
        real = d.calculate({'operation': 'today'})
        self.assertLessEqual(before, datetime.fromisoformat(real['observed_at_utc']))
        self.assertLessEqual(datetime.fromisoformat(real['observed_at_utc']), datetime.now(timezone.utc))

    def store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'assistant.sqlite3')
        return store, store.new_session()

    def test_real_goal_resume_backup_and_tool_result_reaches_model(self):
        store, sid = self.store()
        gid = a.create_goal(store, sid, '推算交付日期')
        initial = a.run_goal(store, sid, gid, Engine(action(add('2026-12-24', 5, 'weekdays',
            ['2026-12-25', '2027-01-01']))), max_steps=1)
        self.assertEqual(initial['status'], 'paused')
        engine = Engine(action(between('2026-12-24', '2027-01-04')),
                        json.dumps({'tool': 'finish', 'args': {'report': '交付日2027-01-04星期一，間隔11天。'}, 'note': '交付'}))
        result = a.run_goal(Store(store.path), sid, gid, engine)
        self.assertEqual(result['status'], 'report_ready')
        self.assertIn('"counted_days": 5', engine.prompts[0])
        self.assertIn('"calendar_days": 11', engine.prompts[1])
        self.assertEqual(len(result['steps']), 3)
        backup = create_backup(store.path)
        target = store.path.parent / 'restored'
        restore_backup(backup['path'], target)
        self.assertEqual(a.get_goal(Store(target / 'assistant.sqlite3'), sid, gid), result)
        with self.assertRaises(ValueError):
            a.get_goal(store, store.new_session(), gid)

    def test_invalid_calendar_is_observed_and_can_be_corrected(self):
        store, sid = self.store()
        gid = a.create_goal(store, sid, '計算日期')
        engine = Engine(action(add('2026-12-24', 1, 'calendar', ['2026-12-25'])),
                        action(add('2026-12-24', 1, 'weekdays', ['2026-12-25'])))
        result = a.run_goal(store, sid, gid, engine, max_steps=2)
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertIn('日曆天不能另外排除日期', engine.prompts[1])
        self.assertEqual(result['steps'][1]['observation']['end']['date'], '2026-12-28')

    def test_cancelled_generation_and_repeated_bad_dates_stop(self):
        store, sid = self.store()
        gid = a.create_goal(store, sid, '計算日期')
        cancel = threading.Event()
        def cancelled():
            cancel.set()
            return action({'operation': 'today'})
        result = a.run_goal(store, sid, gid, Engine(cancelled), cancel=cancel)
        self.assertEqual(result['steps'], [])
        result = run_continuous(store, sid, gid,
            Engine(*[action({'operation': 'weekday', 'date': '2026-02-29'})] * 3), max_steps=8)
        self.assertEqual(len(result['steps']), 3)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'repeated_errors')


if __name__ == '__main__':
    unittest.main()
