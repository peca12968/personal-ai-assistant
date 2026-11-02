import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from decimal import Decimal, localcontext

from assistant.backup import create_backup, restore_backup
from assistant.store import Store
from assistant.tasks import TaskCancelled, execute_task, export_task, task_context
from assistant.workflows import arithmetic_mean, plan_workflow, propose_workflow, validate_spec


def spec():
    return {'group_column':'team', 'value_column':'amount',
            'filters':[{'column':'state','op':'eq','value':'paid'}],
            'sort':{'by':'sum','direction':'desc'}, 'top_n':2}


class Engine:
    def __init__(self, response): self.response = response
    def answer(self, prompt, cancel=None):
        self.prompt = prompt
        return json.dumps(self.response, ensure_ascii=False)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.source = self.root / 'private-source.csv'
        self.source.write_text('team,amount,state\nA,2,paid\nB,7,paid\nA,3,paid\nC,1,paid\nD,99,pending\n', encoding='utf-8')

    def execute(self, workflow=None):
        plan = plan_workflow(self.store, self.sid, self.source, workflow or spec())
        return execute_task(self.store, self.sid, plan['id'])

    def test_multistep_artifact_is_exact_and_totals_are_distinct(self):
        source_bytes = self.source.read_bytes()
        done = self.execute()
        self.assertEqual(done['status'], 'succeeded', done['error'])
        report = done['result']['report']
        self.assertEqual([r['group'] for r in report['groups']], ['B','A'])
        self.assertEqual((report['input_row_count'],report['row_count'],report['excluded_row_count']), (5,4,1))
        self.assertEqual((report['total'],report['returned_total']), ('13','12'))
        self.assertEqual((report['group_count'],report['returned_group_count']), (3,2))
        self.assertTrue(report['selection_limited'])
        self.assertEqual(report['filter_stages'][0]['output_rows'], 4)
        output = self.root / 'report.json'
        export_task(self.store, self.sid, done['id'], output)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')), report)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), done['result']['report_sha256'])
        self.assertEqual(self.source.read_bytes(), source_bytes)
        context = task_context(self.store, self.sid, '工作總額')[0]
        self.assertEqual(context['total'], '13')
        self.assertEqual(context['returned_total'], '12')
        self.assertFalse(context['groups_truncated'])
        self.assertTrue(context['selection_limited'])

    def test_numeric_comparison_is_decimal_and_text_comparison_is_literal(self):
        self.source.write_text('team,amount,state\nA,0.10,paid\nB,0.1,paid\nC,10,paid\nD,2,paid\n')
        workflow = spec()
        workflow['filters'] = [{'column':'amount','op':'num_eq','value':'0.1'}]
        report = self.execute(workflow)['result']['report']
        self.assertEqual(report['row_count'], 2)
        self.assertEqual(report['total'], '0.20')
        workflow['filters'][0]['op'] = 'eq'
        self.assertEqual(self.execute(workflow)['result']['report']['row_count'], 1)
        workflow['filters'] = [{'column':'amount','op':'gt','value':'2'}]
        self.assertEqual(self.execute(workflow)['result']['report']['groups'][0]['group'], 'C')

    def test_filters_are_sequential_and_do_not_coerce_invalid_retained_numbers(self):
        self.source.write_text('team,amount,state\nA,bad,pending\nB,2,paid\n')
        workflow = spec()
        workflow['filters'].append({'column':'amount','op':'gte','value':'1'})
        self.assertEqual(self.execute(workflow)['status'], 'succeeded')
        workflow['filters'].reverse()
        done = self.execute(workflow)
        self.assertEqual(done['status'], 'failed')
        self.assertIsNone(done['result'])

    def test_empty_result_and_deterministic_ties(self):
        self.source.write_text('team,amount,state\nZ,1,paid\nA,1,paid\n')
        workflow = spec()
        workflow['top_n'] = 1
        self.assertEqual(self.execute(workflow)['result']['report']['groups'][0]['group'], 'A')
        workflow['filters'][0]['value'] = 'missing'
        report = self.execute(workflow)['result']['report']
        self.assertEqual((report['row_count'],report['total'],report['groups']), (0,'0',[]))

    def test_specs_reject_unknown_steps_coercion_and_missing_columns(self):
        bad = []
        for key,value in [('group_column','missing'),('top_n',True),('top_n',0),('top_n',5001),('execute','delete')]:
            workflow = spec(); workflow[key]=value; bad.append(workflow)
        for rule in [{'column':'state','op':'eval','value':'shell'}, {'column':'amount','op':'gt','value':'NaN'},
                     {'column':'state','op':'eq','value':1}]:
            workflow=spec(); workflow['filters']=[rule]; bad.append(workflow)
        workflow=spec(); workflow['sort']['by']='average'; bad.append(workflow)
        workflow=spec(); workflow['filters']*=7; bad.append(workflow)
        for workflow in bad:
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                plan_workflow(self.store, self.sid, self.source, workflow)
        self.assertFalse(self.store.tasks(self.sid))

    def test_planner_sees_only_headers_request_and_never_executes(self):
        self.source.write_text('team,amount,state\nPRIVATE-CELL-MARKER,20,paid\n')
        engine = Engine({'status':'ready','spec':spec()})
        response = propose_workflow(self.store,self.sid,self.source,'按 team 加總 amount，只保留 paid，取前二組',engine)
        self.assertEqual(response['status'], 'planned')
        self.assertIsNone(response['task']['result'])
        self.assertNotIn('PRIVATE-CELL-MARKER',engine.prompt)
        self.assertNotIn(str(self.source),engine.prompt)
        self.assertIn('team',engine.prompt)
        self.assertEqual(len(self.store.tasks(self.sid)),1)

    def test_clarification_unsupported_and_malformed_model_output_create_no_task(self):
        for response in [{'status':'needs_clarification','question':'哪個金額欄？'},
                         {'status':'unsupported','reason':'不支援中位數'}]:
            self.assertEqual(propose_workflow(self.store,self.sid,self.source,'需求',Engine(response)),response)
        for response in [{'status':'ready','spec':{**spec(),'command':'upload'}},
                         {'status':'ready','spec':{**spec(),'filters':[{'column':'state','operator':'eq','value':'paid'}]}},
                         {'status':'ready','spec':spec(),'source_path':'elsewhere.csv'},
                         {'status':'success','message':'已完成'}, {'status':'unsupported','reason':''}]:
            with self.assertRaises(ValueError):
                propose_workflow(self.store,self.sid,self.source,'需求',Engine(response))
        self.assertFalse(self.store.tasks(self.sid))

    def test_source_changed_during_planning_or_before_execution_is_rejected(self):
        outer = self
        class Changing(Engine):
            def answer(self,prompt,cancel=None):
                outer.source.write_text('team,amount,state\nA,999,paid\n')
                return super().answer(prompt,cancel)
        with self.assertRaisesRegex(ValueError,'規劃期間'):
            propose_workflow(self.store,self.sid,self.source,'需求',Changing({'status':'ready','spec':spec()}))
        self.assertFalse(self.store.tasks(self.sid))
        plan = plan_workflow(self.store,self.sid,self.source,spec())
        self.source.write_text('team,amount,state\nA,1,paid\n')
        self.assertEqual(execute_task(self.store,self.sid,plan['id'])['status'],'failed')

    def test_plan_spec_is_frozen_and_invalid_saved_spec_cannot_run(self):
        workflow=spec()
        plan=plan_workflow(self.store,self.sid,self.source,workflow)
        workflow['filters'][0]['value']='pending'
        self.assertEqual(plan['plan']['spec']['filters'][0]['value'],'paid')
        plan['plan']['spec']['top_n']=-1
        with self.store.connect() as db:
            db.execute('UPDATE task_runs SET plan_json=? WHERE id=?',(json.dumps(plan['plan']),plan['id']))
        self.assertEqual(execute_task(self.store,self.sid,plan['id'])['status'],'failed')

    def test_cancelled_planning_does_not_save_ready_result(self):
        cancel=threading.Event()
        class Cancel(Engine):
            def answer(self,prompt,cancel=None):
                cancel.set()
                return super().answer(prompt,cancel)
        with self.assertRaises(TaskCancelled):
            propose_workflow(self.store,self.sid,self.source,'需求',Cancel({'status':'ready','spec':spec()}),cancel)
        self.assertFalse(self.store.tasks(self.sid))

    def test_cli_and_backup_preserve_workflow_evidence(self):
        path=self.root/'spec.json'
        path.write_text(json.dumps(spec()),encoding='utf-8')
        def run(*args):
            result=subprocess.run([sys.executable,'-m','assistant','--data-dir',str(self.root/'cli'),*args],capture_output=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        plan=run('--plan-workflow',str(self.source),'--workflow-spec',str(path))
        done=run('--run-task',plan['id'],'--session',plan['session_id'])
        self.assertEqual(done['result']['report']['returned_total'],'12')
        self.assertEqual(plan['plan']['version'],3)
        self.assertEqual(done['result']['report']['mean'],'3.2500')
        self.assertEqual(done['result']['report']['returned_mean'],'4.0000')
        backup=create_backup(self.root/'cli/assistant.sqlite3')
        restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(restored.task(plan['session_id'],plan['id'])['result'],done['result'])

    def test_mean_uses_rows_not_mean_of_group_means_and_preserves_top_n_scope(self):
        self.source.write_text('team,amount,state\nA,0,paid\nA,0,paid\nB,9,paid\nC,99,pending\n')
        workflow = spec(); workflow['sort']['by'] = 'mean'; workflow['top_n'] = 1
        done = self.execute(workflow); self.assertEqual(done['status'], 'succeeded', done['error'])
        report = done['result']['report']
        self.assertEqual((report['mean'], report['returned_mean']), ('3.0000', '9.0000'))
        self.assertEqual((report['row_count'], report['returned_row_count']), (3, 1))
        self.assertEqual(report['groups'], [{'group':'B', 'count':1, 'sum':'9', 'mean':'9.0000', 'mean_rounded':False}])
        context = task_context(self.store, self.sid, '平均是多少？')[0]
        self.assertEqual(context['mean'], '3.0000')
        self.assertEqual(context['returned_mean'], '9.0000')
        destination = self.root / 'mean.json'
        export_task(self.store, self.sid, done['id'], destination)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), report)

    def test_mean_rounding_is_half_even_and_explicit(self):
        for total, count, expected, rounded in [('1',3,'0.3333',True), ('2',3,'0.6667',True),
                ('1.23445',1,'1.2344',True), ('1.23455',1,'1.2346',True),
                ('-1.23445',1,'-1.2344',True), ('-0.00001',1,'0.0000',True),
                ('0.3',3,'0.1000',False), ('0',0,None,False),
                ('9'*60,1,'9'*60+'.0000',False)]:
            with self.subTest(total=total, count=count), localcontext() as context:
                context.prec = 6  # Caller precision cannot corrupt task statistics.
                self.assertEqual(arithmetic_mean(Decimal(total),count), (expected,rounded))

    def test_mean_sort_uses_exact_ratios_even_when_display_values_tie(self):
        self.source.write_text('team,amount,state\nA,0.00001,paid\nZ,0.00002,paid\nB,0.00002,paid\n')
        workflow = spec(); workflow['sort']['by'] = 'mean'; workflow['top_n'] = None
        rows = self.execute(workflow)['result']['report']['groups']
        self.assertEqual([r['group'] for r in rows], ['B','Z','A'])
        self.assertEqual({r['mean'] for r in rows}, {'0.0000'})
        workflow['sort']['direction'] = 'asc'
        self.assertEqual([r['group'] for r in self.execute(workflow)['result']['report']['groups']], ['A','B','Z'])

    def test_empty_mean_is_undefined_not_zero(self):
        workflow = spec(); workflow['filters'][0]['value'] = 'absent'
        report = self.execute(workflow)['result']['report']
        self.assertIsNone(report['mean']); self.assertIsNone(report['returned_mean'])
        self.assertEqual(report['returned_row_count'], 0)
        self.assertEqual(report['groups'], [])
        self.assertTrue(report['verification']['mean_basis_matches'])

    def test_old_v2_plan_keeps_sum_only_report_and_cannot_request_mean_sort(self):
        old = plan_workflow(self.store,self.sid,self.source,spec())['plan']
        old['version'] = 2; old.pop('mean_policy')
        old['steps'] = ['Legacy sum-only plan']; old['rules'] = 'Legacy sum rules'
        tid = self.store.create_task(self.sid,old)
        done = execute_task(self.store,self.sid,tid)
        self.assertEqual(done['status'],'succeeded')
        self.assertNotIn('mean',done['result']['report'])
        self.assertTrue(all('mean' not in r for r in done['result']['report']['groups']))
        self.assertNotIn('mean',task_context(self.store,self.sid,'平均結果')[0])
        old['spec']['sort']['by'] = 'mean'
        tid = self.store.create_task(self.sid,old)
        self.assertEqual(execute_task(self.store,self.sid,tid)['status'],'failed')

    def test_changed_mean_policy_and_missing_verification_cannot_claim_success(self):
        plan = plan_workflow(self.store,self.sid,self.source,spec())['plan']
        plan['mean_policy']['places'] = 2
        tid = self.store.create_task(self.sid,plan)
        self.assertEqual(execute_task(self.store,self.sid,tid)['status'],'failed')
        done = self.execute()
        result = done['result']; result['report']['verification'].pop('mean_basis_matches')
        from assistant.tasks import canonical
        result['report_sha256'] = hashlib.sha256(canonical(result['report'])).hexdigest()
        with self.store.connect() as db:
            db.execute('UPDATE task_runs SET result_json=? WHERE id=?', (json.dumps(result), done['id']))
        self.assertEqual(task_context(self.store,self.sid,'平均結果')[0]['status'],'unverified')


if __name__ == '__main__': unittest.main()
