import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.store import Store
from scripts import evaluate_conversation_search as ev
from scripts.evaluate_understanding import load_report
from scripts.evaluation_reports import GOAL_TRACE_MODE,canonical_digest,validate_report
from test_autonomy import ScriptedEngine,action


class GoalTraceConsistencyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.count=0
        self.case={'id':'one','category':'math','prompt':'計算3加4。','criteria':['得到7']}
        self.suite=self.root/'suite.json';self.suite.write_text(json.dumps([self.case]),encoding='utf-8')

    def collect(self,engine=None):
        self.count+=1
        if engine is None:engine=ScriptedEngine(action('calculate',operation='sum',values=['3','4']),action('finish',report='7'))
        report=load_report(self.root/f'report{self.count}.json',ev.signature(self.suite),['one'],False,mode=GOAL_TRACE_MODE)
        report['results'].append(ev.collect(Store(self.root/f'state{self.count}.sqlite3'),self.case,engine,lambda:None))
        validate_report(report);return report

    def rejects(self,report,change):
        altered=copy.deepcopy(report);change(altered['results'][0])
        with self.assertRaises(ValueError):validate_report(altered)

    def test_answer_result_observation_and_terminal_action_must_agree(self):
        report=self.collect()
        def result(row):row.update(answer='999');row['goal']['result']='999'
        changes=[lambda r:r.update(answer='999'),result,
                 lambda r:r['goal']['steps'][-1]['observation'].update(report='999'),
                 lambda r:r['goal']['steps'][-1]['action']['args'].update(report='999'),
                 lambda r:r['goal']['steps'][-1]['action'].update(tool='calculate')]
        for change in changes:
            with self.subTest(change=change):self.rejects(report,change)

    def test_question_needs_an_actual_ask_and_consistent_question_fields(self):
        ask=self.collect(ScriptedEngine(action('ask',question='單價是多少？')))
        def question(row):row['goal']['question']='偽造問題';row['answer']='偽造問題'
        self.rejects(ask,question)
        self.rejects(ask,lambda r:r['goal']['steps'][-1]['observation'].update(question='偽造問題'))
        def fake_ask(row):row['goal'].update(status='needs_input',result='',question='偽造問題');row['answer']='偽造問題'
        self.rejects(self.collect(),fake_ask)

    def test_running_unknown_and_nontext_outcomes_are_rejected_as_value_errors(self):
        report=self.collect()
        for field,values in {'status':['running','unknown',None,[]], 'result':[None,42],
                             'question':[False,[]], 'notice':[None,{}]}.items():
            for value in values:
                with self.subTest(field=field,value=value):self.rejects(report,lambda r:r['goal'].update({field:value}))
        def missing(row):
            row.pop('prompt');row['fixture']['prompt']=None;row['fixture_sha256']=canonical_digest(row['fixture'])
        self.rejects(report,missing)

    def test_failed_call_cannot_have_an_executed_step_or_completed_goal(self):
        report=self.collect()
        def error(row):
            last=row['calls'][-1];last.pop('raw');last.pop('raw_sha256')
            last.update(status='error',error='故障');row['error']='故障'
        self.rejects(report,error)
        def fail():raise EngineError('合成故障','test')
        for engine,expected_steps in [(ScriptedEngine(fail),0),
            (ScriptedEngine(action('calculate',operation='sum',values=['3','4']),fail),1)]:
            failed=self.collect(engine);row=failed['results'][0]
            self.assertEqual(row['goal']['status'],'failed');self.assertEqual(len(row['goal']['steps']),expected_steps)
            self.rejects(failed,lambda r:r['calls'][-1].update(raw_sha256='0'*64))
            self.rejects(failed,lambda r:r.pop('error'))

    def test_paused_unexecuted_response_and_step_budget_are_still_valid(self):
        class Cancelled:
            def answer(self,prompt,cancel=None):
                cancel.set();return action('finish',report='未執行的回答')
        paused=self.collect(Cancelled())['results'][0]
        self.assertEqual(paused['goal']['status'],'paused');self.assertEqual(paused['goal']['steps'],[])
        self.assertEqual(paused['engine_call_count'],1);self.assertNotEqual(paused['answer'],'未執行的回答')
        budget=self.collect(ScriptedEngine(*[action('calculate',operation='sum',values=['3','4'])]*8))
        self.assertEqual(budget['results'][0]['goal']['status'],'paused')
        self.assertEqual(len(budget['results'][0]['goal']['steps']),8)
        self.rejects(budget,lambda r:r.update(answer='假稱完成'))

    def test_preflight_error_without_calls_is_valid_but_not_a_completed_answer(self):
        with patch.object(ev.autonomy,'run_goal',side_effect=ValueError('合成啟動失敗')):
            report=self.collect()
        row=report['results'][0]
        self.assertEqual(row['goal']['status'],'pending');self.assertEqual(row['engine_call_count'],0)
        self.assertEqual(row['answer'],'');self.assertEqual(row['error'],'合成啟動失敗')
        self.rejects(report,lambda r:r['goal'].update(status='report_ready'))

    def test_tool_error_is_a_saved_step_distinct_from_an_engine_failure(self):
        report=self.collect(ScriptedEngine(action('read_conversation',message_id=999,before=1,after=1),
                                          action('ask',question='需要補充條件。')))
        row=report['results'][0]
        self.assertEqual(row['goal']['steps'][0]['action']['tool'],'invalid')
        self.assertEqual([call['status'] for call in row['calls']],['returned','returned'])
        self.assertEqual(row['goal']['status'],'needs_input')

    def test_cancellation_after_executed_finish_preserves_step_without_claiming_delivery(self):
        original=ev.autonomy.execute
        def cancel_after(*args,**kwargs):
            result=original(*args,**kwargs)
            kwargs['cancel'].set()
            return result
        with patch.object(ev.autonomy,'execute',side_effect=cancel_after):
            report=self.collect(ScriptedEngine(action('finish',report='已算但停止交付。')))
        row=report['results'][0]
        self.assertEqual(len(row['goal']['steps']),1)
        self.assertEqual(row['goal']['status'],'paused');self.assertEqual(row['goal']['result'],'')
        self.assertNotEqual(row['answer'],'已算但停止交付。')


if __name__=='__main__':unittest.main()
