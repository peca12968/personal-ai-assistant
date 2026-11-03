import builtins
import copy
import csv
import io
import json
import random
import statistics
import tempfile
import threading
import time
import unittest
from decimal import Decimal, localcontext, Inexact, Rounded, ROUND_DOWN
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, autonomy_csv
from assistant import csv_statistics as statistics_tool
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from assistant.tasks import execute_task, export_task, task_context, load_csv, TaskCancelled
from assistant.workflows import validate_spec, plan_workflow, calculate, propose_workflow


def spec(*, median=True, top=None, filters=None, order='median'):
    return {'group_column':'team','value_column':'amount','filters':filters or [],
            'sort':{'by':order,'direction':'desc'},'top_n':top,'include_median':median}


def four(value):
    if value is None:return None
    scaled=abs(value)*10000
    q,r=divmod(scaled.numerator,scaled.denominator)
    if 2*r>scaled.denominator or (2*r==scaled.denominator and q%2):q+=1
    return ('-' if value<0 and q else '')+str(q//10000)+'.'+str(q%10000).zfill(4)


class CsvMedianTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'state.sqlite3');self.sid=self.store.new_session()
        self.cases={c['id']:c for c in json.loads((Path(__file__).resolve().parents[1]/'evals/csv_median.json').read_text(encoding='utf-8'))}

    def source(self,cid):
        path=self.root/(cid+'.csv');path.write_text(self.cases[cid]['csv_content'],encoding='utf-8',newline='');return path

    def run_csv(self,cid,workflow):
        task=plan_workflow(self.store,self.sid,self.source(cid),workflow)
        return execute_task(self.store,self.sid,task['id'])

    def assert_median(self,row,values,prefix=''):
        exact=statistics.median(values) if values else None
        self.assertEqual(row[prefix+'median'],four(exact))
        self.assertEqual(Fraction(row[prefix+'median_exact']) if exact is not None else row[prefix+'median_exact'],exact)
        self.assertEqual(row[prefix+'median_rounded'],exact is not None and Fraction(four(exact))!=exact)

    def test_strict_opt_in_sort_requirement_and_legacy_contract(self):
        for bad in (None,0,1,'true',[],{}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_spec(spec(median=bad),['team','amount'])
        for workflow in (spec(median=False),{k:v for k,v in spec().items() if k!='include_median'}):
            with self.assertRaises(ValueError):validate_spec(workflow,['team','amount'])
        plain=spec(order='sum');plain.pop('include_median')
        document,headers,rows=load_csv(self.source('exact_tie_ranking'))
        legacy={'version':2,'spec':plain,'group_column':'team','value_column':'amount'}
        report=calculate(legacy,document,headers,rows,None,time.monotonic()+30)
        self.assertNotIn('mean',report);self.assertNotIn('median',report)
        legacy['spec']['include_median']=False
        with self.assertRaises(ValueError):calculate(legacy,document,headers,rows,None,time.monotonic()+30)
        for workflow in (plain|{'include_median':False},{k:v for k,v in plain.items() if k!='include_median'}):
            with patch('assistant.workflows.median_statistics',side_effect=AssertionError('not opted in')):
                done=self.run_csv('exact_tie_ranking',workflow)
            self.assertEqual(done['status'],'succeeded',done.get('error'))
            self.assertNotIn('median',done['result']['report'])
            self.assertTrue(all('median' not in row for row in done['result']['report']['groups']))

    def test_exact_odd_even_negative_empty_rounding_and_ambient_context(self):
        cases=[[],['1'],['-1','0'],['1.00004','1.00005'],['-0.00004','0'],
               ['9'*60,'0.'+'0'*55+'1'],['-2.34505','-2.34505'],['2.34515','2.34515']]
        rng=random.Random(8104)
        cases += [[str(rng.randrange(-100000,100000)/10000) for _ in range(n)] for n in range(2,40)]
        for values in cases:
            with self.subTest(values=values[:3]), localcontext() as context:
                context.prec=3;context.rounding=ROUND_DOWN;context.traps[Inexact]=True;context.traps[Rounded]=True
                decimals=[Decimal(value) for value in values]
                result=statistics_tool.median_statistics(decimals,None,time.monotonic()+30)
                self.assert_median(result,[Fraction(value) for value in values])
                self.assertEqual(decimals,[Decimal(value) for value in values])

    def test_large_groups_global_and_returned_population_independent_reference(self):
        workflow=spec(top=5,filters=[{'column':'status','op':'eq','value':'paid'}])
        original=copy.deepcopy(workflow)
        done=self.run_csv('ranked_medians',workflow)
        self.assertEqual(done['status'],'succeeded',done.get('error'))
        report=done['result']['report'];groups={}
        for row in csv.DictReader(io.StringIO(self.cases['ranked_medians']['csv_content'])):
            if row['status']=='paid':groups.setdefault(row['team'],[]).append(Fraction(row['amount']))
        order=sorted(groups);order.sort(key=lambda key:statistics.median(groups[key]),reverse=True)
        self.assertEqual([row['group'] for row in report['groups']],order[:5])
        for row in report['groups']:
            values=groups[row['group']]
            self.assert_median(row,values)
            self.assertEqual(row['count'],len(values));self.assertEqual(Fraction(row['sum']),sum(values))
            self.assertEqual(row['mean'],four(sum(values)/len(values)))
        population=[v for values in groups.values() for v in values]
        selected=[v for key in order[:5] for v in groups[key]]
        self.assert_median(report,population);self.assert_median(report,selected,'returned_')
        self.assertEqual((report['row_count'],report['returned_row_count']),(564,45))
        self.assertNotEqual(statistics.median(population),statistics.median([statistics.median(v) for v in groups.values()]))
        self.assertEqual(workflow,original)

    def test_exact_ranking_and_equal_medians_preserve_group_name_ties(self):
        done=self.run_csv('exact_tie_ranking',spec(top=2));report=done['result']['report']
        self.assertEqual([r['group'] for r in report['groups']],['A','C'])
        self.assertEqual([r['median'] for r in report['groups']],['1.0000','1.0000'])
        self.assertEqual(Fraction(report['median_exact']),Fraction('1.00003'))
        self.assertEqual(Fraction(report['returned_median_exact']),Fraction('1.00004'))
        self.assertEqual(report['returned_row_count'],3)

    def test_empty_and_invalid_values_never_become_partial_success(self):
        empty=self.run_csv('empty_median',spec(filters=[{'column':'status','op':'eq','value':'missing'}]))
        report=empty['result']['report'];self.assert_median(report,[]);self.assert_median(report,[],'returned_')
        self.assertEqual(report['groups'],[]);self.assertEqual(report['total'],'0')
        invalid=self.run_csv('invalid_median_value',spec(top=1))
        self.assertEqual(invalid['status'],'failed');self.assertIsNone(invalid['result'])
        self.assertIn('第 2 筆',invalid['error'])
        path=self.root/'hidden.csv';path.write_text('team,amount\nA,100\nZ,oops\n',encoding='utf-8')
        task=plan_workflow(self.store,self.sid,path,spec(top=1))
        self.assertEqual(execute_task(self.store,self.sid,task['id'])['status'],'failed')

    def test_cancellation_after_sort_and_mid_group_never_saves_partial_report(self):
        cancel=threading.Event()
        def stopping(values):
            result=builtins.sorted(values);cancel.set();return result
        with patch('assistant.csv_statistics.sorted',side_effect=stopping,create=True),self.assertRaises(TaskCancelled):
            statistics_tool.median_statistics([Decimal(1),Decimal(3)],cancel,time.monotonic()+30)
        cancel.clear()
        task=plan_workflow(self.store,self.sid,self.source('exact_tie_ranking'),spec())
        original=statistics_tool.median_statistics
        def stop_after_first(*args):
            result=original(*args);cancel.set();return result
        with patch('assistant.workflows.median_statistics',side_effect=stop_after_first):
            result=execute_task(self.store,self.sid,task['id'],cancel)
        self.assertEqual(result['status'],'cancelled');self.assertIsNone(result['result'])

    def test_snapshot_pages_export_backup_and_saved_report_validation(self):
        path=self.source('ranked_medians');did=self.store.attach_document(self.sid,path)
        gid=autonomy.create_goal(self.store,self.sid,'中位數',csv_document_id=did)
        workflow=spec(filters=[{'column':'status','op':'eq','value':'paid'}])
        pid=autonomy_csv.plan(self.store,gid,workflow)['plan_id'];path.unlink()
        first=autonomy_csv.execute(self.store,self.sid,gid,pid)
        self.assertEqual(first['status'],'succeeded',first)
        self.assertTrue(first['page_is_partial']);self.assertEqual(first['next_offset'],20)
        second=autonomy_csv.result_page(self.store,gid,pid,20)
        self.assertEqual(first['report']['median_exact'],second['report']['median_exact'])
        target=self.root/'export.json';autonomy_csv.export(self.store,self.sid,gid,pid,target)
        report=json.loads(target.read_text(encoding='utf-8'));self.assertEqual(len(report['groups']),80)
        self.assertTrue(statistics_tool.valid_median_report(report))
        for change in ({'median_exact':'999','median':'1.0000'},{'median_policy':{}},{'median_rounded':'false'}):
            self.assertFalse(statistics_tool.valid_median_report(report|change))
        saved=create_backup(self.store.path);restore_backup(saved['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        validate_database(restored.path)
        self.assertEqual(autonomy_csv.execute(restored,self.sid,gid,pid)['report_sha256'],first['report_sha256'])

    def test_manual_preview_export_and_advisory_natural_planning(self):
        workflow=spec(top=2)
        class Engine:
            def answer(self,prompt,cancel=None):
                self.prompt=prompt
                return json.dumps({'status':'ready','spec':workflow})
        engine=Engine()
        planned=propose_workflow(self.store,self.sid,self.source('exact_tie_ranking'),'按team計算amount中位數前二組',engine)
        self.assertEqual(planned['task']['status'],'planned')
        self.assertIn('include_median',engine.prompt);self.assertNotIn('1.00003',engine.prompt)
        done=execute_task(self.store,self.sid,planned['task']['id'])
        summary=task_context(self.store,self.sid,'中位數是多少')[0]
        self.assertEqual(summary['median_exact'],done['result']['report']['median_exact'])
        target=self.root/'manual.json';export_task(self.store,self.sid,done['id'],target)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')),done['result']['report'])


if __name__=='__main__':unittest.main()
