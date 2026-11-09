import json
import tempfile
import unittest
from itertools import product
from pathlib import Path

from assistant import autonomy, decision_table, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def gate_table(known=None):
    rows = []
    for entrance, north, south in product(('北門', '南門'), ('是', '否'), ('有', '無')):
        result = '北台' if entrance == '北門' and north == '是' else '待命'
        if entrance == '南門' and south == '有':
            result = '南台'
        rows.append({'when': [entrance, north, south], 'result': result})
    return {'conditions': ['入口', '北台可用', '南台通行證'], 'rows': rows, 'known': known or {}}


class DecisionTableTests(unittest.TestCase):
    def test_optimal_first_question_and_adaptive_stop(self):
        first = decision_table.calculate(gate_table())
        self.assertEqual((first['status'], first['max_questions']), ('needs_check', 2))
        self.assertEqual(first['question'], {
            'condition': '入口',
            'branches': [
                {'value': '北門', 'possible_results': ['北台', '待命'], 'max_questions': 1},
                {'value': '南門', 'possible_results': ['南台', '待命'], 'max_questions': 1},
            ],
        })
        second = decision_table.calculate(gate_table({'入口': '南門'}))
        self.assertEqual(second['question']['condition'], '南台通行證')
        self.assertEqual(second['matching_rows'], [5, 6, 7, 8])
        final = decision_table.calculate(gate_table({'入口': '南門', '南台通行證': '有'}))
        self.assertEqual(final, {'status': 'resolved', 'max_questions': 0,
                                 'matching_rows': [5, 7], 'possible_results': ['南台']})

    def test_sparse_cases_ties_and_invalid_tables(self):
        plan = {'conditions': ['場地', '配電'],
                'rows': [{'when': ['室內', '完成'], 'result': '啟用'},
                         {'when': ['室外', '待查'], 'result': '存放'}],
                'known': {}}
        self.assertEqual(decision_table.calculate(plan)['question']['condition'], '場地')
        plan['known'] = {'場地': '室外', '配電': '完成'}
        self.assertEqual(decision_table.calculate(plan), {'status': 'no_match',
                                                          'matching_rows': [], 'possible_results': []})
        with self.assertRaisesRegex(ValueError, '完整欄名'):
            decision_table.calculate({**plan, 'known': {'未知欄': '完成'}})
        with self.assertRaisesRegex(ValueError, '重複'):
            decision_table.calculate({**plan, 'rows': plan['rows'] + [plan['rows'][0]]})
        with self.assertRaisesRegex(ValueError, '最多4種'):
            decision_table.calculate({'conditions': ['號碼'], 'known': {},
                                      'rows': [{'when': [str(i)], 'result': '存放'} for i in range(5)]})

    def test_declared_domains_distinguish_conflicts_missing_rules_and_exclusions(self):
        plan = {'conditions': ['區位', '吊具'], 'known': {},
                'domains': {'區位': ['北區', '南區'], '吊具': ['輕型', '重型']},
                'excluded': [['北區', '重型']],
                'rows': [{'when': ['北區', '輕型'], 'result': '甲'},
                         {'when': ['南區', '輕型'], 'result': '乙'},
                         {'when': ['南區', '輕型'], 'result': '丙'}]}
        report = decision_table.calculate(plan)
        self.assertEqual(report, {
            'status': 'incomplete_table', 'matching_rows': [1, 2, 3], 'recorded_results': ['甲', '乙', '丙'],
            'coverage': {'missing_rows': [['南區', '重型']], 'excluded_rows': [['北區', '重型']],
                         'conflicting_rows': [{'when': ['南區', '輕型'], 'rows': [2, 3], 'results': ['乙', '丙']}]},
        })
        north = decision_table.calculate({**plan, 'known': {'區位': '北區'}})
        self.assertEqual((north['status'], north['possible_results'], north['max_questions']), ('resolved', ['甲'], 0))
        self.assertEqual(north['coverage']['excluded_rows'], [['北區', '重型']])
        excluded = decision_table.calculate({**plan, 'known': {'區位': '北區', '吊具': '重型'}})
        self.assertEqual((excluded['status'], excluded['possible_results']), ('no_match', []))
        unknown = decision_table.calculate({**plan, 'rows': [], 'known': {'區位': '南區'}})
        self.assertEqual(unknown['coverage']['missing_rows'], [['南區', '輕型'], ['南區', '重型']])
        self.assertEqual(unknown['recorded_results'], [])

    def test_declared_rows_cannot_overlap_exclusions_or_expand_domains(self):
        plan = {'conditions': ['現場'], 'domains': {'現場': ['室內', '室外']},
                'excluded': [['室外']], 'rows': [], 'known': {}}
        for value in ('室外', '另一處'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '宣告選項內'):
                decision_table.calculate({**plan, 'rows': [{'when': [value], 'result': '存放'}]})
        with self.assertRaisesRegex(ValueError, '不得重複'):
            decision_table.calculate({**plan, 'excluded': [['室外'], ['室外']]})
        big = {'conditions': ['甲', '乙', '丙'], 'known': {}, 'rows': [], 'excluded': [],
               'domains': {name: ['一', '二', '三', '四'] for name in ('甲', '乙', '丙')}}
        with self.assertRaisesRegex(ValueError, '全部組合最多32'):
            decision_table.calculate(big)

    def test_autonomous_recheck_and_restore_preserve_rows_and_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對分流')
            seed = gate_table()
            initial = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **seed),
                action('ask', question='實際入口是哪個？')))
            self.assertEqual(initial['status'], 'needs_input')
            self.assertEqual(initial['steps'][0]['observation']['question']['condition'], '入口')
            source = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '南門，南台通行證有。',
                              expected_fingerprint=source['fingerprint'])
            control = {**seed, 'known': {'入口': '南門', '南台通行證': '有'}}
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **control),
                action('finish', report='走南台 [補充#2]。')))
            self.assertEqual(final['status'], 'report_ready')
            self.assertEqual(final['steps'][2]['observation']['possible_results'], ['南台'])
            self.assertEqual(final['steps'][2]['action']['args']['rows'], seed['rows'])
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            with restored.connect() as db:
                value = final['steps'][2]['observation'].copy()
                value['max_questions'] = False
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                           (json.dumps(value, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)
