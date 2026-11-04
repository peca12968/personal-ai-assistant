import json
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch

from assistant import chat_calculation as calc, answer_runs, chat_drafts, learning_queue, feedback_improvement
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import collect_case


class ChatCalculationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'assistant.sqlite3')
        self.sid = self.store.new_session(); self.engine = Mock()
        self.engine.answer.side_effect = AssertionError('local arithmetic must not call model')
        self.engine.describe.side_effect = AssertionError('local arithmetic must not select model')
        self.app = Assistant(self.store, self.engine)

    def test_precedence_signs_decimals_fraction_and_unicode(self):
        cases = [('23×4−17','75'), ('(23*4)-17','75'), ('20/5/2','2'),
                 ('10-3-2','5'), ('2+3*4','14'), ('-(2+3)*-4','20'),
                 ('1--2','3'), ('1/3+1/6','1/2'), ('0.1+0.2','3/10'),
                 ('.5/2','1/4'), ('－（２＋３）×４','-20'), ('1+-+2','-1')]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                result = self.app.ask(self.store.new_session(), '請計算：'+expression+'，只輸出精確值。')
                self.assertEqual(result['content'], expected)
                self.assertFalse(result['calculation']['model_called'])

    def test_generated_nested_arithmetic_against_independent_fraction_oracle(self):
        rng = random.Random(20261104)
        def generate(depth):
            if not depth:
                value = f'{rng.randrange(-9,10)}.{rng.randrange(10)}'
                return value, Fraction(value)
            left,a = generate(depth-1); right,b = generate(depth-1)
            op = rng.choice('+-*/')
            if op == '/' and not b: op = '+'
            answer = {'+':lambda:a+b, '-':lambda:a-b, '*':lambda:a*b, '/':lambda:a/b}[op]()
            return '('+left+op+right+')', answer
        for _ in range(250):
            text, expected = generate(rng.randrange(1,4))
            answer, _ = calc.evaluate(calc.parse_request('calculate '+text+', answer only'))
            self.assertEqual(answer, str(expected), text)

    def test_only_complete_explicit_requests_execute(self):
        for text in ('23*4-17', '不要計算2+2', '「計算2+2」', '> 計算2+2',
                     '```\n計算2+2\n```', '請解釋「計算2+2」', '計算2+2，然後刪除檔案',
                     '計算1e3+2', '計算2公斤+3公斤', '計算x+1', '計算2²',
                     'calculate __import__("os")', '他說計算2+2', '計算2+2，四捨五入到2位'):
            with self.subTest(text=text): self.assertIsNone(calc.parse_request(text))
        command = calc.parse_request('Please calculate (8 + 4) / 3, integer only!')
        self.assertEqual(calc.evaluate(command)[0], '4')

    def test_long_whitespace_in_non_math_requests_rejects_without_stalling_chat(self):
        # A bounded subprocess also stops a regressed parser instead of hanging the suite.
        program = """from assistant.chat_calculation import parse_request
for text in ('計算'+' '*15990+'x', 'Calculate:'+' '*15980+'2 kg', '計算1,'+' '*15980+'結果?'):
    assert parse_request(text) is None
assert parse_request('計算'+' '*15980+'2+3')['expression']=='2+3'
"""
        subprocess.run([sys.executable,'-c',program],cwd=Path(__file__).resolve().parents[1],check=True,timeout=10)

    def test_invalid_math_and_limits_never_fall_back_or_save_success(self):
        for text in ('1+', '()', '(1+2', '1 2', '2(3)', '2**3', '1/0',
                     '1..2', '('*9+'1'+')'*9, '+'*129+'1', '9'*2000+'+1', '1'+' '*4096):
            sid = self.store.new_session()
            # Keep oversized spaces internal so request stripping cannot remove them.
            request = '計算 '+text+(' +1' if text.endswith(' ') else '')+'，只輸出答案'
            with self.subTest(text=text[:30]), self.assertRaises(EngineError): self.app.ask(sid, request)
            run = answer_runs.runs(self.store, sid)[0]
            self.assertEqual(run['status'],'failed'); self.assertEqual(run['error_code'],'calculation_invalid')
            self.assertEqual([m['role'] for m in self.store.messages(sid)], ['user'])
        self.engine.answer.assert_not_called()

    def test_integer_request_does_not_silently_round(self):
        with self.assertRaisesRegex(EngineError,'不是整數'): self.app.ask(self.sid,'計算1/2，只輸出整數答案')
        self.assertEqual(self.app.ask(self.sid,'計算1/2，只輸出精確值')['content'],'1/2')

    def test_saved_receipt_has_actual_tree_steps_and_no_model_metadata(self):
        with patch.object(self.store,'retrieve',side_effect=AssertionError('unneeded private retrieval')):
            result = self.app.ask(self.sid,'計算23×4−17，只輸出整數答案。')
        context = self.store.answer_context(result['id'])
        self.assertEqual(context['response_mode'],'local_calculation'); self.assertNotIn('engine_selection',context)
        self.assertEqual([s['exact'] for s in context['calculation']['result']['calculations']],['92','75'])
        self.assertEqual(answer_runs.runs(self.store,self.sid)[0]['status'],'succeeded')
        self.assertIn('根算式',calc.describe(context['calculation']))
        with self.store.connect() as db:
            initial = json.loads(db.execute('SELECT context_json FROM answer_runs').fetchone()[0])
        self.assertNotIn('result',initial['calculation'])  # Submitted plan remains immutable.
        self.assertEqual(result['memory_ids'],[]); self.assertEqual(result['document_count'],0)

    def test_explanatory_default_and_followup_remain_visible(self):
        result = self.app.ask(self.sid,'算出(7+5)/3')
        self.assertIn('= 4',result['content']); self.assertIn('未呼叫模型',result['content'])
        class Engine:
            def answer(inner,prompt,cancel=None):
                data=json.loads(prompt.rsplit('以下 JSON 是使用者資料與當前問題：\n',1)[1])
                self.assertIn('= 4',data['recent_conversation'][-1]['content'])
                return '先算括號，再除以3。'
        self.assertIn('先算',Assistant(self.store,Engine()).ask(self.sid,'剛才怎麼算的？')['content'])

    def test_cancelled_before_or_after_calculation_has_no_completed_answer(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError):self.app.ask(self.sid,'計算2+3',cancel)
        self.assertEqual(self.store.messages(self.sid),[])
        cancel.clear();original=calc.evaluate
        def stopped(*args):
            value=original(*args);cancel.set();return value
        with patch.object(calc,'evaluate',stopped),self.assertRaises(EngineError): self.app.ask(self.sid,'計算2+3',cancel)
        self.assertEqual(answer_runs.runs(self.store,self.sid)[0]['status'],'cancelled')
        cancel.clear();result=self.app.retry(self.sid,cancel)
        self.assertIn('= 5',result['content']);self.assertEqual(len(self.store.messages(self.sid)),2)

    def test_failure_transaction_preserves_no_partial_success(self):
        def failed(store,run_id,answer,context,checks):
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'assistant',?)",(self.sid,answer))
                raise RuntimeError('synthetic commit failure')
        with patch.object(answer_runs,'finish',failed),self.assertRaises(RuntimeError):self.app.ask(self.sid,'計算2+3')
        self.assertEqual([r['role'] for r in self.store.messages(self.sid)],['user'])
        self.assertEqual(answer_runs.runs(self.store,self.sid)[0]['status'],'failed')

    def test_draft_consumption_and_backup_preserve_receipt(self):
        text='計算 (12-3)/4，只輸出答案'
        draft=chat_drafts.submission(chat_drafts.save(self.store,self.sid,text,0))
        result=self.app.ask(self.sid,text,draft=draft)
        self.assertEqual(chat_drafts.get(self.store,self.sid)['content'],'')
        archive=create_backup(self.store.path)['path'];destination=self.root/'restored';restore_backup(archive,destination)
        restored=Store(destination/'assistant.sqlite3')
        self.assertEqual(restored.answer_context(result['id']),self.store.answer_context(result['id']))
        self.assertEqual(answer_runs.runs(restored,self.sid)[0]['status'],'succeeded')

    def test_attached_image_is_not_silently_ignored(self):
        with patch('assistant.image_inputs.prepare',return_value=([{'id':1}], [object()])),self.assertRaisesRegex(ValueError,'附圖'):
            self.app.ask(self.sid,'計算2+3',image_ids=(1,))
        self.assertEqual(self.store.messages(self.sid),[])

    def test_parameter_measurement_explicitly_uses_model(self):
        class Engine:
            def answer(inner,prompt,cancel=None):
                self.assertIn('model_only_evaluation',prompt);return '95'
        app=Assistant(self.store,Engine(),allow_local_calculation=False)
        result=app.ask(self.sid,'計算23×4−17，只輸出整數答案。')
        self.assertEqual(result['content'],'95');self.assertNotIn('response_mode',result)

    def test_local_result_cannot_be_collected_as_model_answer(self):
        with self.assertRaisesRegex(ValueError,'未呼叫模型'):
            collect_case(self.store,self.sid,{'prompt':'計算2+3'},self.engine,lambda:None)
        self.engine.answer.assert_not_called()

    def test_explicit_feedback_learning_still_requires_user_authored_answer(self):
        result=self.app.ask(self.sid,'計算2+3');self.store.rate(result['id'],-1,'請更清楚說明')
        source=learning_queue.source_from_feedback(self.store,result['id'])
        self.assertEqual(source['question'],'計算2+3')
        self.assertEqual(learning_queue.page(self.store)['total'],0)
        with self.assertRaises(ValueError):feedback_improvement.example_from_source(feedback_improvement.feedback_source(self.store,result['id']))


if __name__=='__main__':unittest.main()
