import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from assistant import autonomy, autonomy_csv
from assistant.backup import create_backup, restore_backup
from assistant.store import Store
from assistant.tasks import execute_task, load_csv
from assistant.workflows import validate_spec, matches, plan_workflow, propose_workflow, calculate, MEAN_POLICY
from scripts.evaluate_csv_membership import prepare


def spec(filters, group='部門', value='金額', top=None):
    return {'group_column': group, 'value_column': value, 'filters': filters,
            'sort': {'by': 'group', 'direction': 'asc'}, 'top_n': top}


def rule(column, op, values):
    return {'column': column, 'op': op, 'value': values}


class CsvMembershipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.cases = {c['id']: c for c in json.loads((Path(__file__).resolve().parents[1] / 'evals/csv_membership.json').read_text('utf-8'))}

    def source(self, cid):
        path = self.root / (cid + '.csv')
        path.write_text(self.cases[cid]['csv_content'], encoding='utf-8', newline='')
        return path

    def run_csv(self, cid, workflow):
        path = self.source(cid)
        task = plan_workflow(self.store, self.sid, path, workflow)
        return execute_task(self.store, self.sid, task['id'])

    def test_literal_set_validation_is_strict_bounded_and_detached(self):
        values = ['A', 'a', 'Ａ', '', 'x,y', 'x|y', '.*', ' A ', '1', '01', '1.0']
        original = spec([rule('代碼', 'in', values)])
        validated = validate_spec(original, ['代碼', '部門', '金額'])
        original['filters'][0]['value'].append('later')
        self.assertNotIn('later', validated['filters'][0]['value'])
        for invalid in ([], ['x']*2, list(map(str, range(21))), 'A,B', None, [1], [True], [None], [['A']], ['x'*1001], ['a\x00b']):
            with self.subTest(value=repr(invalid)[:50]), self.assertRaises(ValueError):
                validate_spec(spec([rule('代碼', 'in', invalid)]), ['代碼', '部門', '金額'])
        for op in ('eq', 'num_eq', 'or', 'num_in'):
            with self.assertRaises(ValueError): validate_spec(spec([rule('代碼', op, ['A'])]), ['代碼', '部門', '金額'])
        for op in ('in', 'not_in'):
            validated = validate_spec(spec([rule('代碼', op, list(map(str, range(20))))]), ['代碼', '部門', '金額'])
            self.assertEqual(len(validated['filters'][0]['value']), 20)
        with self.assertRaises(ValueError):
            validate_spec(spec([rule('代碼', 'in', [str(i)+'🙂'*999 for i in range(9)])]), ['代碼', '部門', '金額'])
        validate_spec(spec([rule('代碼', 'in', ['🙂'*1000])]), ['代碼', '部門', '金額'])

    def test_membership_is_exact_text_without_numeric_regex_or_separator_coercion(self):
        selected = ['North', 'north,west', '', '.*', '01']
        values = ['North', 'north,west', '', '.*', '01', 'north', ' North', 'NORTH', 'north|west', '1', '1.0', 'anything']
        for value in values:
            self.assertEqual(matches(value, rule('代碼', 'in', selected), 1), value in selected)
            self.assertEqual(matches(value, rule('代碼', 'not_in', selected), 1), value not in selected)

    def test_real_csv_quoting_empty_and_case_sensitive_selection(self):
        workflow = spec([rule('代碼', 'in', ['north,west', 'North', ''])])
        original = copy.deepcopy(workflow)
        done = self.run_csv('literal_members', workflow)
        self.assertEqual(done['status'], 'succeeded', done.get('error'))
        report = done['result']['report']
        self.assertEqual((report['row_count'], report['total'], report['mean']), (4, '25', '6.2500'))
        self.assertEqual([(r['group'], r['count'], r['sum'], r['mean']) for r in report['groups']],
                         [('乙', 2, '8', '4.0000'), ('甲', 2, '17', '8.5000')])
        self.assertEqual(report['filter_stages'][0]['filter'], workflow['filters'][0])
        self.assertEqual(workflow, original)

    def test_exclusion_precedes_numeric_validation_and_order_remains_observable(self):
        statuses = ['draft', 'void', 'test', 'hold', 'refunded', 'duplicate', 'archived', 'suspended']
        filters = [rule('狀態', 'not_in', statuses), rule('金額', 'gt', '0')]
        done = self.run_csv('exclude_before_numeric', spec(filters))
        self.assertEqual(done['status'], 'succeeded', done.get('error'))
        report = done['result']['report']
        self.assertEqual([r['output_rows'] for r in report['filter_stages']], [16, 14])
        self.assertEqual((report['row_count'], report['total']), (14, '252'))
        self.assertEqual({r['group']: (r['count'], r['sum'], r['mean']) for r in report['groups']},
                         {'甲': (7, '84', '12.0000'), '乙': (7, '168', '24.0000')})
        bad = self.run_csv('exclude_before_numeric', spec(list(reversed(filters))))
        self.assertEqual(bad['status'], 'failed')
        self.assertIn('第 1 筆資料', bad['error'])

    def test_invalid_retained_value_fails_and_empty_match_is_successful_null_mean(self):
        failed = self.run_csv('retained_invalid_stops', spec([rule('地區', 'in', ['台北'])]))
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('第 2 筆資料', failed['error'])
        empty = self.run_csv('retained_invalid_stops', spec([rule('地區', 'in', ['不存在'])]))
        self.assertEqual(empty['status'], 'succeeded')
        report = empty['result']['report']
        self.assertEqual((report['row_count'], report['total'], report['groups']), (0, '0', []))
        self.assertIsNone(report['mean'])
        self.assertIsNone(report['returned_mean'])

    def test_autonomous_snapshot_reuse_backup_restore_and_cancel(self):
        path = self.source('literal_members')
        did = self.store.attach_document(self.sid, path)
        gid = autonomy.create_goal(self.store, self.sid, '合成分析', csv_document_id=did)
        workflow = spec([rule('代碼', 'in', ['North', 'north,west', ''])])
        pid = autonomy_csv.plan(self.store, gid, workflow)['plan_id']
        self.assertEqual(autonomy_csv.plan(self.store, gid, workflow)['plan_id'], pid)
        path.unlink()  # Explicitly imported snapshot remains the source.
        first = autonomy_csv.execute(self.store, self.sid, gid, pid)
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(first['report']['total'], '25')
        backup = create_backup(self.store.path, self.root / 'backup.paibackup')
        restore_backup(backup['path'], self.root / 'restored')
        copy_store = Store(self.root / 'restored' / 'assistant.sqlite3')
        second = autonomy_csv.execute(copy_store, self.sid, gid, pid)
        self.assertEqual(second['report_sha256'], first['report_sha256'])
        self.assertEqual(second['report']['workflow'], workflow)
        other = autonomy_csv.plan(copy_store, gid, spec([rule('代碼', 'not_in', ['north '])]))['plan_id']
        cancel = threading.Event(); cancel.set()
        self.assertEqual(autonomy_csv.execute(copy_store, self.sid, gid, other, cancel)['status'], 'cancelled')

    def test_natural_planner_preserves_set_spec_and_only_creates_a_plan(self):
        workflow = spec([rule('代碼', 'in', ['North', 'north,west', ''])])
        class Engine:
            def answer(self, prompt, cancel=None):
                self.prompt = prompt
                return json.dumps({'status': 'ready', 'spec': workflow}, ensure_ascii=False)
        engine = Engine()
        result = propose_workflow(self.store, self.sid, self.source('literal_members'), '分析三個指定代碼', engine)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['task']['status'], 'planned')
        self.assertEqual(result['task']['plan']['spec'], workflow)
        self.assertIn('not_in', engine.prompt)
        self.assertIn('跨欄位', engine.prompt)
        self.assertNotIn('invalid', engine.prompt)  # Headers/request only, no rows.

    def test_legacy_v2_retains_original_operator_contract(self):
        path = self.source('retained_invalid_stops')
        document, headers, rows = load_csv(path)
        plan = {'version': 2, 'group_column': '部門', 'value_column': '金額',
                'spec': spec([rule('地區', 'eq', '台南')])}
        report = calculate(plan, document, headers, rows, None, time.monotonic()+30)
        self.assertEqual(report['total'], '30')
        self.assertNotIn('mean', report)
        plan['spec']['filters'][0] = rule('地區', 'in', ['台南'])
        with self.assertRaises(ValueError): calculate(plan, document, headers, rows, None, time.monotonic()+30)

    def test_collector_explicit_source_delivery_and_fixed_fixture_are_isolated(self):
        case = copy.deepcopy(self.cases['literal_members'])
        original = copy.deepcopy(case)
        sid, gid = prepare(self.store, case)
        goal = autonomy.get_goal(self.store, sid, gid)
        self.assertIsNotNone(goal['csv_source'])
        self.assertTrue(goal['file_delivery'])
        self.assertEqual(goal['objective'], case['prompt'])
        self.assertEqual(case, original)
        with self.assertRaises(ValueError): prepare(self.store, {**case, 'file_delivery': 'yes'})


if __name__ == '__main__':
    unittest.main()
