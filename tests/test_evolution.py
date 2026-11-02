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
from assistant.evolution_status import improvement_context
from scripts import publish


class CommunicationTests(unittest.TestCase):
    def test_self_authored_sentence_and_fullwidth_improvement(self):
        # Independent acceptance cases added after inspecting the actual model edit.
        for question in ('兩句就好', '一句話回答', '用三句說明', 'in two sentences',
                         'within 2 sentences', 'ｂｒｉｅｆ'):
            with self.subTest(question=question):
                self.assertEqual(conversation_style(question, [])['length_hint'], 'concise')

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

    def install_acceptance_fixture(self, value='mixed'):
        for name in ('assistant/acceptance.py', 'scripts/observe_acceptance.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ev.ROOT / name).read_bytes())
        (self.root / 'assistant/__init__.py').write_text('', encoding='utf-8')
        (self.root / self.name).write_text(
            'def conversation_style(question, history):\n    return {"length_hint": "' + value + '"}\n', encoding='utf-8')
        self.command('add', '.')
        self.command('commit', '-qm', 'acceptance fixture')
        return {'version':1, 'cases':[{'id':'fix', 'kind':'communication', 'question':'不要簡短，詳細說明',
                                     'history':[], 'expect':{'length_hint':'detailed'}}]}

    def test_already_satisfied_cases_stop_without_calling_model(self):
        suite = self.install_acceptance_fixture('detailed')
        result = ev.improve('goal', [self.name], root=self.root, engine=self.engine(), acceptance=suite)
        self.assertEqual(result['status'], 'no_change_needed')
        self.assertEqual(result['acceptance']['status'], 'already_satisfied')
        self.assertFalse(hasattr(self, 'prompt'))
        self.assertEqual(ev.git(self.root, 'status', '--porcelain'), '')

    def test_modified_acceptance_file_is_rejected(self):
        suite = self.install_acceptance_fixture()
        original = ev.run_checked
        def run(command, *args, **kwargs):
            original(command, *args, **kwargs)
            if '--suite' in command:
                Path(command[command.index('--suite') + 1]).write_text('{}', encoding='utf-8')
        with patch.object(ev, 'run_checked', side_effect=run), self.assertRaisesRegex(ValueError, '驗收案例被改動'):
            ev.improve('goal', [self.name], root=self.root, engine=self.engine(), acceptance=suite)
        self.assertFalse(hasattr(self, 'prompt'))
        self.assertEqual(ev.git(self.root, 'status', '--porcelain'), '')

    def test_acceptance_failure_blocks_apply_despite_green_regressions(self):
        suite = self.install_acceptance_fixture()
        old = (self.root / self.name).read_bytes()
        content = 'def conversation_style(question, history):\n    return {"length_hint": "concise"}\n'
        with patch.object(ev, 'CHECKS', []), self.assertRaisesRegex(ValueError, '驗收未通過'):
            ev.improve('goal', [self.name], root=self.root, engine=self.engine(content), acceptance=suite)
        self.assertEqual((self.root / self.name).read_bytes(), old)
        report = self.reports()[0]
        self.assertEqual(report['status'], 'not_applied')
        self.assertEqual(report['acceptance']['candidate_passed'], 0)

    def test_acceptance_fix_uses_fresh_process_and_reaches_publication(self):
        suite = self.install_acceptance_fixture()
        content = 'def conversation_style(question, history):\n    return {"length_hint": "detailed"}\n'
        original = ev.run_checked
        def run(command, *args, **kwargs):
            if str(command[1]).endswith('publish.py'):
                raise ValueError('publication reached')
            return original(command, *args, **kwargs)
        with patch.object(ev, 'CHECKS', []), patch.object(ev, 'run_checked', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'publication reached'):
                ev.improve('goal', [self.name], root=self.root, engine=self.engine(content), acceptance=suite)
        report = self.reports()[0]
        self.assertEqual(report['acceptance']['baseline_passed'], 0)
        self.assertEqual(report['acceptance']['candidate_passed'], 1)
        self.assertEqual(report['acceptance']['status'], 'verified_improvement')
        self.assertEqual((self.root / self.name).read_text(), content)

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

    def test_publisher_rejects_same_path_with_untested_content(self):
        def run(*args, capture=False):
            if args[:3] == ('git', 'rev-parse', 'HEAD'):
                return self.base
            if args[:3] == ('git', 'diff', 'HEAD'):
                return self.name
            if args[:3] == ('git', 'ls-files', '--others'):
                return ''
            return ''
        argv = ['publish.py', '--expected-head', self.base, '--expected-file', self.name,
                '--expected-sha256', self.name + '=' + ev.digest(b'NOT the tested contents')]
        with patch.object(publish, 'ROOT', self.root), patch.object(publish, 'run', side_effect=run), patch.object(sys, 'argv', argv):
            with self.assertRaisesRegex(RuntimeError, 'source content differs'):
                publish.main()

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
        context = improvement_context(self.root, '你是不是改了程式？')
        self.assertTrue(context[0]['revision_verified'])
        self.assertTrue(context[0]['current_files_match'])
        self.assertNotIn('goal', context[0])
        self.assertNotIn('directory', context[0])
        self.assertEqual(improvement_context(self.root, 'hello'), [])
        with patch.object(ev, 'run_checked', side_effect=run):
            reverted = ev.rollback(result['id'], root=self.root)
        self.assertEqual(reverted['status'], 'published')
        self.assertEqual((self.root / self.name).read_text(), 'VALUE = 1\n')
        context = improvement_context(self.root, '自我改進')
        self.assertTrue(context[0]['rollback_recorded'])
        self.assertFalse(context[0]['current_files_match'])
        path = Path(result['directory']) / 'report.json'
        report = json.loads(path.read_text(encoding='utf-8'))
        report['files'][self.name] = '0' * 64
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertFalse(improvement_context(self.root, '自我改進')[0]['revision_verified'])
        with self.assertRaises(ValueError):
            ev.rollback(result['id'], root=self.root)


if __name__ == '__main__':
    unittest.main()
