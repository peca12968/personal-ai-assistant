import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from assistant.autonomy_status import goal_context


def action(tool, **args):
    return json.dumps({'tool': tool, 'args': args, 'note': '執行必要步驟'}, ensure_ascii=False)


class ScriptedEngine:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts = []

    def answer(self, prompt, cancel=None):
        self.prompts.append(prompt)
        value = next(self.answers)
        return value() if callable(value) else value


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def goal(self, objective='整理文件並完成可用成果'):
        return a.create_goal(self.store, self.sid, objective)

    def attach(self, content, sid=None, name='note.txt'):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, path)

    def test_tool_loop_uses_observations_and_persists_report(self):
        did = self.attach('方案甲費用120元\n方案乙費用80元\n保留30天')
        gid = self.goal()
        report = f'合計200元。[D{did}:L1-L2]'
        engine = ScriptedEngine(action('search', query='費用'),
            action('read', document_id=did, start_line=1, end_line=3),
            action('calculate', operation='sum', values=['120', '80']), action('finish', report=report))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['result'], report)
        self.assertIn('保留30天', engine.prompts[2])
        self.assertIn('"exact": "200"', engine.prompts[3])
        self.assertEqual(result['steps'][-1]['observation']['citation_checks'][0]['range_valid'], True)
        self.assertEqual(a.get_goal(Store(self.store.path), self.sid, gid), result)
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None), result)

    def test_ask_reply_resume_and_session_isolation(self):
        gid = self.goal('安排會議')
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='哪一天？')))
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None), result)
        other = self.store.new_session()
        with self.assertRaises(ValueError):
            a.reply(self.store, other, gid, '明天')
        self.assertEqual(a.goals(self.store, other), [])
        a.reply(self.store, self.sid, gid, '明天下午')
        engine = ScriptedEngine(action('finish', report='明天下午的會議準備清單。'))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertIn('明天下午', engine.prompts[0])
        self.assertEqual(len(result['steps']), 2)
        with self.assertRaises(ValueError):
            a.reply(self.store, self.sid, gid, '不應覆寫成果')

    def test_unread_citation_rejected_then_repaired(self):
        did = self.attach('已確認事項')
        gid = self.goal()
        engine = ScriptedEngine(action('finish', report=f'事項[D{did}:L1]'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('finish', report=f'事項[D{did}:L1]'))
        result = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(result['status'], 'report_ready')

    def test_read_cannot_access_other_session_and_reports_incomplete_range(self):
        other = self.store.new_session()
        secret = self.attach('PRIVATE_OTHER_SESSION', other)
        did = self.attach('public', name='public.txt')
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(
            action('read', document_id=secret, start_line=1, end_line=1),
            action('read', document_id=did, start_line=1, end_line=2)), max_steps=2)
        self.assertNotIn('PRIVATE_OTHER_SESSION', json.dumps(result))
        self.assertEqual([s['observation']['requested_ranges'][0]['status'] for s in result['steps']],
                         ['document_not_attached', 'line_out_of_bounds'])

    def test_source_change_before_or_during_model_call_stops_action(self):
        did = self.attach('old')
        gid = self.goal()
        def mutate():
            self.attach('new')
            return action('finish', report='不能保存過時成果')
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(mutate))
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['steps'], [])
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None)['steps'], [])
        self.assertIn('改變', result['notice'])

    def test_cancelled_generation_does_not_execute_action(self):
        cancel = threading.Event()
        def response():
            cancel.set()
            return action('finish', report='不應保存')
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(response), cancel=cancel)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['steps'], [])

    def test_failure_preserves_steps_then_recovery_continues(self):
        gid = self.goal()
        def fail():
            raise RuntimeError('PRIVATE_ENGINE_ERROR')
        with self.assertRaises(RuntimeError):
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('calculate', operation='sum', values=['1']), fail))
        saved = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(saved['status'], 'failed')
        self.assertNotIn('PRIVATE_ENGINE_ERROR', json.dumps(saved))
        a.update(self.store, gid, 'running')  # Previous process exited; no active lease.
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report='保留先前計算1。')))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(len(result['steps']), 2)

    def test_active_lease_blocks_duplicate_runner_without_state_change(self):
        gid = self.goal()
        with lease(self.store, gid):
            with self.assertRaises(LeaseBusy):
                a.run_goal(self.store, self.sid, gid, None)
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'pending')

    def test_step_budgets_survive_restart_and_invalid_actions_count(self):
        gid = self.goal()
        invalid = action('shell', command='DO_NOT_EXECUTE')
        for _ in range(3):
            result = a.run_goal(Store(self.store.path), self.sid, gid, ScriptedEngine(*([invalid] * 12)), max_steps=12)
        self.assertEqual(len(result['steps']), 32)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(a.run_goal(self.store, self.sid, gid, None)['steps'], result['steps'])
        self.assertTrue(all(s['action']['tool'] == 'invalid' for s in result['steps']))

    def test_exact_arithmetic_and_rejection(self):
        self.assertEqual(a.calculate({'operation': 'sum', 'values': ['0.1', '0.2']})['exact'], '3/10')
        self.assertTrue(a.calculate({'operation': 'quotient', 'values': ['1', '3']})['rounded'])
        self.assertEqual(a.calculate({'operation': 'product', 'values': ['-2', '0.5']})['exact'], '-1')
        self.assertEqual(a.calculate({'operation': 'difference', 'values': ['1', '3']})['exact'], '-2')
        for args in [{'operation': 'sum', 'values': ['__import__("os")']},
                     {'operation': 'quotient', 'values': ['1', '0']},
                     {'operation': 'sum', 'values': [1]}, {'operation': 'sum', 'values': ['1e20']}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                a.calculate(args)

    def test_recall_excludes_disabled_memory(self):
        self.store.remember('喜歡簡短清楚的回答', 'preference')
        old = self.store.remember('DISABLED_PRIVATE_MEMORY', 'preference')
        self.store.forget(old)
        result = a.run_goal(self.store, self.sid, self.goal(), ScriptedEngine(action('recall', query='回答偏好')), max_steps=1)
        self.assertNotIn('DISABLED_PRIVATE_MEMORY', json.dumps(result, ensure_ascii=False))
        self.assertTrue(result['steps'][0]['observation']['memories'])

    def test_backup_roundtrip_and_legacy_database_migration(self):
        gid = self.goal()
        result = a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='日期？')))
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        with restored.connect() as db:
            db.execute('DROP TABLE autonomous_steps')
            db.execute('DROP TABLE autonomous_goals')
        self.assertNotIn('autonomous_goals', validate_database(restored.path))
        upgraded = Store(restored.path)
        self.assertEqual(a.goals(upgraded, self.sid), [])
        with upgraded.connect() as db:
            db.execute('DROP TABLE autonomous_steps')
        with self.assertRaisesRegex(ValueError, '不完整'):
            validate_database(upgraded.path)

    def test_cli_can_create_and_read_without_model(self):
        base = [sys.executable, '-m', 'assistant', '--data-dir', str(self.root), '--session', self.sid]
        result = subprocess.run([*base, '--new-goal', '比較文件'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        gid = json.loads(result.stdout)['id']
        result = subprocess.run([*base, '--goal-status', gid], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(json.loads(result.stdout)['objective'], '比較文件')
        result = subprocess.run([*base, '--run-goal', gid, '--goal-steps', '0'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_chat_status_is_bounded_and_does_not_replay_saved_tool_data(self):
        for _ in range(4):
            gid = self.goal('x' * 450)
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report='PRIVATE_OLD_REPORT')))
        context = goal_context(self.store, self.sid)
        self.assertTrue(context['limited'])
        self.assertEqual(len(context['goals']), 3)
        self.assertTrue(context['goals'][0]['objective_truncated'])
        self.assertNotIn('PRIVATE_OLD_REPORT', json.dumps(context))
        self.assertEqual(context['goals'][0]['recorded_status'], 'report_ready')
        self.assertEqual(goal_context(self.store, self.store.new_session())['goals'], [])


if __name__ == '__main__':
    unittest.main()
