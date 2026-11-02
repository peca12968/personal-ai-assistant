from datetime import datetime, timedelta, timezone
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, goal_review, reminders as r
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store


class ReminderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name);self.store = Store(self.root/'state.sqlite3')
        self.sid = self.store.new_session()
        self.now = datetime(2026,11,3,19,0,tzinfo=timezone(timedelta(hours=8)))

    def enable(self, daily_time='19:00'):
        return r.save_settings(self.store, enabled=True, daily_time=daily_time, expected_revision=r.settings(self.store)['revision'])

    def goal(self, status='pending', sid=None):
        gid = a.create_goal(self.store, sid or self.sid, 'PRIVATE_FOLLOWUP_'+status)
        a.update(self.store,gid,status,question='需要日期' if status=='needs_input' else '')
        return gid

    def test_default_off_before_time_busy_and_empty_days_do_not_consume_notice(self):
        self.goal()
        self.assertFalse(r.settings(self.store)['enabled'])
        self.assertIsNone(r.check(self.store,now=self.now))
        self.enable()
        self.assertIsNone(r.check(self.store,now=self.now-timedelta(seconds=1)))
        self.assertIsNone(r.check(self.store,now=self.now,busy=True))
        self.assertIsNone(r.settings(self.store)['last_day'])
        notice = r.check(self.store,now=self.now)
        self.assertEqual(notice['count'],1)
        r.respond(self.store,notice['token'],now=self.now)
        self.assertIsNone(r.check(Store(self.store.path),now=self.now+timedelta(hours=1)))

    def test_empty_late_open_and_next_day_have_no_catchup_burst(self):
        self.enable()
        self.assertIsNone(r.check(self.store,now=self.now))
        self.assertIsNone(r.settings(self.store)['last_day'])
        self.goal()
        late = self.now+timedelta(days=4,hours=2)
        notice=r.check(self.store,now=late)
        self.assertEqual(r.settings(self.store)['last_day'],'2026-11-07')
        r.respond(self.store,notice['token'],now=late)
        self.assertIsNone(r.check(self.store,now=late+timedelta(minutes=1)))
        self.assertIsNotNone(r.check(self.store,now=late+timedelta(days=1)))

    def test_snooze_survives_restart_and_cross_midnight_then_disable_cancels(self):
        self.goal();self.enable('23:55')
        now=self.now.replace(hour=23,minute=55)
        notice=r.check(self.store,now=now)
        r.respond(self.store,notice['token'],snooze=True,now=now)
        reopened=Store(self.store.path)
        self.assertIsNone(r.check(reopened,now=now+timedelta(minutes=14,seconds=59)))
        resumed=r.check(reopened,now=now+timedelta(minutes=15))
        self.assertNotEqual(resumed['token'],notice['token'])
        self.assertEqual(r.settings(reopened)['last_day'],'2026-11-04')
        r.save_settings(reopened,enabled=False,daily_time='23:55',expected_revision=1)
        self.assertIsNone(r.check(reopened,now=now+timedelta(days=2)))
        self.assertIsNone(r.settings(reopened)['notice_token'])

    def test_two_windows_share_one_notice_and_old_actions_cannot_override_snooze(self):
        self.goal();self.enable()
        barrier=threading.Barrier(2);results=[];errors=[]
        def worker():
            try:
                barrier.wait();results.append(r.check(self.store,now=self.now))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertFalse(errors)
        self.assertEqual(len({n['token'] for n in results}),1)
        r.respond(self.store,results[0]['token'],snooze=True,now=self.now)
        with self.assertRaisesRegex(ValueError,'其他視窗'):
            r.respond(self.store,results[1]['token'],now=self.now)
        self.assertIsNone(r.check(self.store,now=self.now))

    def test_hidden_and_recorded_running_excluded_priority_and_full_count(self):
        hidden=self.goal();running=self.goal('running')
        row=next(x for x in goal_review.snapshot(self.store)['items'] if x['goal_id']==hidden)
        goal_review.set_hidden(self.store,self.sid,hidden,fingerprint=row['fingerprint'],hidden=True)
        for status in ('pending','paused','failed','report_ready','needs_input'):self.goal(status)
        self.enable();notice=r.check(self.store,now=self.now)
        self.assertEqual(notice['count'],5)
        self.assertEqual([v['recorded_status'] for v in notice['items']],['needs_input','report_ready','failed'])
        self.assertIn('其餘項目',r.describe(notice))
        self.assertIn('追加工作需另建',r.describe(notice))
        self.assertNotIn(running,[v['goal_id'] for v in notice['items']])

    def test_active_notice_refreshes_status_and_hiding_all_removes_it_without_execution(self):
        gid=self.goal();self.enable()
        notice=r.check(self.store,now=self.now)
        a.update(self.store,gid,'needs_input',question='哪一天？')
        refreshed=r.check(self.store,now=self.now+timedelta(minutes=1))
        self.assertEqual(refreshed['token'],notice['token'])
        self.assertEqual(refreshed['items'][0]['next_action'],'reply')
        row=goal_review.snapshot(self.store)['items'][0]
        goal_review.set_hidden(self.store,self.sid,gid,fingerprint=row['fingerprint'],hidden=True)
        self.assertIsNone(r.check(self.store,now=self.now+timedelta(minutes=2)))
        self.assertIsNone(r.settings(self.store)['notice_token'])
        self.assertEqual(a.get_goal(self.store,self.sid,gid)['status'],'needs_input')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_steps').fetchone()[0],0)

    def test_selected_source_change_does_not_suggest_resume_and_no_private_chat_injection(self):
        other=self.store.new_session();gid=self.goal(sid=other)
        path=self.root/'terms.txt';path.write_text('新增來源',encoding='utf-8');self.store.attach_document(other,path)
        self.enable();notice=r.check(self.store,now=self.now)
        self.assertEqual(notice['items'][0]['next_action'],'new_goal')
        context=Assistant(self.store,None).prepare_context(self.sid,'今天聊什麼？')
        self.assertNotIn('PRIVATE_FOLLOWUP_',json.dumps(context,ensure_ascii=False))
        self.assertEqual(self.store.messages(self.sid),[])
        self.assertEqual(self.store.memories(),[])

    def test_settings_stale_revision_invalid_time_and_toggling_do_not_reset_today(self):
        self.goal();self.enable();notice=r.check(self.store,now=self.now)
        for value in ('7:00','24:00','12:60','19:00\n','no'):
            with self.assertRaises(ValueError):r.save_settings(self.store,enabled=True,daily_time=value,expected_revision=1)
        with self.assertRaises(ValueError):r.save_settings(self.store,enabled=1,daily_time='19:00',expected_revision=1)
        r.save_settings(self.store,enabled=False,daily_time='18:00',expected_revision=1)
        with self.assertRaisesRegex(ValueError,'其他視窗'):
            r.save_settings(self.store,enabled=True,daily_time='20:00',expected_revision=1)
        self.enable('18:00')
        self.assertIsNone(r.check(self.store,now=self.now))
        self.assertEqual(r.settings(self.store)['last_day'],'2026-11-03')
        with self.assertRaises(ValueError):r.respond(self.store,notice['token'],now=self.now)

    def test_clock_rollback_and_offset_change_do_not_repeat_consumed_day(self):
        self.goal();self.enable();notice=r.check(self.store,now=self.now)
        r.respond(self.store,notice['token'],now=self.now)
        self.assertIsNone(r.check(self.store,now=self.now-timedelta(days=1)))
        self.assertIsNone(r.check(self.store,now=self.now.astimezone(timezone(timedelta(hours=-8)))))
        self.assertIsNone(r.check(self.store,now=self.now+timedelta(minutes=30)))
        self.assertIsNotNone(r.check(self.store,now=self.now+timedelta(days=1)))
        with self.assertRaises(ValueError):r.check(self.store,now=datetime(2026,11,3))

    def test_dismiss_old_visible_notice_before_todays_time_means_today_is_dismissed(self):
        self.goal();self.enable();notice=r.check(self.store,now=self.now)
        morning=self.now+timedelta(hours=12)
        reopened=r.check(Store(self.store.path),now=morning)
        self.assertEqual(reopened['token'],notice['token'])
        r.respond(self.store,reopened['token'],now=morning)
        self.assertIsNone(r.check(self.store,now=self.now+timedelta(days=1)))
        self.assertIsNotNone(r.check(self.store,now=self.now+timedelta(days=2)))

    def test_backup_keeps_snooze_and_legacy_database_upgrades_disabled(self):
        self.goal();self.enable();notice=r.check(self.store,now=self.now)
        r.respond(self.store,notice['token'],snooze=True,now=self.now)
        backup=create_backup(self.store.path,self.root/'saved.paibackup')
        restored=restore_backup(backup['path'],self.root/'restored')
        copy=Store(Path(restored['data_dir'])/'assistant.sqlite3')
        self.assertEqual(r.settings(copy),r.settings(self.store))
        self.assertIsNone(r.check(copy,now=self.now+timedelta(minutes=10)))
        self.assertIsNotNone(r.check(copy,now=self.now+timedelta(minutes=15)))
        with self.store.connect() as db:db.execute('DROP TABLE review_reminders')
        validate_database(self.store.path)
        old=Store(self.store.path)
        self.assertFalse(r.settings(old)['enabled'])

    def test_backup_rejects_malformed_state_and_due_write_failure_does_not_consume_day(self):
        self.goal();self.enable()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_notice BEFORE UPDATE OF last_day ON review_reminders BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):r.check(self.store,now=self.now)
        self.assertIsNone(r.settings(self.store)['last_day'])
        with self.store.connect() as db:
            db.execute('DROP TRIGGER reject_notice')
            db.execute("UPDATE review_reminders SET snooze_until=100,notice_token=NULL WHERE id=1")
        with self.assertRaisesRegex(ValueError,'缺少來源'):validate_database(self.store.path)


if __name__=='__main__':unittest.main()
