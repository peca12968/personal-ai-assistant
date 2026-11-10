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

    def test_priority_rules_keep_overrides_and_select_the_next_condition(self):
        plan = {'conditions': ['西門', '東門', '類別'], 'known': {}, 'excluded': [],
                'domains': {'西門': ['有', '無'], '東門': ['有', '無'], '類別': ['甲', '乙']},
                'rules': [{'priority': 0, 'when': {}, 'result': '待命'},
                          {'priority': 1, 'when': {'西門': '有'}, 'result': '西台'},
                          {'priority': 2, 'when': {'類別': '乙'}, 'result': '待命'},
                          {'priority': 3, 'when': {'類別': '乙', '東門': '有'}, 'result': '東台'}]}
        before = decision_table.calculate(plan)
        self.assertEqual((before['status'], before['question']['condition'], before['max_questions']),
                         ('needs_check', '類別', 2))
        self.assertEqual(before['priority_cases'][1], {
            'when': ['有', '有', '乙'], 'status': 'resolved', 'priority': 3,
            'winning_rules': [4], 'overridden_rules': [1, 2, 3], 'recorded_results': ['東台']})
        after = decision_table.calculate({**plan, 'known': {'類別': '乙'}})
        self.assertEqual((after['question']['condition'], after['max_questions']), ('東門', 1))
        final = decision_table.calculate({**plan, 'known': {'類別': '乙', '東門': '有'}})
        self.assertEqual((final['status'], final['possible_results']), ('resolved', ['東台']))
        self.assertNotIn('question', final)

    def test_priority_rules_separate_conflict_missing_and_explicit_exclusion(self):
        plan = {'conditions': ['分級'], 'domains': {'分級': ['低', '高', '待', '免']},
                'known': {}, 'excluded': [['免']],
                'rules': [{'priority': 0, 'when': {'分級': '低'}, 'result': '前台'},
                          {'priority': 1, 'when': {'分級': '高'}, 'result': '甲'},
                          {'priority': 1, 'when': {'分級': '高'}, 'result': '乙'}]}
        report = decision_table.calculate(plan)
        self.assertEqual(report['status'], 'incomplete_table')
        self.assertEqual(report['recorded_results'], ['前台', '甲', '乙'])
        self.assertEqual(report['coverage'], {
            'missing_rows': [['待']], 'conflicting_rows': [{'when': ['高'], 'rules': [2, 3], 'results': ['甲', '乙']}],
            'excluded_rows': [['免']]})
        self.assertEqual(report['question']['branches'], [
            {'value': '低', 'max_questions': 0, 'possible_results': ['前台'], 'needs_evidence': False},
            {'value': '高', 'max_questions': 0, 'possible_results': [], 'needs_evidence': True},
            {'value': '待', 'max_questions': 0, 'possible_results': [], 'needs_evidence': True}])
        high = decision_table.calculate({**plan, 'known': {'分級': '高'}})
        self.assertTrue(high['needs_evidence'])
        self.assertNotIn('question', high)
        excluded = decision_table.calculate({**plan, 'known': {'分級': '免'}})
        self.assertEqual((excluded['status'], excluded['possible_results']), ('no_match', []))

    def test_priority_rules_validate_bounds_and_saved_winners(self):
        default = {'priority': 0, 'when': {}, 'result': '前台'}
        plan = {'conditions': ['級別'], 'domains': {'級別': ['高', '低']},
                'known': {'級別': '高'}, 'excluded': [], 'rules': [default,
                    {'priority': 1, 'when': {'級別': '高'}, 'result': '側架'}]}
        for rule in ({**default, 'priority': True}, {**default, 'when': {'級別': '外'}},
                     {**default, 'when': {'版號': '甲'}}, {**default, 'priority': 101}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                decision_table.calculate({**plan, 'rules': [rule]})
        with self.assertRaises(ValueError):
            decision_table.calculate({**plan, 'rules': [default] * 17})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對分級處置')
            state = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **plan), action('finish', report='送側架。')))
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), state)
            observation = state['steps'][0]['observation']
            self.assertEqual(observation['priority_cases'][0]['winning_rules'], [2])
            observation['priority_cases'][0]['winning_rules'] = [1]
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=1',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_partial_precedence_transmits_only_undisputed_links(self):
        plan = {'conditions': ['標籤', '門證'], 'domains': {'標籤': ['有', '無'], '門證': ['有', '無']},
                'known': {'標籤': '有', '門證': '無'}, 'excluded': [],
                'rules': [{'when': {'標籤': '有'}, 'result': '藍席'},
                          {'when': {'門證': '有'}, 'result': '銀席'},
                          {'when': {}, 'result': '灰席'}],
                'precedence': [[1, 2], [2, 3]]}
        resolved = decision_table.calculate(plan)
        self.assertEqual((resolved['status'], resolved['possible_results']), ('resolved', ['藍席']))
        self.assertEqual(resolved['priority_cases'][0]['overridden_rules'], [3])
        disputed = decision_table.calculate({**plan, 'precedence': [[1, 2], [2, 3], [2, 1]]})
        self.assertEqual(disputed['disputed_relations'], [[1, 2], [2, 1]])
        self.assertEqual(disputed['status'], 'incomplete_table')
        case = disputed['priority_cases'][0]
        self.assertEqual((case['winning_rules'], case['unranked_pairs'], case['cyclic_pairs']),
                         ([1, 3], [[1, 3]], []))
        self.assertNotIn('question', disputed)

    def test_partial_precedence_cycle_only_affects_competing_assignments(self):
        plan = {'conditions': ['級別'], 'domains': {'級別': ['高', '低']}, 'known': {}, 'excluded': [],
                'rules': [{'when': {'級別': '高'}, 'result': '東廳'},
                          {'when': {'級別': '高'}, 'result': '西廳'},
                          {'when': {'級別': '低'}, 'result': '前台'}],
                'precedence': [[1, 2], [2, 1]]}
        report = decision_table.calculate(plan)
        self.assertEqual(report['priority_cases'][0]['cyclic_pairs'], [[1, 2]])
        self.assertEqual(report['priority_cases'][0]['unranked_pairs'], [])
        self.assertEqual(report['question']['branches'], [
            {'value': '高', 'max_questions': 0, 'possible_results': [], 'needs_evidence': True},
            {'value': '低', 'max_questions': 0, 'possible_results': ['前台'], 'needs_evidence': False}])
        low = decision_table.calculate({**plan, 'known': {'級別': '低'}})
        self.assertEqual((low['status'], low['possible_results']), ('resolved', ['前台']))
        self.assertEqual(low['disputed_relations'], [[1, 2], [2, 1]])
        same = {**plan, 'known': {'級別': '高'}, 'rules': [plan['rules'][0],
                {**plan['rules'][1], 'result': '東廳'}, plan['rules'][2]]}
        self.assertEqual(decision_table.calculate(same)['possible_results'], ['東廳'])

    def test_partial_precedence_validation_and_backup_recheck(self):
        plan = {'conditions': ['級別'], 'domains': {'級別': ['高']}, 'known': {}, 'excluded': [],
                'rules': [{'when': {}, 'result': '甲'}, {'when': {}, 'result': '乙'}],
                'precedence': [[1, 2], [2, 1]]}
        for edges in ([[1, 1]], [[True, 2]], [[1, 3]], [[1, 2], [1, 2]], [[1]], {'1': 2}):
            with self.subTest(edges=edges), self.assertRaises(ValueError):
                decision_table.calculate({**plan, 'precedence': edges})
        with self.assertRaises(ValueError):
            decision_table.calculate({**plan, 'rules': [{**rule, 'priority': 0} for rule in plan['rules']]})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對優先依據')
            state = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **plan), action('finish', report='甲、乙仍待核對。')))
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), state)
            observation = state['steps'][0]['observation']
            observation['disputed_relations'] = []
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=1',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

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
