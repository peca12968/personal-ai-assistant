import copy
import json
import tempfile
import threading
import unittest
from decimal import Inexact, ROUND_UP, localcontext
from pathlib import Path

from assistant import autonomy as a, expression_math as m
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def op(operation,*values):return {'operation':operation,'values':list(values)}
def args(expression):return {'expression':expression}


class ExpressionMathTests(unittest.TestCase):
    def test_quote_has_all_twelve_intermediates_and_an_exact_final_result(self):
        pairs=[('7','125'),('8','140'),('3','79'),('12','46'),('5','234'),('9','63'),('4','185'),('6','112')]
        subtotal=op('sum',*[op('product',q,p) for q,p in pairs])
        expression=op('product',op('sum',op('product',subtotal,'9/10'),'55'),'1.05')
        original=copy.deepcopy(expression);result=m.calculate(args(expression))
        self.assertEqual(result['exact'],'1132887/200');self.assertEqual(result['decimal'],'5664.435')
        self.assertFalse(result['rounded']);self.assertEqual(expression,original)
        self.assertEqual((result['node_count'],result['number_count'],result['max_depth']),(31,19,6))
        trace=result['calculations']
        self.assertEqual([x['exact'] for x in trace],
                         ['875','1120','237','552','1170','567','740','672','5933','53397/10','53947/10','1132887/200'])
        self.assertEqual([x['path'] for x in trace[-4:]],
                         ['/values/0/values/0/values/0','/values/0/values/0','/values/0',''])
        self.assertEqual(result['operation_count'],12);self.assertTrue(result['all_calculations_provided'])
        self.assertNotIn('excerpts',result)

    def test_nested_fractions_and_literal_root_preserve_precision(self):
        expression=op('quotient',op('sum','1/7','2/3'),op('difference','5/11','1/13'))
        result=m.calculate(args(expression))
        self.assertEqual([x['exact'] for x in result['calculations']],['17/21','54/143','2431/1134'])
        self.assertTrue(result['rounded']);self.assertEqual(result['decimal_precision'],40)
        literal=m.calculate(args('0.125'))
        self.assertEqual(literal['exact'],'1/8');self.assertEqual(literal['calculations'],[])
        self.assertEqual((literal['node_count'],literal['number_count'],literal['operation_count'],literal['max_depth']),(1,1,0,1))

    def test_schema_and_literals_never_allow_code_or_implicit_numeric_coercion(self):
        bad=[None,{}, {'expression':'1','filename':'x.py'},args(1),args(True),args(None),args(['1']),
             args('1+2'),args('__import__("os")'),args('1e3'),args(' 1'),args('1/0'),args('1'+'0'*2000),
             args(op('power','2','3')),args(op('sum')),args(op('difference','1')),args(op('quotient','1','2','3')),
             args({'operation':'sum','values':['1'],'extra':True}),args({'operation':'sum','values':'1'})]
        for value in bad:
            with self.subTest(value=value),self.assertRaises(ValueError):m.validate(value)

    def test_node_and_depth_bounds_include_operators_and_number_leaves(self):
        self.assertEqual(m.calculate(args(op('sum',*['1']*31)))['node_count'],32)
        with self.assertRaises(ValueError):m.calculate(args(op('sum',*['1']*32)))
        expression='1'
        for _ in range(7):expression=op('sum',expression)
        self.assertEqual(m.calculate(args(expression))['max_depth'],8)
        with self.assertRaises(ValueError):m.calculate(args(op('sum',expression)))
        cycle=op('sum');cycle['values'].append(cycle)
        with self.assertRaises(ValueError):m.validate(args(cycle))

    def test_invalid_children_and_overflow_fail_the_whole_expression(self):
        huge='1'+'0'*1999
        for expression in (op('quotient','5/7',op('difference','3','3')),
                           op('product','0',op('quotient','1','0')),
                           op('difference',op('product',huge,'10'),op('product',huge,'10')),
                           op('sum','9'*2000,'1','-1')):
            with self.subTest(operation=expression['operation']),self.assertRaises(ValueError):m.calculate(args(expression))

    def test_decimal_preview_does_not_inherit_global_precision_or_traps(self):
        expression=op('product',op('quotient','1','7'),'49')
        expected=m.calculate(args(expression))
        with localcontext() as context:
            context.prec=2;context.rounding=ROUND_UP;context.traps[Inexact]=True
            self.assertEqual(m.calculate(args(expression)),expected)
        self.assertEqual(expected['exact'],'7')

    def test_cancelled_execution_does_not_return_a_partial_tree(self):
        event=threading.Event();event.set()
        expression=op('sum',op('product','3','4'),op('quotient','7','8'))
        with self.assertRaises(EngineError):m.calculate(args(expression),event)
        class StopDuringWalk:
            def __init__(self):self.checks=0
            def is_set(self):self.checks+=1;return self.checks>=4
        original=copy.deepcopy(expression)
        with self.assertRaises(EngineError):m.calculate(args(expression),StopDuringWalk())
        self.assertEqual(expression,original)

    def test_autonomous_dispatch_stage_omissions_and_citation_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            gid=a.create_goal(store,sid,'計算並核對',file_delivery=True)
            engine=ScriptedEngine(action('calculate_expression',expression=op('sum','1234567','1')),
                action('write_artifact',filename='test.txt',content='數據[D1:L1]'),action('ask',question='暫停'))
            goal=a.run_goal(store,sid,gid,engine,max_steps=3)
            self.assertEqual(goal['steps'][0]['observation']['exact'],'1234568')
            self.assertEqual(goal['steps'][1]['action']['tool'],'invalid')
            child=a.create_goal(store,sid,'接續',parent_id=gid)
            stage=json.dumps(a.get_goal(store,sid,child)['previous_stage'],ensure_ascii=False)
            self.assertNotIn('1234567',stage)
            self.assertIn('expression_details_omitted',stage);self.assertIn('calculation_details_omitted',stage)

    def test_saved_expression_trace_survives_backup_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'state.sqlite3');sid=store.new_session()
            gid=a.create_goal(store,sid,'計算並保存結果')
            expression=op('product',op('sum','1/7','2/3'),'21')
            goal=a.run_goal(store,sid,gid,ScriptedEngine(action('calculate_expression',expression=expression)),max_steps=1)
            self.assertEqual(goal['status'],'paused');self.assertEqual(goal['steps'][0]['observation']['exact'],'17')
            saved=create_backup(store.path,root/'copy.paibackup')
            restore_backup(saved['path'],root/'restored')
            restored=Store(root/'restored'/'assistant.sqlite3')
            self.assertEqual(a.get_goal(restored,sid,gid)['steps'],goal['steps'])
            finished=a.run_goal(restored,sid,gid,ScriptedEngine(action('finish',report='精確結果17。')),max_steps=1)
            self.assertEqual(finished['status'],'report_ready');self.assertEqual(len(finished['steps']),2)
            self.assertEqual(finished['steps'][0],goal['steps'][0])


if __name__=='__main__':unittest.main()
