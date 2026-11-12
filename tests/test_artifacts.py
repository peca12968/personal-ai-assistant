import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import artifacts as f, autonomy as a, goal_guidance, goal_replies, goal_review
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from assistant.autonomy_status import goal_context
from scripts.publish import validate_path
from test_autonomy import ScriptedEngine, action


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'state' / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.gid = a.create_goal(self.store, self.sid, '交付整理文件', file_delivery=True)

    def test_actual_utf8_bytes_and_delivery_receipt_match(self):
        body = '# 測試成果\n\n可使用的內容。\n'
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='成果.md', content=body),
            action('deliver', report='文件已交付，待確認。', filenames=['成果.md'])))
        self.assertEqual(result['status'], 'report_ready')
        receipt = result['steps'][-1]['observation']['artifacts'][0]
        actual = (f.directory(self.store, self.gid) / '成果.md').read_bytes()
        self.assertEqual(actual, body.encode('utf-8'))
        self.assertEqual(receipt['sha256'], hashlib.sha256(actual).hexdigest())
        self.assertEqual(receipt['size_bytes'], len(actual))
        summary = goal_context(self.store, self.sid)['goals'][0]
        self.assertTrue(summary['file_delivery'])
        self.assertEqual(summary['saved_artifact_count'], 1)
        self.assertNotIn(body, json.dumps(summary, ensure_ascii=False))
        self.assertNotIn('成果.md', json.dumps(summary, ensure_ascii=False))
        self.assertNotIn(str(self.store.path), json.dumps(summary))

    def test_screen_goal_cannot_write_or_inherit_file_scope(self):
        screen = a.create_goal(self.store, self.sid, '一般目標')
        result = a.run_goal(self.store, self.sid, screen, ScriptedEngine(
            action('write_artifact', filename='no.txt', content='不應寫出'), action('finish', report='文字成果')))
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(result['artifacts'], [])
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='yes.txt', content='內容'), action('deliver', report='完成', filenames=['yes.txt'])))
        child = a.create_goal(self.store, self.sid, '新階段', parent_id=self.gid)
        self.assertFalse(a.get_goal(self.store, self.sid, child)['file_delivery'])

    def test_file_goal_requires_real_files_before_report_ready(self):
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('finish', report='我已寫入不存在的文件'), action('deliver', report='已完成', filenames=['missing.md'])), max_steps=2)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['result'], '')
        self.assertTrue(all(s['action']['tool'] == 'invalid' for s in result['steps']))
        self.assertFalse((self.store.path.parent / 'autonomy-artifacts').exists())

    def test_filenames_formats_json_and_limits_are_validated_before_write(self):
        for name in ['../escape.md', 'C:\\escape.md', 'report.md:stream', 'CON.txt', 'com¹.txt', '.secret.md',
                     'report.md ', 'report.py', 'report.html', 'a/' + 'b.md']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                f.write(self.store, self.gid, name, 'text')
        for content in ['{"x":1,"x":2}', 'NaN', '{bad}']:
            with self.assertRaises(ValueError):
                f.write(self.store, self.gid, 'bad.json', content)
        for content in ['', '\x00', 'x' * 12001, '\ud800']:
            with self.assertRaises(ValueError):
                f.write(self.store, self.gid, 'bad.txt', content)
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['artifacts'], [])
        for i in range(4):
            f.write(self.store, self.gid, f'report-{i}.json', '{"sum":30}')
        with self.assertRaises(ValueError):
            f.write(self.store, self.gid, 'fifth.txt', 'too many')

    def test_same_content_retry_reuses_file_and_different_content_never_overwrites(self):
        f.write(self.store, self.gid, 'report.md', 'original')
        path = f.directory(self.store, self.gid) / 'report.md'
        stamp = path.stat().st_mtime_ns
        f.write(self.store, self.gid, 'REPORT.MD', 'original')
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        with self.assertRaises(ValueError):
            f.write(self.store, self.gid, 'report.md', 'replacement')
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(len(a.get_goal(self.store, self.sid, self.gid)['artifacts']), 1)

    def test_external_existing_file_is_not_replaced(self):
        path = f.directory(self.store, self.gid, create=True) / 'report.txt'
        path.write_text('EXTERNAL_CONTENT', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '未覆寫'):
            f.write(self.store, self.gid, 'report.txt', 'new report')
        self.assertEqual(path.read_text(), 'EXTERNAL_CONTENT')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['artifacts'][0]['recorded_status'], 'uncertain')

    def test_changed_file_is_not_delivered_as_verified(self):
        f.write(self.store, self.gid, 'report.txt', 'original')
        path = f.directory(self.store, self.gid) / 'report.txt'
        path.write_text('changed', encoding='utf-8')
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('deliver', report='已完成', filenames=['report.txt'])), max_steps=1)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(path.read_text(), 'changed')

    def test_interruption_after_intent_can_resume_without_duplicate_file(self):
        with patch('assistant.artifacts.materialize', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
                    action('write_artifact', filename='resume.md', content='saved intent')))
        saved = a.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['artifacts'][0]['recorded_status'], 'prepared')
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('deliver', report='已恢復文件', filenames=['resume.md'])))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(len(result['artifacts']), 1)
        self.assertEqual((f.directory(self.store, self.gid) / 'resume.md').read_text(), 'saved intent')

    def test_flush_failure_leaves_saved_content_and_retry_verifies_existing_bytes(self):
        with patch('assistant.artifacts.os.fsync', side_effect=OSError('simulated')):
            with self.assertRaises(ValueError):
                f.write(self.store, self.gid, 'resume.txt', 'recoverable')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['artifacts'][0]['recorded_status'], 'uncertain')
        receipt = f.materialize(self.store, self.gid, 'resume.txt')
        self.assertEqual(receipt['sha256'], hashlib.sha256(b'recoverable').hexdigest())

    def test_cancellation_keeps_executed_file_but_stops_before_delivery(self):
        cancel = threading.Event()
        original = f.write
        def write_then_cancel(*args):
            receipt = original(*args)
            cancel.set()
            return receipt
        engine = ScriptedEngine(action('write_artifact', filename='kept.txt', content='kept'),
                                action('deliver', report='不應執行', filenames=['kept.txt']))
        with patch('assistant.artifacts.write', side_effect=write_then_cancel):
            result = a.run_goal(self.store, self.sid, self.gid, engine, cancel=cancel)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['result'], '')
        self.assertEqual(len(engine.prompts), 1)
        self.assertEqual((f.directory(self.store, self.gid) / 'kept.txt').read_text(), 'kept')

    def test_unread_citations_cannot_be_saved_as_artifacts(self):
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='bad.md', content='unsupported [D99:L1]')), max_steps=1)
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(result['artifacts'], [])

    def test_directory_redirection_is_rejected_without_writing_outside_goal(self):
        outside = self.root / 'elsewhere'; outside.mkdir()
        link = self.store.path.parent / 'autonomy-artifacts'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            f.write(self.store, self.gid, 'blocked.md', 'must stay inside')
        self.assertEqual(list(outside.iterdir()), [])

    def test_backup_preserves_content_and_rebuild_is_explicit_and_scoped(self):
        f.write(self.store, self.gid, 'backup.json', '{"total":30}')
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertFalse((target / 'autonomy-artifacts').exists())
        with self.assertRaises(ValueError):
            f.rebuild(restored, restored.new_session(), self.gid)
        with lease(restored, self.gid), self.assertRaises(LeaseBusy):
            f.rebuild(restored, self.sid, self.gid)
        rebuilt = f.rebuild(restored, self.sid, self.gid)
        self.assertEqual((Path(rebuilt['directory']) / 'backup.json').read_text(), '{"total":30}')
        with restored.connect() as db:
            db.execute("UPDATE autonomous_artifacts SET content='tampered'")
        with self.assertRaises(ValueError):
            validate_database(restored.path)

    def test_legacy_backup_gets_no_file_permission_and_artifacts_are_not_publishable(self):
        plain = a.create_goal(self.store, self.sid, 'legacy')
        with self.store.connect() as db:
            db.execute('DROP TABLE autonomous_delivery_changes')
            db.execute('DROP TABLE autonomous_artifacts')
            db.execute('DROP TABLE autonomous_delivery')
        backup = create_backup(self.store.path)
        target = self.root / 'legacy'
        restore_backup(backup['path'], target)
        upgraded = Store(target / 'assistant.sqlite3')
        self.assertFalse(a.get_goal(upgraded, self.sid, plain)['file_delivery'])
        for name in ['assistant/autonomy-artifacts/private.md', 'docs/AUTONOMY-ARTIFACTS/report.json']:
            self.assertFalse(validate_path(name))

    def test_reply_switches_to_text_and_preserves_the_existing_draft(self):
        before = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='draft.txt', content='原草稿'), action('ask', question='簽收人是誰？')))
        reply = '簽收人是簡同仁。這次先不用出檔，直接在這裡交文字。'
        view = goal_replies.snapshot(self.store, self.sid, self.gid)
        goal_replies.save(self.store, self.sid, self.gid, reply, expected_fingerprint=view['fingerprint'])
        state = a.get_goal(self.store, self.sid, self.gid)
        self.assertFalse(state['file_delivery'])
        self.assertEqual(state['steps'], before['steps'])
        receipt = state['delivery_mode_change']
        self.assertEqual(reply[receipt['start']:receipt['end']], '這次先不用出檔，直接在這裡交文字')
        self.assertEqual(receipt['source_sha256'], state['reply_sources'][-1]['sha256'])
        self.assertEqual(receipt['citation'], state['reply_sources'][-1]['citation'])
        self.assertFalse(goal_context(self.store, self.sid)['goals'][0]['file_delivery'])
        self.assertFalse(goal_review.snapshot(self.store)['items'][0]['file_delivery'])
        engine = ScriptedEngine(action('write_artifact', filename='extra.txt', content='多餘'),
                                action('deliver', report='原草稿', filenames=['draft.txt']),
                                action('finish', report=f'簽收人為簡同仁。{receipt["citation"]}'))
        result = a.run_goal(self.store, self.sid, self.gid, engine)
        self.assertEqual([step['action']['tool'] for step in result['steps'][-3:]], ['invalid', 'invalid', 'finish'])
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['steps'][-1]['observation']['artifacts'], [])
        self.assertEqual(result['artifacts'], before['artifacts'])
        folder = f.directory(self.store, self.gid)
        self.assertEqual([path.name for path in folder.iterdir()], ['draft.txt'])
        self.assertIn('"file_delivery": false', engine.prompts[0])
        backup = create_backup(self.store.path, self.root / 'changed-mode.paibackup')
        target = self.root / 'changed-mode'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), result)
        self.assertFalse((target / 'autonomy-artifacts').exists())
        rebuilt = f.rebuild(restored, self.sid, self.gid)
        self.assertEqual((Path(rebuilt['directory']) / 'draft.txt').read_text(encoding='utf-8'), '原草稿')

    def test_guidance_change_while_waiting_keeps_question_and_can_restore_original_mode(self):
        question = '窗口是誰？'
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('ask', question=question)))
        stale = goal_replies.snapshot(self.store, self.sid, self.gid)
        current = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '取消文件交付，改為文字。', expected_fingerprint=current['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(waiting['question'], question)
        self.assertFalse(waiting['file_delivery'])
        self.assertEqual(waiting['delivery_mode_change']['citation'], '[更正#1]')
        with self.assertRaisesRegex(ValueError, '重新載入'):
            goal_replies.save(self.store, self.sid, self.gid, '許同仁', expected_fingerprint=stale['fingerprint'])
        current = goal_replies.snapshot(self.store, self.sid, self.gid)
        goal_replies.save(self.store, self.sid, self.gid, '窗口是許同仁。改回交檔。',
                          expected_fingerprint=current['fingerprint'])
        state = a.get_goal(self.store, self.sid, self.gid)
        self.assertTrue(state['file_delivery'])
        self.assertEqual(state['delivery_mode_change']['citation'], '[補充#1]')
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='answer.txt', content='窗口是許同仁。'),
            action('deliver', report='已交付', filenames=['answer.txt'])))
        self.assertEqual(result['status'], 'report_ready')
        validate_database(self.store.path)

    def test_quoted_conditional_questioned_and_conflicting_instructions(self):
        for text in ('舊草稿說「不用出檔。」，這段未採用。', '如果對方不收，這次不用出檔。', '這次先不用出檔？',
                     '回覆範例：\n```\n不用出檔。\n```', '回覆範例：\n~~~\n不用出檔。\n~~~',
                     "摘錄：'\n不用出檔。\n'", '> 不用出檔。'):
            with self.subTest(text=text):
                current = goal_guidance.snapshot(self.store, self.sid, self.gid)
                goal_guidance.save(self.store, self.sid, self.gid, text, expected_fingerprint=current['fingerprint'])
                self.assertTrue(a.get_goal(self.store, self.sid, self.gid)['file_delivery'])
        current = goal_guidance.snapshot(self.store, self.sid, self.gid)
        with self.assertRaisesRegex(ValueError, '同時取消及恢復'):
            goal_guidance.save(self.store, self.sid, self.gid, '不用出檔。照原要求交檔。',
                               expected_fingerprint=current['fingerprint'])
        self.assertEqual(goal_guidance.snapshot(self.store, self.sid, self.gid), current)

    def test_text_goal_cannot_enable_file_delivery_from_a_saved_note(self):
        screen = a.create_goal(self.store, self.sid, '文字目標')
        current = goal_guidance.snapshot(self.store, self.sid, screen)
        goal_guidance.save(self.store, self.sid, screen, '恢復文件交付。', expected_fingerprint=current['fingerprint'])
        state = a.get_goal(self.store, self.sid, screen)
        self.assertFalse(state['file_delivery'])
        self.assertNotIn('delivery_mode_change', state)
        with self.assertRaisesRegex(ValueError, '未啟用'):
            f.write(self.store, screen, 'unapproved.txt', '不應產生檔案')

    def test_backup_rejects_edited_or_missing_delivery_change_evidence(self):
        view = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '不用出檔。', expected_fingerprint=view['fingerprint'])
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('finish', report='文字成果')))
        validate_database(self.store.path)
        with self.store.connect() as db:
            raw = db.execute('SELECT receipt_json FROM autonomous_delivery_changes').fetchone()[0]
            edited = {**json.loads(raw), 'quote': '不用交檔'}
            db.execute('UPDATE autonomous_delivery_changes SET receipt_json=?', (json.dumps(edited, ensure_ascii=False),))
        with self.assertRaisesRegex(ValueError, '交付方式更正.*原話'):
            validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('DELETE FROM autonomous_delivery_changes')
        with self.assertRaisesRegex(ValueError, '交付步驟.*交付方式'):
            validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('DROP TABLE autonomous_delivery_changes')
        with self.assertRaisesRegex(ValueError, '交付步驟.*交付方式'):
            validate_database(self.store.path)

    def test_cli_rebuilds_saved_files_without_initializing_model(self):
        from assistant.__main__ import main
        with patch('assistant.artifacts.materialize', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                f.write(self.store, self.gid, 'cli.txt', 'local content')
        with patch('assistant.__main__.CodexEngine', side_effect=AssertionError('no model')):
            with patch('sys.argv', ['assistant', '--data-dir', str(self.store.path.parent), '--session', self.sid,
                                    '--rebuild-goal-files', self.gid]), contextlib.redirect_stdout(io.StringIO()) as output:
                main()
        result = json.loads(output.getvalue())
        self.assertEqual((Path(result['directory']) / 'cli.txt').read_text(), 'local content')


if __name__ == '__main__':
    unittest.main()
