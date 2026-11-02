import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy as a, goal_stages
from assistant.answer_runs import LeaseBusy, lease
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def paused(self, *actions, **kwargs):
        gid = a.create_goal(self.store, self.sid, '整理計算與文件', **kwargs)
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(*actions), max_steps=len(actions))
        return gid

    def attach(self, content):
        file = self.root / 'note.txt'
        file.write_text(content, encoding='utf-8')
        return self.store.attach_document(self.sid, file)

    def test_exhausted_parent_can_fork_without_mutation_execution_or_budget_reset(self):
        calc = action('calculate', operation='sum', values=['3', '4'])
        parent = self.paused(*([calc] * 12))
        for count in (12, 8):
            a.run_goal(self.store, self.sid, parent, ScriptedEngine(*([calc] * count)), max_steps=count)
        before = a.get_goal(self.store, self.sid, parent)
        child = a.create_goal(self.store, self.sid, '接續整理', parent_id=parent)
        saved = a.get_goal(self.store, self.sid, child)
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['steps'], [])
        self.assertEqual(before, a.get_goal(self.store, self.sid, parent))
        checkpoint = saved['previous_stage']['checkpoint']
        self.assertEqual(checkpoint['step_count'], 32)
        self.assertEqual([r['sequence'] for r in checkpoint['activity']], list(range(1, 33)))
        self.assertEqual(checkpoint['activity'][-1]['observation_metadata']['value']['exact'], '7')
        engine = ScriptedEngine()
        self.assertEqual(len(a.run_goal(self.store, self.sid, parent, engine)['steps']), 32)
        self.assertEqual(engine.prompts, [])

    def test_question_notes_and_failed_progress_are_frozen_without_answering(self):
        parent = self.paused(action('ask', question='需要多少份？'))
        child = a.create_goal(self.store, self.sid, '確認需求後繼續', parent_id=parent)
        before = a.get_goal(self.store, self.sid, child)['previous_stage']
        self.assertEqual(before['checkpoint']['question'], '需要多少份？')
        a.reply(self.store, self.sid, parent, '三份，保留原件。')
        a.update(self.store, parent, 'failed', notice='引擎中斷')
        newer = a.create_goal(self.store, self.sid, '繼續', parent_id=parent)
        saved = a.get_goal(self.store, self.sid, newer)['previous_stage']
        self.assertIn('三份，保留原件', saved['user_notes'])
        self.assertEqual(saved['recorded_status'], 'failed')
        self.assertEqual(saved['checkpoint']['question'], '')
        self.assertEqual(saved['checkpoint']['notice'], '引擎中斷')
        self.assertEqual(before, a.get_goal(Store(self.store.path), self.sid, child)['previous_stage'])

    def test_metadata_is_bounded_excludes_private_bodies_and_is_not_citation_coverage(self):
        did = self.attach('PRIVATE_DOCUMENT_BODY')
        self.store.remember('PRIVATE_MEMORY_BODY', 'preference')
        parent = self.paused(action('read', document_id=did, start_line=1, end_line=1),
                             action('recall', query='偏好'), action('calculate', operation='quotient', values=['1', '0']))
        child = a.create_goal(self.store, self.sid, '引用原文', parent_id=parent)
        saved = a.get_goal(self.store, self.sid, child)['previous_stage']
        encoded = json.dumps(saved)
        self.assertNotIn('PRIVATE_DOCUMENT_BODY', encoded)
        self.assertNotIn('PRIVATE_MEMORY_BODY', encoded)
        self.assertNotIn('companion_profile', encoded)
        rows = saved['checkpoint']['activity']
        self.assertEqual(rows[0]['observation_metadata']['value']['provided_ranges'][0]['document_id'], did)
        self.assertEqual(rows[-1]['tool'], 'invalid')
        self.assertIn('error', rows[-1]['observation_metadata']['value'])
        result = a.run_goal(self.store, self.sid, child, ScriptedEngine(
            action('finish', report=f'原文[D{did}:L1]'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('finish', report=f'已重新讀取[D{did}:L1]')))
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(result['status'], 'report_ready')
        long = goal_stages.activity({'sequence': 1, 'action_json': json.dumps(
            {'tool': 'calculate', 'args': {'operation': 'sum', 'values': ['1' * 20 + '.' + '1' * 10] * 50}, 'note': '長' * 400}),
            'observation_json': json.dumps({'requested_ranges': [{'x': 'z' * 2000}]})})
        self.assertTrue(long['purpose']['truncated'])
        self.assertTrue(long['request']['truncated'])
        self.assertTrue(long['observation_metadata']['truncated'])
        self.assertLess(len(json.dumps(long, ensure_ascii=False)), 3500)

    def test_csv_plan_receipts_do_not_copy_spec_results_or_scope(self):
        from test_autonomy_csv import DATA, spec
        file = self.root / 'sales.csv'
        file.write_text(DATA, encoding='utf-8')
        did = self.store.attach_document(self.sid, file)
        parent = self.paused(action('plan_csv', spec=spec()), csv_document_id=did)
        plan_id = a.get_goal(self.store, self.sid, parent)['csv_plans'][0]['plan_id']
        a.run_goal(self.store, self.sid, parent, ScriptedEngine(action('execute_csv', plan_id=plan_id)), max_steps=1)
        child = a.create_goal(self.store, self.sid, '繼續分析', parent_id=parent)
        goal = a.get_goal(self.store, self.sid, child)
        self.assertIsNone(goal['csv_source'])
        self.assertEqual(goal['csv_plans'], [])
        saved = goal['previous_stage']['checkpoint']
        self.assertEqual(saved['saved_csv_plans'][0]['status'], 'succeeded')
        self.assertNotIn('group_column', json.dumps(saved))
        self.assertNotIn('24.1667', json.dumps(saved))
        for configured in (child, a.create_goal(self.store, self.sid, '另選CSV', parent_id=parent, csv_document_id=did)):
            result = a.run_goal(self.store, self.sid, configured, ScriptedEngine(action('read_csv_result', plan_id=plan_id, offset=0)), max_steps=1)
            self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')

    def test_comparison_page_cursor_and_date_timestamp_remain_historical(self):
        old = self.attach('\n'.join(f'固定{i}\n舊{i}' for i in range(8)))
        file = self.root / 'new.txt'
        file.write_text('\n'.join(f'固定{i}\n新{i}' for i in range(8)), encoding='utf-8')
        new = self.store.attach_document(self.sid, file)
        parent = self.paused(action('compare_documents', old_document_id=old, new_document_id=new, offset=0),
                             action('date_math', operation='today'))
        child = a.create_goal(self.store, self.sid, '接續剩下差異', parent_id=parent)
        rows = a.get_goal(self.store, self.sid, child)['previous_stage']['checkpoint']['activity']
        meta = rows[0]['observation_metadata']['value']
        self.assertEqual(meta['total_changes'], 8)
        self.assertEqual(meta['next_offset'], 6)
        self.assertFalse(meta['all_changes_in_page'])
        date = rows[1]['observation_metadata']['value']
        self.assertIn('observed_at_utc', date)
        self.assertEqual(date['timezone_source'], 'computer_local')

    def test_changed_sources_are_current_and_old_files_and_permissions_are_not_inherited(self):
        did = self.attach('舊版費用10元')
        parent = self.paused(action('read', document_id=did, start_line=1, end_line=1),
                             action('write_artifact', filename='draft.txt', content='PRIVATE_DRAFT_BODY'), file_delivery=True)
        self.attach('新版費用20元')
        child = a.create_goal(self.store, self.sid, '依新版接續', parent_id=parent)
        goal = a.get_goal(self.store, self.sid, child)
        self.assertFalse(goal['previous_stage']['sources_match_current'])
        self.assertFalse(goal['file_delivery'])
        self.assertEqual(goal['artifacts'], [])
        self.assertEqual(goal['previous_stage']['checkpoint']['saved_files'][0]['name'], 'draft.txt')
        self.assertNotIn('PRIVATE_DRAFT_BODY', json.dumps(goal['previous_stage']))
        result = a.run_goal(self.store, self.sid, child, ScriptedEngine(
            action('deliver', filenames=['draft.txt'], report='交付'),
            action('read', document_id=did, start_line=1, end_line=1)), max_steps=2)
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertIn('20元', result['steps'][1]['observation']['excerpts'][0]['text'])
        self.store.detach_document(self.sid, did)
        removed = a.create_goal(self.store, self.sid, '來源已移除', parent_id=parent)
        self.assertEqual(a.get_goal(self.store, self.sid, removed)['sources'], [])

    def test_pending_running_other_session_and_active_lease_are_rejected_atomically(self):
        parent = self.paused(action('calculate', operation='sum', values=['1']))
        before = a.goals(self.store, self.sid)
        with lease(self.store, parent), self.assertRaises(LeaseBusy):
            a.create_goal(self.store, self.sid, '不能帶入', parent_id=parent)
        with self.assertRaises(ValueError):
            a.create_goal(self.store, self.store.new_session(), '不能帶入', parent_id=parent)
        self.assertEqual(before, a.goals(self.store, self.sid))
        for status in ('running', 'pending'):
            a.update(self.store, parent, status)
            with self.assertRaises(ValueError):
                a.create_goal(self.store, self.sid, '不能帶入', parent_id=parent)
        self.assertEqual(len(a.goals(self.store, self.sid)), 1)

    def test_only_direct_progress_is_carried_and_ordinary_chat_has_no_log(self):
        parent = self.paused(action('calculate', operation='sum', values=['71839']))
        child = a.create_goal(self.store, self.sid, '接續', parent_id=parent)
        a.run_goal(self.store, self.sid, child, ScriptedEngine(action('calculate', operation='sum', values=['42'])), max_steps=1)
        grandchild = a.create_goal(self.store, self.sid, '再接續', parent_id=child)
        saved = json.dumps(a.get_goal(self.store, self.sid, grandchild)['previous_stage'])
        self.assertNotIn('71839', saved)
        self.assertIn('42', saved)
        self.assertNotIn('checkpoint', json.dumps(goal_context(self.store, self.sid)))
        ordinary = a.create_goal(self.store, self.sid, '獨立目標')
        self.assertIsNone(a.get_goal(self.store, self.sid, ordinary)['previous_stage'])

    def test_backup_and_full_checkpoint_integrity(self):
        parent = self.paused(action('ask', question='哪一天？'))
        child = a.create_goal(self.store, self.sid, '接續', parent_id=parent)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(self.store, self.sid, child), a.get_goal(restored, self.sid, child))
        with self.store.connect() as db:
            value = json.loads(db.execute('SELECT context_json FROM autonomous_handoffs WHERE goal_id=?', (child,)).fetchone()[0])
            value['checkpoint']['question'] = '偷偷改掉的問題'
            db.execute('UPDATE autonomous_handoffs SET context_json=? WHERE goal_id=?', (json.dumps(value), child))
        with self.assertRaisesRegex(ValueError, '快照不一致'):
            a.get_goal(self.store, self.sid, child)
        with self.assertRaisesRegex(ValueError, '快照不一致'):
            validate_database(self.store.path)

    def test_cli_can_save_checkpoint_child_without_model(self):
        parent = self.paused(action('calculate', operation='sum', values=['1']))
        result = subprocess.run([sys.executable, '-m', 'assistant', '--data-dir', str(self.root),
            '--session', self.sid, '--new-goal', '接續整理', '--from-goal', parent],
            capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['steps'], [])
        self.assertEqual(saved['previous_stage']['checkpoint']['step_count'], 1)


if __name__ == '__main__':
    unittest.main()
