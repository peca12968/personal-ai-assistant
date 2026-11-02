import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from assistant.tasks import execute_task, export_task, load_csv, plan_csv, task_context


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.source = self.root / 'orders.csv'
        self.source.write_text('sku,amount\nA,0.1\nB,2\nA,0.2\n', encoding='utf-8')

    def plan(self):
        return plan_csv(self.store, self.sid, self.source, 'sku', 'amount')

    def execute(self):
        plan = self.plan()
        return execute_task(self.store, self.sid, plan['id'])

    def test_plan_execute_export_preserves_source_and_exact_decimals(self):
        before = self.source.read_bytes()
        task = self.plan()
        self.assertEqual(task['status'], 'planned')
        self.assertIsNone(task['result'])
        done = execute_task(self.store, self.sid, task['id'])
        self.assertEqual(done['status'], 'succeeded')
        report = done['result']['report']
        self.assertEqual(report['total'], '2.3')
        self.assertEqual(report['groups'][0], {'group': 'A', 'count': 2, 'sum': '0.3'})
        output = self.root / 'result.json'
        self.assertTrue(export_task(self.store, self.sid, task['id'], output)['verified'])
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')), report)
        self.assertEqual(self.source.read_bytes(), before)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.task(self.sid, task['id'])['result']['report'], done['result']['report'])
        saved_export = reopened.task(self.sid, task['id'])['result']['exports'][0]
        self.assertTrue(saved_export['verified'])
        summary = task_context(reopened, self.sid, '已匯出嗎')[0]
        self.assertEqual(summary['verified_exports'][0]['file_name'], 'result.json')
        self.assertNotIn(str(self.root), json.dumps(summary))

    def test_changed_source_after_plan_fails_with_record(self):
        task = self.plan()
        self.source.write_text('sku,amount\nA,99\n')
        result = execute_task(self.store, self.sid, task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['result'])
        self.assertIn('改變', result['error'])

    def test_source_change_during_execution_is_not_success(self):
        task = self.plan()
        from assistant import tasks
        original = tasks.read_document
        counter = 0
        def changing(path):
            nonlocal counter
            counter += 1
            if counter == 2:
                self.source.write_text('sku,amount\nA,99\n')
            return original(path)
        with patch('assistant.tasks.read_document', side_effect=changing):
            result = execute_task(self.store, self.sid, task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('執行期間', result['error'])

    def test_invalid_numeric_values_are_not_silently_skipped(self):
        for value in ['', 'NaN', 'Infinity', '1e3', '$10', '1,000']:
            self.source.write_text('sku,amount\nA,"' + value + '"\n')
            result = self.execute()
            self.assertEqual(result['status'], 'failed', value)
            self.assertIsNone(result['result'])

    def test_headers_and_row_width_must_be_unambiguous(self):
        for content in ['sku,sku\nA,1', 'sku,\nA,1', 'sku,amount\nA,1,extra', 'sku,amount\n"unterminated,1', 'x'*201 + ',amount\nA,1']:
            self.source.write_text(content)
            with self.assertRaises(ValueError):
                self.plan()
        self.source.write_text('sku,amount\nA,1')
        with self.assertRaises(ValueError):
            plan_csv(self.store, self.sid, self.source, 'missing', 'amount')

    def test_csv_quotes_newlines_and_group_whitespace_preserved(self):
        self.source.write_text('sku,amount\n"A, B",1\n"multi\nline",2\n A,3\nA,4\n', encoding='utf-8')
        report = self.execute()['result']['report']
        self.assertEqual(report['row_count'], 4)
        self.assertEqual(report['group_count'], 4)
        self.assertEqual(report['total'], '10')
        self.assertIn('multi\r\nline' if sys.platform == 'win32' else 'multi\nline', [row['group'] for row in report['groups']])

    def test_large_and_tiny_numbers_sum_without_rounding(self):
        large = '9' * 59
        tiny = '0.' + '0' * 55 + '1'
        self.source.write_text(f'sku,amount\nA,{large}\nA,{tiny}\n')
        report = self.execute()['result']['report']
        self.assertEqual(report['total'], large + '.' + '0' * 55 + '1')

    def test_cancel_timeout_and_group_limit_never_export_success(self):
        event = threading.Event()
        event.set()
        first = self.plan()
        self.assertEqual(execute_task(self.store, self.sid, first['id'], event)['status'], 'cancelled')
        second = self.plan()
        self.assertEqual(execute_task(self.store, self.sid, second['id'], timeout=0)['status'], 'failed')
        with patch('assistant.tasks.MAX_GROUPS', 1):
            third = self.execute()
        self.assertEqual(third['status'], 'failed')
        with self.assertRaises(ValueError):
            export_task(self.store, self.sid, third['id'], self.root / 'bad.json')

    def test_task_is_claimed_once_and_scoped_to_session(self):
        task = self.plan()
        other = self.store.new_session()
        with self.assertRaises(ValueError):
            execute_task(self.store, other, task['id'])
        execute_task(self.store, self.sid, task['id'])
        with self.assertRaises(ValueError):
            execute_task(self.store, self.sid, task['id'])
        with self.assertRaises(ValueError):
            self.store.finish_task(self.sid, task['id'], 'failed')

    def test_report_export_cannot_overwrite_or_use_wrong_extension(self):
        task = self.execute()
        output = self.root / 'existing.json'
        output.write_text('keep')
        with self.assertRaises(FileExistsError):
            export_task(self.store, self.sid, task['id'], output)
        self.assertEqual(output.read_text(), 'keep')
        with self.assertRaises(ValueError):
            export_task(self.store, self.sid, task['id'], self.source)

    def test_tampered_report_is_not_trusted_or_exported(self):
        task = self.execute()
        task['result']['report']['total'] = '999'
        with self.store.connect() as db:
            db.execute('UPDATE task_runs SET result_json=? WHERE id=?', (json.dumps(task['result']), task['id']))
        self.assertEqual(task_context(self.store, self.sid, '工作結果')[0]['status'], 'unverified')
        with self.assertRaises(ValueError):
            export_task(self.store, self.sid, task['id'], self.root / 'bad.json')

    def test_chat_context_uses_actual_status_and_is_bounded(self):
        self.source.write_text('sku,amount\n' + '\n'.join(f'group{i},1' for i in range(15)))
        done = self.execute()
        pending = self.plan()
        summary = task_context(self.store, self.sid, '工作完成了嗎')
        self.assertEqual(summary[0]['status'], 'planned')
        self.assertEqual(summary[1]['status'], 'succeeded')
        self.assertTrue(summary[1]['groups_truncated'])
        self.assertEqual(len(summary[1]['groups_preview']), 10)
        self.assertNotIn(str(self.root), json.dumps(summary))
        self.assertEqual(task_context(self.store, self.sid, '解釋冰為何浮起'), [])
        self.assertEqual(task_context(self.store, self.store.new_session(), '工作結果'), [])
        class Engine:
            def answer(self, prompt, cancel=None):
                return '目前有一個計畫尚未執行。'
        answer = Assistant(self.store, Engine()).ask(self.sid, '工作結果')
        snapshot = self.store.answer_context(answer['id'])
        execute_task(self.store, self.sid, pending['id'])
        self.assertEqual(snapshot['task_runs'][0]['status'], 'planned')

    def test_tasks_roundtrip_backup_and_old_backup_upgrades(self):
        task = self.execute()
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored/assistant.sqlite3')
        self.assertEqual(restored.task(self.sid, task['id'])['result'], task['result'])
        with self.store.connect() as db:
            db.execute('DROP TABLE task_runs')
        legacy = create_backup(self.store.path)
        restore_backup(legacy['path'], self.root / 'legacy')
        old = Store(self.root / 'legacy/assistant.sqlite3')
        self.assertEqual(old.tasks(self.sid), [])
        self.assertEqual(old.sessions()[0]['id'], self.sid)

    def test_cli_plan_run_export_without_model(self):
        def run(*args):
            result = subprocess.run([sys.executable, '-m', 'assistant', '--data-dir', str(self.root / 'cli'), *args],
                                    capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        plan = run('--plan-csv', str(self.source), '--group-column', 'sku', '--value-column', 'amount')
        self.assertEqual(plan['status'], 'planned')
        result = run('--run-task', plan['id'], '--session', plan['session_id'])
        self.assertEqual(result['status'], 'succeeded')
        export = run('--export-task', plan['id'], '--session', plan['session_id'], '--output', str(self.root / 'cli-result.json'))
        self.assertTrue(export['verified'])


if __name__ == '__main__':
    unittest.main()
