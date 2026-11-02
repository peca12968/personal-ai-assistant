import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, autonomy_csv as c
from assistant.answer_runs import lease, LeaseBusy
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.goal_review import snapshot, set_hidden
from assistant.store import Store
from assistant.tasks import canonical
from test_autonomy import ScriptedEngine, action


DATA = 'region,state,amount\n北,paid,10\n北,paid,20\n中,paid,100\n南,paid,5\n南,paid,5\n南,paid,5\n西,unpaid,999\n'


def spec(**changes):
    return {'group_column': 'region', 'value_column': 'amount',
            'filters': [{'column': 'state', 'op': 'eq', 'value': 'paid'}],
            'sort': {'by': 'sum', 'direction': 'desc'}, 'top_n': 2, **changes}


class AutonomousCsvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.path = self.root / 'sales.csv'
        self.path.write_text(DATA, encoding='utf-8')
        self.did = self.store.attach_document(self.sid, self.path)
        self.gid = a.create_goal(self.store, self.sid, '分析已付款費用', csv_document_id=self.did)

    def plan(self, **changes):
        return c.plan(self.store, self.gid, spec(**changes))['plan_id']

    def test_real_plan_execute_observe_loop_has_exact_totals_and_weighted_means(self):
        engine = ScriptedEngine(action('plan_csv', spec=spec()),
            lambda: action('execute_csv', plan_id=a.get_goal(self.store, self.sid, self.gid)['csv_plans'][0]['plan_id']),
            action('finish', report='依完整計算結果整理分析。'))
        goal = a.run_goal(self.store, self.sid, self.gid, engine)
        self.assertEqual([s['action']['tool'] for s in goal['steps']], ['plan_csv', 'execute_csv', 'finish'])
        self.assertEqual(goal['steps'][0]['observation']['status'], 'planned')
        result = goal['steps'][1]['observation']
        self.assertEqual(result['status'], 'succeeded')
        report = result['report']
        self.assertEqual((report['input_row_count'], report['row_count'], report['group_count']), (7, 6, 3))
        self.assertEqual((report['total'], report['mean']), ('145', '24.1667'))
        self.assertEqual((report['returned_total'], report['returned_row_count'], report['returned_mean']), ('130', 3, '43.3333'))
        self.assertTrue(report['selection_limited'])
        self.assertEqual([g['group'] for g in result['groups']], ['中', '北'])
        self.assertEqual([g['mean'] for g in result['groups']], ['100.0000', '15.0000'])
        self.assertIn('43.3333', engine.prompts[-1])
        self.assertNotIn(str(self.path), engine.prompts[-1])

    def test_scope_selection_and_malformed_csv_roll_back_goal_creation(self):
        other = self.store.new_session()
        original = len(a.goals(self.store, self.sid))
        with self.assertRaises(ValueError):
            a.create_goal(self.store, other, '錯誤範圍', csv_document_id=self.did)
        self.assertEqual(a.goals(self.store, other), [])
        self.path.write_text('x,x\n1,2', encoding='utf-8')
        self.store.attach_document(self.sid, self.path)
        with self.assertRaises(ValueError):
            a.create_goal(self.store, self.sid, '損壞CSV', csv_document_id=self.did, file_delivery=True)
        self.assertEqual(len(a.goals(self.store, self.sid)), original)
        with self.assertRaises(ValueError):
            a.create_goal(self.store, self.sid, '不接受布林編號', csv_document_id=True)

    def test_unselected_goal_and_next_stage_do_not_gain_csv_tools(self):
        no_csv = a.create_goal(self.store, self.sid, '沒有選CSV')
        goal = a.run_goal(self.store, self.sid, no_csv, ScriptedEngine(action('plan_csv', spec=spec())), max_steps=1)
        self.assertEqual(goal['steps'][0]['action']['tool'], 'invalid')
        self.assertEqual(goal['csv_plans'], [])
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('finish', report='分析計畫待安排，尚未計算。')))
        child = a.create_goal(self.store, self.sid, '下一階段', parent_id=self.gid)
        self.assertIsNone(a.get_goal(self.store, self.sid, child)['csv_source'])

    def test_plans_are_saved_before_execution_deduplicated_bounded_and_typed(self):
        pid = self.plan()
        self.assertEqual(pid, self.plan())
        self.assertIsNone(c.details(self.store, self.sid, self.gid, pid)['report'])
        for n in [1, 3, 4]:
            self.plan(top_n=n)
        with self.assertRaisesRegex(ValueError, '4份'):
            self.plan(top_n=5)
        with self.assertRaises(ValueError):
            c.plan(self.store, self.gid, {**spec(), 'code': 'DO_NOT_EXECUTE'})
        with self.assertRaises(ValueError):
            self.plan(sort={'by': 'median', 'direction': 'desc'})
        with self.assertRaises(ValueError):
            c.result_page(self.store, self.gid, pid, 0)

    def test_imported_snapshot_not_external_path_and_source_change_during_calculation(self):
        pid = self.plan()
        self.path.write_text('changed externally', encoding='utf-8')
        with patch('assistant.tasks.read_document', side_effect=AssertionError('must not reopen original')):
            result = c.execute(self.store, self.sid, self.gid, pid)
        self.assertEqual(result['report']['total'], '145')
        self.assertIn('external original not reopened', result['report']['verification']['source_scope'])
        second = self.plan(top_n=None)
        real = c.calculate
        def changed(*args, **kwargs):
            report = real(*args, **kwargs)
            self.path.write_text(DATA.replace('100', '200'), encoding='utf-8')
            self.store.attach_document(self.sid, self.path)
            return report
        with patch.object(c, 'calculate', side_effect=changed):
            result = c.execute(self.store, self.sid, self.gid, second)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(c.details(self.store, self.sid, self.gid, second)['report'])

    def test_invalid_number_zero_rows_and_timeout_are_distinct(self):
        empty = self.plan(filters=[{'column': 'state', 'op': 'eq', 'value': 'missing'}])
        report = c.execute(self.store, self.sid, self.gid, empty)['report']
        self.assertEqual(report['row_count'], 0)
        self.assertIsNone(report['mean'])
        timeout = self.plan()
        self.assertEqual(c.execute(self.store, self.sid, self.gid, timeout, timeout=0)['status'], 'failed')
        self.path.write_text(DATA.replace('paid,100', 'paid,invalid'), encoding='utf-8')
        self.store.attach_document(self.sid, self.path)
        gid = a.create_goal(self.store, self.sid, '損壞數值', csv_document_id=self.did)
        pid = c.plan(self.store, gid, spec())['plan_id']
        result = c.execute(self.store, self.sid, gid, pid)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('第 3 筆', result['error'])
        self.assertIsNone(c.details(self.store, self.sid, gid, pid)['report'])

    def test_cancelled_calculation_does_not_finish_goal_or_save_a_successful_result(self):
        pid = self.plan()
        cancel = threading.Event()
        original = c.calculate
        def cancelled(*args, **kwargs):
            report = original(*args, **kwargs)
            cancel.set()
            return report
        with patch.object(c, 'calculate', side_effect=cancelled):
            goal = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('execute_csv', plan_id=pid)), cancel=cancel)
        self.assertEqual(goal['status'], 'paused')
        self.assertEqual(goal['steps'][-1]['observation']['status'], 'cancelled')
        self.assertIsNone(c.details(self.store, self.sid, self.gid, pid)['report'])

    def test_resume_running_plan_and_completed_retry_never_duplicate_or_cross_scope(self):
        pid = self.plan()
        with self.store.connect() as db:
            db.execute("UPDATE autonomous_csv_plans SET status='running' WHERE id=?", (pid,))
        with lease(self.store, self.gid), self.assertRaises(LeaseBusy):
            a.run_goal(self.store, self.sid, self.gid, None)
        result = a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('execute_csv', plan_id=pid)), max_steps=1)
        self.assertEqual(result['csv_plans'][0]['status'], 'succeeded')
        with patch.object(c, 'calculate', side_effect=AssertionError('must reuse completed result')):
            self.assertEqual(c.execute(self.store, self.sid, self.gid, pid)['report']['total'], '145')
        other = a.create_goal(self.store, self.sid, '另一目標', csv_document_id=self.did)
        with self.assertRaises(ValueError):
            c.execute(self.store, self.sid, other, pid)
        with self.assertRaises(ValueError):
            c.execute(self.store, self.store.new_session(), self.gid, pid)

    def test_paginated_results_are_explicit_and_full_export_keeps_every_group(self):
        self.path.write_text('region,state,amount\n' + 'X' * 250 + ',paid,0\n' + ''.join(f'group{i:02d},paid,{i}\n' for i in range(1, 45)), encoding='utf-8')
        self.store.attach_document(self.sid, self.path)
        gid = a.create_goal(self.store, self.sid, '全部組別', csv_document_id=self.did)
        pid = c.plan(self.store, gid, spec(top_n=None))['plan_id']
        first = c.execute(self.store, self.sid, gid, pid)
        second = c.result_page(self.store, gid, pid, first['next_offset'])
        third = c.result_page(self.store, gid, pid, second['next_offset'])
        self.assertEqual([len(p['groups']) for p in [first, second, third]], [20, 20, 5])
        self.assertTrue(all(p['page_is_partial'] for p in [first, second, third]))
        self.assertIsNone(third['next_offset'])
        self.assertTrue(third['groups'][-1]['group_truncated'])
        self.assertEqual(len(third['groups'][-1]['group']), 200)
        self.assertEqual(len({g['group'] for p in [first, second, third] for g in p['groups']}), 45)
        path = self.root / 'full.json'
        receipt = c.export(self.store, self.sid, gid, pid, path)
        body = path.read_bytes()
        self.assertEqual(len(json.loads(body)['groups']), 45)
        self.assertEqual(len(json.loads(body)['groups'][-1]['group']), 250)
        self.assertEqual(receipt['sha256'], hashlib.sha256(body).hexdigest())
        with self.assertRaises(FileExistsError):
            c.export(self.store, self.sid, gid, pid, path)
        self.assertEqual(path.read_bytes(), body)
        with self.assertRaises(ValueError):
            c.result_page(self.store, gid, pid, 45)

    def test_backup_roundtrip_preserves_results_and_legacy_has_no_csv_permission(self):
        pid = self.plan()
        c.execute(self.store, self.sid, self.gid, pid)
        before = c.details(self.store, self.sid, self.gid, pid)
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        store = Store(target / 'assistant.sqlite3')
        self.assertEqual(c.details(store, self.sid, self.gid, pid), before)
        self.path.unlink()
        c.export(store, self.sid, self.gid, pid, target / 'result.json')
        with store.connect() as db:
            db.execute("UPDATE autonomous_csv_plans SET report_json='{}' WHERE id=?", (pid,))
        with self.assertRaisesRegex(ValueError, '指紋'):
            validate_database(store.path)
        with store.connect() as db:
            db.execute('DROP TABLE autonomous_csv_plans')
            db.execute('DROP TABLE autonomous_csv_sources')
        validate_database(store.path)
        store = Store(store.path)
        self.assertIsNone(a.get_goal(store, self.sid, self.gid)['csv_source'])

    def test_csv_progress_resurfaces_hidden_goal_without_leaking_report_into_review(self):
        row = snapshot(self.store)['items'][0]
        set_hidden(self.store, self.sid, self.gid, fingerprint=row['fingerprint'], hidden=True)
        pid = self.plan()
        self.assertEqual(snapshot(self.store)['visible_count'], 1)
        row = snapshot(self.store)['items'][0]
        set_hidden(self.store, self.sid, self.gid, fingerprint=row['fingerprint'], hidden=True)
        c.execute(self.store, self.sid, self.gid, pid)
        current = snapshot(self.store)
        self.assertEqual(current['visible_count'], 1)
        self.assertNotIn('43.3333', json.dumps(current))

    def test_cli_selection_and_export_do_not_initialize_model(self):
        from assistant.__main__ import main
        with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--session', self.sid,
                '--new-goal', 'CSV分析', '--csv-document', str(self.did)]), \
                patch('assistant.__main__.CodexEngine', side_effect=AssertionError('must not call model')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        self.assertEqual(json.loads(output.getvalue())['csv_source']['document_id'], self.did)
        pid = self.plan()
        c.execute(self.store, self.sid, self.gid, pid)
        path = self.root / 'cli-export.json'
        with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--session', self.sid,
                '--export-goal-csv', pid, '--csv-goal', self.gid, '--output', str(path)]), \
                patch('assistant.__main__.CodexEngine', side_effect=AssertionError('must not call model')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        self.assertTrue(json.loads(output.getvalue())['verified'])
        self.assertEqual(json.loads(path.read_bytes())['total'], '145')
