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

    def _wait_on_named_draft(self):
        from scripts.evaluate_artifact_filenames import prepare
        case = {'objective': '核對門號和領用人。若交檔請交 report.md。',
                'draft': 'report.md', 'also_draft': 'report-final.md', 'text_round': True}
        self.store, self.sid, self.gid = prepare(self.root / 'named-draft', case)
        engine = ScriptedEngine()
        goal = a.run_goal(self.store, self.sid, self.gid, engine)
        self.assertFalse(engine.prompts)
        self.assertEqual(goal['status'], 'needs_input')
        self.assertIn('report-final-2.md', goal['question'])
        return goal

    def test_named_draft_waits_for_new_filename_and_uses_literal_answer(self):
        from scripts.evaluate_artifact_filenames import DRAFT, reply
        before = self._wait_on_named_draft()
        for unusable in ('REPORT.md。', '定稿用「report-final.md」。', '定稿用 report-final.txt。', '../unsafe.md'):
            with self.subTest(unusable=unusable), self.assertRaisesRegex(ValueError, '同格式且尚未使用'):
                reply(self.store, self.sid, self.gid, unusable)
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), before)
        saved = reply(self.store, self.sid, self.gid, '用戶.md。')
        proof = saved['work_plan']['scope_reply']['filename_choice']
        self.assertEqual(proof['selection']['filename'], '用戶.md')
        self.assertEqual(saved['reply_sources'][-1]['question'], before['question'])
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        self.assertEqual(saved['work_plan']['items'], before['work_plan']['items'])
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='other.md', content='錯誤檔名'),
            action('write_artifact', filename='用戶.md', content='領用人宋一珊，門號 8 號。'),
            action('deliver', report='已完成', filenames=['report.md']),
            action('deliver', report='已完成', filenames=['用戶.md'])))
        self.assertEqual([step['action']['tool'] for step in result['steps'][-4:]], ['invalid', 'write_artifact', 'invalid', 'deliver'])
        self.assertEqual([row['name'] for row in result['artifacts']], ['report.md', 'report-final.md', '用戶.md'])
        self.assertEqual((f.directory(self.store, self.gid) / 'report.md').read_text(encoding='utf-8'), DRAFT)
        validate_database(self.store.path)

    def test_filename_ask_and_actual_choice_replay_and_reject_a_changed_draft_pointer(self):
        from scripts.evaluate_artifact_filenames import reply
        waiting = self._wait_on_named_draft()
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored-named')
        restored = Store(self.root / 'restored-named/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), waiting)
        saved = reply(restored, self.sid, self.gid, '定稿用「result.md」。')
        self.assertEqual(saved['status'], 'paused')
        validate_database(restored.path)
        ask = waiting['steps'][-1]
        altered = {**ask['observation'], 'artifact_filename_question': {**ask['observation']['artifact_filename_question'],
                                                                         'suggested': 'forged.md'}}
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(altered), self.gid, ask['sequence']))
        with self.assertRaisesRegex(ValueError, '定稿檔名問題與原指示或舊草稿不一致'):
            validate_database(restored.path)

    def test_filename_reply_applies_field_revision_and_requires_the_confirmed_name(self):
        from scripts.evaluate_artifact_filenames import DRAFT, reply
        waiting = self._wait_on_named_draft()
        for invalid in ('領用人改為 result.md。', '定稿用 report.md。領用人改為周雅文。'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '同格式且尚未使用'):
                reply(self.store, self.sid, self.gid, invalid)
            self.assertEqual(a.get_goal(self.store, self.sid, self.gid), waiting)
        raw = '定稿用「result.md」，「領用人」改為周雅文。'
        saved = reply(self.store, self.sid, self.gid, raw)
        proof = saved['work_plan']['scope_reply']
        spans = (proof['filename_choice']['selection'], proof['revisions'][0]['evidence'])
        self.assertEqual([raw[row['start']:row['end']] for row in spans], ['定稿用「result.md」', '「領用人」改為周雅文'])
        self.assertEqual(proof['retained_items'], [2])
        self.assertEqual(proof['revisions'][0]['previous']['value'], '宋一珊')
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='other.md', content='錯誤檔名'),
            action('write_artifact', filename='result.md', content='領用人周雅文，門號 8 號。'),
            action('deliver', report='已完成', filenames=['result.md'])))
        self.assertEqual([step['action']['tool'] for step in result['steps'][-3:]], ['invalid', 'write_artifact', 'deliver'])
        self.assertEqual((f.directory(self.store, self.gid) / 'report.md').read_text(encoding='utf-8'), DRAFT)
        validate_database(self.store.path)

    def test_filename_reply_can_switch_to_text_and_replays_its_delivery_change(self):
        from scripts.evaluate_artifact_filenames import reply
        waiting = self._wait_on_named_draft()
        with self.assertRaisesRegex(ValueError, '同格式且尚未使用'):
            reply(self.store, self.sid, self.gid, '若主管同意，改成文字回覆。')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), waiting)
        saved = reply(self.store, self.sid, self.gid, '改成文字回覆。領用人改為周雅文。')
        self.assertEqual(saved['status'], 'paused')
        self.assertFalse(saved['file_delivery'])
        proof = saved['work_plan']['scope_reply']
        self.assertEqual(proof['delivery_mode']['receipt']['quote'], '改成文字回覆')
        self.assertEqual(proof['revisions'][0]['value'], '周雅文')
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('finish', report='領用人周雅文，門號 8 號。')))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['artifacts'], waiting['artifacts'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'text-after-filename')
        restored = Store(self.root / 'text-after-filename/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), result)
        with restored.connect() as db:
            db.execute('DELETE FROM autonomous_delivery_changes WHERE goal_id=? AND citation=?',
                       (self.gid, proof['citation']))
        with self.assertRaisesRegex(ValueError, '交付方式'):
            validate_database(restored.path)

    def test_filename_is_retained_during_a_competing_field_choice(self):
        from scripts.evaluate_artifact_filenames import reply
        self._wait_on_named_draft()
        pending = reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。定稿檔名改為 result.md。')
        self.assertEqual(pending['status'], 'needs_input')
        self.assertEqual(pending['question'], '領用人要改為「周雅文」或「江明洋」，還是沿用「宋一珊」？')
        self.assertEqual(pending['work_plan']['scope_reply']['retained_items'], [2, 3])
        saved = reply(self.store, self.sid, self.gid, '用江明洋。')
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='result.md', content='領用人江明洋，門號 8 號。'),
            action('deliver', report='已完成', filenames=['result.md'])))
        self.assertEqual(result['status'], 'report_ready')
        validate_database(self.store.path)

    def test_restoring_file_delivery_keeps_the_chosen_name_and_current_field_evidence(self):
        from scripts.evaluate_artifact_filenames import reply
        self._wait_on_named_draft()
        text = reply(self.store, self.sid, self.gid, '改成文字回覆，定稿用 later.md，門號改為 9 號。')
        self.assertFalse(text['file_delivery'])
        view = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '恢復文件交付。', expected_fingerprint=view['fingerprint'])
        saved = a.get_goal(self.store, self.sid, self.gid)
        self.assertTrue(saved['file_delivery'])
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        self.assertEqual(saved['work_plan']['items'], text['work_plan']['items'])
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='wrong.md', content='錯誤檔名'),
            action('write_artifact', filename='later.md', content='領用人宋一珊，門號 9 號。'),
            action('deliver', report='已完成', filenames=['later.md'])))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual([step['action']['tool'] for step in result['steps'][-3:]], ['invalid', 'write_artifact', 'deliver'])
        validate_database(self.store.path)

    def test_field_only_reply_reopens_the_filename_question_with_a_new_source_binding(self):
        from scripts.evaluate_artifact_filenames import reply
        from assistant import artifact_names
        waiting = self._wait_on_named_draft()
        old_view = goal_guidance.snapshot(self.store, self.sid, self.gid)
        saved = reply(self.store, self.sid, self.gid, '「領用人」改為周雅文。')
        self.assertEqual(saved['question'], waiting['question'])
        self.assertEqual(saved['artifacts'], waiting['artifacts'])
        old_ask, new_ask = waiting['steps'][-1], saved['steps'][-1]
        self.assertEqual(new_ask['sequence'], old_ask['sequence'] + 2)
        self.assertEqual(new_ask['observation'][artifact_names.ASK_MARK]['draft'], old_ask['observation'][artifact_names.ASK_MARK]['draft'])
        self.assertEqual(saved['reply_sources'][-1]['ask_sequence'], old_ask['sequence'])
        self.assertEqual(saved['work_plan']['scope_reply']['retained_items'], [2])
        with self.assertRaisesRegex(ValueError, '尚未保存'):
            goal_replies.save(self.store, self.sid, self.gid, 'result.md。', expected_fingerprint=old_view['fingerprint'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'refreshed-filename')
        restored = Store(self.root / 'refreshed-filename/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), saved)
        with restored.connect() as db:
            db.execute('DELETE FROM autonomous_steps WHERE goal_id=? AND sequence=?', (self.gid, new_ask['sequence']))
            db.execute("UPDATE autonomous_goals SET status='paused',question='' WHERE id=?", (self.gid,))
        with self.assertRaisesRegex(ValueError, '缺少接續的定稿檔名問題'):
            validate_database(restored.path)

    def test_combined_field_choice_binds_the_current_field_and_earlier_filename_questions(self):
        from assistant.goal_stages import digest
        from scripts.evaluate_artifact_filenames import reply
        waiting = self._wait_on_named_draft()
        pending = reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。')
        raw = '檔名叫「result.md」，用江明洋。'
        chosen = reply(self.store, self.sid, self.gid, raw)
        self.assertEqual(chosen['status'], 'paused')
        self.assertEqual(chosen['artifacts'], waiting['artifacts'])
        proof = chosen['work_plan']['scope_reply']
        choice = proof['filename_choice']
        self.assertEqual(choice['question_sha256'], digest(waiting['steps'][-1]))
        self.assertEqual(proof['ask_sha256'], digest(pending['steps'][-1]))
        spans = (choice['selection'], proof['language']['revision_resolution']['evidence'])
        self.assertEqual([raw[span['start']:span['end']] for span in spans], ['檔名叫「result.md」', '用江明洋'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'combined-choice')
        restored = Store(self.root / 'combined-choice/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), chosen)
        step = chosen['steps'][-1]
        step['observation']['scope_reply']['filename_choice']['question_step'] = pending['steps'][-1]['sequence']
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(step['observation']), self.gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_field_choice_does_not_replace_a_filename_that_is_already_confirmed(self):
        from scripts.evaluate_artifact_filenames import reply
        self._wait_on_named_draft()
        pending = reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。定稿用 result.md。')
        with self.assertRaisesRegex(ValueError, '待選更正仍未確認'):
            reply(self.store, self.sid, self.gid, '用江明洋。定稿用 replacement.md。')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), pending)
        validate_database(self.store.path)

    def test_combined_field_choice_and_text_mode_require_the_saved_mode_event(self):
        from scripts.evaluate_artifact_filenames import reply
        self._wait_on_named_draft()
        reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。')
        state = reply(self.store, self.sid, self.gid, '用周雅文，改成文字回覆，定稿用 later.md。')
        self.assertFalse(state['file_delivery'])
        proof = state['work_plan']['scope_reply']
        self.assertEqual(proof['filename_choice']['selection']['filename'], 'later.md')
        self.assertEqual(proof['delivery_mode']['receipt']['quote'], '改成文字回覆')
        validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('DELETE FROM autonomous_delivery_changes WHERE goal_id=? AND citation=?',
                       (self.gid, proof['citation']))
        with self.assertRaisesRegex(ValueError, '交付方式'):
            validate_database(self.store.path)

    def test_filename_only_refreshes_the_field_question_and_survives_backup(self):
        from scripts.evaluate_artifact_filenames import DRAFT, reply
        waiting = self._wait_on_named_draft()
        pending = reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。')
        old_view = goal_guidance.snapshot(self.store, self.sid, self.gid)
        raw = '定稿用「result.md」，「領用人」晚點再決定。'
        named = reply(self.store, self.sid, self.gid, raw)
        self.assertEqual(named['status'], 'needs_input')
        self.assertEqual(named['question'], pending['question'])
        self.assertEqual(named['artifacts'], waiting['artifacts'])
        proof = named['work_plan']['scope_reply']
        self.assertEqual(proof['filename_choice']['question_step'], waiting['steps'][-1]['sequence'])
        self.assertEqual(named['reply_sources'][-1]['ask_sequence'], pending['steps'][-1]['sequence'])
        self.assertEqual(proof['language']['pending_revisions'], pending['work_plan']['scope_reply']['language']['pending_revisions'])
        self.assertIsNone(proof['language']['revision_resolution'])
        span = proof['field_deferral']['evidence']
        self.assertEqual(raw[span['start']:span['end']], '「領用人」晚點再決定')
        with self.assertRaisesRegex(ValueError, '尚未保存'):
            goal_replies.save(self.store, self.sid, self.gid, '用江明洋。', expected_fingerprint=old_view['fingerprint'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'name-before-field')
        restored = Store(self.root / 'name-before-field/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), named)
        chosen = reply(restored, self.sid, self.gid, '用江明洋。')
        self.assertEqual(chosen['reply_sources'][-1]['ask_sequence'], named['steps'][-1]['sequence'])
        final = a.run_goal(restored, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='other.md', content='錯誤檔名'),
            action('write_artifact', filename='result.md', content='領用人江明洋，門號 8 號。'),
            action('deliver', report='已完成', filenames=['result.md'])))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual([step['action']['tool'] for step in final['steps'][-3:]], ['invalid', 'write_artifact', 'deliver'])
        f.rebuild(restored, self.sid, self.gid)
        self.assertEqual((f.directory(restored, self.gid) / 'report.md').read_text(encoding='utf-8'), DRAFT)
        validate_database(restored.path)

    def test_filename_saved_in_text_mode_remains_active_when_files_resume_before_field_choice(self):
        from scripts.evaluate_artifact_filenames import reply
        self._wait_on_named_draft()
        pending = reply(self.store, self.sid, self.gid, '領用人改為周雅文或江明洋。')
        named = reply(self.store, self.sid, self.gid, '改成文字回覆，「later.md」。')
        self.assertFalse(named['file_delivery'])
        self.assertEqual(named['question'], pending['question'])
        view = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '恢復文件交付。', expected_fingerprint=view['fingerprint'])
        restored_mode = a.get_goal(self.store, self.sid, self.gid)
        self.assertTrue(restored_mode['file_delivery'])
        self.assertEqual(restored_mode['question'], pending['question'])
        reply(self.store, self.sid, self.gid, '用周雅文。')
        final = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='later.md', content='領用人周雅文，門號 8 號。'),
            action('deliver', report='已完成', filenames=['later.md'])))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['steps'][-1]['action']['args']['filenames'], ['later.md'])
        validate_database(self.store.path)

    def test_separate_filename_or_text_guidance_can_resolve_filename_question(self):
        for statement, expected in [('定稿檔名改為 selected.md。', True), ('改成文字回覆。', False)]:
            with self.subTest(statement=statement), tempfile.TemporaryDirectory() as scratch:
                self.root = Path(scratch)
                waiting = self._wait_on_named_draft()
                view = goal_guidance.snapshot(self.store, self.sid, self.gid)
                goal_guidance.save(self.store, self.sid, self.gid, statement, expected_fingerprint=view['fingerprint'])
                saved = a.get_goal(self.store, self.sid, self.gid)
                self.assertEqual(saved['file_delivery'], expected)
                self.assertFalse(saved['question'])
                self.assertTrue(saved['work_plan']['delivery_review']['ready'])
                self.assertEqual(saved['withdrawn_questions'][-1]['ask_sequence'], waiting['steps'][-1]['sequence'])
                validate_database(self.store.path)

    def test_current_exact_file_can_be_delivered_without_filename_question(self):
        from assistant import artifact_names
        self.assertIsNone(artifact_names.declaration('請使用 template.md。'))
        self.gid = a.create_goal(self.store, self.sid, '整理摘要。請交 report.md。', file_delivery=True)
        current = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('write_artifact', filename='report.md', content='完整摘要。')), max_steps=1)
        self.assertEqual(current['status'], 'paused')
        final = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('deliver', report='完成', filenames=['report.md'])), max_steps=1)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['steps'][-1]['action']['args']['filenames'], ['report.md'])
        validate_database(self.store.path)


if __name__ == '__main__':
    unittest.main()
