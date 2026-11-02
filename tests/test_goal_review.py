import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, goal_review as r, artifacts
from assistant.answer_runs import lease
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def row(self, gid):
        return next(row for row in r.snapshot(self.store, include_hidden=True)['items'] if row['goal_id'] == gid)

    def hide(self, gid):
        row = self.row(gid)
        return r.set_hidden(self.store, row['session_id'], gid, fingerprint=row['fingerprint'], hidden=True)

    def test_cross_session_review_does_not_replay_other_sessions_into_chat(self):
        gid = a.create_goal(self.store, self.sid, 'OLD_SESSION_OBJECTIVE')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='何時生效？')))
        other = self.store.new_session()
        new = a.create_goal(self.store, other, '新工作')
        rows = r.snapshot(Store(self.store.path))['items']
        self.assertEqual([row['goal_id'] for row in rows], [gid, new])
        self.assertEqual(rows[0]['question'], '何時生效？')
        self.assertEqual(rows[0]['next_action'], 'reply')
        self.assertNotIn('OLD_SESSION_OBJECTIVE', json.dumps(goal_context(self.store, other)))

    def test_hide_persists_without_accepting_or_altering_goal_and_new_progress_resurfaces(self):
        gid = a.create_goal(self.store, self.sid, '整理方案')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='日期？')))
        before = a.get_goal(self.store, self.sid, gid)
        self.hide(gid)
        reopened = Store(self.store.path)
        self.assertEqual(r.snapshot(reopened)['visible_count'], 0)
        self.assertEqual(r.snapshot(reopened)['hidden_count'], 1)
        self.assertEqual(a.get_goal(reopened, self.sid, gid), before)
        a.reply(reopened, self.sid, gid, '11月1日')
        self.assertEqual(r.snapshot(reopened)['visible_count'], 1)
        self.assertEqual(self.row(gid)['next_action'], 'resume')

    def test_stale_review_and_wrong_session_cannot_hide_changed_goal(self):
        gid = a.create_goal(self.store, self.sid, '目標')
        old = self.row(gid)
        a.update(self.store, gid, 'needs_input', question='請補充')
        with self.assertRaisesRegex(ValueError, '新進度'):
            r.set_hidden(self.store, self.sid, gid, fingerprint=old['fingerprint'], hidden=True)
        with self.assertRaisesRegex(ValueError, '此對話'):
            r.set_hidden(self.store, self.store.new_session(), gid, fingerprint=self.row(gid)['fingerprint'], hidden=True)
        self.assertEqual(r.snapshot(self.store)['visible_count'], 1)

    def test_source_change_resurfaces_and_does_not_suggest_impossible_resume(self):
        path = self.root / 'terms.txt'
        path.write_text('費用120元', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '比較費用')
        self.hide(gid)
        # Editing the external original alone does not change the imported snapshot.
        path.write_text('費用150元', encoding='utf-8')
        self.assertEqual(r.snapshot(self.store)['visible_count'], 0)
        self.store.attach_document(self.sid, path)
        row = self.row(gid)
        self.assertFalse(row['hidden'])
        self.assertFalse(row['sources_match'])
        self.assertEqual(row['next_action'], 'new_goal')
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'pending')

    def test_step_limit_and_saved_running_status_are_not_marked_resumable_or_dead(self):
        gid = a.create_goal(self.store, self.sid, '長工作')
        for _ in range(3):
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(*([action('calculate', operation='sum', values=['1'])] * 12)), max_steps=12)
        self.assertEqual(self.row(gid)['step_count'], 32)
        self.assertEqual(self.row(gid)['next_action'], 'new_goal')
        other = a.create_goal(self.store, self.sid, '執行紀錄')
        a.update(self.store, other, 'running')
        with lease(self.store, other):
            self.assertEqual(self.row(other)['next_action'], 'inspect')
            self.assertEqual(a.get_goal(self.store, self.sid, other)['status'], 'running')
        self.assertEqual(self.row(other)['next_action'], 'inspect')
        self.assertEqual(a.get_goal(self.store, self.sid, other)['status'], 'running')

    def test_report_is_reviewable_even_when_sources_change_and_never_implies_acceptance(self):
        gid = a.create_goal(self.store, self.sid, '成果目標')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report='PRIVATE_REPORT_BODY')))
        path = self.root / 'new.txt'
        path.write_text('新來源', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        row = self.row(gid)
        self.assertEqual(row['next_action'], 'review')
        self.assertFalse(row['sources_match'])
        self.assertNotIn('PRIVATE_REPORT_BODY', json.dumps(row))
        self.hide(gid)
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'report_ready')
        hidden = self.row(gid)
        r.set_hidden(self.store, self.sid, gid, fingerprint=hidden['fingerprint'], hidden=False)
        self.assertEqual(r.snapshot(self.store)['visible_count'], 1)

    def test_saved_file_progress_resurfaces_without_reading_files_or_returning_contents(self):
        gid = a.create_goal(self.store, self.sid, '文件目標', file_delivery=True)
        self.hide(gid)
        artifacts.write(self.store, gid, 'private-name.md', 'PRIVATE_FILE_CONTENT')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('review must not read external file')):
            row = self.row(gid)
        self.assertFalse(row['hidden'])
        self.assertEqual(row['saved_artifact_count'], 1)
        self.assertNotIn('private-name.md', json.dumps(row))
        self.assertNotIn('PRIVATE_FILE_CONTENT', json.dumps(row))
        self.assertNotIn(str(self.store.path), json.dumps(row))

    def test_pagination_counts_clipping_and_no_silent_loss(self):
        for index in range(105):
            a.create_goal(self.store, self.sid, str(index) + 'x' * 650)
        first = r.snapshot(self.store)
        second = r.snapshot(self.store, offset=100)
        self.assertEqual(len(first['items']), 100)
        self.assertEqual(len(second['items']), 5)
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        ids = {row['goal_id'] for row in first['items'] + second['items']}
        self.assertEqual(len(ids), 105)
        self.assertTrue(all(len(row['objective']) == 600 and row['objective_truncated'] for row in first['items']))
        self.hide(first['items'][0]['goal_id'])
        self.assertEqual(r.snapshot(self.store)['visible_count'], 104)
        self.assertEqual(r.snapshot(self.store, include_hidden=True)['total_count'], 105)
        for kwargs in [{'offset': -1}, {'offset': True}, {'limit': 0}, {'include_hidden': 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                r.snapshot(self.store, **kwargs)

    def test_backup_preserves_hidden_state_and_legacy_schema_upgrades_visible(self):
        gid = a.create_goal(self.store, self.sid, '待辦')
        self.hide(gid)
        saved = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(saved['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(r.snapshot(restored)['hidden_count'], 1)
        with restored.connect() as db:
            db.execute('DROP TABLE goal_reviews')
        self.assertNotIn('goal_reviews', validate_database(restored.path))
        restored = Store(restored.path)
        self.assertEqual(r.snapshot(restored)['visible_count'], 1)
        with restored.connect() as db:
            db.execute('INSERT INTO goal_reviews(goal_id,fingerprint) VALUES(?,?)', (gid, 'invalid'))
        with self.assertRaisesRegex(ValueError, '指紋'):
            validate_database(restored.path)

    def test_cli_review_is_offline_read_only_and_rejects_mixed_scope(self):
        from assistant.__main__ import main
        gid = a.create_goal(self.store, self.sid, '離線回顧')
        before = a.get_goal(self.store, self.sid, gid)
        with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--review-goals']), \
                patch('assistant.__main__.CodexEngine', side_effect=AssertionError('must not call model')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        self.assertEqual(json.loads(output.getvalue())['items'][0]['goal_id'], gid)
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
        with patch('sys.argv', ['assistant', '--review-goals', '--session', self.sid]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
