import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from assistant import answer_runs
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError
from assistant.store import Store


class AnswerRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3'); self.sid = self.store.new_session()

    def fail(self, code='timeout'):
        class Engine:
            def answer(inner, *args):
                raise EngineError('PRIVATE_ERROR_DETAIL', code)
        with self.assertRaises(EngineError):
            Assistant(self.store, Engine()).ask(self.sid, '原始問題')
        return answer_runs.runs(self.store, self.sid)[0]

    def test_failure_status_survives_restart_without_private_error(self):
        run = self.fail()
        reopened = Store(self.store.path)
        self.assertEqual(answer_runs.runs(reopened, self.sid)[0]['status'], 'failed')
        self.assertEqual(answer_runs.runs(reopened, self.sid)[0]['error_code'], 'timeout')
        self.assertNotIn('PRIVATE_ERROR_DETAIL', json.dumps(answer_runs.context_status(reopened, self.sid)))
        self.assertIsNone(run['answer_message_id'])
        self.assertEqual([row['role'] for row in reopened.messages(self.sid)], ['user'])

    def test_retry_reuses_question_and_current_references(self):
        mid = self.store.remember('OLD_PRIVATE_MEMORY', 'preference')
        doc = self.root / 'note.txt'; doc.write_text('OLD_PRIVATE_DOCUMENT', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        first = self.fail()
        self.store.forget(mid); self.store.detach_document(self.sid, did)
        self.store.remember('NEW_PREFERENCE', 'preference')
        class Engine:
            def answer(inner, prompt, cancel=None):
                self.assertNotIn('OLD_PRIVATE_MEMORY', prompt)
                self.assertNotIn('OLD_PRIVATE_DOCUMENT', prompt)
                self.assertIn('NEW_PREFERENCE', prompt)
                self.assertEqual(prompt.count('原始問題'), 1)
                return '重試成功'
        result = Assistant(self.store, Engine()).retry(self.sid)
        self.assertEqual(result['user_message_id'], first['user_message_id'])
        self.assertEqual([row['role'] for row in self.store.messages(self.sid)], ['user', 'assistant'])
        rows = answer_runs.runs(self.store, self.sid)
        self.assertEqual([row['status'] for row in rows], ['succeeded', 'failed'])
        self.assertEqual(rows[0]['answer_message_id'], result['id'])
        self.assertEqual(self.store.answer_context(result['id'])['retry_of']['run_id'], first['id'])

    def test_retry_rejects_completed_or_later_conversation(self):
        self.fail()
        self.store.add_message(self.sid, 'user', '另一個問題')
        with self.assertRaisesRegex(ValueError, '其他對話'):
            Assistant(self.store, None).retry(self.sid)
        other = self.store.new_session()
        with self.assertRaises(ValueError):
            Assistant(self.store, None).retry(other)

    def test_cancellation_is_distinct_and_pre_cancelled_has_no_run(self):
        row = self.fail('cancelled')
        self.assertEqual(row['status'], 'cancelled')
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(EngineError):
            Assistant(self.store, None).retry(self.sid, cancel)
        self.assertEqual(len(answer_runs.runs(self.store, self.sid)), 1)

    def test_live_lease_blocks_recovery_and_concurrent_model_call(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        class Engine:
            def answer(inner, *args):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test release missing')
                return '第一個答案'
        def work():
            try:
                Assistant(self.store, Engine()).ask(self.sid, '同時提問')
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=work); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            answer_runs.recover(self.store, self.sid)
            self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'running')
            with self.assertRaisesRegex(ValueError, '尚未完成'):
                Assistant(self.store, None).ask(self.sid, '不能同時啟動')
            self.assertEqual(len(self.store.messages(self.sid)), 1)
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])

    def test_real_process_lease_released_after_process_exit(self):
        run_id = uuid.uuid4().hex
        code = '''import sys,time
from pathlib import Path
from assistant.store import Store
from assistant.answer_runs import lease,begin
store=Store(Path(sys.argv[1]))
with lease(store,sys.argv[3]):
    begin(store,sys.argv[2],sys.argv[3],'interrupted question',{})
    print('ready',flush=True)
    time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.store.path), self.sid, run_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        timer = threading.Timer(10, process.kill); timer.start()
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            answer_runs.recover(self.store, self.sid)
            self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'running')
            process.kill(); process.communicate(timeout=5)
            answer_runs.recover(self.store, self.sid)
            row = answer_runs.runs(self.store, self.sid)[0]
            self.assertEqual(row['status'], 'interrupted')
            self.assertEqual(row['error_code'], 'owner_lost')
            self.assertEqual(answer_runs.retry_target(self.store, self.sid)['id'], run_id)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_answer_write_failure_rolls_back_partial_answer(self):
        class Engine:
            def answer(self, *args):
                return '不得留下半份答案'
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_context BEFORE INSERT ON answer_contexts BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            Assistant(self.store, Engine()).ask(self.sid, '測試交易')
        self.assertEqual([row['role'] for row in self.store.messages(self.sid)], ['user'])
        self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'failed')

    def test_retry_rechecks_concurrent_changes_in_transaction(self):
        self.fail()
        target = answer_runs.retry_target(self.store, self.sid)
        self.store.add_message(self.sid, 'user', '後來的新訊息')
        with self.assertRaisesRegex(ValueError, '狀態已變更'):
            answer_runs.begin(self.store, self.sid, uuid.uuid4().hex, '原始問題', {}, target)
        self.assertEqual(len(answer_runs.runs(self.store, self.sid)), 1)

    def test_backup_restore_export_and_legacy_schema_preserve_evidence(self):
        run = self.fail()
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored/assistant.sqlite3')
        self.assertEqual(answer_runs.runs(restored, self.sid), answer_runs.runs(self.store, self.sid))
        output = self.root / 'export.json'; restored.export_session(self.sid, output)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))[0]['answer_attempts'][0]['id'], run['id'])
        with restored.connect() as db:
            db.execute('DROP TABLE answer_runs')
        old_backup = create_backup(restored.path)
        restore_backup(old_backup['path'], self.root / 'legacy')
        legacy = Store(self.root / 'legacy/assistant.sqlite3')
        self.assertEqual(answer_runs.runs(legacy, self.sid), [])

    def test_context_reports_only_current_session_and_local_commands_not_retried(self):
        self.fail()
        app = Assistant(self.store, None)
        self.assertEqual(app.prepare_context(self.store.new_session(), '剛剛怎麼了')['answer_attempts'], [])
        app.ask(self.sid, '列出記憶')
        context = app.prepare_context(self.sid, '剛才發生什麼？')
        self.assertEqual(context['conversation_tail']['local_memory'], 1)
        self.assertEqual(context['conversation_tail']['role'], 'assistant')
        with self.assertRaises(ValueError):
            app.retry(self.sid)
        self.assertEqual(len(answer_runs.runs(self.store, self.sid)), 1)

    def test_failed_recovery_check_does_not_claim_interrupted(self):
        run_id = uuid.uuid4().hex
        answer_runs.begin(self.store, self.sid, run_id, '未知持有權', {})
        with patch('assistant.answer_runs.lease', side_effect=OSError('cannot inspect')):
            with self.assertRaises(OSError):
                answer_runs.recover(self.store, self.sid)
        self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'running')

    def test_running_backup_copy_recovers_without_changing_original_owner(self):
        run_id = uuid.uuid4().hex
        with answer_runs.lease(self.store, run_id):
            answer_runs.begin(self.store, self.sid, run_id, 'still active', {})
            backup = create_backup(self.store.path)
            restore_backup(backup['path'], self.root / 'restored')
            copy = Store(self.root / 'restored/assistant.sqlite3')
            answer_runs.recover(copy, self.sid)
            self.assertEqual(answer_runs.runs(copy, self.sid)[0]['status'], 'interrupted')
            answer_runs.recover(self.store, self.sid)
            self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'running')

    def test_cli_retries_once_without_duplicate_user_message(self):
        self.fail()
        backup = create_backup(self.store.path)
        directory = self.root / 'cli'
        restore_backup(backup['path'], directory)
        code = '''import sys
from unittest.mock import patch
from assistant.__main__ import main
sys.argv=['assistant','--data-dir',sys.argv[1],'--session',sys.argv[2],'--retry-answer']
with patch('assistant.engine.CodexEngine.answer',return_value='CLI retry success'):
    main()
'''
        command = [sys.executable, '-c', code, str(directory), self.sid]
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CLI retry success', result.stdout)
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 1)
        copied = Store(directory / 'assistant.sqlite3')
        self.assertEqual([row['role'] for row in copied.messages(self.sid)], ['user', 'assistant'])


if __name__ == '__main__':
    unittest.main()
