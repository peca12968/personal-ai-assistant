import copy
import csv
import hashlib
import io
import json
import statistics
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, autonomy_csv, csv_grouping
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from assistant.tasks import execute_task, export_task, task_context, canonical
from assistant.workflows import MEAN_POLICY, calculate, plan_workflow, propose_workflow, validate_spec
from test_csv_median import four


def spec(**extra):
    return {'group_column': 'region', 'secondary_group_column': 'product', 'value_column': 'amount',
            'filters': [], 'sort': {'by': 'group', 'direction': 'asc'}, 'top_n': None, **extra}


class CsvGroupingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'assistant.sqlite3'); self.sid = self.store.new_session()
        self.cases = json.loads((Path(__file__).resolve().parents[1] / 'evals/csv_grouping.json').read_text(encoding='utf-8'))

    def source(self, case_id):
        path = self.root / (case_id + '.csv')
        path.write_text(next(c['csv_content'] for c in self.cases if c['id'] == case_id), encoding='utf-8', newline='')
        return path

    def direct(self, rows, workflow=None, version=3):
        workflow = workflow if workflow is not None else spec()
        plan = {'version': version, 'group_column': workflow['group_column'], 'value_column': 'amount',
                'spec': workflow, 'mean_policy': dict(MEAN_POLICY)}
        return calculate(plan, {'name': 'data.csv', 'sha256': 'a' * 64},
                         ['region', 'product', 'amount', 'status'], rows, None, time.monotonic() + 30)

    def test_strict_optional_column_detachment_and_legacy_shape(self):
        headers = ['region', 'product', 'amount']
        original = spec(); detached = validate_spec(original, headers)
        original['secondary_group_column'] = 'amount'
        self.assertEqual(detached['secondary_group_column'], 'product')
        for value in (None, False, 2, [], ['product'], 'missing', 'region', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_spec(spec(secondary_group_column=value), headers)
        with self.assertRaises(ValueError):
            validate_spec(spec(third_group_column='amount'), headers)
        with self.assertRaises(ValueError):
            self.direct([['A', 'x', '1', 'paid']], version=2)
        old = spec(); old.pop('secondary_group_column')
        for version in (2, 3):
            result = self.direct([['A', 'x', '1', 'paid'], ['A', 'y', '2', 'paid']], old, version)
            self.assertEqual(result['groups'][0]['group'], 'A')
            self.assertNotIn('group_values', result['groups'][0])
            self.assertNotIn('group_columns', result); self.assertNotIn('grouping_policy', result)
            self.assertTrue(csv_grouping.valid_grouping_report(result))

    def test_literal_tuple_identity_and_raw_column_order_not_json_label_order(self):
        source = next(c['csv_content'] for c in self.cases if c['id'] == 'literal_pair_identity')
        rows = list(csv.reader(io.StringIO(source)))[1:] + [['\n', 'x', '1', 'paid'], ['"', 'x', '1', 'paid']]
        expected = {}
        for a, b, value, _ in rows:
            expected.setdefault((a, b), []).append(Fraction(value))
        report = self.direct(rows)
        self.assertEqual([tuple(g['group_values']) for g in report['groups']], sorted(expected))
        self.assertEqual(len(report['groups']), 11)
        for group in report['groups']:
            key = tuple(group['group_values']); values = expected[key]
            self.assertEqual(json.loads(group['group']), list(key))
            self.assertEqual(group['count'], len(values)); self.assertEqual(Fraction(group['sum']), sum(values))
            self.assertEqual(group['mean'], four(sum(values) / len(values)))
        descending = self.direct(rows, spec(sort={'by': 'group', 'direction': 'desc'}))
        self.assertEqual([tuple(g['group_values']) for g in descending['groups']], sorted(expected, reverse=True))

    def test_exact_numeric_ranking_ties_and_selected_median_population(self):
        rows = [['A', 'x', '1.00003', 'paid'], ['A', 'x', '1.00005', 'paid'],
                ['A', 'y', '1.00004', 'paid'], ['B', 'z', '1.000039', 'paid']]
        for order in ('mean', 'median'):
            report = self.direct(rows, spec(include_median=True, top_n=2, sort={'by': order, 'direction': 'desc'}))
            self.assertEqual([g['group_values'] for g in report['groups']], [['A', 'x'], ['A', 'y']])
            self.assertEqual(report['returned_row_count'], 3)
            self.assertEqual(Fraction(report['returned_median_exact']), Fraction('1.00004'))
            self.assertEqual(Fraction(report['median_exact']), Fraction('1.0000395'))

    def test_large_filtered_top_groups_match_independent_fraction_statistics(self):
        raw = next(c['csv_content'] for c in self.cases if c['id'] == 'ranked_pair_groups')
        rows = list(csv.reader(io.StringIO(raw)))[1:]
        groups = {}
        for a, b, value, status in rows:
            if status == 'paid': groups.setdefault((a, b), []).append(Fraction(value))
        keys = sorted(groups); keys.sort(key=lambda key: sum(groups[key]), reverse=True)
        report = self.direct(rows, spec(include_median=True, filters=[{'column': 'status', 'op': 'eq', 'value': 'paid'}],
                                        top_n=5, sort={'by': 'sum', 'direction': 'desc'}))
        self.assertEqual([tuple(g['group_values']) for g in report['groups']], keys[:5])
        self.assertEqual((report['group_count'], report['row_count'], report['excluded_row_count']), (80, 480, 6))
        for group in report['groups']:
            values = groups[tuple(group['group_values'])]
            self.assertEqual(group['count'], len(values)); self.assertEqual(Fraction(group['sum']), sum(values))
            self.assertEqual(group['mean'], four(sum(values) / len(values)))
            self.assertEqual(Fraction(group['median_exact']), statistics.median(values))
        for prefix, values in [('', [v for values in groups.values() for v in values]),
                               ('returned_', [v for key in keys[:5] for v in groups[key]])]:
            self.assertEqual(report[prefix + 'row_count'], len(values))
            self.assertEqual(Fraction(report[prefix + 'total']), sum(values))
            self.assertEqual(report[prefix + 'mean'], four(sum(values) / len(values)))
            self.assertEqual(Fraction(report[prefix + 'median_exact']), statistics.median(values))

    def test_empty_invalid_group_bound_and_cancelled_work_are_complete_or_fail(self):
        report = self.direct([], spec(include_median=True))
        self.assertEqual(report['groups'], []); self.assertEqual(report['group_columns'], ['region', 'product'])
        self.assertIsNone(report['mean']); self.assertIsNone(report['median'])
        self.assertTrue(csv_grouping.valid_grouping_report(report))
        with self.assertRaisesRegex(ValueError, '第 2 筆'):
            self.direct([['A', 'x', '100', 'paid'], ['Z', 'y', 'oops', 'paid']], spec(top_n=1))
        rows = [[str(n), 'x', '1', 'paid'] for n in range(5001)]
        self.assertEqual(self.direct(rows[:-1], spec(top_n=1))['group_count'], 5000)
        with self.assertRaisesRegex(ValueError, '5000'):
            self.direct(rows, spec(top_n=1))
        task = plan_workflow(self.store, self.sid, self.source('ranked_pair_groups'),
                             spec(include_median=True, filters=[{'column': 'status', 'op': 'eq', 'value': 'paid'}]))
        cancel = threading.Event()
        from assistant.workflows import median_statistics
        def stop(*args):
            result = median_statistics(*args); cancel.set(); return result
        with patch('assistant.workflows.median_statistics', side_effect=stop):
            done = execute_task(self.store, self.sid, task['id'], cancel)
        self.assertEqual(done['status'], 'cancelled'); self.assertIsNone(done['result'])

    def test_imported_pages_export_backup_and_rehashed_bad_keys_are_rejected(self):
        path = self.source('ranked_pair_groups'); did = self.store.attach_document(self.sid, path)
        gid = autonomy.create_goal(self.store, self.sid, '雙欄統計', csv_document_id=did)
        workflow = spec(include_median=True, filters=[{'column': 'status', 'op': 'eq', 'value': 'paid'}])
        pid = autonomy_csv.plan(self.store, gid, workflow)['plan_id']; path.unlink()
        first = autonomy_csv.execute(self.store, self.sid, gid, pid)
        self.assertEqual(first['status'], 'succeeded', first)
        pages = [first] + [autonomy_csv.result_page(self.store, gid, pid, n) for n in (20, 40, 60)]
        keys = [tuple(group['group_values']) for page in pages for group in page['groups']]
        self.assertEqual(len(set(keys)), 80); self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(page['report'] == first['report'] for page in pages))
        target = self.root / 'export.json'; autonomy_csv.export(self.store, self.sid, gid, pid, target)
        report = json.loads(target.read_text(encoding='utf-8'))
        self.assertTrue(csv_grouping.valid_grouping_report(report))
        backup = create_backup(self.store.path); restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3'); validate_database(restored.path)
        self.assertEqual(autonomy_csv.execute(restored, self.sid, gid, pid)['report_sha256'], first['report_sha256'])
        bad = copy.deepcopy(report); bad['groups'][0]['group_values'][0] = '不同原文'
        payload = canonical(bad); digest = hashlib.sha256(payload).hexdigest()
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_csv_plans SET report_json=?,report_sha256=? WHERE id=?', (payload.decode(), digest, pid))
        with self.assertRaises(ValueError): autonomy_csv.result_page(self.store, gid, pid, 0)
        with self.assertRaises(ValueError): validate_database(self.store.path)

    def test_shape_policy_and_full_key_uniqueness_validation(self):
        report = self.direct([['A', 'x', '1', 'paid'], ['A', 'y', '2', 'paid']])
        changes = []
        for field, value in [('group_columns', ['product', 'region']), ('grouping_policy', {}), ('group_column', 'wrong'), ('workflow', [])]:
            bad = copy.deepcopy(report); bad[field] = value; changes.append(bad)
        for value in (None, 'A|x', ['A'], ['A', 1]):
            bad = copy.deepcopy(report); bad['groups'][0]['group_values'] = value; changes.append(bad)
        bad = copy.deepcopy(report); bad['groups'][1] = copy.deepcopy(bad['groups'][0]); changes.append(bad)
        bad = copy.deepcopy(report); del bad['workflow']['secondary_group_column']; changes.append(bad)
        for bad in changes: self.assertFalse(csv_grouping.valid_grouping_report(bad), bad)

    def test_manual_planning_summary_clips_each_component_and_exports_full_keys(self):
        key = '甲🙂' * 140
        path = self.root / 'long.csv'
        out = io.StringIO(newline=''); writer = csv.writer(out)
        writer.writerow(['region', 'product', 'amount', 'status']); writer.writerow([key, key + '乙', '127.001', 'paid'])
        path.write_text(out.getvalue(), encoding='utf-8', newline='')
        workflow = spec(include_median=True)
        class Engine:
            def answer(self, prompt, cancel=None):
                self.prompt = prompt
                return json.dumps({'status': 'ready', 'spec': workflow})
        engine = Engine()
        proposal = propose_workflow(self.store, self.sid, path, '同時按region與product統計amount並計算中位數', engine)
        task = proposal['task']; self.assertEqual(task['status'], 'planned')
        self.assertIn('secondary_group_column', engine.prompt); self.assertNotIn(key, engine.prompt)
        self.assertTrue(any('region' in step and 'product' in step for step in task['plan']['steps']))
        done = execute_task(self.store, self.sid, task['id']); self.assertEqual(done['status'], 'succeeded')
        summary = task_context(self.store, self.sid, 'CSV結果')[0]
        self.assertEqual(summary['group_columns'], ['region', 'product'])
        preview = summary['groups_preview'][0]
        self.assertEqual(preview['group_values'], [key[:200], (key + '乙')[:200]])
        self.assertEqual(preview['group_values_truncated'], [True, True]); self.assertTrue(preview['label_truncated'])
        report = done['result']['report']; self.assertEqual(report['groups'][0]['group_values'], [key, key + '乙'])
        output = self.root / 'manual.json'; export_task(self.store, self.sid, task['id'], output)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')), report)
        bad = copy.deepcopy(report); bad['groups'][0]['group'] = '錯誤標籤'
        with self.store.connect() as db:
            db.execute('UPDATE task_runs SET result_json=? WHERE id=?',
                       (json.dumps({'report': bad, 'report_sha256': hashlib.sha256(canonical(bad)).hexdigest()}), task['id']))
        self.assertEqual(task_context(self.store, self.sid, 'CSV結果')[0]['status'], 'unverified')
        with self.assertRaises(ValueError): export_task(self.store, self.sid, task['id'], self.root / 'bad.json')

    def test_task_window_displays_both_columns_and_keeps_execution_explicit(self):
        import tkinter as tk
        from assistant.task_ui import TaskWindow
        from assistant.tasks import plan_csv
        root = tk.Tk(); root.withdraw()
        view = None
        try:
            path = self.source('literal_pair_identity')
            paired = plan_workflow(self.store, self.sid, path, spec())
            single = plan_csv(self.store, self.sid, path, 'region', 'amount')
            view = TaskWindow(root, self.store, self.sid); view.window.withdraw()
            view.refresh(paired['id']); view.show()
            self.assertIn('分組欄：region、product', view.detail.get('1.0', 'end'))
            self.assertEqual(self.store.task(self.sid, paired['id'])['status'], 'planned')
            self.assertEqual(str(view.execute_btn['state']), 'normal')
            self.assertEqual(str(view.export_btn['state']), 'disabled')
            done = execute_task(self.store, self.sid, paired['id'])
            self.assertEqual(done['status'], 'succeeded')
            view.refresh(paired['id']); view.show()
            self.assertIn('group_values', view.detail.get('1.0', 'end'))
            self.assertEqual(str(view.execute_btn['state']), 'disabled')
            self.assertEqual(str(view.export_btn['state']), 'normal')
            view.refresh(single['id']); view.show()
            self.assertIn('分組欄：region　加總欄：amount', view.detail.get('1.0', 'end'))
            self.assertEqual(self.store.task(self.sid, single['id'])['status'], 'planned')
        finally:
            for timer in root.tk.call('after', 'info'):
                root.tk.call('after', 'cancel', timer)
            root.destroy()
