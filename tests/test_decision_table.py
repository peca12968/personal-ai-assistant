import json
import tempfile
import unittest
from itertools import product
from pathlib import Path

from assistant import autonomy, decision_table, goal_guidance, goal_replies
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

    def test_scoped_precedence_reverses_by_shift_without_a_cycle(self):
        plan = {'conditions': ['班別', '標籤'], 'domains': {'班別': ['早', '晚'], '標籤': ['有', '無']},
                'known': {}, 'excluded': [],
                'rules': [{'when': {}, 'result': '東櫃'}, {'when': {}, 'result': '西櫃'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '早'}},
                               {'higher': 2, 'lower': 1, 'when': {'班別': '晚'}}]}
        result = decision_table.calculate(plan)
        self.assertEqual((result['status'], result['max_questions'], result['question']['condition']),
                         ('needs_check', 1, '班別'))
        self.assertEqual(result['disputed_relations'], [])
        self.assertEqual([r['active_precedence'] for r in result['priority_cases']], [[1], [1], [2], [2]])
        late = decision_table.calculate({**plan, 'known': {'班別': '晚'}})
        self.assertEqual((late['status'], late['possible_results']), ('resolved', ['西櫃']))
        self.assertNotIn('question', late)

    def test_scoped_precedence_separates_overlap_and_inactive_links(self):
        plan = {'conditions': ['班別', '區位'], 'domains': {'班別': ['早', '晚'], '區位': ['內', '外']},
                'known': {}, 'excluded': [],
                'rules': [{'when': {}, 'result': '暖棚'}, {'when': {}, 'result': '涼棚'},
                          {'when': {}, 'result': '前台'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '晚'}},
                               {'higher': 2, 'lower': 1, 'when': {'區位': '外'}},
                               [2, 3]]}
        result = decision_table.calculate(plan)
        self.assertEqual(result['disputed_relations'], plan['precedence'][:2])
        early_inside, early_outside, late_inside, late_outside = result['priority_cases']
        self.assertEqual(early_inside['unranked_pairs'], [[1, 2]])
        self.assertEqual(early_inside['active_precedence'], [3])
        self.assertEqual(early_outside['winning_rules'], [2])
        self.assertEqual(late_inside['winning_rules'], [1])
        self.assertEqual(late_inside['overridden_rules'], [2, 3])
        self.assertEqual(late_outside['cyclic_pairs'], [[1, 2]])
        self.assertEqual(late_outside['disputed_precedence'], [1, 2])
        self.assertEqual(late_outside['overridden_rules'], [3])
        outside = {**plan, 'known': {'班別': '早', '區位': '內'},
                   'rules': [plan['rules'][0], {'when': {'區位': '外'}, 'result': '涼棚'}, plan['rules'][2]]}
        case = decision_table.calculate(outside)['priority_cases'][0]
        self.assertEqual((case['winning_rules'], case['unranked_pairs']), ([1, 3], [[1, 3]]))

    def test_scoped_precedence_validates_conditions_and_saved_evidence(self):
        edge = {'higher': 1, 'lower': 2, 'when': {'版本': '二'}}
        plan = {'conditions': ['版本'], 'domains': {'版本': ['一', '二']}, 'known': {'版本': '二'},
                'excluded': [], 'rules': [{'when': {}, 'result': '甲'}, {'when': {}, 'result': '乙'}],
                'precedence': [edge]}
        for invalid in ([{**edge, 'when': {'版本': '三'}}], [{**edge, 'when': {'班別': '晚'}}],
                        [{**edge, 'lower': True}], [edge, edge], [[1, 2], {**edge, 'when': {}}]):
            with self.subTest(edges=invalid), self.assertRaises(ValueError):
                decision_table.calculate({**plan, 'precedence': invalid})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '按版本核對優先關係')
            state = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **plan), action('finish', report='第二版採甲。')))
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), state)
            observation = state['steps'][0]['observation']
            self.assertEqual(observation['priority_cases'][0]['active_precedence'], [1])
            observation['priority_cases'][0]['active_precedence'] = []
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=1',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_formal_decision_revision_chain_preserves_original_rules_and_sources(self):
        plan = {'conditions': ['班別'], 'domains': {'班別': ['早', '晚']}, 'known': {}, 'excluded': [],
                'rules': [{'when': {}, 'result': '甲桌'}, {'when': {}, 'result': '乙桌'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '早'}},
                               {'higher': 1, 'lower': 2, 'when': {'班別': '晚'}}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對班別與原規則')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='正式班別？')))
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '核對#1｜條件｜班別｜晚',
                              expected_fingerprint=waiting['fingerprint'])
            paused = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[補充#2]')), max_steps=1)
            self.assertEqual(paused['steps'][2]['observation']['revision']['change'],
                             {'kind': 'condition', 'field': '班別', 'before': None, 'after': '晚'})
            editable = goal_guidance.snapshot(store, sid, gid)
            goal_guidance.save(store, sid, gid, '核對#3｜優先｜2｜2｜1',
                               expected_fingerprint=editable['fingerprint'])
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[更正#1]'),
                action('finish', report='依班別補充 [補充#2] 與優先更正 [更正#1] 送乙桌。')))
            observation = final['steps'][3]['observation']
            self.assertEqual(observation['possible_results'], ['乙桌'])
            receipt = observation['revision']
            self.assertEqual((receipt['base_step'], receipt['citation']), (3, '[更正#1]'))
            self.assertEqual(receipt['change']['before'], plan['precedence'][1])
            self.assertEqual(receipt['change']['after'], {'higher': 2, 'lower': 1, 'when': {'班別': '晚'}})
            self.assertEqual(final['steps'][0]['action']['args'], plan)
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            receipt['source_sha256'] = '0' * 64
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_formal_decision_revision_rejects_stale_ambiguous_or_undeclared_edits(self):
        plan = {'conditions': ['班別'], 'domains': {'班別': ['早', '晚']}, 'known': {},
                'excluded': [], 'rows': [{'when': ['早'], 'result': '甲'}, {'when': ['晚'], 'result': '乙'}]}
        step = {'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                'observation': decision_table.calculate(plan)}
        ask = {'sequence': 2, 'action': {'tool': 'ask'}, 'observation': {}}
        source = {'citation': '[補充#2]', 'ask_sequence': 2, 'sha256': 'b' * 64,
                  'answer': '核對#1｜條件｜班別｜晚'}
        args = {'revision': '[補充#2]'}
        for answer in ('核對#2｜條件｜班別｜晚', '核對#1｜條件｜班別｜中',
                       '核對#1｜條件｜區位｜晚', '引述：核對#1｜條件｜班別｜晚'):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                decision_table.observe(args, [step, ask], [{**source, 'answer': answer}])
        with self.assertRaises(ValueError):
            decision_table.observe(args, [step, ask], [source], [
                {'citation': '[更正#1]', 'steps_seen': 2, 'content': '後續更正', 'sha256': 'c' * 64}])
        with self.assertRaises(ValueError):
            decision_table.observe(args, [step, ask], [])
        with self.assertRaises(ValueError):
            decision_table.validate_tool({**args, 'known': {'班別': '早'}})

    def test_formal_precedence_revision_consolidates_identical_confirmed_direction(self):
        plan = {'conditions': ['班別'], 'domains': {'班別': ['早', '晚']}, 'known': {'班別': '晚'},
                'excluded': [], 'rules': [{'when': {}, 'result': '甲'}, {'when': {}, 'result': '乙'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '晚'}},
                               {'higher': 2, 'lower': 1, 'when': {'班別': '晚'}}]}
        steps = [{'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                  'observation': decision_table.calculate(plan)}]
        source = {'citation': '[更正#1]', 'steps_seen': 1, 'sha256': 'a' * 64,
                  'content': '核對#1｜優先｜1｜2｜1'}
        revised = decision_table.observe({'revision': '[更正#1]'}, steps, guidance=[source])
        self.assertEqual(revised['possible_results'], ['乙'])
        self.assertEqual(revised['revision']['change']['retained_relation'], 1)
        self.assertEqual(revised['priority_cases'][0]['active_precedence'], [1])
        self.assertEqual(plan['precedence'][0], {'higher': 1, 'lower': 2, 'when': {'班別': '晚'}})

    def test_revision_comparison_keeps_current_result_separate_from_other_scopes(self):
        plan = {'conditions': ['班別', '區位', '封箱'],
                'domains': {'班別': ['早', '晚'], '區位': ['內', '外'], '封箱': ['有', '無']},
                'known': {'班別': '晚', '區位': '外', '封箱': '有'},
                'excluded': [['早', '外', '有']],
                'rules': [{'when': {}, 'result': '上台'}, {'when': {}, 'result': '下台'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '早'}},
                               {'higher': 1, 'lower': 2, 'when': {'班別': '晚', '區位': '外'}}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對前後及其他範圍')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='本次正式優先關係？')))
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '核對#1｜優先｜2｜2｜1',
                              expected_fingerprint=waiting['fingerprint'])
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[補充#2]', compare=True),
                action('finish', report='本次改為下台 [補充#2]。')))
            observation = result['steps'][2]['observation']
            comparison = observation['revision']['comparison']
            self.assertEqual(observation['possible_results'], ['下台'])
            self.assertEqual(comparison['before'], {'status': 'resolved', 'possible_results': ['上台']})
            self.assertEqual(comparison['after'], {'status': 'resolved', 'possible_results': ['下台']})
            self.assertEqual(comparison['retained_known'], plan['known'])
            early_inside, early_outside, late_inside, late_outside = comparison['scope_comparison']
            self.assertEqual(early_inside['before'], early_inside['after'])
            self.assertEqual(early_inside['after']['possible_results'], ['上台'])
            self.assertEqual(early_outside['after']['status'], 'no_match')
            self.assertEqual(late_inside['after'], {'status': 'incomplete_table', 'recorded_results': ['上台', '下台']})
            self.assertEqual([p['within_revision_scope'] for p in comparison['scope_comparison']],
                             [False, False, False, True])
            self.assertEqual(late_outside['after'], comparison['after'])
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            early_inside['after']['possible_results'] = ['下台']
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_revision_comparison_preserves_prior_uncertainty_and_optional_shape(self):
        plan = {'conditions': ['班別', '門禁'], 'domains': {'班別': ['早', '晚'], '門禁': ['有']},
                'known': {'門禁': '有'}, 'excluded': [],
                'rows': [{'when': ['早', '有'], 'result': '甲'}, {'when': ['晚', '有'], 'result': '乙'}]}
        step = {'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                'observation': decision_table.calculate(plan)}
        source = {'citation': '[更正#1]', 'steps_seen': 1, 'sha256': 'a' * 64,
                  'content': '核對#1｜條件｜班別｜晚'}
        args = {'revision': '[更正#1]'}
        old_shape = decision_table.observe(args, [step], guidance=[source])
        self.assertNotIn('comparison', old_shape['revision'])
        self.assertEqual(old_shape, decision_table.observe({**args, 'compare': False}, [step], guidance=[source]))
        revised = decision_table.observe({**args, 'compare': True}, [step], guidance=[source])
        self.assertEqual(revised['revision']['comparison'], {
            'before': {'status': 'needs_check', 'possible_results': ['甲', '乙']},
            'after': {'status': 'resolved', 'possible_results': ['乙']}, 'retained_known': {'門禁': '有'}})
        with self.assertRaises(ValueError):
            decision_table.validate_tool({**args, 'compare': 1})

    def test_batch_revision_keeps_original_indexes_through_merging_and_restore(self):
        plan = {'conditions': ['班別'], 'domains': {'班別': ['晚']}, 'known': {'班別': '晚'},
                'excluded': [], 'rules': [{'when': {}, 'result': value} for value in ('甲', '乙', '丙')],
                'precedence': [[1, 2], [2, 1], [3, 2]]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '整批核對')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='請提供整批正式優先關係。')))
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '核對#1｜優先｜1｜2｜1\r\n核對#1｜優先｜3｜2｜3',
                              expected_fingerprint=waiting['fingerprint'])
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[補充#2]'), action('finish', report='交乙 [補充#2]。')))
            observation = result['steps'][2]['observation']
            self.assertEqual(observation['possible_results'], ['乙'])
            changes = observation['revision']['changes']
            self.assertEqual(changes, [
                {'kind': 'precedence', 'relation': 1, 'before': [1, 2], 'after': [2, 1], 'retained_relation': 1},
                {'kind': 'precedence', 'relation': 3, 'before': [3, 2], 'after': [2, 3]}])
            self.assertEqual(result['steps'][0]['action']['args'], plan)
            backup = create_backup(store.path)
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            changes[1]['relation'] = 2
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_batch_revision_invalid_item_or_combination_preserves_original_plan(self):
        plan = {'conditions': ['區位', '門證'], 'domains': {'區位': ['北', '南'], '門證': ['有', '無']},
                'known': {'區位': '北', '門證': '無'}, 'excluded': [['南', '有']],
                'rules': [{'priority': 0, 'when': {}, 'result': '前台'},
                          {'priority': 1, 'when': {'區位': '南'}, 'result': '南廳'}]}
        saved = json.dumps(plan, ensure_ascii=False)
        step = {'sequence': 1, 'action': {'tool': 'check_decision', 'args': plan},
                'observation': decision_table.calculate(plan)}
        prefix = '核對#1｜條件｜區位｜南\n'
        for text, reason in (
                (prefix + '核對#1｜條件｜門證｜臨時', '第 2 行'),
                (prefix + '核對#1｜條件｜區位｜北', '第 2 行'),
                (prefix + '核對#2｜條件｜門證｜有', '第 2 行'),
                (prefix + '核對#1｜條件｜門證｜有', '可行情境不相容'),
                (prefix * 5 + '核對#1｜條件｜門證｜無', '一至四行')):
            source = {'citation': '[更正#1]', 'steps_seen': 1, 'sha256': 'a' * 64, 'content': text}
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, reason):
                decision_table.observe({'revision': '[更正#1]'}, [step], guidance=[source])
            self.assertEqual(json.dumps(plan, ensure_ascii=False), saved)
            self.assertEqual(step['observation']['possible_results'], ['前台'])

    def test_batch_comparison_keeps_other_conditions_per_version_of_plan(self):
        plan = {'conditions': ['班別', '門證'], 'domains': {'班別': ['早', '晚'], '門證': ['有', '無']},
                'known': {'班別': '晚', '門證': '無'}, 'excluded': [],
                'rules': [{'when': {}, 'result': '甲'}, {'when': {'門證': '有'}, 'result': '乙'}],
                'precedence': [{'higher': 1, 'lower': 2, 'when': {'班別': '晚'}}]}
        step = {'sequence': 1, 'action': {'tool': 'check_decision', 'args': plan},
                'observation': decision_table.calculate(plan)}
        source = {'citation': '[更正#1]', 'steps_seen': 1, 'sha256': 'a' * 64,
                  'content': '核對#1｜條件｜門證｜有\n核對#1｜優先｜1｜2｜1'}
        result = decision_table.observe({'revision': '[更正#1]', 'compare': True}, [step], guidance=[source])
        comparison = result['revision']['comparison']
        self.assertEqual(comparison['retained_known'], {'班別': '晚'})
        self.assertEqual(comparison['before']['possible_results'], ['甲'])
        self.assertEqual(comparison['after']['possible_results'], ['乙'])
        early, late = comparison['scope_comparison']
        self.assertFalse(early['within_revision_scope'])
        self.assertEqual(early['before']['possible_results'], ['甲'])
        self.assertEqual(early['after'], {'status': 'incomplete_table', 'recorded_results': ['甲', '乙']})
        self.assertEqual(late['after'], comparison['after'])

    def test_replace_two_rejected_batches_from_the_original_and_restore(self):
        plan = {'conditions': ['區位', '門證'], 'domains': {'區位': ['北', '南'], '門證': ['有', '無']},
                'known': {'區位': '北', '門證': '無'}, 'excluded': [],
                'rules': [{'priority': 0, 'when': {}, 'result': '前台'},
                          {'priority': 1, 'when': {'區位': '南'}, 'result': '南席'}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '依正式補正核對')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='請提供首批補充。')))
            for citation, reply in (
                    ('[補充#2]', '核對#1｜條件｜區位｜南\n核對#1｜條件｜門證｜臨時'),
                    ('[補充#4]', '取代｜[補充#2]\r\n核對#1｜條件｜門證｜未領')):
                waiting = goal_replies.snapshot(store, sid, gid)
                goal_replies.save(store, sid, gid, reply, expected_fingerprint=waiting['fingerprint'])
                paused = autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', revision=citation),
                    action('ask', question='請提供完整補正。')))
                self.assertEqual(paused['steps'][-2]['action']['tool'], 'invalid')
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '取代｜[補充#4]\n核對#1｜條件｜門證｜有',
                              expected_fingerprint=waiting['fingerprint'])
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[補充#6]', compare=True),
                action('finish', report='北區前台，門證有。')))
            checked = final['steps'][6]['observation']
            receipt = checked['revision']
            self.assertEqual(receipt['base_step'], 1)
            self.assertEqual(receipt['comparison']['retained_known'], {'區位': '北'})
            self.assertEqual(checked['possible_results'], ['前台'])
            self.assertEqual(receipt['replaces'], {'step': 5, 'citation': '[補充#4]',
                'source_sha256': final['reply_sources'][1]['sha256'],
                'error': final['steps'][4]['observation']['error']})
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            changed = {**final['steps'][2]['observation'], 'error': '替換原退回原因'}
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                           (json.dumps(changed, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '取代首行'):
                validate_database(restored.path)

    def test_replacement_requires_right_rejection_time_and_scope(self):
        plan = {'conditions': ['區位', '門證'], 'domains': {'區位': ['北', '南'], '門證': ['有', '無']},
                'known': {'區位': '北', '門證': '有'}, 'excluded': [['南', '有']],
                'rules': [{'priority': 0, 'when': {}, 'result': '前台'}]}
        base = {'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                'observation': decision_table.calculate(plan)}
        ask = {'sequence': 2, 'action': {'tool': 'ask'}, 'observation': {}}
        first = {'citation': '[補充#2]', 'ask_sequence': 2, 'sha256': 'a' * 64,
                 'answer': '核對#1｜條件｜區位｜南\n核對#1｜條件｜門證｜臨時'}
        failed_action = json.loads(action('check_decision', revision=first['citation']))
        with self.assertRaisesRegex(ValueError, '第 2 行') as error:
            decision_table.observe(failed_action['args'], [base, ask], replies=[first])
        failure = {'sequence': 3, 'action': {'tool': 'invalid'}, 'observation': {
            'error': str(error.exception), 'error_phase': 'execution', 'failed_action': failed_action}}
        replacement = {'citation': '[更正#1]', 'steps_seen': 3, 'sha256': 'b' * 64,
                       'content': '取代｜[補充#2]\n核對#1｜條件｜門證｜無'}
        args = {'revision': replacement['citation']}
        steps = [base, ask, failure]
        valid = decision_table.observe(args, steps, replies=[first], guidance=[replacement])
        self.assertEqual(valid['revision']['replaces']['step'], 3)
        for guidance in ([{**replacement, 'steps_seen': 2}],
                         [{**replacement, 'content': '取代｜[補充#1]\n核對#1｜條件｜門證｜無'}],
                         [replacement, {**replacement, 'citation': '[更正#2]'}]):
            with self.subTest(guidance=guidance), self.assertRaisesRegex(ValueError, '取代首行'):
                decision_table.observe(args, steps, replies=[first], guidance=guidance)
        with self.assertRaisesRegex(ValueError, '可行情境不相容'):
            decision_table.observe(args, steps, replies=[first], guidance=[
                {**replacement, 'content': '取代｜[補充#2]\n核對#1｜條件｜區位｜南'}])

    def test_natural_known_update_retracts_only_selected_value_and_requires_latest_source(self):
        plan = {'conditions': ['入口', '證件'], 'known': {'入口': '東', '證件': '有'},
                'rows': [{'when': [entrance, permit], 'result': '入場' if permit == '有' else '等待'}
                         for entrance, permit in product(('東', '西'), ('有', '無'))]}
        step = {'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                'observation': decision_table.calculate(plan)}
        ask = {'sequence': 2, 'action': {'tool': 'ask'}, 'observation': {}}
        source = {'citation': '[補充#2]', 'ask_sequence': 2, 'sha256': 'b' * 64,
                  'answer': '證件原值撤回。入口不動。'}
        args = {'known_update': {'證件': None}, 'source': source['citation']}
        result = decision_table.observe(args, [step, ask], [source])
        self.assertEqual(result['possible_results'], ['入場', '等待'])
        self.assertEqual(result['question']['condition'], '證件')
        self.assertEqual(result['matching_rows'], [1, 2])
        self.assertEqual(result['known_update']['changes'], [{'field': '證件', 'before': '有', 'after': None}])
        self.assertEqual(plan['known'], {'入口': '東', '證件': '有'})
        with self.assertRaisesRegex(ValueError, '最新且可辨認'):
            decision_table.observe(args, [step, ask], [source], [
                {'citation': '[更正#1]', 'steps_seen': 2, 'sha256': 'c' * 64, 'content': '最新補充。'}])
        with self.assertRaisesRegex(ValueError, '正式格式'):
            decision_table.observe(args, [step, ask], [{**source, 'answer': '核對#1｜條件｜證件｜無'}])
        for change in ({'證件': '未發'}, {'證': None}, {'入口': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decision_table.observe({**args, 'known_update': change}, [step, ask], [source])

    def test_natural_what_if_keeps_actual_base_for_later_formal_revision_and_restore(self):
        plan = {'conditions': ['班別', '門證'], 'domains': {'班別': ['早', '晚'], '門證': ['有', '無']},
                'known': {'班別': '早', '門證': '有'}, 'excluded': [['晚', '無']],
                'rules': [{'priority': 0, 'when': {}, 'result': '前台'},
                          {'priority': 1, 'when': {'班別': '晚'}, 'result': '南席'}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '核對實際及假設安排')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='請提供試算條件。')))
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '試算晚班，原安排維持早班。',
                              expected_fingerprint=waiting['fingerprint'])
            initial = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', known_update={'班別': '晚'}, source='[補充#2]', what_if=True),
                action('ask', question='請提供正式條件。')))
            trial = initial['steps'][2]['observation']
            self.assertEqual(trial['possible_results'], ['前台'])
            self.assertEqual(trial['what_if_result']['possible_results'], ['南席'])
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '核對#1｜條件｜門證｜無',
                              expected_fingerprint=waiting['fingerprint'])
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', revision='[補充#4]'), action('finish', report='本次前台。')))
            checked = final['steps'][4]['observation']
            self.assertEqual(checked['possible_results'], ['前台'])
            self.assertEqual(checked['revision']['base_step'], 1)
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            changed = {**trial, 'what_if_result': {'possible_results': ['假結論']}}
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                           (json.dumps(changed, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_mixed_natural_update_runs_trial_from_updated_actual_and_replays_only_actual(self):
        plan = {'conditions': ['入口', '門證'], 'known': {'入口': '東', '門證': '有'},
                'rows': [{'when': [entrance, permit],
                          'result': '前台' if entrance == '東' else '放行' if permit == '有' else '等待'}
                         for entrance, permit in product(('東', '西'), ('有', '無'))]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '分開核對目前安排及試算')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='正式更新和試算條件？')))
            waiting = goal_replies.snapshot(store, sid, gid)
            text = '入口正式改西。撤回門證有的觀察。假設門證無，只做試算。'
            goal_replies.save(store, sid, gid, text, expected_fingerprint=waiting['fingerprint'])

            def evidence(quote):
                return [{'citation': '[補充#2]', 'quote': quote}]

            official = {'入口': evidence('入口正式改西'), '門證': evidence('撤回門證有的觀察')}
            trial = {'updates': {'門證': '無'}, 'evidence': {'門證': evidence('假設門證無')}}
            args = {'known_update': {'入口': '西', '門證': None}, 'source': '[補充#2]',
                    'evidence': official, 'scenario': trial}
            first = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **{**args, 'evidence': {**official, '入口': evidence(text)}}),
                action('check_decision', **{**args, 'scenario': {**trial, 'evidence': {'門證': evidence('門證無')}}}),
                action('check_decision', **args), action('ask', question='門證正式確認了嗎？')))
            self.assertIn('互不重疊', first['steps'][2]['observation']['error'])
            self.assertIn('假設或試算標記', first['steps'][3]['observation']['error'])
            mixed = first['steps'][4]['observation']
            self.assertEqual(mixed['known_update']['base_step'], 1)
            self.assertEqual(mixed['possible_results'], ['放行', '等待'])
            self.assertEqual(mixed['what_if_result']['possible_results'], ['等待'])
            self.assertEqual(mixed['what_if_update']['changes'], [{'field': '門證', 'before': None, 'after': '無'}])
            bound = mixed['what_if_update']['evidence']['門證'][0]
            self.assertEqual(text[bound['start']:bound['end']], '假設門證無')
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '門證正式確認有。', expected_fingerprint=waiting['fingerprint'])
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', known_update={'門證': '有'}, source='[補充#6]'),
                action('finish', report='西入口放行。')))
            checked = final['steps'][6]['observation']
            self.assertEqual(checked['possible_results'], ['放行'])
            self.assertEqual(checked['known_update']['base_step'], 5)
            self.assertEqual(checked['known_update']['changes'][0]['before'], None)
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            bound['start'] += 1
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                           (json.dumps(mixed, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_current_alternatives_constrain_questions_and_replay_without_changing_rules(self):
        plan = {'conditions': ['日期', '件數'], 'known': {'日期': '週三', '件數': '6'},
                'domains': {'日期': ['週三', '週五'], '件數': ['6', '10']}, 'excluded': [['週五', '10']],
                'rows': [{'when': ['週三', '6'], 'result': '原台'}, {'when': ['週三', '10'], 'result': '甲台'},
                         {'when': ['週五', '6'], 'result': '乙台'}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '只核對本輪互斥配對')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='本輪更新？')))

            def reply(text):
                state = goal_replies.snapshot(store, sid, gid)
                goal_replies.save(store, sid, gid, text, expected_fingerprint=state['fingerprint'])

            quote = '本輪只有週三10件或週五6件'
            reply('原日期與件數撤回。' + quote + '。')
            cases = [{'日期': '週三', '件數': '10'}, {'日期': '週五', '件數': '6'}]
            update = {'known_update': {'日期': None, '件數': None}, 'source': '[補充#2]'}
            first = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', **update, alternatives={'cases': [cases[0], {'日期': '週五', '件數': '10'}], 'quote': quote}),
                action('check_decision', **update, alternatives={'cases': cases, 'quote': quote}),
                action('ask', question='確認日期？')))
            self.assertIn('不相容', first['steps'][2]['observation']['error'])
            checked = first['steps'][3]['observation']
            self.assertEqual(checked['matching_rows'], [2, 3])
            self.assertEqual(checked['possible_results'], ['甲台', '乙台'])
            self.assertEqual(checked['coverage']['excluded_rows'], [])
            self.assertEqual(checked['question']['condition'], '日期')
            self.assertEqual(checked['max_questions'], 1)
            self.assertEqual(checked['current_alternatives']['remaining_cases'], cases)
            reply('正式確認日期為週五。')
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', known_update={'日期': '週五'}, source='[補充#5]'),
                action('finish', report='週五，6件，乙台。')))
            decision = final['steps'][5]['observation']
            self.assertEqual(decision['possible_results'], ['乙台'])
            self.assertEqual(decision['current_alternatives']['remaining_cases'], [cases[1]])
            self.assertEqual(decision['current_alternatives']['implied_known'], {'件數': '6'})
            previous, _, _ = decision_table._recheck_saved(final['steps'], final['reply_sources'], ())
            self.assertEqual(previous[1]['excluded'], plan['excluded'])
            self.assertEqual(previous[1]['rows'], plan['rows'])
            self.assertEqual(previous[1]['known'], {'日期': '週五'})
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            decision['current_alternatives']['implied_known']['件數'] = '10'
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                           (json.dumps(decision, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

        priority = {key: value for key, value in plan.items() if key not in {'rows', 'known'}}
        priority.update(known={}, rules=[{'priority': 0, 'when': {}, 'result': '共用台'}])
        common = decision_table.calculate(priority, alternatives={'cases': cases}, require_all=True)
        self.assertEqual(common['possible_results'], ['共用台'])
        self.assertEqual(common['max_questions'], 0)
        self.assertEqual(common['current_alternatives']['remaining_cases'], cases)

    def test_natural_update_binds_two_saved_excerpts_and_rechecks_restored_offsets(self):
        plan = {'conditions': ['入口'], 'domains': {'入口': ['開', '關']}, 'known': {'入口': '關'},
                'excluded': [], 'rows': [{'when': ['開'], 'result': '內廊'}, {'when': ['關'], 'result': '等待'}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '只核對檜園')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('check_decision', **plan),
                action('ask', question='有最新狀態嗎？')))
            state = goal_replies.snapshot(store, sid, gid)
            first = '📍有一個園入口開了，還不知道是哪個。'
            goal_replies.save(store, sid, gid, first, expected_fingerprint=state['fingerprint'])
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('ask', question='是哪個園？')))
            state = goal_replies.snapshot(store, sid, gid)
            last = '變動的是本次的檜園。'
            goal_replies.save(store, sid, gid, last, expected_fingerprint=state['fingerprint'])
            excerpts = [{'citation': '[補充#2]', 'quote': '有一個園入口開了'},
                        {'citation': '[補充#3]', 'quote': '變動的是本次的檜園'}]
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_decision', known_update={'入口': '開'}, source='[補充#3]', evidence={'入口': excerpts}),
                action('finish', report='檜園走內廊。')))
            observation = result['steps'][3]['observation']
            self.assertEqual(observation['possible_results'], ['內廊'])
            bound = observation['known_update']['evidence']['入口']
            for receipt, excerpt, saved, original in zip(bound, excerpts, result['reply_sources'], (first, last)):
                self.assertEqual(receipt['source_sha256'], saved['sha256'])
                self.assertEqual(original[receipt['start']:receipt['end']], excerpt['quote'])
            self.assertEqual(bound[0]['start'], 1)
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            bound[0]['start'] = 0
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                           (json.dumps(observation, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '決策表結果'):
                validate_database(restored.path)

    def test_natural_evidence_requires_unique_literal_current_ordered_quotes(self):
        plan = {'conditions': ['入口'], 'known': {'入口': '關'},
                'rows': [{'when': ['開'], 'result': '內廊'}, {'when': ['關'], 'result': '等待'}]}
        steps = [{'sequence': 1, 'action': json.loads(action('check_decision', **plan)),
                  'observation': decision_table.calculate(plan)},
                 {'sequence': 2, 'action': {'tool': 'ask'}, 'observation': {}}]
        older = {'citation': '[補充#1]', 'ask_sequence': 1, 'sha256': 'a' * 64, 'answer': '入口開了'}
        latest = {'citation': '[補充#2]', 'ask_sequence': 2, 'sha256': 'b' * 64,
                  'answer': '入口開了，入口開了。'}
        args = {'known_update': {'入口': '開'}, 'source': latest['citation']}
        legacy = decision_table.observe(args, steps, [older, latest])
        self.assertNotIn('evidence', legacy['known_update'])
        for excerpts, reason in (
                ([{'citation': '[補充#2]', 'quote': '入口開了'}], '唯一定位'),
                ([{'citation': '[補充#2]', 'quote': '入口已開'}], '唯一定位'),
                ([{'citation': '[補充#1]', 'quote': '入口開了'}], '最新補充作結'),
                ([{'citation': '[補充#1]', 'quote': '入口開了'},
                  {'citation': '[補充#2]', 'quote': latest['answer']}], '原核對後'),
                ([{'citation': '[補充#2]', 'quote': latest['answer']}] * 2, '依序引用')):
            with self.subTest(excerpts=excerpts), self.assertRaisesRegex(ValueError, reason):
                decision_table.observe({**args, 'evidence': {'入口': excerpts}}, steps, [older, latest])
        result = decision_table.observe({**args, 'evidence': {
            '入口': [{'citation': '[補充#2]', 'quote': latest['answer']}]}}, steps, [older, latest])
        self.assertEqual(result['possible_results'], legacy['possible_results'])

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
