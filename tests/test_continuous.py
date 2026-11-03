from assistant.goal_replies import snapshot as reply_snapshot
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, continuous as c, artifacts
from assistant.answer_runs import lease, LeaseBusy
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.companion_profile import get_profile, save_profile
from assistant.engine import EngineError
from assistant.goal_review import snapshot, set_hidden
from assistant.store import Store
from test_autonomy import action, ScriptedEngine


CALC = action('calculate', operation='product', values=['7.5', '4'])
FINISH = action('finish', report='合計30元。')


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.gid = a.create_goal(self.store, self.sid, '完成費用整理')

    def run_goal(self, *answers, **kwargs):
        return c.run_continuous(self.store, self.sid, self.gid, ScriptedEngine(*answers), **kwargs)

    def test_crosses_default_six_steps_without_manual_restart_and_persists_limits(self):
        ordinary = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(*([CALC] * 7 + [FINISH])))
        self.assertEqual((ordinary['status'], len(ordinary['steps'])), ('paused', 6))
        self.gid = a.create_goal(self.store, self.sid, '完整完成八步')
        result = self.run_goal(*([CALC] * 7 + [FINISH]), max_steps=10, max_seconds=120)
        self.assertEqual((result['status'], len(result['steps'])), ('report_ready', 8))
        record = result['continuous_runs'][0]
        self.assertEqual((record['start_step'], record['end_step'], record['max_steps'], record['max_seconds']), (0, 8, 10, 120))
        self.assertEqual(record['reason'], 'report_ready')
        self.assertIsNotNone(record['stopped_at'])
        self.assertEqual(a.get_goal(Store(self.store.path), self.sid, self.gid), result)
        self.assertEqual(self.run_goal(), result)  # Completed work cannot restart.

    def test_budget_resume_and_cumulative_limit(self):
        result = self.run_goal(*([CALC] * 20), max_steps=20)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'step_budget')
        result = self.run_goal(*([CALC] * 12), max_steps=24)
        self.assertEqual((len(result['steps']), result['continuous_runs'][0]['reason']), (32, 'goal_limit'))
        self.assertEqual(result['continuous_runs'][0]['start_step'], 20)
        self.assertIn('下一階段', result['notice'])
        self.assertEqual(self.run_goal()['continuous_runs'][0]['reason'], 'goal_limit')

    def test_ask_waits_and_does_not_auto_reply(self):
        result = self.run_goal(action('ask', question='請提供日期。'), FINISH)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'needs_input')
        self.assertEqual(self.run_goal(), result)
        a.reply(self.store, self.sid, self.gid, '十一月一日', expected_fingerprint=reply_snapshot(self.store, self.sid, self.gid)["fingerprint"])
        self.assertEqual(self.run_goal(FINISH)['status'], 'report_ready')

    def test_deadline_stops_pending_model_and_cannot_execute_late_action(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.gid = a.create_goal(self.store, self.sid, '逾時停止')
                now = [100.0]
                class Engine:
                    def answer(inner, prompt, cancel):
                        now[0] = 161.0
                        self.assertTrue(cancel.is_set())
                        if raises:
                            raise EngineError('cancelled', 'cancelled')
                        return FINISH
                with patch.object(c.time, 'monotonic', side_effect=lambda: now[0]):
                    result = c.run_continuous(self.store, self.sid, self.gid, Engine(), max_seconds=60)
                self.assertEqual(result['status'], 'paused')
                self.assertEqual(result['steps'], [])
                self.assertEqual(result['continuous_runs'][0]['reason'], 'deadline')

    def test_cancellation_before_start_and_after_written_file_preserves_receipt(self):
        cancel = threading.Event(); cancel.set()
        result = self.run_goal(cancel=cancel)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'cancelled')
        cancel.clear()
        self.gid = a.create_goal(self.store, self.sid, '交付文件', file_delivery=True)
        original = artifacts.write
        def write(*args):
            receipt = original(*args)
            cancel.set()
            return receipt
        with patch.object(artifacts, 'write', side_effect=write):
            result = self.run_goal(action('write_artifact', filename='result.txt', content='已保存的內容'), cancel=cancel)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'cancelled')
        self.assertEqual(len(result['steps']), 1)
        self.assertEqual(len(result['artifacts']), 1)
        self.assertEqual(result['artifacts'][0]['recorded_status'], 'verified')
        self.assertEqual(result['result'], '')

    def test_source_change_at_start_and_between_steps_never_auto_retries(self):
        path = self.root / 'note.txt'; path.write_text('old', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        self.assertEqual(self.run_goal()['continuous_runs'][0]['reason'], 'sources_changed')
        self.gid = a.create_goal(self.store, self.sid, 'read source')
        def changed(event):
            path.write_text('new', encoding='utf-8')
            self.store.attach_document(self.sid, path)
        result = self.run_goal(CALC, FINISH, progress=changed)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'sources_changed')
        self.assertEqual(len(result['steps']), 1)

    def test_three_consecutive_errors_stop_but_repaired_attempt_resets_count(self):
        result = self.run_goal('bad', 'bad', CALC, 'bad', 'bad', FINISH)
        self.assertEqual(result['status'], 'report_ready')
        self.gid = a.create_goal(self.store, self.sid, 'invalid actions')
        result = self.run_goal('bad', 'bad', 'bad', FINISH)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'repeated_errors')
        self.assertEqual(len(result['steps']), 3)
        self.gid = a.create_goal(self.store, self.sid, 'bad read')
        bad_read = action('read', document_id=999, start_line=1, end_line=1)
        result = self.run_goal(bad_read, bad_read, bad_read, FINISH)
        self.assertEqual(result['continuous_runs'][0]['reason'], 'repeated_errors')

    def test_engine_failure_and_cleanup_failure_are_not_retried_or_hidden_by_cancel(self):
        for code in ('process_failed', 'cleanup_unconfirmed'):
            self.gid = a.create_goal(self.store, self.sid, 'failed engine')
            def fail():
                raise EngineError('test failure', code)
            with self.assertRaises(EngineError):
                self.run_goal(CALC, fail, FINISH)
            saved = a.get_goal(self.store, self.sid, self.gid)
            self.assertEqual(saved['continuous_runs'][0]['reason'], 'failed')
            self.assertEqual(saved['continuous_runs'][0]['end_step'], 1)

    def test_same_goal_exclusive_for_entire_operation_and_wrong_session_no_mutation(self):
        def attempt(event):
            with self.assertRaises(LeaseBusy):
                self.run_goal(FINISH)
            with self.assertRaises(LeaseBusy):
                a.run_goal(self.store, self.sid, self.gid, None)
        result = self.run_goal(*([CALC] * 7 + [FINISH]), progress=attempt)
        self.assertEqual(len(result['continuous_runs']), 1)
        other = self.store.new_session()
        with self.assertRaises(ValueError):
            c.run_continuous(self.store, other, self.gid, None)
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), result)

    def test_owner_loss_recovery_needs_lease_and_also_applies_to_manual_resume(self):
        with self.store.connect() as db:
            db.execute('''INSERT INTO autonomous_continuous_runs
                (id,goal_id,max_steps,max_seconds,start_step) VALUES('interrupted',?,24,1800,0)''', (self.gid,))
        with lease(self.store, self.gid), self.assertRaises(LeaseBusy):
            self.run_goal()
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['continuous_runs'][0]['reason'], 'running')
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(FINISH))
        old = result['continuous_runs'][0]
        self.assertEqual((old['reason'], old['end_step']), ('interrupted', 0))

    def test_limits_invalid_before_model_or_records_and_profile_frozen_per_operation(self):
        for steps, seconds in [(0,60), (33,60), (True,60), (2,59), (2,7201), (2,float('nan'))]:
            with self.assertRaises(ValueError):
                self.run_goal(max_steps=steps, max_seconds=seconds)
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['continuous_runs'], [])
        def change_profile(event):
            if event['step'] == 1:
                profile = get_profile(self.store)
                save_profile(self.store, {**profile['settings'], 'assistant_name':'新版測試名字'}, expected_revision=profile['revision'])
        result = self.run_goal(CALC, CALC, max_steps=2, progress=change_profile)
        self.assertEqual([step['observation']['companion_profile']['revision'] for step in result['steps']], [0,0])
        result = self.run_goal(FINISH)
        self.assertEqual(result['steps'][-1]['observation']['companion_profile']['revision'], 1)

    def test_backup_migration_and_new_attempt_resurfaces_hidden_goal(self):
        result = self.run_goal('bad', 'bad', 'bad')
        row = snapshot(self.store)['items'][0]
        set_hidden(self.store,self.sid,self.gid,fingerprint=row['fingerprint'],hidden=True)
        cancel = threading.Event(); cancel.set()
        self.run_goal(cancel=cancel)
        self.assertEqual(snapshot(self.store)['visible_count'], 1)
        current = a.get_goal(self.store,self.sid,self.gid)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,self.gid),current)
        with restored.connect() as db:
            db.execute('DROP TABLE autonomous_continuous_runs')
        validate_database(restored.path)
        restored = Store(restored.path)
        self.assertEqual(a.get_goal(restored,self.sid,self.gid)['continuous_runs'], [])

    def test_cli_explicit_limits_and_incompatible_arguments(self):
        from assistant.__main__ import main
        args = ['assistant','--data-dir',str(self.root),'--session',self.sid,'--run-goal',self.gid,
                '--continuous-steps','10','--continuous-minutes','2']
        with patch('sys.argv',args), patch('assistant.__main__.CodexEngine',return_value=ScriptedEngine(*([CALC]*7+[FINISH]))), contextlib.redirect_stdout(io.StringIO()) as out:
            main()
        result = json.loads(out.getvalue())
        self.assertEqual(result['continuous_runs'][0]['max_seconds'],120)
        for invalid in [['--continuous-minutes','2'], ['--continuous-steps','33'], args[1:]+['--goal-steps','2']]:
            with patch('sys.argv',['assistant']+invalid), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main()
            self.assertEqual(exc.exception.code,2)

    def test_upgrade_keeps_preexisting_hidden_state_until_actual_new_work(self):
        # Fingerprint generated by v0.25 (790792a) for these immutable fixture rows.
        legacy = '36ec61f5c36e5893b21cf6c5acb0004abd1d17cc3214236c199cf777f147540c'
        sid, gid = 'a' * 32, 'b' * 32
        with self.store.connect() as db:
            db.execute('INSERT INTO sessions(id,title,created_at) VALUES(?,?,?)',
                (sid, '舊版已收起', '2026-11-03 00:00:00'))
            db.execute('''INSERT INTO autonomous_goals(id,session_id,objective,status,
                sources_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
                (gid,sid,'待辦','paused','[]','2026-11-03 00:00:00','2026-11-03 00:00:00'))
            db.execute('INSERT INTO goal_reviews(goal_id,fingerprint) VALUES(?,?)', (gid,legacy))
        row = next(r for r in snapshot(self.store, include_hidden=True)['items'] if r['goal_id'] == gid)
        self.assertEqual(row['fingerprint'], legacy)
        self.assertTrue(row['hidden'])
        c.run_continuous(self.store,sid,gid,ScriptedEngine(CALC),max_steps=1)
        row = next(r for r in snapshot(self.store)['items'] if r['goal_id'] == gid)
        self.assertFalse(row['hidden'])
