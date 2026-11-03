import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, expression_math as m
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action
from test_expression_math import op,args


class ExpressionDiagnosticsTests(unittest.TestCase):
    def failure(self,expression):
        with self.assertRaises(m.CalculationFailure) as caught:m.calculate(args(expression))
        return caught.exception

    def test_root_and_first_deepest_failing_operation_are_identified(self):
        root=self.failure(op('quotient','5/7',op('difference','3','3')))
        self.assertEqual(root.details(),{'path':'','operation':'quotient','reason':'不能除以零。','reason_truncated':False})
        self.assertIn('根',str(root))
        expression=op('sum',op('product','2',op('quotient','9',op('difference','4','4'))),
                      op('quotient','5',op('difference','3','3')))
        original=copy.deepcopy(expression)
        failure=self.failure(expression)
        self.assertEqual(failure.details()['path'],'/values/0/values/1')
        self.assertEqual(failure.details()['operation'],'quotient')
        self.assertEqual(expression,original)
        expression['values'][0]['values'][1]['values'][1]['values'][1]='3'
        self.assertEqual(self.failure(expression).details()['path'],'/values/1')

    def test_overflow_identifies_child_without_revealing_operands_or_partial_values(self):
        huge='1'+'0'*1999
        failure=self.failure(op('sum',op('product',huge,'10'),'1'))
        data=failure.details()
        self.assertEqual((data['path'],data['operation']),('/values/0','product'))
        self.assertIn('2000',data['reason'])
        self.assertNotIn(huge,json.dumps(data))
        self.assertNotIn('exact',data);self.assertNotIn('calculations',data)

    def test_validation_and_cancellation_are_not_misclassified_as_operation_failures(self):
        with self.assertRaises(ValueError) as caught:m.calculate(args(op('sum','1/0','2')))
        self.assertNotIsInstance(caught.exception,m.CalculationFailure)
        event=threading.Event();event.set()
        with self.assertRaises(EngineError):m.calculate(args(op('sum','1','2')),event)
        with patch.object(m.exact_math,'calculate',side_effect=EngineError('取消','cancelled')):
            with self.assertRaises(EngineError):m.calculate(args(op('sum','1','2')))

    def test_reason_is_bounded_and_diagnostic_dict_is_detached(self):
        with patch.object(m.exact_math,'calculate',side_effect=ValueError('x'*700)):
            failure=self.failure(op('sum','1','2'))
        data=failure.details();self.assertEqual(len(data['reason']),500);self.assertTrue(data['reason_truncated'])
        data['path']='偽造';self.assertEqual(failure.details()['path'],'')

    def test_saved_failure_survives_handoff_backup_and_authorized_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite3');sid=store.new_session()
            gid=a.create_goal(store,sid,'先核對原式，除以零時將4-4改為4-3，其他不變。')
            expression=op('sum','987654321',op('quotient','9',op('difference','4','4')))
            failed=a.run_goal(store,sid,gid,ScriptedEngine(action('calculate_expression',expression=expression)),max_steps=1)
            observation=failed['steps'][0]['observation']
            self.assertEqual(failed['steps'][0]['action']['tool'],'invalid')
            self.assertEqual(observation['error_phase'],'execution')
            self.assertEqual(observation['calculation_failure']['path'],'/values/1')
            self.assertEqual(observation['failed_action']['args']['expression'],expression)
            self.assertNotIn('calculations',observation);self.assertNotIn('exact',observation);self.assertNotIn('excerpts',observation)
            child=a.create_goal(store,sid,'檢查先前失敗',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'],ensure_ascii=False)
            self.assertIn('calculation_failure',stage);self.assertIn('/values/1',stage)
            self.assertIn('expression_details_omitted',stage);self.assertIn('calculation_details_omitted',stage)
            self.assertNotIn('987654321',stage)
            backup=create_backup(store.path,root/'copy.paibackup');restore_backup(backup['path'],root/'restored')
            restored=Store(root/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],failed['steps'])
            fixed=copy.deepcopy(expression);fixed['values'][1]['values'][1]['values'][1]='3'
            finished=a.run_goal(restored,sid,gid,ScriptedEngine(action('calculate_expression',expression=fixed),
                                  action('finish',report='更正後987654330。')),max_steps=2)
            self.assertEqual(finished['status'],'report_ready')
            self.assertEqual(finished['steps'][1]['observation']['exact'],'987654330')
            self.assertEqual(finished['steps'][0],failed['steps'][0])

    def test_only_typed_expression_execution_errors_get_location_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session();gid=a.create_goal(store,sid,'核對')
            engine=ScriptedEngine(action('calculate_expression',expression=op('power','2','3')),
                                  action('calculate',operation='quotient',values=['1','0']))
            result=a.run_goal(store,sid,gid,engine,max_steps=2)
            for step in result['steps']:
                self.assertNotIn('calculation_failure',step['observation'])
            self.assertEqual(result['steps'][0]['observation']['error_phase'],'validation')
            self.assertNotIn('failed_action',result['steps'][0]['observation'])
            self.assertEqual(result['steps'][1]['observation']['error_phase'],'execution')


if __name__=='__main__':unittest.main()
