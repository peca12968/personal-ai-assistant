import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import evolution as ev
from assistant.communication import conversation_style
from assistant.engine import Assistant, EngineError
from assistant.store import Store


class CommunicationTests(unittest.TestCase):
    def test_length_correction_and_history_are_advisory(self):
        result = conversation_style('你誤會了，簡短回答', [{'role': 'user', 'content': 'old'}])
        self.assertEqual(result['length_hint'], 'concise')
        self.assertTrue(result['possible_correction'])
        self.assertTrue(result['has_prior_user_turn'])
        self.assertTrue(result['hints_only'])
        self.assertEqual(conversation_style('簡短結論，再詳細說明', [])['length_hint'], 'mixed')
        self.assertEqual(conversation_style('hello', [])['length_hint'], 'unspecified')

    def test_answer_snapshot_keeps_preferences_and_latest_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'db.sqlite3')
            sid = store.new_session()
            store.remember('偏好簡短回答', 'preference')
            class Engine:
                def answer(self, prompt, cancel=None):
                    self.prompt = prompt
                    return '依本次要求詳細解釋。'
            engine = Engine()
            result = Assistant(store, engine).ask(sid, '這次請詳細說明')
            context = store.answer_context(result['id'])
            self.assertEqual(context['communication']['length_hint'], 'detailed')
            self.assertTrue(context['memories'])
            self.assertIn('當前要求優先', engine.prompt)


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.name = ev.EDITABLE[0]
        (self.root / 'assistant').mkdir()
        (self.root / self.name).write_text('VALUE = 1\n', encoding='utf-8')
        (self.root / '.gitignore').write_text('data/\n__pycache__/\n', encoding='utf-8')
        self.command('init', '-q')
        self.command('config', 'user.email', 'test@example.invalid')
        self.command('config', 'user.name', 'test')
        self.command('add', '.')
        self.command('commit', '-qm', 'baseline')
        self.base = ev.git(self.root, 'rev-parse', 'HEAD')

    def command(self, *args):
        subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True)

    def engine(self, content='VALUE = 2\n'):
        outer = self
        class Engine:
            def answer(self, prompt, cancel=None):
                outer.prompt = prompt
                return json.dumps({'files': [{'path': outer.name, 'content': content}]})
        return Engine()

    def reports(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.root / 'data/evolution').glob('*/report.json')]

    def test_rejects_escape_controls_duplicates_empty_and_syntax_errors(self):
        for name in ('../outside.py', 'assistant/evolution.py', 'tests/test_evolution.py',
                     'data/private.txt', 'assistant\\communication.py', 'C:/bad.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ev.parse_proposal(json.dumps({'files': [{'path': name, 'content': 'x=1'}]}), [self.name])
        for content in ('', 'x=(', '\0'):
            with self.assertRaises((ValueError, SyntaxError)):
                ev.parse_proposal(json.dumps({'files': [{'path': self.name, 'content': content}]}), [self.name])
        item = {'path': self.name, 'content': 'x=1'}
        with self.assertRaises(ValueError):
            ev.parse_proposal(json.dumps({'files': [item, item]}), [self.name])

    def test_dirty_project_and_concurrent_lock_stop_before_model(self):
        (self.root / self.name).write_text('dirty', encoding='utf-8')
        with self.assertRaises(ValueError):
            ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertFalse(hasattr(self, 'prompt'))
        lock = self.root / 'data/evolution/active.lock'
        self.assertFalse(lock.exists())
        lock.write_text('another process', encoding='utf-8')
        with self.assertRaises(ValueError):
            ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertEqual(lock.read_text(), 'another process')

    def test_failing_real_check_preserves_original_and_error_record(self):
        with patch.object(ev, 'CHECKS', [('-c', 'raise SystemExit(1)')]):
            with self.assertRaises(ValueError):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 1\n')
        self.assertEqual(self.reports()[0]['status'], 'not_applied')
        self.assertEqual(ev.git(self.root, 'rev-parse', 'HEAD'), self.base)

    def test_cancellation_preserves_original(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(ev, 'CHECKS', [('-c', 'import time; time.sleep(30)')]):
            with self.assertRaises(EngineError):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine(), cancel=cancel)
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 1\n')

    def test_candidate_cannot_change_protected_files_during_checks(self):
        command = ('-c', "from pathlib import Path; Path('.gitignore').write_text('changed')")
        with patch.object(ev, 'CHECKS', [command]):
            with self.assertRaises(ValueError):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertEqual(self.reports()[0]['status'], 'not_applied')
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 1\n')

    def test_concurrent_version_change_prevents_application(self):
        def changed(status):
            if status == 'testing':
                (self.root / self.name).write_text('other work', encoding='utf-8')
        with patch.object(ev, 'CHECKS', []):
            with self.assertRaises(ValueError):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine(), progress=changed)
        self.assertEqual((self.root / self.name).read_text(), 'other work')

    def test_publish_failure_is_not_reported_as_success(self):
        with patch.object(ev, 'CHECKS', []), patch.object(ev, 'run_checked', side_effect=ValueError('offline')):
            with self.assertRaises(ValueError):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertEqual(self.reports()[0]['status'], 'applied_unpublished')
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 2\n')

    def test_real_candidate_and_local_git_publish_keep_private_data_out(self):
        remote = self.root / 'data/remote.git'
        remote.parent.mkdir(exist_ok=True)
        (remote.parent / 'private.txt').write_text('private-marker-DO-NOT-SEND', encoding='utf-8')
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.command('remote', 'add', 'origin', str(remote))
        original_run = ev.run_checked
        def run(command, cwd, log, cancel=None, timeout=180):
            if 'publish.py' in str(command[1]):
                if ev.git(self.root, 'status', '--porcelain'):
                    self.command('add', self.name)
                    self.command('commit', '-qm', 'verified test')
                self.command('push', '-q', 'origin', 'HEAD')
            else:
                original_run(command, cwd, log, cancel, timeout)
        with patch.object(ev, 'CHECKS', [('-m', 'py_compile', self.name)]), patch.object(ev, 'run_checked', side_effect=run):
            result = ev.improve('goal', [self.name], root=self.root, engine=self.engine())
        self.assertEqual(result['status'], 'published')
        self.assertNotEqual(result['commit'], self.base)
        self.assertNotIn('private-marker', self.prompt)
        self.assertFalse((Path(result['directory']) / 'candidate/data/private.txt').exists())
        self.assertEqual(ev.clean_head(self.root), result['commit'])
        with patch.object(ev, 'run_checked', side_effect=run):
            reverted = ev.rollback(result['id'], root=self.root)
        self.assertEqual(reverted['status'], 'published')
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 1\n')
        with self.assertRaises(ValueError):
            ev.rollback(result['id'], root=self.root)


if __name__ == '__main__':
    unittest.main()
