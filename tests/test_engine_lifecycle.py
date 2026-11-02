"""Exercise real subprocess I/O, termination and persisted-answer boundaries."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant, CodexEngine, EngineError
from assistant.store import Store


class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = CodexEngine(self.root, timeout=3)
        self.processes = []
        self.watchdogs = []
        self.watchdog_fired = threading.Event()
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            def watchdog():
                if process.poll() is None:
                    self.watchdog_fired.set()
                    process.kill()
            timer = threading.Timer(10, watchdog)
            timer.daemon = True
            timer.start()
            self.watchdogs.append(timer)
            return process

        # Safety watchdog bounds even the old, broken implementation during regressions.
        self.spawn = start

    def tearDown(self):
        for timer in self.watchdogs:
            timer.cancel()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.assertFalse(self.watchdog_fired.is_set(), 'Engine required the external test watchdog')
        self.assertEqual(list(self.engine.workdir.iterdir()), [], 'Temporary prompt/output survived cleanup')

    def invoke(self, code, prompt='hello', cancel=None):
        def command(output):
            return [sys.executable, '-c', code, str(output)]
        with patch.object(self.engine, 'command', side_effect=command), patch('assistant.engine.subprocess.Popen', side_effect=self.spawn):
            return self.engine.answer(prompt, cancel)

    def test_large_unicode_prompt_is_read_exactly_after_child_cwd_change(self):
        prompt = '問題與上下文 🙂\n' * 40000
        expected = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        answer = self.invoke('import sys,hashlib; from pathlib import Path; '
            'data=sys.stdin.buffer.read(); Path(sys.argv[1]).write_text(hashlib.sha256(data).hexdigest(),encoding="utf-8")', prompt)
        self.assertEqual(answer, expected)
        self.assertEqual(self.processes[0].returncode, 0)

    def test_timeout_when_child_never_reads_large_input_terminates_it(self):
        self.engine.timeout = 0.3
        with self.assertRaises(EngineError) as caught:
            self.invoke('import time; time.sleep(30)', 'x' * 2_000_000)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertIsNotNone(self.processes[0].poll())

    def test_cancel_when_child_never_reads_input_terminates_it(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            with self.assertRaises(EngineError) as caught:
                self.invoke('import time; time.sleep(30)', 'x' * 2_000_000, cancel)
            self.assertEqual(caught.exception.code, 'cancelled')
        finally:
            timer.cancel()
        self.assertIsNotNone(self.processes[0].poll())

    def test_pre_cancelled_turn_does_not_spawn_or_save_a_question(self):
        cancel = threading.Event(); cancel.set()
        store = Store(self.root / 'state.sqlite3'); sid = store.new_session()
        with patch('assistant.engine.subprocess.Popen') as launch, self.assertRaises(EngineError) as caught:
            Assistant(store, self.engine).ask(sid, '不要送出這題', cancel)
        self.assertEqual(caught.exception.code, 'cancelled')
        launch.assert_not_called()
        self.assertEqual(store.messages(sid), [])

    def test_nonzero_exit_rejects_output_and_does_not_echo_stderr(self):
        with self.assertRaises(EngineError) as caught:
            self.invoke('import sys; from pathlib import Path; '
                'Path(sys.argv[1]).write_text("fake success"); '
                'sys.stderr.write("PRIVATE_INPUT_MARKER"); sys.exit(7)', 'x' * 2_000_000)
        self.assertEqual(caught.exception.code, 'process_failed')
        self.assertIn('7', str(caught.exception))
        self.assertNotIn('PRIVATE_INPUT_MARKER', str(caught.exception))

    def test_missing_empty_and_invalid_utf8_output_are_distinct_failures(self):
        for code, expected in [('pass', 'empty_response'),
                              ('import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b" \\n")', 'empty_response'),
                              ('import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(bytes([255]))', 'invalid_response')]:
            with self.subTest(expected=expected), self.assertRaises(EngineError) as caught:
                self.invoke(code)
            self.assertEqual(caught.exception.code, expected)

    def test_output_limit_accepts_exact_boundary_and_rejects_overflow(self):
        with patch('assistant.engine.MAX_ANSWER_BYTES', 16):
            self.assertEqual(self.invoke('import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b"a"*16)'), 'a' * 16)
            with self.assertRaises(EngineError) as caught:
                self.invoke('import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b"a"*17)')
            self.assertEqual(caught.exception.code, 'output_too_large')

    def test_input_byte_limit_and_invalid_input_fail_before_launch(self):
        with patch('assistant.engine.MAX_PROMPT_BYTES', 10), patch('assistant.engine.subprocess.Popen') as launch:
            for text, code in [('中' * 4, 'input_too_large'), (None, 'invalid_input'), ('\ud800', 'invalid_input')]:
                with self.subTest(code=code), self.assertRaises(EngineError) as caught:
                    self.engine.answer(text)
                self.assertEqual(caught.exception.code, code)
            launch.assert_not_called()

    def test_startup_failure_and_local_io_failure_are_readable(self):
        with patch.object(self.engine, 'command', return_value=[str(self.root / 'missing.exe')]), self.assertRaises(EngineError) as caught:
            self.engine.answer('hello')
        self.assertEqual(caught.exception.code, 'launch_failed')
        with patch('assistant.engine.tempfile.TemporaryFile', side_effect=OSError('PRIVATE_PATH')), self.assertRaises(EngineError) as caught:
            self.engine.answer('hello')
        self.assertEqual(caught.exception.code, 'local_io')
        self.assertNotIn('PRIVATE_PATH', str(caught.exception))

    def test_late_cancellation_rejects_finished_output(self):
        cancel = threading.Event()
        original_spawn = self.spawn
        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs)
            poll = process.poll
            def finished():
                result = poll()
                if result is not None:
                    cancel.set()
                return result
            process.poll = finished
            return process
        self.spawn = spawn
        with self.assertRaises(EngineError) as caught:
            self.invoke('import sys; from pathlib import Path; Path(sys.argv[1]).write_text("late answer")', cancel=cancel)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_timeout_includes_prelaunch_work(self):
        self.engine.timeout = 0.05
        def slow_command(output):
            time.sleep(0.1)
            return [sys.executable, '-c', 'pass']
        with patch.object(self.engine, 'command', side_effect=slow_command), patch('assistant.engine.subprocess.Popen') as launch, self.assertRaises(EngineError) as caught:
            self.engine.answer('hello')
        self.assertEqual(caught.exception.code, 'timeout')
        launch.assert_not_called()

    def test_invalid_timeouts_cannot_disable_deadline(self):
        for timeout in (0, -1, float('nan'), float('inf'), True, '240'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                CodexEngine(self.root, timeout=timeout)

    @unittest.skipUnless(os.name == 'nt', 'Windows process-tree termination fallback')
    def test_failed_tree_termination_still_kills_the_owned_process(self):
        self.engine.timeout = 0.3
        with patch('assistant.engine.subprocess.run', side_effect=subprocess.TimeoutExpired('taskkill', 5)), self.assertRaises(EngineError) as caught:
            self.invoke('import time; time.sleep(30)', 'x' * 2_000_000)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertIsNotNone(self.processes[0].poll())

    def test_cli_reports_engine_failure_without_traceback_or_fabricated_answer(self):
        code = ('import sys; from unittest.mock import patch; from assistant.engine import EngineError; '
                'from assistant.__main__ import main; '
                'sys.argv=["assistant","--data-dir",sys.argv[1],"--ask","test question"]; '
                'p=patch("assistant.engine.CodexEngine.answer",side_effect=EngineError("controlled failure")); '
                'p.start(); main()')
        result = subprocess.run([sys.executable, '-c', code, str(self.root / 'cli')], capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('回答未完成', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        store = Store(self.root / 'cli/assistant.sqlite3')
        self.assertEqual([row['role'] for row in store.messages(store.sessions()[0]['id'])], ['user'])


if __name__ == '__main__':
    unittest.main()
