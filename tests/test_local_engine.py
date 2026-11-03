import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant, EngineError
from assistant.local_engine import LocalEngine
from assistant import local_models
from assistant.store import Store


SUCCESS = '''
import json,sys
from pathlib import Path
sys.stdin.reconfigure(encoding='utf-8')
request=json.load(sys.stdin)
result={'schema':'local_inference_result_v1','status':'succeeded','answer':'答案',
        'selection_fingerprint':request['selection']['fingerprint'],
        'input_tokens':100,'output_tokens':2,'peak_gpu_bytes':1000}
'''
WRITE = "\nPath(sys.argv[1]).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')\n"


class LocalEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = LocalEngine(self.root, timeout=3)
        self.processes = []
        self.real_popen = subprocess.Popen

    def spawn(self, *args, **kwargs):
        process = self.real_popen(*args, **kwargs)
        self.processes.append(process)
        return process

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.assertEqual(list(self.engine.workdir.iterdir()), [])

    def invoke(self, code, prompt='問題', cancel=None):
        with patch.object(self.engine, 'command', side_effect=lambda output: [sys.executable, '-c', code, str(output)]), \
                patch('assistant.local_engine.subprocess.Popen', side_effect=self.spawn):
            return self.engine.answer(prompt, cancel)

    def test_exact_unicode_input_and_immutable_selection(self):
        prompt = '問題🙂\n' * 30000
        import hashlib
        code = SUCCESS + "\nimport hashlib\nresult['answer']=hashlib.sha256(request['prompt'].encode('utf-8')).hexdigest()\n" + WRITE
        self.assertEqual(self.invoke(code, prompt), hashlib.sha256(prompt.encode('utf-8')).hexdigest())
        selected = self.engine.selection
        selected['revision'] = 'tampered'
        self.assertNotEqual(self.engine.selection['revision'], 'tampered')
        self.assertNotIn('torch', sys.modules)

    def test_wrong_version_invalid_json_bounds_and_failed_worker_rejected(self):
        cases = (
            (SUCCESS+"result['selection_fingerprint']='different'\n"+WRITE, 'invalid_response'),
            (SUCCESS+"result['input_tokens']=24577\n"+WRITE, 'invalid_response'),
            (SUCCESS+"result['output_tokens']=True\n"+WRITE, 'invalid_response'),
            (SUCCESS+"result['answer']=''\n"+WRITE, 'invalid_response'),
            (SUCCESS+"result['answer']='\\ud800'\n"+WRITE.replace('ensure_ascii=False','ensure_ascii=True'), 'invalid_response'),
            ("import sys; from pathlib import Path; Path(sys.argv[1]).write_text('{invalid')", 'invalid_response'),
            ("import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*(3*1024*1024))", 'invalid_response'),
            (SUCCESS+WRITE+"sys.exit(5)\n", 'invalid_response'),
            (SUCCESS+"result={'schema':'local_inference_result_v1','status':'failed','code':'gpu_busy'}\n"+WRITE+"sys.exit(1)\n", 'local_gpu_busy'),
            (SUCCESS+"result={'schema':'local_inference_result_v1','status':'failed','code':'unknown'}\n"+WRITE, 'invalid_response'),
            (SUCCESS+"result={'schema':'local_inference_result_v1','status':'failed','code':[]}\n"+WRITE, 'invalid_response'),
        )
        for code, expected in cases:
            with self.subTest(expected=expected, code=code[-100:]):
                with self.assertRaises(EngineError) as error:
                    self.invoke(code)
                self.assertEqual(error.exception.code, expected)

    def test_cancellation_timeout_and_no_stdin_read_reap_real_children(self):
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            with self.assertRaises(EngineError) as error:
                self.invoke('import time; time.sleep(15)', '字'*200000, cancel)
            self.assertEqual(error.exception.code, 'cancelled')
        finally:
            timer.cancel()
        self.assertTrue(all(p.poll() is not None for p in self.processes))
        self.engine.timeout = 0.3
        with self.assertRaises(EngineError) as error:
            self.invoke('import time; time.sleep(15)', '字'*200000)
        self.assertEqual(error.exception.code, 'timeout')
        self.assertTrue(all(p.poll() is not None for p in self.processes))

    def test_pre_cancel_and_oversized_input_do_not_launch(self):
        cancelled = threading.Event(); cancelled.set()
        with patch('assistant.local_engine.subprocess.Popen') as launch:
            for prompt, cancel, expected in [('test', cancelled, 'cancelled'),
                                              ('x'*(2*1024*1024+1), None, 'input_too_large'),
                                              ('\ud800', None, 'invalid_input')]:
                with self.assertRaises(EngineError) as error:
                    self.engine.answer(prompt, cancel)
                self.assertEqual(error.exception.code, expected)
            launch.assert_not_called()

    def test_cli_local_diagnostics_and_conflicting_options(self):
        command = [sys.executable, '-m', 'assistant', '--data-dir', str(self.root/'cli')]
        for options, code in (
            (['--engine','local','--doctor'], 0),
            (['--local-experiment','fixture','--doctor'], 2),
            (['--engine','local','--improve','example'], 2),
            (['--engine','local','--local-experiment','../escape','--doctor'], 1),
        ):
            with self.subTest(options=options):
                result = subprocess.run(command+options, cwd=local_models.ROOT,
                    capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                if code == 0:
                    self.assertIn('unadapted_base', result.stdout)
                    self.assertIn('Optional runtime present:', result.stdout)
        self.assertFalse((self.root/'cli/assistant.sqlite3').exists())

    def test_diagnostic_output_limit_and_missing_runtime(self):
        with self.assertRaises(EngineError) as error:
            self.invoke(SUCCESS+"sys.stderr.buffer.write(b'x'*(2*1024*1024)); sys.stderr.flush()\n"+WRITE)
        self.assertEqual(error.exception.code, 'local_log_limit')
        self.engine.python = self.root/'absent-python'
        with self.assertRaises(EngineError) as error:
            self.engine.answer('hello')
        self.assertEqual(error.exception.code, 'local_environment')

    def test_actual_assistant_persists_selected_version_and_failure_status(self):
        store = Store(self.root/'assistant.sqlite3')
        sid = store.new_session()
        app = Assistant(store, self.engine)
        def command(output):
            return [sys.executable, '-c', SUCCESS+WRITE, str(output)]
        with patch.object(self.engine, 'command', side_effect=command):
            answer = app.ask(sid, '簡單回答')
        with store.connect() as db:
            row = db.execute('SELECT status,context_json FROM answer_runs').fetchone()
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(json.loads(row['context_json'])['engine_selection'], self.engine.describe())
        self.assertEqual(answer['content'], '答案')
        from assistant.backup import create_backup, restore_backup
        saved = create_backup(store.path, self.root/'saved.paibackup')
        restored = self.root/'restored'
        restore_backup(saved['path'], restored)
        restored_store = Store(restored/'assistant.sqlite3')
        self.assertEqual(restored_store.answer_context(answer['id'])['engine_selection'], self.engine.describe())
        with patch.object(self.engine, 'command', return_value=[sys.executable, '-c', 'raise SystemExit(2)']):
            with self.assertRaises(EngineError):
                app.ask(sid, '第二題')
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM answer_runs WHERE status='failed'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 1)

    def test_autonomous_steps_save_current_engine_without_replaying_prior_selection(self):
        from assistant.autonomy import create_goal, run_goal
        store = Store(self.root/'goals.sqlite3'); sid = store.new_session()
        gid = create_goal(store, sid, '計算1+2並回報結果')
        captured = []
        def answer(prompt, cancel=None):
            payload = json.loads(prompt.split('目標與已觀察資料：\n', 1)[1])
            captured.append(payload)
            if len(captured) == 1:
                return json.dumps({'tool':'calculate_expression', 'args':{'expression':{'operation':'sum','values':['1','2']}}, 'note':'計算'})
            return json.dumps({'tool':'finish', 'args':{'report':'結果是3'}, 'note':'回報'})
        with patch.object(self.engine, 'answer', side_effect=answer):
            goal = run_goal(store, sid, gid, self.engine, max_steps=2)
        self.assertEqual(goal['status'], 'report_ready')
        self.assertEqual(len(goal['steps']), 2)
        self.assertTrue(all(step['observation']['engine_selection'] == self.engine.describe() for step in goal['steps']))
        self.assertTrue(all(payload['engine_selection'] == self.engine.describe() for payload in captured))
        self.assertNotIn('engine_selection', captured[1]['steps'][0]['observation'])

    def test_gui_labels_and_historical_engine_tab(self):
        import tkinter as tk
        from tkinter import ttk
        from assistant.gui import App
        from assistant.context_ui import show_context
        root = tk.Tk(); root.withdraw()
        try:
            store = Store(self.root/'ui.sqlite3'); sid = store.new_session()
            with patch.object(self.engine, 'answer', return_value='答案'):
                answer = Assistant(store, self.engine).ask(sid, '問題')
            App(root, store, self.engine)
            self.assertIn('本機 Qwen3-4B', root.title())
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            labels = [str(widget.cget('text')) for widget in descendants(root) if isinstance(widget, ttk.Button)]
            self.assertIn('自我改進（Codex）', labels)
            show_context(root, store, answer['id'])
            for window in root.winfo_children():
                if isinstance(window, tk.Toplevel):
                    window.withdraw()
            books = [widget for widget in descendants(root) if isinstance(widget, ttk.Notebook)]
            tabs = [(book, tab) for book in books for tab in book.tabs() if book.tab(tab, 'text') == '推理引擎']
            self.assertEqual(len(tabs), 1)
            pane = root.nametowidget(tabs[0][1])
            texts = [widget.get('1.0', 'end') for widget in descendants(pane) if isinstance(widget, tk.Text)]
            self.assertIn(self.engine.selection['fingerprint'], ''.join(texts))
        finally:
            for timer in root.tk.call('after', 'info'):
                root.tk.call('after', 'cancel', timer)
            root.destroy()

    def test_explicit_research_name_and_metadata_limits(self):
        for name in ('../outside', 'C:\\outside', '', 12):
            with self.assertRaises(ValueError):
                local_models.select(name)
        path = self.root/'metadata.json'
        for value in ('{"a":1,"a":2}', '{"x":NaN}'):
            path.write_text(value, encoding='utf-8')
            with self.assertRaises(ValueError):
                local_models.read_json(path)
        path.write_text('x'*101)
        with self.assertRaises(ValueError):
            local_models.read_json(path, 100)

    def test_asset_corruption_and_selected_manifest_change_are_rejected(self):
        from scripts.learning.protocol import file_hash
        base = self.root/'data/learning/models/Qwen3-4B'/('a'*40)
        base.mkdir(parents=True)
        asset = base/'model.safetensors'; asset.write_bytes(b'fixture-weights')
        manifest = {'model_id':'Qwen/Qwen3-4B', 'revision':'a'*40,
                    'files':[{'name':asset.name, 'bytes':asset.stat().st_size, 'sha256':file_hash(asset)}]}
        path = self.root/'scripts/learning/model.json'; path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest), encoding='utf-8')
        with patch.object(local_models, 'ROOT', self.root):
            selected = local_models.select()
            self.assertEqual(local_models.verify_assets(selected), (base, None))
            asset.write_bytes(b'changed-weights')
            with self.assertRaises(ValueError):
                local_models.verify_assets(selected)
            asset.write_bytes(b'fixture-weights')
            path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            with self.assertRaises(ValueError):
                local_models.verify_assets(selected)

    def test_research_selection_recomputes_assessment_and_binds_all_evidence(self):
        from scripts.learning import experiment, protocol
        model = self.root/'scripts/learning/model.json'; model.parent.mkdir(parents=True)
        model.write_bytes((protocol.HERE/'model.json').read_bytes())
        with patch.object(local_models, 'ROOT', self.root), patch.object(experiment, 'ROOT', self.root):
            experiment.initialize('fixture')
            folder = experiment.local_folder('fixture')
            with self.assertRaises(FileNotFoundError):
                local_models.select('fixture')
            reports = {}
            for phase in protocol.PHASES:
                rows = []
                for case in protocol.make_suite()['evaluation']:
                    correct = case['group'] == 'guard' or phase in ('stage-b','reload-b') or (phase == 'stage-a' and case['group'] == 'a')
                    rows.append({**case, 'output':case['answer'] if correct else 'unknown', 'correct':correct})
                reports[phase] = {'status':'complete', 'evaluation':rows,
                                  'weights_before':'a' if phase == 'stage-b' else 'initial',
                                  'weights_after':'a' if phase == 'stage-a' else 'b'}
            reports['stage-b']['adapter_files'] = {name:'a'*64 for name in ('README.md','adapter_config.json','adapter_model.safetensors')}
            for phase, report in reports.items():
                experiment.save(folder/(phase+'.json'), report)
            assessment = protocol.assess(reports)
            experiment.save(folder/'assessment.json', assessment)
            selected = local_models.select('fixture')
            self.assertEqual(len(selected['evidence_sha256']), 7)
            # Even harmless evidence byte changes cannot relabel the old selection.
            path = folder/'baseline.json'; path.write_text(path.read_text('utf-8')+'\n', encoding='utf-8')
            self.assertNotEqual(local_models.select('fixture')['fingerprint'], selected['fingerprint'])
            with self.assertRaises(ValueError):
                local_models.verify_assets(selected)
            # A persisted pass is never accepted in place of recomputing the grades.
            next(row for row in reports['stage-a']['evaluation'] if row['group'] == 'guard').update(output='unknown', correct=False)
            experiment.save(folder/'stage-a.json', reports['stage-a'])
            with self.assertRaises(ValueError):
                local_models.select('fixture')
            experiment.save(folder/'assessment.json', protocol.assess(reports))
            with self.assertRaises(ValueError):
                local_models.select('fixture')


if __name__ == '__main__':
    unittest.main()
