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
