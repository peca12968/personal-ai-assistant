import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from assistant import artifacts, autonomy, goal_calculation, goal_guidance, goal_partial_recheck, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalCalculationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state' / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.doc = self.attach('批次量：17 袋。\n每袋：3 件。\n退回量：21 袋。\n窗口：北門。')

    def attach(self, content, name='清單.txt'):
        path = self.root / name
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(self.sid, path)

    def create(self, objective='做交接表，附總件數與窗口。', *, files=False, checked=True):
        if checked:
            objective = goal_calculation.DIRECTIVE + '\n' + objective
        self.gid = autonomy.create_goal(self.store, self.sid, objective, file_delivery=files)

    def run_actions(self, *actions):
        engine = ScriptedEngine(*actions)
        return autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=len(actions))

    def initial(self):
        return action('read', document_id=self.doc, start_line=1, end_line=4)

    def cite(self, line):
        return f'[D{self.doc}:L{line}]'

    def formula(self, *, quantity='17', citation=None, quote='批次量：17 袋', field='批次量', unit='袋'):
        return action('calculate_expression', expression={'operation': 'product', 'values': [quantity, '3']},
                      operands=[
                          {'path': '/values/0', 'citation': citation or self.cite(1),
                           'quote': quote, 'field': field, 'unit': unit},
                          {'path': '/values/1', 'citation': self.cite(2),
                           'quote': '每袋：3 件', 'field': '每袋', 'unit': '件'},
                      ])

    def reply(self, answer):
        snap = goal_replies.snapshot(self.store, self.sid, self.gid)
        return goal_replies.save(self.store, self.sid, self.gid, answer, expected_fingerprint=snap['fingerprint'])

    def restore(self):
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        return Store(self.root / 'restored' / 'assistant.sqlite3')

    def test_corrected_quantity_binds_value_and_field_and_controls_delivery_bytes(self):
        self.create(files=True)
        self.run_actions(self.initial(), action('ask', question='批次量的正式複點是多少？'))
        self.reply('批次量正式改為 19 袋。')
        report = [{'label': '總件數', 'calculation_step': 7, 'unit': '件'},
                  {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        result = self.run_actions(
            self.formula(),
            self.formula(quantity='21', citation='[補充#2]', quote='批次量正式改為 21 袋'),
            self.formula(quantity='21', citation=self.cite(3), quote='退回量：21 袋'),
            self.formula(quantity='19', citation='[補充#2]', quote='批次量正式改為 19 袋', unit='箱'),
            self.formula(quantity='19', citation='[補充#2]', quote='批次量正式改為 19 袋'),
            action('write_artifact', filename='交接.json', calculation_report=report),
            action('deliver', filenames=['交接.json'], calculation_report=report))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual([row['action']['tool'] for row in result['steps'][2:6]], ['invalid'] * 4)
        self.assertIn('較新的', result['steps'][2]['observation']['error'])
        self.assertIn('唯一定位', result['steps'][3]['observation']['error'])
        proof = result['steps'][6]['observation'][goal_calculation.PROOF]
        operand = proof['operands'][0]
        self.assertEqual((operand['number'], operand['unit'], operand['citation']), ('19', '袋', '[補充#2]'))
        content = artifacts.directory(self.store, self.gid).joinpath('交接.json').read_bytes()
        saved = json.loads(content)
        self.assertEqual(saved[0]['精確結果'], '57')
        self.assertEqual([row['引用'] for row in saved[0]['來源']], ['[補充#2]', self.cite(2)])
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('交接.json').read_bytes(), content)
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['result'], result['result'])
        with restored.connect() as db:
            row = result['steps'][6]['observation']
            row[goal_calculation.PROOF]['operands'][0]['unit_start'] += 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=7',
                       (json.dumps(row, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_candidate_keeps_independent_fact_and_total_waiting(self):
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量的正式複點是多少？'))
        self.reply('批次量暫估 21 袋，批次量待確認。')
        partial = [{'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                   {'label': '批次量', 'pending': True}, {'label': '總件數', 'pending': True}]
        result = self.run_actions(
            self.formula(),
            self.formula(quantity='21', citation='[補充#2]', quote='批次量暫估 21 袋'),
            action('finish', report='總件數 63 件'),
            action('ask', question='批次量確認為多少袋？', calculation_report=partial),
        )
        self.assertEqual([row['action']['tool'] for row in result['steps'][2:5]], ['invalid'] * 3)
        self.assertTrue(result['partial_result'].endswith('批次量：待查\n總件數：待查'))
        self.assertEqual(result['partial_result'].splitlines()[0], f'窗口：北門 {self.cite(4)}')
        restored = self.restore()
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['partial_result'], result['partial_result'])

    def test_unrelated_change_reuses_ordinary_calculation_without_a_tool_call(self):
        self.create()
        original = [{'label': '總件數', 'calculation_step': 2, 'unit': '件'}]
        self.run_actions(self.initial(), self.formula(),
                         action('ask', question='窗口有無更正？', calculation_report=original))
        self.reply('窗口正式改為南門。')
        current = [*original, {'label': '窗口', 'citation': '[補充#3]', 'quote': '窗口正式改為南門'}]
        result = self.run_actions(action('finish', calculation_report=current))
        self.assertEqual(len(result['steps']), 4)
        self.assertIn('51 件', result['result'])
        self.assertIn('南門 [補充#3]', result['result'])
        validate_database(self.store.path)

    def test_ordinary_arithmetic_remains_available_and_conversion_can_be_checked(self):
        did = self.attach('單罐淨重：1.5 kg。\n瓶數：2 瓶。', '重量.txt')
        self.create()
        result = self.run_actions(
            action('read_ranges', ranges=[{'document_id': did, 'start_line': 1, 'end_line': 2}]),
            action('calculate_expression', expression={'operation': 'product', 'values': ['1500', '2']},
                   operands=[
                       {'path': '/values/0', 'citation': f'[D{did}:L1]', 'field': '單罐淨重',
                        'quote': '單罐淨重：1.5 kg', 'conversion': {'from': 'kg', 'to': 'g'}},
                       {'path': '/values/1', 'citation': f'[D{did}:L2]', 'field': '瓶數', 'quote': '瓶數：2 瓶', 'unit': '瓶'},
                   ]),
            action('finish', calculation_report=[{'label': '淨重', 'calculation_step': 2, 'unit': 'g'}]))
        self.assertIn('3000 g', result['result'])
        self.assertIn('1.5 kg', result['result'])
        validate_database(self.store.path)

        self.create(checked=False)
        result = self.run_actions(action('calculate_expression', expression='7'), action('finish', report='7'))
        self.assertEqual(result['result'], '7')

    def test_ordinary_calculation_preserves_current_group_and_zero_in_full_document_quote(self):
        did = self.attach('本次影像組：架具常備量 7 件。\n本次影像組：架具備援量為 0 件。\n'
                          '本次聲音組：架具備援量為 3 件。', '器材.txt')
        self.create(checked=False)
        quotes = ['本次影像組：架具常備量 7 件', '本次影像組：架具備援量為 0 件']
        goal = self.run_actions(
            action('read', document_id=did, start_line=1, end_line=3),
            action('calculate_expression', expression={'operation': 'sum', 'values': ['7', '0']}, operands=[
                {'path': f'/values/{index}', 'citation': f'[D{did}:L{index+1}]', 'quote': quote,
                 'field': label, 'unit': '件'}
                for index, (quote, label) in enumerate(zip(quotes, ('架具常備量', '架具備援量'), strict=True))
            ]),
            action('finish', report='本次影像組架具總量 7 件。'))
        self.assertEqual(goal['status'], 'report_ready')
        step = goal['steps'][1]
        observation = step['observation']
        self.assertEqual(observation['exact'], '7')
        proof = observation[goal_calculation.PROOF]
        self.assertEqual(proof['document_group_version'], 1)
        self.assertEqual([row['number'] for row in proof['operands']], ['7', '0'])
        for row, quote in zip(proof['operands'], quotes, strict=True):
            group = row['document_group']
            self.assertEqual(group['name'], '本次影像組')
            self.assertEqual(quote[group['start']:group['end']], group['name'])
            self.assertEqual(quote[row['field_start']:row['field_end']], row['field'])
            self.assertEqual(row['quote'], quote)
        restored = self.restore()
        validate_database(restored.path)
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['result'], goal['result'])
        del proof['document_group_version']
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaises(ValueError):
            validate_database(restored.path)

    def test_full_group_quote_does_not_adopt_a_clipped_unconfirmed_or_mismatched_source(self):
        valid = '本次影像組：架具備援量為 0 件'
        scenarios = [
            (valid, valid.replace('影像組', '聲音組'), '架具備援量', '件', False),
            (valid, valid, '備援量', '件', False),
            (valid, valid, '架具備援量', None, False),
            (valid + '，仍未確認', valid, '架具備援量', '件', False),
            ('備註只引述' + valid, valid, '架具備援量', '件', False),
            (valid.replace('本次', '前次'), valid.replace('本次', '前次'), '架具備援量', '件', False),
            (valid.replace('影像組', '待確認影像組'), valid.replace('影像組', '待確認影像組'), '架具備援量', '件', False),
            ('本次影像組：架具備援量尚未登記', valid, '架具備援量', '件', False),
            (valid, valid, '架具備援量', '件', True),
        ]
        for text, quote, label, unit, strict in scenarios:
            with self.subTest(text=text, quote=quote, label=label, unit=unit, strict=strict):
                did = self.attach(text + '。', '器材.txt')
                self.create(checked=strict)
                operand = {'path': '', 'citation': f'[D{did}:L1]', 'quote': quote, 'field': label}
                if unit:
                    operand['unit'] = unit
                result = self.run_actions(action('read', document_id=did, start_line=1, end_line=1),
                    action('calculate_expression', expression='0', operands=[operand]))
                self.assertEqual(result['steps'][-1]['action']['tool'], 'invalid')
                validate_database(self.store.path)

    def test_ordinary_zero_from_confirmed_absence_binds_document_and_declared_rule(self):
        line = '本批紙材組：備紙量確認無備紙。'
        rule = '原文「確認無備紙」表示 0 張'
        objective = f'交付本批紙材組總張數。{rule}。'
        did = self.attach(f'本批紙材組：常備量 12 張。\n{line}', '紙材.txt')
        self.create(objective, checked=False, files=True)
        formula = action('calculate_expression', expression={'operation': 'sum', 'values': ['12', '0']}, operands=[
            {'path': '/values/0', 'citation': f'[D{did}:L1]', 'quote': '本批紙材組：常備量 12 張',
             'field': '常備量', 'unit': '張'},
            {'path': '/values/1', 'citation': f'[D{did}:L2]', 'quote': line,
             'field': '備紙量', 'unit': '張'},
        ])
        body = f'本批紙材組合計 12 張。[D{did}:L1-L2]'
        goal = self.run_actions(
            action('read', document_id=did, start_line=1, end_line=2), formula,
            action('write_artifact', filename='紙材.md', content=body),
            action('deliver', filenames=['紙材.md'], report=body))
        self.assertEqual(goal['status'], 'report_ready')
        observation = goal['steps'][1]['observation']
        self.assertEqual(observation['exact'], '12')
        proof = observation[goal_calculation.PROOF]
        self.assertEqual(proof['explicit_zero_version'], 1)
        receipt = proof['operands'][1]
        zero = receipt['explicit_zero']
        self.assertEqual(line[zero['phrase_start']:zero['phrase_end']], '確認無備紙')
        self.assertEqual(line[receipt['field_start']:receipt['field_end']], '備紙量')
        self.assertEqual(receipt['document_group']['name'], '本批紙材組')
        self.assertEqual((zero['exact'], zero['rule']['unit'], zero['rule']['citation']), ('0', '張', '[目標]'))
        self.assertEqual(zero['rule']['quote'], rule)
        start = zero['rule']['quote_start']
        self.assertEqual(objective[start:start + len(rule)], rule)
        self.assertEqual(zero['rule']['source_sha256'], hashlib.sha256(objective.encode('utf-8')).hexdigest())
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('紙材.md').read_bytes(), body.encode('utf-8'))
        zero['rule']['quote_start'] -= 1
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_ordinary_zero_requires_one_applicable_rule_and_current_complete_confirmation(self):
        base_rule = '文字「已確認沒有備援」代表確定為零台。'
        line = '備援量已確認沒有備援'
        cases = [
            ('核對備援量。', line, line, '備援量', '台', False, None),
            (base_rule + '「已確認沒有備援」表示 2 台。', line, line, '備援量', '台', False, None),
            ('假如只是試算，' + base_rule, line, line, '備援量', '台', False, None),
            (base_rule, line, line, '備援量', '箱', False, None),
            (base_rule, line, line, '援量', '台', False, None),
            (base_rule, line + '，結果仍待核', line, '備援量', '台', False, None),
            (base_rule, '前批放映組：' + line, line, '備援量', '台', False, None),
            (base_rule, '備援量尚未登記', '備援量尚未登記', '備援量', '台', False, None),
            (base_rule.replace('備援」', '備援清單」'), line + '清單', line + '清單', '備援量', '台', False, None),
            (base_rule, line, line, '備援量', '台', True, None),
            (base_rule, line, line, '備援量', '台', False, '備援量正式改為 2 台。'),
            (base_rule, line, line, '備援量', '台', False, '更正，原文「已確認沒有備援」表示 2 台。'),
        ]
        for objective, text, quote, field, unit, strict, guidance in cases:
            with self.subTest(objective=objective, text=text, quote=quote, unit=unit, strict=strict, guidance=guidance):
                did = self.attach(text + '。', '備料.txt')
                self.create(objective, checked=strict)
                self.run_actions(action('read', document_id=did, start_line=1, end_line=1))
                if guidance:
                    current = goal_guidance.snapshot(self.store, self.sid, self.gid)
                    goal_guidance.save(self.store, self.sid, self.gid, guidance,
                                       expected_fingerprint=current['fingerprint'])
                result = self.run_actions(action('calculate_expression', expression='0', operands=[
                    {'path': '', 'citation': f'[D{did}:L1]', 'quote': quote, 'field': field, 'unit': unit},
                ]))
                self.assertEqual(result['steps'][-1]['action']['tool'], 'invalid')
                validate_database(self.store.path)

    def test_rechecked_partial_keeps_the_original_question_and_reuses_the_new_calculation(self):
        self.create()
        rows = [{'label': '總件數', 'calculation_step': 2, 'unit': '件'}]
        question = '本批點收員是誰？'
        self.run_actions(self.initial(), self.formula(), action('ask', question=question, calculation_report=rows))
        snap = goal_guidance.snapshot(self.store, self.sid, self.gid)
        changed = goal_guidance.save(self.store, self.sid, self.gid, '批次量正式改為 19 袋。',
                                     expected_fingerprint=snap['fingerprint'])
        formula_action = json.loads(self.formula(quantity='19', citation='[更正#1]', quote='批次量正式改為 19 袋'))
        del formula_action['note']
        finish = {'calculation_report': [{'label': '總件數', 'calculation_step': 4, 'unit': '件'}],
                  'withdrawal_source': None}
        waiting = goal_partial_recheck.run(
            self.store, self.sid, self.gid, ScriptedEngine(json.dumps(formula_action), json.dumps(finish)),
            expected_fingerprint=changed['fingerprint'])
        self.assertIn('57 件', waiting['goal']['partial_result'])
        self.assertEqual(waiting['goal']['question'], question)
        self.store = self.restore()
        self.reply('點收員：何青石。')
        delivered = self.run_actions(action('finish', calculation_report=[
            {'label': '總件數', 'recheck_calculation': 1, 'unit': '件'},
            {'label': '點收員', 'citation': '[補充#3]', 'quote': '點收員：何青石'},
        ]))
        self.assertIn('57 件', delivered['result'])
        self.assertEqual(len(delivered['steps']), 4)
        validate_database(self.store.path)

    def test_joint_or_ambiguous_values_cannot_become_a_confirmed_operand(self):
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量的正式複點是多少？'))
        self.reply('批次量：19 袋、21 袋。')
        result = self.run_actions(
            self.formula(quantity='19', citation='[補充#2]', quote='批次量：19 袋、21 袋'),
            action('ask', question='請確認唯一正式批次量。', calculation_report=[
                {'label': '總件數', 'pending': True},
            ]))
        self.assertEqual(result['steps'][2]['action']['tool'], 'invalid')
        self.assertIn('完整且已確認', result['steps'][2]['observation']['error'])
        self.assertEqual(result['partial_result'], '總件數：待查')

    def test_paged_named_ordinal_replays_both_pages_after_detach(self):
        content = '\n'.join(['摘要。', *(f'附記{i}：批次量另列。' for i in range(1, 8)),
                             '時段：晚班。', '批次量：22 袋。', '結束。'])
        revised = self.attach(content, '晚班盤點.txt')
        self.create(files=True)
        self.run_actions(self.initial(),
                         action('ask', question=f'批次量要採用「原值」{self.cite(1)} 還是晚班盤點？'))
        self.reply('第二份。')
        ref = f'[D{revised}:L10]'
        rows = [{'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        engine = ScriptedEngine(
            action('find_document_text', document_id=revised, query='批次量', start_line=1),
            self.formula(),
            action('find_document_text', document_id=revised, query='批次量', start_line=8),
            self.formula(quantity='22', citation=ref, quote='批次量：22 袋'),
            action('write_artifact', filename='盤點.json', calculation_report=rows),
            action('deliver', filenames=['盤點.json'], calculation_report=rows))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=6)
        payload = json.loads(engine.prompts[1].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(payload['calculation_source_adoptions'][0]['needs_lookup']['start_line'], 8)
        self.assertIn('採用尚待核對', result['steps'][3]['observation']['error'])
        proof, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        page_proof = proof['spoken_selection']['ordinal']['options'][1]['lookup_evidence']
        self.assertEqual([row['step_sequence'] for row in page_proof['pages']], [3, 5])
        self.assertEqual([row['start_line'] for row in page_proof['pages']], [1, 8])
        self.assertEqual(page_proof['total_matching_lines'], 8)
        self.assertIn('66 件', result['result'])
        original = artifacts.directory(self.store, self.gid).joinpath('盤點.json').read_bytes()
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('盤點.json').read_bytes(), original)
        with restored.connect() as db:
            page = result['steps'][4]['observation']
            page['matches'][0]['match_start_char'] += 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(page, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '欄位查找結果'):
            validate_database(restored.path)

    def test_paged_named_choice_requires_real_cursors_and_keeps_two_fields_competing(self):
        content = '\n'.join(['摘要。', '批次量：21 袋。', *(f'附記{i}：批次量待覆核。' for i in range(1, 7)),
                             '區位：木架。', '批次量：24 袋。', '結尾。'])
        revised = self.attach(content, '複點.txt')
        other = self.attach(content, '另一區.txt')
        self.create()
        self.run_actions(self.initial(), action('ask', question='本批批次量採用清單還是複點？'))
        self.reply('第二份。')
        engine = ScriptedEngine(
            action('find_document_text', sources=[
                {'document_id': revised, 'queries': [{'query': '批次量', 'start_line': 1}]},
                {'document_id': other, 'queries': [{'query': '批次量', 'start_line': 8}]}]),
            self.formula(),
            action('find_document_text', document_id=revised, queries=[{'query': '批次量', 'start_line': 8}]),
            self.formula(quantity='24', citation=f'[D{revised}:L10]', quote='批次量：24 袋'),
            action('ask', question=f'批次量要採用 [D{revised}:L2] 還是 [D{revised}:L10]？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=5)
        self.assertIn('採用尚待核對', result['steps'][3]['observation']['error'])
        state = json.loads(engine.prompts[3].rsplit('目標與已觀察資料：\n', 1)[1])
        candidate = state['calculation_source_adoptions'][0]['spoken_selection']['ordinal']['options'][1]
        self.assertEqual(candidate['status'], 'competing_lines')
        self.assertEqual(candidate['matching_lines'], [f'[D{revised}:L2]', f'[D{revised}:L10]'])
        self.assertIn('採用尚待核對', result['steps'][5]['observation']['error'])
        self.assertIn('窗口：北門', result['partial_result'])
        validate_database(self.store.path)

    def test_paged_named_lookup_requests_only_missing_full_original_line(self):
        late = self.attach('\n'.join(['摘要。', *(f'附記{i}：批次量另列。' for i in range(1, 7)),
                                     '附錄：' + '測' * 8300 + '批次量另列。',
                                     '區位：木架。', '批次量：22 袋。', '結尾。']), '複點.txt')
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量要採用哪一份文件？'))
        self.reply('就用複點那份。')
        engine = ScriptedEngine(
            action('find_document_text', document_id=late, query='批次量', start_line=1),
            action('find_document_text', document_id=late, query='批次量', start_line=8),
            action('read_ranges', ranges=[{'document_id': late, 'start_line': 8, 'end_line': 8}]),
            self.formula(quantity='22', citation=f'[D{late}:L10]', quote='批次量：22 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 6, 'unit': '件'}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=5)
        self.assertFalse(result['steps'][3]['observation']['matches'][0]['full_line_provided'])
        suggestion = json.loads(engine.prompts[2].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(suggestion['calculation_source_adoptions'][0]['needs_read'],
                         {'ranges': [{'document_id': late, 'start_line': 8, 'end_line': 8}]})
        adoption, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(len(adoption['spoken_selection']['lookup_evidence']['pages']), 2)
        self.assertIn('66 件', result['result'])
        validate_database(self.store.path)

    def segmented_source(self, ending, *, files=False):
        long_line = '紀錄曾提到批次量。' + '測' * 10190 + '。' + ending
        late = self.attach('\n'.join(['摘要。', *(f'附記{i}：批次量另列。' for i in range(1, 7)),
                                     long_line, '區位：木架。', '批次量：22 袋。', '結尾。']), '複點.txt')
        self.create(files=files)
        rank_question = f'批次量用「原單」{self.cite(1)} 還是複點？'
        self.run_actions(self.initial(), action('ask', question=rank_question))
        self.reply('第二份。')
        return late

    def test_character_pages_close_long_note_and_keep_legacy_report_citations(self):
        late = self.segmented_source('仍待交班。', files=True)
        late_ref = f'[D{late}:L10]'
        rows = [{'label': '總件數', 'calculation_step': 9, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        engine = ScriptedEngine(
            action('find_document_text', document_id=late, query='批次量', start_line=1),
            action('find_document_text', document_id=late, query='批次量', start_line=8),
            action('read_line', document_id=late, line_number=8, start_char=8001),
            action('read_line', document_id=late, line_number=8, start_char=1),
            self.formula(quantity='22', citation=late_ref, quote='批次量：22 袋'),
            action('read_line', document_id=late, line_number=8, start_char=4001),
            self.formula(quantity='22', citation=late_ref, quote='批次量：22 袋'),
            action('write_artifact', filename='核對.json', calculation_report=rows),
            action('deliver', filenames=['核對.json'], calculation_report=rows))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=9)
        for index, start in [(2, 1), (3, 1), (4, 4001), (5, 4001)]:
            payload = json.loads(engine.prompts[index].rsplit('目標與已觀察資料：\n', 1)[1])
            self.assertEqual(payload['calculation_source_adoptions'][0]['needs_line'],
                             {'document_id': late, 'line_number': 8, 'start_char': start})
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['steps'][6]['action']['tool'], 'invalid')
        proof, = result['steps'][8]['observation'][goal_calculation.PROOF]['line_page_proofs']
        self.assertEqual((proof['document_id'], proof['line_number']), (late, 8))
        self.assertEqual([row['sequence'] for row in proof['pages']], [6, 8, 5])
        self.assertIn('66 件', result['result'])
        self.assertIn('窗口：北門', result['result'])
        name = '核對.json'
        saved = artifacts.directory(self.store, self.gid).joinpath(name).read_bytes()
        self.store.detach_document(self.sid, late)
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(name).read_bytes(), saved)
        with restored.connect() as db:
            observation = result['steps'][4]['observation']
            body = observation['excerpts'][0]['text']
            observation['excerpts'][0]['text'] = body.replace('測', '改', 1)
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_formal_field_in_late_character_page_requires_choice_and_survives_backup(self):
        late = self.segmented_source('核簽點：金栗臺。批次量：29 袋。')
        first, second = f'[D{late}:L8]', f'[D{late}:L10]'
        result = self.run_actions(
            action('find_document_text', document_id=late, query='批次量', start_line=1),
            action('find_document_text', document_id=late, query='批次量', start_line=8),
            *(action('read_line', document_id=late, line_number=8, start_char=start)
              for start in (1, 4001, 8001)),
            action('ask', question=f'批次量要採用 {first} 還是 {second}？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        self.assertEqual(result['status'], 'needs_input')
        self.assertIn('總件數：待查', result['partial_result'])
        self.assertEqual([row['citation'] for row in result['steps'][-1]['observation']['question_citations']['documents']],
                         [first, second])
        self.reply('第一筆。')
        final = self.run_actions(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
                                 action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 9,
                                                                         'unit': '件'}]))
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('87 件', final['result'])
        self.assertIn(first, final['result'])
        self.assertEqual(autonomy.get_goal(self.restore(), self.sid, self.gid)['result'], final['result'])

    def prepare_short_quantity(self, alternative, *, displayed='29 袋', files=False):
        revised = self.attach(f'核簽點：金栗臺。批次量：29 袋。\n批次量：{alternative}。', '複點.txt')
        self.create(files=files)
        first, second = f'[D{revised}:L1]', f'[D{revised}:L2]'
        setup = self.run_actions(action('read_ranges', ranges=[
            {'document_id': self.doc, 'start_line': 1, 'end_line': 4},
            {'document_id': revised, 'start_line': 1, 'end_line': 2}]),
            action('ask', question=f'批次量要採用「{displayed}」{first} 還是「{alternative}」{second}？'))
        self.assertEqual(setup['status'], 'needs_input')
        return first, second

    def test_short_quantity_uses_confirmed_original_clause_and_saves_complete_option_evidence(self):
        first, second = self.prepare_short_quantity('22 袋', displayed='第 1 行（29 袋）', files=True)
        self.reply('就用 ２９．０ 袋。')
        rows = [{'label': '總件數', 'calculation_step': 3, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        filename = '新核對.json'
        result = self.run_actions(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
                                  action('write_artifact', filename=filename, calculation_report=rows),
                                  action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(result['status'], 'report_ready')
        self.assertIn('87 件', result['result'])
        self.assertIn('窗口：北門', result['result'])
        proof = result['steps'][2]['observation'][goal_calculation.PROOF]
        adopted, = proof['source_adoptions']
        choice = adopted['spoken_selection']['value_choice']
        self.assertEqual(adopted['user_quote'], '就用 ２９．０ 袋')
        self.assertEqual(choice['reply_quantity'], {'number': '２９．０', 'exact': '29', 'unit': '袋'})
        self.assertEqual(choice['matching_positions'], [1])
        self.assertEqual([row['citation'] for row in choice['options']], [first, second])
        self.assertEqual([row['quantity']['exact'] for row in choice['options']], ['29', '22'])
        self.assertEqual(adopted['candidates'][0]['citation'], first)
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            observation = result['steps'][2]['observation']
            choice['options'][1]['quantity']['exact'] = '29'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                       (json.dumps(observation, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_short_quantity_keeps_same_amount_wrong_unit_and_mismatched_display_pending(self):
        cases = [('29 袋', '29 袋', '29 袋。', 'choice_quantity_ambiguous'),
                 ('22 袋', '29 袋', '29 箱。', 'choice_quantity_no_match'),
                 ('22 袋', '22 袋', '29 袋。', 'choice_quantity_unverified'),
                 ('22 袋', '22 袋（晚班）', '29 袋。', 'choice_quantity_unverified'),
                 ('22 袋', '第 1 行（22 袋）', '29 袋。', 'choice_quantity_unverified'),
                 ('22 袋', '29 袋（22 袋）', '29 袋。', 'choice_quantity_unverified')]
        for alternative, displayed, reply, reason in cases:
            with self.subTest(reason=reason):
                first, second = self.prepare_short_quantity(alternative, displayed=displayed)
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
                                        action('ask', question=f'批次量要採用 {first} 還是 {second}？', calculation_report=[
                                            {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                                            {'label': '總件數', 'pending': True}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                matching = payload['calculation_source_adoptions'][0]
                self.assertEqual(matching['reason'], reason)
                self.assertIn('採用尚待核對', active['steps'][2]['observation']['error'])
                self.assertEqual(active['status'], 'needs_input')
                self.assertIn('窗口：北門', active['partial_result'])
                self.assertIn('總件數：待查', active['partial_result'])
                validate_database(self.store.path)

    def test_explicit_correction_remains_the_operative_source_after_a_choice_question(self):
        self.prepare_short_quantity('22 袋')
        self.reply('批次量正式改為 18 袋。')
        active = self.run_actions(self.formula(quantity='18', citation='[補充#2]', quote='批次量正式改為 18 袋'),
                                  action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 3,
                                                                        'unit': '件'}]))
        self.assertEqual(active['status'], 'report_ready')
        step = active['steps'][2]['observation'][goal_calculation.PROOF]
        self.assertEqual(step['source_adoptions'], [])
        self.assertEqual(step['operands'][0]['citation'], '[補充#2]')
        self.assertIn('54 件', active['result'])
        validate_database(self.store.path)

    def test_compound_quantity_choice_keeps_correction_and_restores_separate_clause_positions(self):
        first, _ = self.prepare_short_quantity('22 袋', files=True)
        reply = '窗口正式改為 南門；就用 ２９ 袋。'
        self.reply(reply)
        filename = '窗口選擇.json'
        rows = [{'label': '總件數', 'calculation_step': 3, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#2]', 'quote': '窗口正式改為 南門'}]
        result = self.run_actions(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
                                  action('write_artifact', filename=filename, calculation_report=rows),
                                  action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(result['status'], 'report_ready')
        proof = result['steps'][2]['observation'][goal_calculation.PROOF]
        adopted, = proof['source_adoptions']
        choice = adopted['spoken_selection']['value_choice']
        self.assertEqual(adopted['user_quote_start'], reply.index('就用'))
        self.assertEqual(choice['companion_clauses'],
                         [{'field': '窗口', 'quote': '窗口正式改為 南門', 'quote_start': 0}])
        self.assertIn('87 件', result['result'])
        self.assertIn('窗口正式改為 南門 [補充#2]', result['result'])
        self.assertNotIn('北門', result['result'])
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            choice['companion_clauses'][0]['quote_start'] = 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                       (json.dumps(result['steps'][2]['observation'], ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_compound_amount_does_not_confirm_quotes_negation_or_unresolved_scope(self):
        cases = [('22 袋', '「29 袋」，窗口正式改為 南門。', 'choice_reply_not_confirmed'),
                 ('22 袋', '不要用 29 袋，窗口正式改為 南門。', 'choice_reply_not_confirmed'),
                 ('29 袋', '29 袋，窗口正式改為 南門。', 'choice_quantity_ambiguous'),
                 ('22 袋', '29 袋，窗口正式改為 南門；先等等。', 'choice_reply_not_confirmed')]
        for alternative, reply, reason in cases:
            with self.subTest(reply=reply, reason=reason):
                first, second = self.prepare_short_quantity(alternative)
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
                    action('ask', question=f'批次量要採用 {first} 還是 {second}？', calculation_report=[
                        {'label': '窗口', 'citation': '[補充#2]', 'quote': '窗口正式改為 南門'},
                        {'label': '總件數', 'pending': True}]))
                result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(result['steps'][2]['action']['tool'], 'invalid')
                self.assertEqual(result['status'], 'needs_input')
                self.assertIn('窗口正式改為 南門 [補充#2]', result['partial_result'])
                self.assertIn('總件數：待查', result['partial_result'])
                validate_database(self.store.path)

    def test_compound_amount_yields_to_an_explicit_same_field_replacement(self):
        first, _ = self.prepare_short_quantity('22 袋')
        self.reply('29 袋；批次量正式改為 18 袋；窗口正式改為 南門。')
        result = self.run_actions(self.formula(quantity='29', citation=first, quote='批次量：29 袋'),
            self.formula(quantity='18', citation='[補充#2]', quote='批次量正式改為 18 袋'),
            action('finish', calculation_report=[
                {'label': '總件數', 'calculation_step': 4, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#2]', 'quote': '窗口正式改為 南門'}]))
        self.assertIn('另有較新的', result['steps'][2]['observation']['error'])
        self.assertEqual(result['status'], 'report_ready')
        proof = result['steps'][3]['observation'][goal_calculation.PROOF]
        self.assertEqual(proof['source_adoptions'], [])
        self.assertEqual(proof['operands'][0]['citation'], '[補充#2]')
        self.assertIn('54 件', result['result'])
        self.assertIn('窗口正式改為 南門 [補充#2]', result['result'])
        validate_database(self.store.path)

    def prepare_single_confirmation(self, question, *, files=False):
        revised = self.attach('批次量：29 袋。', '複查.txt')
        self.create(files=files)
        ref = f'[D{revised}:L1]'
        original = question.format(ref=ref)
        result = self.run_actions(action('read_ranges', ranges=[
            {'document_id': self.doc, 'start_line': 1, 'end_line': 4},
            {'document_id': revised, 'start_line': 1, 'end_line': 1}]),
            action('ask', question=original))
        self.assertEqual(result['status'], 'needs_input')
        return ref, original

    def test_single_confirmation_binds_ack_and_independent_correction_after_restore(self):
        ref, question = self.prepare_single_confirmation(
            '請確認批次量是否正式採用《複查》的第 1 行（29 袋）{ref}？', files=True)
        reply = '窗口正式改為 南門；好！'
        self.reply(reply)
        filename = '單筆選定.json'
        rows = [{'label': '總件數', 'calculation_step': 3, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#2]', 'quote': '窗口正式改為 南門'}]
        active = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(active['status'], 'report_ready')
        self.assertIn('87 件', active['result'])
        self.assertIn('窗口正式改為 南門 [補充#2]', active['result'])
        saved = active['steps'][2]['observation']
        adopted, = saved[goal_calculation.PROOF]['source_adoptions']
        evidence = adopted['spoken_selection']['confirmation']
        self.assertEqual(adopted['user_quote_start'], reply.index('好'))
        self.assertEqual(question[evidence['question_start']:evidence['question_end']], ref)
        self.assertEqual(evidence['reply_status'], 'affirmed')
        self.assertEqual(evidence['displayed_quantities'][0]['exact'], '29')
        self.assertEqual(evidence['source_quantity']['quote'], '批次量：29 袋')
        self.assertEqual(evidence['companion_clauses'][0]['quote_start'], 0)
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            evidence['question_start'] += 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                       (json.dumps(saved, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_single_confirmation_checks_declines_conditions_and_question_discrepancies(self):
        question = '是否採用 {ref} 的 29 袋作為批次量？'
        cases = [(question, '不是。', 'confirmation_declined'),
                 (question, '對，但須等收件人同意。', 'confirmation_unconfirmed'),
                 (question, '「好」。', 'confirmation_unconfirmed'),
                 ('批次量要採用 29 箱 {ref} 嗎？', '對。', 'confirmation_quantity_conflict'),
                 ('批次量是否採用《另一份文件》第 1 行 {ref}？', '好。', 'confirmation_reference_conflict'),
                 ('批次量是否採用《複查》第 2 行 {ref}？', '好。', 'confirmation_reference_conflict')]
        for proposed, reply, reason in cases:
            with self.subTest(reply=reply, question=proposed):
                ref, _ = self.prepare_single_confirmation(proposed)
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量正式採用哪一行？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(active['steps'][2]['action']['tool'], 'invalid')
                self.assertEqual(active['status'], 'needs_input')
                self.assertIn('窗口：北門', active['partial_result'])
                validate_database(self.store.path)

    def test_single_confirmation_never_replaces_a_saved_formal_update(self):
        ref, _ = self.prepare_single_confirmation('批次量是否正式採用 29 袋 {ref}？')
        checkpoint = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '批次量正式改為 18 袋。',
                           expected_fingerprint=checkpoint['fingerprint'])
        self.reply('對。')
        result = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            self.formula(quantity='18', citation='[更正#1]', quote='批次量正式改為 18 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 4, 'unit': '件'}]))
        self.assertIn('另有較新的', result['steps'][2]['observation']['error'])
        self.assertIn('54 件', result['result'])
        proof = result['steps'][3]['observation'][goal_calculation.PROOF]
        self.assertEqual(proof['source_adoptions'], [])
        self.assertEqual(proof['operands'][0]['citation'], '[更正#1]')
        validate_database(self.store.path)

    def prepare_conditional_approval(self, *, files=False, first_reply='好，但要等收件人同意。',
                                     actor='收件人', verb='同意', line=1, guidance=None):
        revised = self.attach('批次量：29 袋。\n批次量：22 袋。', '當班複核.txt')
        self.create(files=files)
        first, target = f'[D{revised}:L1]', f'[D{revised}:L{line}]'
        waiting = self.run_actions(action('read_ranges', ranges=[
            {'document_id': self.doc, 'start_line': 1, 'end_line': 4},
            {'document_id': revised, 'start_line': 1, 'end_line': 2}]),
            action('ask', question=f'批次量是否採用 29 袋 {first}？'))
        self.assertEqual(waiting['status'], 'needs_input')
        self.reply(first_reply)
        if guidance:
            checkpoint = goal_guidance.snapshot(self.store, self.sid, self.gid)
            goal_guidance.save(self.store, self.sid, self.gid, guidance,
                               expected_fingerprint=checkpoint['fingerprint'])
        amount = '29' if line == 1 else '22'
        question = f'{actor}是否已{verb}採用 {amount} 袋 {target} 作為正式批次量？'
        self.assertEqual(self.run_actions(action('ask', question=question))['status'], 'needs_input')
        return first

    def test_followup_approval_replays_original_condition_and_preserves_both_reply_sources(self):
        ref = self.prepare_conditional_approval(files=True, actor='林組長', verb='核准',
            first_reply='好；還要等林組長核准後才能採用。')
        self.reply('林組長已核准。')
        filename = '條件核對.json'
        rows = [{'label': '總件數', 'calculation_step': 4, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        active = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(active['status'], 'report_ready')
        self.assertIn('87 件', active['result'])
        saved = active['steps'][3]['observation']
        adopted, = saved[goal_calculation.PROOF]['source_adoptions']
        proof = adopted['spoken_selection']['confirmation']
        self.assertEqual(adopted['user_citation'], '[補充#3]')
        self.assertEqual(adopted['user_quote'], '林組長已核准')
        prior = proof['prior_condition']
        self.assertEqual((prior['citation'], prior['actor'], prior['verb']), ('[補充#2]', '林組長', '核准'))
        self.assertEqual(prior['quote_start'], 2)
        self.assertEqual(prior['proposal']['citation'], ref)
        self.assertEqual(prior['source_quantity']['exact'], '29')
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            prior['actor'] = '其他主管'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(saved, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_followup_approval_needs_the_same_condition_actor_action_and_source(self):
        cases = [({'actor': '主管'}, '對。', 'approval_actor_or_action_changed'),
                 ({'verb': '批准'}, '對。', 'approval_actor_or_action_changed'),
                 ({'line': 2}, '對。', 'approval_proposal_changed'),
                 ({'first_reply': '請再看看。'}, '對。', 'approval_condition_unverified'),
                 ({'first_reply': '好，但等收件人還沒同意。'}, '對。', 'approval_condition_unverified'),
                 ({}, '收件人還沒同意。', 'confirmation_declined'),
                 ({}, '主管已同意。', 'confirmation_unconfirmed'),
                 ({}, '好。', 'confirmation_unconfirmed')]
        for settings, reply, reason in cases:
            with self.subTest(settings=settings, reply=reply):
                ref = self.prepare_conditional_approval(**settings)
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量何時能正式採用？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(active['steps'][3]['action']['tool'], 'invalid')
                self.assertEqual(active['status'], 'needs_input')
                self.assertIn('窗口：北門', active['partial_result'])
                validate_database(self.store.path)

    def test_followup_approval_leaves_a_prior_formal_update_operative(self):
        ref = self.prepare_conditional_approval(guidance='批次量正式改為 18 袋。')
        self.reply('對。')
        active = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            self.formula(quantity='18', citation='[更正#1]', quote='批次量正式改為 18 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 5, 'unit': '件'}]))
        self.assertIn('另有較新的', active['steps'][3]['observation']['error'])
        self.assertIn('54 件', active['result'])
        proof = active['steps'][4]['observation'][goal_calculation.PROOF]
        self.assertEqual(proof['source_adoptions'], [])
        self.assertEqual(proof['operands'][0]['citation'], '[更正#1]')
        validate_database(self.store.path)

    def approval_wait_notice(self, ref, *, source=3, actor='收件人', value='29'):
        return (f'目前{actor}尚未同意 [補充#{source}]。'
                f'等{actor}同意採用 {value} 袋 {ref} 作為正式批次量後，請回覆確認。')

    def test_repeated_waits_keep_the_earlier_condition_and_restore_the_decline_chain(self):
        ref = self.prepare_conditional_approval(files=True)
        self.reply('收件人還沒同意。')
        first_notice = self.approval_wait_notice(ref)
        self.assertEqual(self.run_actions(action('ask', question=first_notice))['status'], 'needs_input')
        self.reply('還沒。')
        second_notice = self.approval_wait_notice(ref, source=4)
        self.assertEqual(self.run_actions(action('ask', question=second_notice))['status'], 'needs_input')
        self.reply('收件人已同意。')
        filename = '續等核對.json'
        rows = [{'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        result = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(result['status'], 'report_ready')
        self.assertIn('87 件', result['result'])
        self.assertIn('窗口：北門', result['result'])
        stored = result['steps'][5]['observation']
        adopted, = stored[goal_calculation.PROOF]['source_adoptions']
        proof = adopted['spoken_selection']['confirmation']
        self.assertEqual(adopted['user_citation'], '[補充#5]')
        self.assertEqual(proof['prior_condition']['citation'], '[補充#2]')
        self.assertEqual([row['citation'] for row in proof['deferred_replies']], ['[補充#3]', '[補充#4]'])
        self.assertEqual(proof['deferred_replies'][1]['question'], first_notice)
        self.assertEqual(proof['deferred_replies'][1]['quote'], '還沒')
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            proof['deferred_replies'][0]['citation'] = '[補充#4]'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(stored, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_wait_notice_rejects_another_actor_or_a_reference_to_the_wrong_reply(self):
        cases = [({}, '主管已同意。', 'confirmation_unconfirmed'),
                 ({}, '對。', 'confirmation_unconfirmed'),
                 ({'source': 2}, '收件人已同意。', 'approval_wait_citation_conflict'),
                 ({'actor': '主管'}, '主管已同意。', 'approval_actor_or_action_changed')]
        for settings, reply, reason in cases:
            with self.subTest(settings=settings, reply=reply):
                ref = self.prepare_conditional_approval()
                self.reply('收件人還沒同意。')
                self.assertEqual(self.run_actions(action('ask', question=self.approval_wait_notice(
                    ref, **settings)))['status'], 'needs_input')
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量要採用哪個正式原行？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(active['steps'][4]['action']['tool'], 'invalid')
                self.assertEqual(active['status'], 'needs_input')
                self.assertIn('窗口：北門', active['partial_result'])
                validate_database(self.store.path)

    def test_repeated_approval_cannot_jump_over_a_different_proposal_but_direct_new_choice_works(self):
        ref = self.prepare_conditional_approval()
        other = ref.replace(':L1]', ':L2]')
        self.reply('還沒。')
        self.run_actions(action('ask', question=self.approval_wait_notice(other, value='22')))
        self.reply('還沒。')
        self.run_actions(action('ask', question=self.approval_wait_notice(ref, source=4)))
        self.reply('收件人已同意。')
        engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                                action('ask', question=f'批次量要採用 {ref} 還是 {other}？'))
        pending = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
        payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], 'approval_proposal_changed')
        self.assertEqual(pending['steps'][5]['action']['tool'], 'invalid')
        self.reply(f'就用 {other} 那行。')
        final = self.run_actions(self.formula(quantity='22', citation=other, quote='批次量：22 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 8, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('66 件', final['result'])
        adopted, = final['steps'][7]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(adopted['candidates'][0]['citation'], other)
        self.assertNotIn('spoken_selection', adopted)
        validate_database(self.store.path)

    def other_approval_notice(self, ref, *, actor='主管', verb='同意', source=4):
        return (f'已收到{actor}{verb}的回覆 [補充#{source}]。'
                f'等收件人同意採用 29 袋 {ref} 作為正式批次量後，請回覆確認。')

    def test_other_approval_is_preserved_and_designated_approval_releases_original_line(self):
        ref = self.prepare_conditional_approval(files=True)
        self.reply('收件人還沒同意。')
        self.run_actions(action('ask', question=self.approval_wait_notice(ref)))
        self.reply('主管已同意，窗口正式改為 南門。')
        question = self.other_approval_notice(ref)
        self.run_actions(action('ask', question=question))
        self.reply('收件人已同意。')
        filename = '兩人回覆.json'
        rows = [{'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#4]', 'quote': '窗口正式改為 南門'}]
        active = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(active['status'], 'report_ready')
        self.assertIn('87 件', active['result'])
        self.assertIn('窗口正式改為 南門 [補充#4]', active['result'])
        stored = active['steps'][5]['observation']
        adopted, = stored[goal_calculation.PROOF]['source_adoptions']
        proof = adopted['spoken_selection']['confirmation']
        self.assertEqual(adopted['user_citation'], '[補充#5]')
        self.assertEqual(proof['prior_condition']['actor'], '收件人')
        self.assertEqual(proof['prior_condition']['citation'], '[補充#2]')
        self.assertEqual(proof['approval']['reported_other_approval']['citation'], '[補充#4]')
        deferred = proof['deferred_replies']
        self.assertEqual([row['citation'] for row in deferred], ['[補充#3]', '[補充#4]'])
        self.assertNotIn('kind', deferred[0])
        self.assertEqual((deferred[1]['kind'], deferred[1]['actor'], deferred[1]['quote']),
                         ('other_approval', '主管', '主管已同意'))
        content = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), content)
        with restored.connect() as db:
            deferred[1]['quote'] = '資材員已同意'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(stored, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_other_approval_recaps_cannot_substitute_or_misstate_the_actual_waiting_reply(self):
        cases = [({}, '主管已同意。', '主管已同意。', 'confirmation_unconfirmed'),
                 ({}, '資材員已同意。', '收件人已同意。', 'approval_wait_citation_conflict'),
                 ({'verb': '核准'}, '主管已同意。', '收件人已同意。', 'approval_wait_citation_conflict'),
                 ({'source': 3}, '主管已同意。', '收件人已同意。', 'approval_wait_citation_conflict'),
                 ({}, '「主管已同意」。', '收件人已同意。', 'approval_wait_citation_conflict')]
        for recap, earlier, reply, reason in cases:
            with self.subTest(recap=recap, earlier=earlier, reply=reply):
                ref = self.prepare_conditional_approval()
                self.reply('收件人還沒同意。')
                self.run_actions(action('ask', question=self.approval_wait_notice(ref)))
                self.reply(earlier)
                self.run_actions(action('ask', question=self.other_approval_notice(ref, **recap)))
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量是否有正式採用的原行？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(active['steps'][5]['action']['tool'], 'invalid')
                self.assertEqual(active['status'], 'needs_input')
                self.assertIn('窗口：北門', active['partial_result'])
                validate_database(self.store.path)

    def test_other_actor_context_cannot_fill_a_missing_original_condition(self):
        ref = self.prepare_conditional_approval(first_reply='再核對。')
        self.reply('收件人還沒同意。')
        self.run_actions(action('ask', question=self.approval_wait_notice(ref)))
        self.reply('主管已同意。')
        self.run_actions(action('ask', question=self.other_approval_notice(ref)))
        self.reply('收件人已同意。')
        engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))
        active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=1)
        payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], 'approval_condition_unverified')
        self.assertEqual(active['steps'][5]['action']['tool'], 'invalid')
        validate_database(self.store.path)

    def test_two_required_approvals_release_the_same_line_and_survive_restore(self):
        ref = self.prepare_conditional_approval(files=True,
            first_reply='好，但要等收件人和品質員都同意。')
        self.reply('收件人已同意，窗口正式改為 南門。')
        question = (f'已收到收件人同意的補答 [補充#3]。'
                    f'等品質員同意採用 29 袋 {ref} 作為正式批次量後，請回覆確認。')
        engine = ScriptedEngine(action('ask', question=question))
        waiting = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=1)
        preview = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
        adoption, = preview['calculation_source_adoptions']
        self.assertEqual(adoption['status'], 'pending')
        self.assertEqual(adoption['spoken_selection']['confirmation']['approval_group']['awaiting'], ['品質員'])
        self.assertEqual(waiting['status'], 'needs_input')
        self.reply('品質員已同意。')
        rows = [{'label': '總件數', 'calculation_step': 5, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#3]', 'quote': '窗口正式改為 南門'}]
        filename = '共同同意.json'
        active = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(active['status'], 'report_ready')
        self.assertIn('87 件', active['result'])
        self.assertIn('窗口正式改為 南門 [補充#3]', active['result'])
        stored = active['steps'][4]['observation']
        adopted, = stored[goal_calculation.PROOF]['source_adoptions']
        group = adopted['spoken_selection']['confirmation']['approval_group']
        self.assertEqual(group['origin']['actors'], ['收件人', '品質員'])
        self.assertEqual(group['origin']['citation'], '[補充#2]')
        self.assertEqual(group['awaiting'], [])
        self.assertEqual([(row['actor'], row['citation'], row['quote_start']) for row in group['reports']],
                         [('收件人', '[補充#3]', 0), ('品質員', '[補充#4]', 0)])
        original = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), original)
        with restored.connect() as db:
            group['reports'][0]['quote_start'] += 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(stored, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_group_keeps_unverified_people_or_changed_sources_pending(self):
        cases = [
            ('收件人已同意。', '品質員', '同意', 1, '收件人已同意。', 'approval_group_pending'),
            ('收件人已同意。', '品質員', '同意', 1, '主管已同意。', 'approval_group_pending'),
            ('收件人已同意。', '品質員', '同意', 1, '品質員還沒同意。', 'approval_group_pending'),
            ('收件人已同意。', '品質員', '同意', 1, '品質員只同意舊版。', 'confirmation_unconfirmed'),
            ('「收件人已同意」。', '品質員', '同意', 1, '品質員已同意。', 'confirmation_unconfirmed'),
            ('收件人已同意。', '品質員', '核准', 1, '品質員已核准。', 'approval_actor_or_action_changed'),
            ('收件人已同意。', '品質員', '同意', 2, '品質員已同意。', 'approval_proposal_changed'),
        ]
        for first, actor, verb, line, reply, reason in cases:
            with self.subTest(first=first, reply=reply, line=line):
                ref = self.prepare_conditional_approval(first_reply='好，但要等收件人與品質員都同意。')
                self.reply(first)
                target = ref if line == 1 else ref.replace('L1', 'L2')
                amount = '29' if line == 1 else '22'
                self.run_actions(action('ask', question=f'{actor}是否已{verb}採用 {amount} 袋 {target} 作為正式批次量？'))
                self.reply(reply)
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量的同意條件是否已成立？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                active = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(payload['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(active['steps'][4]['action']['tool'], 'invalid')
                self.assertIn('窗口：北門', active['partial_result'])
                validate_database(self.store.path)

    def test_group_waiting_notice_requires_factual_report_and_preserves_direct_selection(self):
        ref = self.prepare_conditional_approval(first_reply='好，但要等收件人、品質員都同意。')
        self.reply('收件人已同意。')
        self.run_actions(action('ask', question=
            f'等品質員同意採用 29 袋 {ref} 作為正式批次量後，請回覆確認。'))
        self.reply('對。')
        self.assertEqual(self.run_actions(
            self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))['steps'][-1]['action']['tool'], 'invalid')
        for _ in range(2):
            self.run_actions(action('ask', question=f'批次量是否採用 29 袋 {ref}？'))
            self.reply('好。')
            self.assertEqual(self.run_actions(
                self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))['steps'][-1]['action']['tool'],
                'invalid')
        self.run_actions(action('ask', question='批次量請指定採用的原行？'))
        self.reply(f'就用 {ref} 那行。')
        self.assertEqual(self.run_actions(
            self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))['steps'][-1]['observation']['exact'], '87')
        validate_database(self.store.path)

    def prepare_limited_approval(self, report='品質員只同意換窗口。'):
        ref = self.prepare_conditional_approval(files=True,
            first_reply='好，但要等收件人和品質員都同意。')
        self.reply('收件人已同意。')
        self.run_actions(action('ask', question=
            f'品質員是否已同意採用 29 袋 {ref} 作為正式批次量？'))
        self.reply(report)
        return ref

    def test_limited_approval_of_other_field_allows_a_later_scoped_confirmation(self):
        ref = self.prepare_limited_approval('品質員只同意換窗口，窗口正式改為 南門。')
        question = (f'品質員目前只同意更換窗口 [補充#4]。'
                    f'品質員是否已另行同意採用 29 袋 {ref} 作為正式批次量？')
        self.run_actions(action('ask', question=question))
        self.reply('品質員已同意。')
        rows = [{'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#4]', 'quote': '窗口正式改為 南門'}]
        filename = '交接確認.json'
        goal = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=filename, calculation_report=rows),
            action('deliver', filenames=[filename], calculation_report=rows))
        self.assertEqual(goal['status'], 'report_ready')
        self.assertIn('87 件', goal['result'])
        self.assertIn('窗口正式改為 南門 [補充#4]', goal['result'])
        stored = goal['steps'][5]['observation']
        adopted, = stored[goal_calculation.PROOF]['source_adoptions']
        proof = adopted['spoken_selection']['confirmation']
        approvals = proof['approval_group']
        self.assertEqual(approvals['awaiting'], [])
        self.assertEqual([(row['actor'], row['status'], row['citation']) for row in approvals['reports']], [
            ('收件人', 'affirmed', '[補充#3]'), ('品質員', 'limited', '[補充#4]'), ('品質員', 'affirmed', '[補充#5]')])
        self.assertEqual(approvals['reports'][1]['other_field'], '窗口')
        self.assertEqual(proof['approval']['reported_limited_scope']['citation'], '[補充#4]')
        content = artifacts.directory(self.store, self.gid).joinpath(filename).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(filename).read_bytes(), content)
        with restored.connect() as db:
            approvals['reports'][1]['other_field'] = '放行口'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(stored, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_group_limited_recaps_must_match_the_actual_reply_and_distinct_field(self):
        cases = [
            ('品質員只同意換窗口。', '品質員目前只同意更換窗口 [補充#3]。', 'approval_wait_citation_conflict'),
            ('品質員只同意換窗口。', '收件人目前只同意更換窗口 [補充#4]。', 'approval_wait_citation_conflict'),
            ('品質員只同意換窗口。', '品質員目前只同意更換退回量 [補充#4]。', 'approval_wait_citation_conflict'),
            ('品質員只同意換窗口。', '品質員目前只核准更換窗口 [補充#4]。', 'approval_wait_citation_conflict'),
            ('品質員只同意換批次量。', '', 'approval_scope_unverified'),
            ('品質員只同意換不在文件內的欄位。', '', 'approval_scope_unverified'),
            ('「品質員只同意換窗口」。', '', 'confirmation_unconfirmed'),
        ]
        for report, recap, reason in cases:
            with self.subTest(report=report, recap=recap):
                ref = self.prepare_limited_approval(report)
                self.run_actions(action('ask', question=
                    f'{recap}品質員是否已另行同意採用 29 袋 {ref} 作為正式批次量？'))
                self.reply('品質員已同意。')
                engine = ScriptedEngine(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
                    action('ask', question='批次量是否已確認？', calculation_report=[
                        {'label': '總件數', 'pending': True},
                        {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]))
                goal = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
                context = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
                self.assertEqual(context['calculation_source_adoptions'][0]['reason'], reason)
                self.assertEqual(goal['steps'][5]['action']['tool'], 'invalid')
                self.assertIn('總件數：待查', goal['partial_result'])
                validate_database(self.store.path)

    def test_group_repeated_limited_scope_preserves_pending_approval_until_a_factual_report(self):
        for answer, expected_tool in [('對。', 'invalid'), ('品質員已另外同意。', 'calculate_expression')]:
            with self.subTest(answer=answer):
                ref = self.prepare_limited_approval()
                self.run_actions(action('ask', question=
                    f'品質員是否已另行同意採用 29 袋 {ref} 作為正式批次量？'))
                self.reply('品質員仍只同意換窗口。')
                self.run_actions(action('ask', question=
                    f'品質員目前仍只同意更換窗口 [補充#5]。'
                    f'品質員是否已同意採用 29 袋 {ref} 作為正式批次量？請在取得同意後回覆。'))
                self.reply(answer)
                goal = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))
                step = goal['steps'][-1]
                self.assertEqual(step['action']['tool'], expected_tool)
                if expected_tool != 'invalid':
                    adopted, = step['observation'][goal_calculation.PROOF]['source_adoptions']
                    group = adopted['spoken_selection']['confirmation']['approval_group']
                    self.assertEqual([item['status'] for item in group['reports']],
                                     ['affirmed', 'limited', 'limited', 'affirmed'])
                validate_database(self.store.path)

    def test_group_question_checks_its_displayed_text_against_the_exact_field(self):
        for shown, should_deliver in [('西側緩坡', True), ('玻璃廊', False), ('藍旗架', False)]:
            with self.subTest(shown=shown):
                doc = self.attach('入場路線：西側緩坡。\n分流旗：藍旗架。', '動線.txt')
                ref = f'[D{doc}:L1]'
                self.create('整理入場路線。')
                self.run_actions(action('read', document_id=doc, start_line=1, end_line=2),
                                 action('ask', question=f'入場路線是否採用 {ref}？'))
                self.reply('好，但要等門房和場務都批准。')
                self.run_actions(action('ask', question=f'門房是否已批准採用 {ref} 作為正式入場路線？'))
                self.reply('門房已批准。')
                self.run_actions(action('ask', question=
                    f'場務是否也已另行批准採用「{shown}」 {ref} 作為正式入場路線？'))
                self.reply('對。')
                goal = self.run_actions(action('finish', calculation_report=[
                    {'label': '入場路線', 'citation': ref, 'quote': '入場路線：西側緩坡'}]))
                self.assertEqual(goal['status'] == 'report_ready', should_deliver)
                if should_deliver:
                    self.assertIn('西側緩坡', goal['result'])
                else:
                    self.assertEqual(goal['steps'][-1]['action']['tool'], 'invalid')
                validate_database(self.store.path)

    def test_group_question_can_replace_a_wrong_quantity_and_replay_exact_repair(self):
        ref = self.prepare_limited_approval('品質員只同意換窗口，窗口正式改為 南門。')
        self.run_actions(action('ask', question=f'品質員是否已同意採用 28 袋 {ref} 作為正式批次量？'))
        self.reply('是的。')
        self.run_actions(action('ask', question=
            f'上一問顯示的數量與引用原文不一致。品質員是否已同意採用 29 袋 {ref} 作為正式批次量？'))
        self.reply('是的。')
        rows = [{'label': '總件數', 'calculation_step': 7, 'unit': '件'},
                {'label': '窗口', 'citation': '[補充#4]', 'quote': '窗口正式改為 南門'}]
        name = '更正後交接.json'
        goal = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'),
            action('write_artifact', filename=name, calculation_report=rows),
            action('deliver', filenames=[name], calculation_report=rows))
        self.assertEqual(goal['status'], 'report_ready')
        self.assertIn('87 件', goal['result'])
        stored = goal['steps'][6]['observation']
        adopted, = stored[goal_calculation.PROOF]['source_adoptions']
        group = adopted['spoken_selection']['confirmation']['approval_group']
        replaced, = group['replaced_questions']
        self.assertEqual((replaced['citation'], replaced['replacement_reply'], replaced['reason']),
                         ('[補充#5]', '[補充#6]', 'confirmation_quantity_conflict'))
        self.assertEqual([report['citation'] for report in group['reports']], ['[補充#3]', '[補充#4]', '[補充#6]'])
        content = artifacts.directory(self.store, self.gid).joinpath(name).read_bytes()
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath(name).read_bytes(), content)
        with restored.connect() as db:
            replaced['replacement_reply'] = '[補充#5]'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=7',
                       (json.dumps(stored, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_group_recap_repair_requires_the_real_reply_and_the_same_proposal(self):
        cases = [('[補充#4]', '品質員', 1, '是的。', True), ('[補充#3]', '品質員', 1, '是的。', False),
                 ('[補充#4]', '主管', 1, '是的。', False), ('[補充#4]', '品質員', 2, '是的。', False),
                 ('[補充#4]', '品質員', 1, '品質員尚未同意。', False)]
        for citation, actor, line, reply, expected in cases:
            with self.subTest(citation=citation, actor=actor, line=line, reply=reply):
                ref = self.prepare_limited_approval()
                self.run_actions(action('ask', question=
                    f'品質員目前只同意換窗口 [補充#3]。品質員是否已同意採用 29 袋 {ref} 作為正式批次量？'))
                self.reply('是的。')
                amount, selected = ((29, ref) if line == 1 else (22, ref.replace('L1', 'L2')))
                self.run_actions(action('ask', question=
                    f'品質員目前只同意換窗口 {citation}。更正前一問的引用後，'
                    f'{actor}是否已同意採用 {amount} 袋 {selected} 作為正式批次量？'))
                self.reply(reply)
                step = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))['steps'][-1]
                self.assertEqual(step['action']['tool'] == 'calculate_expression', expected)
                validate_database(self.store.path)

    def test_group_clarification_binds_ambiguous_reply_and_a_fresh_factual_confirmation(self):
        cases = [(5, '對', 1, '批次量', '品質員', True), (4, '對', 1, '批次量', '品質員', False),
                 (5, '好', 1, '批次量', '品質員', False), (5, '對', 2, '批次量', '品質員', False),
                 (5, '對', 1, '退回量', '品質員', False), (5, '對', 1, '批次量', '主管', False),
                 (5, '對', 1, '批次量', None, False)]
        for cite, quote, line, field, actor, expected in cases:
            with self.subTest(cite=cite, quote=quote, line=line, field=field, actor=actor):
                ref = self.prepare_limited_approval()
                self.run_actions(action('ask', question=
                    f'等品質員同意採用 29 袋 {ref} 作為正式批次量後，請回覆確認。'))
                self.reply('對。')
                proposal = f'品質員已同意採用 {ref} 作為正式批次量'
                self.run_actions(action('ask', question=
                    f'請確認你說的「{quote}」[補充#{cite}] 是指{proposal}嗎？若已同意，請回覆「{proposal}」。'))
                cited = ref if line == 1 else ref.replace('L1','L2')
                self.reply(f'{actor}已同意採用 {cited} 作為正式{field}。' if actor else '對。')
                step = self.run_actions(self.formula(quantity='29', citation=ref, quote='批次量：29 袋'))['steps'][-1]
                self.assertEqual(step['action']['tool'] == 'calculate_expression', expected)
                if expected:
                    adopted, = step['observation'][goal_calculation.PROOF]['source_adoptions']
                    group = adopted['spoken_selection']['confirmation']['approval_group']
                    self.assertEqual([row['status'] for row in group['reports']],
                                     ['affirmed','limited','unclear','affirmed'])
                    self.assertEqual(group['reports'][-1]['scope']['citation'], ref)
                validate_database(self.store.path)

    def test_paged_lookup_cannot_continue_after_import_changes(self):
        name = '複點.txt'
        late = self.attach('\n'.join(['摘要。', *(f'附記{i}：批次量另列。' for i in range(1, 8)),
                                     '區位：木架。', '批次量：22 袋。']), name)
        self.create()
        self.run_actions(self.initial(), action('ask', question=f'批次量用「原單」{self.cite(1)} 還是複點？'))
        self.reply('第二份。')
        self.run_actions(action('find_document_text', document_id=late, query='批次量', start_line=1))
        replaced = self.attach('附記：批次量待更新。\n批次量：26 袋。', name)
        self.assertEqual(replaced, late)
        engine = ScriptedEngine(action('find_document_text', document_id=late, query='批次量', start_line=8))
        paused = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=1)
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(len(paused['steps']), 3)
        self.assertEqual(engine.prompts, [])

    def test_hypothetical_named_numbered_question_does_not_confirm_current_source(self):
        revised = self.attach('批次量：23 袋。', '晚班.txt')
        self.create()
        question = '如果只是試算，批次量要採用哪一份？\n1. 清單\n2. 晚班'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=question))
        self.reply('第二份。')
        waiting = self.run_actions(
            self.formula(quantity='23', citation=f'[D{revised}:L1]', quote='批次量：23 袋'),
            action('ask', question='正式批次量要用哪一行？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        self.assertIn('採用尚待核對', waiting['steps'][3]['observation']['error'])
        self.assertIn('窗口：北門', waiting['partial_result'])
        validate_database(self.store.path)

    def test_named_ordinal_uses_only_displayed_titles_and_restores_json(self):
        revised = self.attach('批次量：23 袋。', '晚班.txt')
        extra = self.attach('批次量：41 袋。', '插閱.txt')
        self.create(files=True)
        question = '批次量要採用哪一份：晚班還是清單？'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('read', document_id=extra, start_line=1, end_line=1), action('ask', question=question))
        self.reply('第二份。')
        roster = [{'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                  {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        result = self.run_actions(
            self.formula(quantity='23', citation=f'[D{revised}:L1]', quote='批次量：23 袋'),
            self.formula(),
            action('write_artifact', filename='定稿.json', calculation_report=roster),
            action('deliver', filenames=['定稿.json'], calculation_report=roster))
        self.assertEqual(result['status'], 'report_ready')
        self.assertIn('目前採用', result['steps'][4]['observation']['error'])
        proof, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(proof['spoken_selection']['candidate_documents'], [self.doc, revised])
        ordered = proof['spoken_selection']['ordinal']['options']
        self.assertEqual([row['citation'] for row in ordered], [f'[D{revised}:L1]', self.cite(1)])
        self.assertEqual([question[row['question_start']:row['question_end']] for row in ordered], ['晚班', '清單'])
        original = artifacts.directory(self.store, self.gid).joinpath('定稿.json').read_bytes()
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('定稿.json').read_bytes(), original)
        validate_database(restored.path)

    def test_mixed_ordinal_checks_title_against_another_displayed_citation(self):
        revised = self.attach('批次量：23 袋。', '晚班.txt')
        self.create()
        question = f'批次量要採用清單 [D{revised}:L1] 還是晚班？'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=question))
        self.reply('第二份。')
        engine = ScriptedEngine(
            self.formula(quantity='23', citation=f'[D{revised}:L1]', quote='批次量：23 袋'),
            action('ask', question='請確認批次量採哪個來源？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
        self.assertIn('reference_conflict', engine.prompts[0])
        self.assertIn('採用尚待核對', result['steps'][3]['observation']['error'])
        self.assertIn('窗口：北門', result['partial_result'])
        validate_database(self.store.path)

    def test_numbered_aliases_use_the_preface_and_ignore_background_refs(self):
        revised = self.attach('批次量：24 袋。\n批次量：21 袋。', '夜間記錄.txt')
        self.create()
        night = f'[D{revised}:L2]'
        original = self.cite(1)
        question = (f'案號類別是「早點」{original} 與「晚點」{night}。'
                    f'每袋件數照 {self.cite(2)}。批次量要採用哪一筆？\n'
                    '1. 「晚點」\n2. 「早點」')
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=2),
                         action('ask', question=question))
        self.reply('最後那筆。')
        result = self.run_actions(self.formula(), action('finish', calculation_report=[
            {'label': '總件數', 'calculation_step': 4, 'unit': '件'}]))
        self.assertIn('51 件', result['result'])
        proof, = result['steps'][3]['observation'][goal_calculation.PROOF]['source_adoptions']
        options = proof['spoken_selection']['ordinal']['options']
        self.assertEqual([row['citation'] for row in options], [night, original])
        self.assertEqual([row['displayed_citation'] for row in options], [None, None])
        validate_database(self.store.path)

    def test_named_ordinal_keeps_same_title_and_same_field_lines_unresolved(self):
        revised = self.attach('批次量：23 袋。\n批次量：24 袋。', '晚班.txt')
        other_dir = self.root / '暗室'
        other_dir.mkdir()
        path = other_dir / '晚班.txt'
        path.write_text('批次量：29 袋。', encoding='utf-8')
        duplicate = self.store.attach_document(self.sid, path)
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=2),
                         action('read', document_id=duplicate, start_line=1, end_line=1),
                         action('ask', question='批次量要採用哪一份：清單還是晚班？'))
        self.reply('第一份。')
        engine = ScriptedEngine(self.formula(), action('ask', question=(
            f'要採用清單 {self.cite(1)} 還是晚班 [D{revised}:L1]？'), calculation_report=[
            {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
            {'label': '總件數', 'pending': True}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
        self.assertIn('ambiguous_title', engine.prompts[0])
        self.assertIn('採用尚待核對', result['steps'][4]['observation']['error'])
        self.reply('第一份。')
        final = self.run_actions(self.formula(), action('finish', calculation_report=[
            {'label': '總件數', 'calculation_step': 7, 'unit': '件'}]))
        self.assertIn('51 件', final['result'])
        validate_database(self.store.path)

    def test_unread_named_candidate_uses_exact_lookup_before_selecting_other_option(self):
        revised = self.attach('備註行。\n簡報架：西。 \n批次量：27 袋。', '晚點複查.txt')
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量要採用哪一份：晚點複查還是清單？'))
        self.reply('第二份。')
        engine = ScriptedEngine(
            self.formula(),
            action('find_document_text', document_id=revised, query='批次量', start_line=1),
            self.formula(),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 5, 'unit': '件'}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=4)
        self.assertIn('needs_lookup', engine.prompts[0])
        self.assertIn('採用尚待核對', result['steps'][2]['observation']['error'])
        self.assertEqual(result['steps'][3]['observation']['excerpts'][0]['start_line'], 3)
        self.assertIn('51 件', result['result'])
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['result'], result['result'])

    def test_ordinal_uses_question_order_and_replays_json_after_detaching_version(self):
        revised = self.attach('批次量：23 袋。', '晚班.txt')
        self.create(files=True)
        late = f'[D{revised}:L1]'
        first = self.cite(1)
        question = f'批次量要採用「晚班」{late} 還是「原批」{first}？'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=question))
        self.reply('第二份。')
        rows = [{'label': '總件數', 'calculation_step': 5, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        result = self.run_actions(
            self.formula(quantity='23', citation=late, quote='批次量：23 袋'),
            self.formula(),
            action('write_artifact', filename='定稿.json', calculation_report=rows),
            action('deliver', filenames=['定稿.json'], calculation_report=rows))
        self.assertIn(f'目前採用 {first}', result['steps'][3]['observation']['error'])
        self.assertIn('51 件', result['result'])
        proof, = result['steps'][4]['observation'][goal_calculation.PROOF]['source_adoptions']
        ordered = proof['spoken_selection']['ordinal']
        self.assertEqual(ordered['position'], 2)
        self.assertEqual([item['citation'] for item in ordered['options']], [late, first])
        for item in ordered['options']:
            displayed = question[item['question_start']:item['question_end']]
            self.assertEqual(displayed, f'「{item["name"]}」{item["displayed_citation"]}')
        old = artifacts.directory(self.store, self.gid).joinpath('定稿.json').read_bytes()
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('定稿.json').read_bytes(), old)
        with restored.connect() as db:
            obs = result['steps'][4]['observation']
            ordered['options'].reverse()
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(obs, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_last_numbered_choice_ignores_background_citation(self):
        revised = self.attach('批次量：26 袋。\n批次量：20 袋。', '點驗.txt')
        self.create()
        first = f'[D{revised}:L1]'
        last = f'[D{revised}:L2]'
        question = (f'每袋件數仍看 {self.cite(2)}。批次量要選用哪一筆？\n'
                    f'1. 初次點驗 {first}\n（一）另外的說明\n2. 複點 {last}')
        invalid = question
        question = question.replace('\n（一）另外的說明', '')
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=2),
                         action('ask', question=invalid))
        self.reply('最後那筆。')
        result = self.run_actions(
            self.formula(quantity='20', citation=last, quote='批次量：20 袋'),
            action('ask', question=question, calculation_report=[{'label': '總件數', 'pending': True}]))
        self.assertIn('採用尚待核對', result['steps'][3]['observation']['error'])
        self.reply('最後那筆。')
        result = self.run_actions(self.formula(quantity='20', citation=last, quote='批次量：20 袋'),
                                  action('finish', calculation_report=[
                                      {'label': '總件數', 'calculation_step': 6, 'unit': '件'}]))
        self.assertIn('60 件', result['result'])
        proof, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(proof['spoken_selection']['ordinal']['mode'], 'last')
        self.assertEqual(proof['spoken_selection']['question_references'], [first, last])
        validate_database(self.store.path)

    def test_out_of_range_ordinal_does_not_confirm_and_later_new_order_is_used(self):
        revised = self.attach('批次量：21 袋。', '點驗.txt')
        self.create()
        original, late = self.cite(1), f'[D{revised}:L1]'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=f'批次量要採用 {late} 還是 {original}？'))
        self.reply('第九份。')
        pending = [{'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                   {'label': '總件數', 'pending': True}]
        reversed_question = f'批次量要採用 {original} 還是 {late}？'
        waiting = self.run_actions(self.formula(),
                                   action('ask', question=reversed_question, calculation_report=pending))
        self.assertIn('採用尚待核對', waiting['steps'][3]['observation']['error'])
        self.assertIn('窗口：北門', waiting['partial_result'])
        self.store = self.restore()
        self.reply('第２筆。')
        result = self.run_actions(
            self.formula(quantity='21', citation=late, quote='批次量：21 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 6, 'unit': '件'}]))
        self.assertIn('63 件', result['result'])
        proof, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(proof['spoken_selection']['ask_sequence'], 5)
        self.assertEqual(proof['spoken_selection']['question_references'], [original, late])
        validate_database(self.store.path)

    def test_user_reorders_visible_options_before_reply_and_needs_new_question(self):
        revised = self.attach('批次量：21 袋。', '點驗.txt')
        self.create()
        original, late = self.cite(1), f'[D{revised}:L1]'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=f'批次量要採用 {late} 還是 {original}？'))
        snapshot = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid,
                           '請重新調換順序，把點驗列成第二份，再讓我選。',
                           expected_fingerprint=snapshot['fingerprint'])
        self.reply('第二份。')
        pending = [{'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                   {'label': '總件數', 'pending': True}]
        engine = ScriptedEngine(
            self.formula(),
            action('ask', question=f'批次量要採用 {original} 還是 {late}？', calculation_report=pending))
        waiting = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=2)
        self.assertIn('choice_order_changed', engine.prompts[0])
        self.assertIn('採用尚待核對', waiting['steps'][3]['observation']['error'])
        self.assertEqual(waiting['status'], 'needs_input')
        self.reply('用第二份。')
        final = self.run_actions(
            self.formula(quantity='21', citation=late, quote='批次量：21 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 6, 'unit': '件'}]))
        self.assertIn('63 件', final['result'])
        validate_database(self.store.path)

    def test_unrelated_later_ordinal_keeps_previously_confirmed_field(self):
        revised = self.attach('批次量：23 袋。', '點驗.txt')
        self.create()
        original, late = self.cite(1), f'[D{revised}:L1]'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=f'批次量要採用 {original} 還是 {late}？'))
        snap = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '窗口改為南門。同事轉述「請重排候選」。',
                           expected_fingerprint=snap['fingerprint'])
        self.reply('第二份。')
        self.run_actions(action('ask', question='集合時間是上午還是下午？'))
        self.reply('第一個。')
        result = self.run_actions(
            self.formula(),
            self.formula(quantity='23', citation=late, quote='批次量：23 袋'),
            action('finish', calculation_report=[
                {'label': '總件數', 'calculation_step': 6, 'unit': '件'},
                {'label': '窗口', 'citation': '[更正#1]', 'quote': '窗口改為南門'}]))
        self.assertIn('目前採用', result['steps'][4]['observation']['error'])
        self.assertEqual(result['steps'][5]['observation']['exact'], '69')
        proof, = result['steps'][5]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(proof['spoken_selection']['ask_sequence'], 3)
        self.assertNotIn('[補充#4]', result['result'])
        validate_database(self.store.path)

    def test_mixed_fields_in_option_list_do_not_change_other_field(self):
        revised = self.attach('窗口：南門。', '附記.txt')
        self.create()
        original, other = self.cite(1), f'[D{revised}:L1]'
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=f'批次量要採用 {original} 還是 {other}？'))
        self.reply('第 2 筆。')
        pending = self.run_actions(self.formula(), action('ask', question='請確認哪一行是正式批次量？',
            calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        self.assertIn('採用尚待核對', pending['steps'][3]['observation']['error'])
        self.assertIn('窗口：北門', pending['partial_result'])
        validate_database(self.store.path)

    def test_document_title_chooses_unique_shared_field_and_restores_file_with_detached_copy(self):
        revised = self.attach('複點說明。\n批次量：24 袋。', '晚班清點.txt')
        self.create(files=True)
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=2),
                         action('ask', question='本次以哪份清點為準？'))
        self.reply('就用晚班清點那份。每袋件數沿用原先。')
        rows = [{'label': '總件數', 'calculation_step': 5, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門。'}]
        result = self.run_actions(
            self.formula(),
            self.formula(quantity='24', citation=f'[D{revised}:L2]', quote='批次量：24 袋'),
            action('write_artifact', filename='新版.json', calculation_report=rows),
            action('deliver', filenames=['新版.json'], calculation_report=rows))
        self.assertIn(f'目前採用 [D{revised}:L2]', result['steps'][3]['observation']['error'])
        self.assertIn('72 件', result['result'])
        proof, = result['steps'][4]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual(proof['spoken_selection']['ask_sequence'], 3)
        self.assertEqual(proof['spoken_selection']['question'], '本次以哪份清點為準？')
        self.assertEqual(proof['spoken_selection']['name'], '晚班清點')
        original = artifacts.directory(self.store, self.gid).joinpath('新版.json').read_bytes()
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('新版.json').read_bytes(), original)
        with restored.connect() as db:
            obs = result['steps'][4]['observation']
            obs[goal_calculation.PROOF]['source_adoptions'][0]['spoken_selection']['name'] = '假文件'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(obs, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_short_alias_uses_the_line_bound_in_original_question_only(self):
        revised = self.attach('批次量：25 袋。\n批次量：27 袋。', '夜間記錄.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=2),
                         action('ask', question=f'批次量要採用「初查」{self.cite(1)} 還是「晚班」[D{revised}:L2]？'))
        self.reply('晚班。')
        result = self.run_actions(
            self.formula(quantity='25', citation=f'[D{revised}:L1]', quote='批次量：25 袋'),
            self.formula(quantity='27', citation=f'[D{revised}:L2]', quote='批次量：27 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 5, 'unit': '件'}]))
        self.assertIn(f'目前採用 [D{revised}:L2]', result['steps'][3]['observation']['error'])
        self.assertIn('81 件', result['result'])
        validate_database(self.store.path)

    def test_deictic_reply_requires_one_question_reference(self):
        revised = self.attach('批次量：25 袋。', '新查點.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question=f'批次量採用 {self.cite(1)} 還是 [D{revised}:L1]？'))
        self.reply('就用剛才那份。')
        self.run_actions(
            self.formula(),
            action('ask', question=f'本次要採用 [D{revised}:L1] 嗎？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        waiting = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertIn('採用尚待核對', waiting['steps'][3]['observation']['error'])
        self.assertIn('窗口：北門', waiting['partial_result'])
        self.reply('剛才那份。')
        result = self.run_actions(
            self.formula(quantity='25', citation=f'[D{revised}:L1]', quote='批次量：25 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 6, 'unit': '件'}]))
        self.assertIn('75 件', result['result'])
        self.assertIn('[補充#5]', result['result'])
        validate_database(self.store.path)

    def test_same_title_keeps_both_versions_pending_until_exact_source_selection(self):
        ids = []
        for subdir, amount in [('west', 19), ('east', 22)]:
            path = self.root / subdir / '複點.txt'
            path.parent.mkdir()
            path.write_text(f'批次量：{amount} 袋。', encoding='utf-8')
            ids.append(self.store.attach_document(self.sid, path))
        self.create()
        self.run_actions(self.initial(), *(action('read', document_id=did, start_line=1, end_line=1) for did in ids),
                         action('ask', question=f'批次量要用 [D{ids[0]}:L1] 還是 [D{ids[1]}:L1]？'))
        self.reply('就用複點那份。')
        result = self.run_actions(
            self.formula(quantity='19', citation=f'[D{ids[0]}:L1]', quote='批次量：19 袋'),
            action('ask', question=f'批次量要用 [D{ids[0]}:L1] 還是 [D{ids[1]}:L1]？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True}]))
        self.assertIn('採用尚待核對', result['steps'][4]['observation']['error'])
        self.assertIn('總件數：待查', result['partial_result'])
        restored = self.restore()
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['partial_result'], result['partial_result'])

    def test_named_adoption_during_waiting_recheck_keeps_the_original_question(self):
        revised = self.attach('晚班點收說明。\n批次量：26 袋。', '晚班複點.txt')
        self.create()
        question = '交班員是誰？'
        self.run_actions(self.initial(), self.formula(),
                         action('ask', question=question, calculation_report=[
                             {'label': '總件數', 'calculation_step': 2, 'unit': '件'}]))
        snap = goal_guidance.snapshot(self.store, self.sid, self.gid)
        updated = goal_guidance.save(self.store, self.sid, self.gid, '欄位「批次量」改用晚班複點那份。',
                                     expected_fingerprint=snap['fingerprint'])
        calculated = json.loads(self.formula(quantity='26', citation=f'[D{revised}:L2]', quote='批次量：26 袋'))
        del calculated['note']
        engine = ScriptedEngine(
            json.dumps({'tool': 'read_ranges', 'args': {'ranges': [
                {'document_id': revised, 'start_line': 1, 'end_line': 2}]}}, ensure_ascii=False),
            json.dumps(calculated, ensure_ascii=False),
            json.dumps({'calculation_report': [{'label': '總件數', 'calculation_step': 5, 'unit': '件'}],
                        'withdrawal_source': None}, ensure_ascii=False))
        review = goal_partial_recheck.run(self.store, self.sid, self.gid, engine,
                                           expected_fingerprint=updated['fingerprint'])
        self.assertEqual(review['goal']['question'], question)
        self.assertIn('needed_document_coverage', engine.prompts[0])
        self.assertIn('78 件', review['goal']['partial_result'])
        self.store = self.restore()
        self.reply('交班員：徐遠棠。')
        final = self.run_actions(action('finish', calculation_report=[
            {'label': '總件數', 'recheck_calculation': 1, 'unit': '件'},
            {'label': '交班員', 'citation': '[補充#3]', 'quote': '交班員：徐遠棠'}]))
        self.assertIn('採用依據 [更正#1]', final['result'])
        self.assertIn('78 件', final['result'])
        validate_database(self.store.path)

    def test_named_unread_document_uses_one_exhaustive_field_lookup(self):
        revised = self.attach('新紀錄。\n箱碼：A31。\n批次量：20 袋。', '現場複點.txt')
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量要採用哪一份文件？'))
        self.reply('就用現場複點那份。')
        engine = ScriptedEngine(
            action('find_document_text', document_id=revised, query='批次量', start_line=1),
            self.formula(quantity='20', citation=f'[D{revised}:L3]', quote='批次量：20 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 4, 'unit': '件'}]))
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=3)
        self.assertIn('needs_lookup', engine.prompts[0])
        self.assertEqual([s['action']['tool'] for s in result['steps'][2:]], [
            'find_document_text', 'calculate_expression', 'finish'])
        self.assertIn('60 件', result['result'])
        self.assertNotIn('箱碼', result['result'])
        self.store.detach_document(self.sid, revised)
        restored = self.restore()
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['result'], result['result'])

    def test_filename_choice_stays_pending_with_two_lines_for_one_field(self):
        revised = self.attach('批次量：25 袋。\n批次量：26 袋。', '現場複點.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question='批次量要採哪一份？'))
        self.reply('就用現場複點那份。')
        result = self.run_actions(
            self.formula(),
            action('find_document_text', document_id=revised, query='批次量', start_line=1),
            self.formula(quantity='25', citation=f'[D{revised}:L1]', quote='批次量：25 袋'),
            action('ask', question=f'批次量採用 [D{revised}:L1] 或 [D{revised}:L2]？', calculation_report=[
                {'label': '總件數', 'pending': True}]))
        self.assertEqual([row['action']['tool'] for row in result['steps'][3:]], [
            'invalid', 'find_document_text', 'invalid', 'ask'])
        self.assertIn('總件數：待查', result['partial_result'])
        validate_database(self.store.path)

    def test_adopted_document_blocks_old_calculation_and_preserves_independent_evidence(self):
        revised = self.attach('班別：晚班。\n批次量：21 袋。\n窗口：南門。', '晚班複點.txt')
        self.create(files=True)
        old = [{'label': '總件數', 'calculation_step': 3, 'unit': '件'}]
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=3),
                         self.formula(), action('ask', question='本批以哪份複點為準？', calculation_report=old))
        self.reply(f'本次以晚班複點的 [D{revised}:L2] 為準。窗口照用 {self.cite(4)}。')
        rows = [{'label': '總件數', 'calculation_step': 8, 'unit': '件'},
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'}]
        engine = ScriptedEngine(
            self.formula(),
            action('write_artifact', filename='早班.json', calculation_report=old),
            self.formula(quantity='21', citation=self.cite(3), quote='退回量：21 袋'),
            self.formula(quantity='21', citation=f'[D{revised}:L2]', quote='批次量：21 袋'),
            action('write_artifact', filename='晚班.json', calculation_report=rows),
            action('deliver', filenames=['晚班.json'], calculation_report=rows),
        )
        result = autonomy.run_goal(self.store, self.sid, self.gid, engine, max_steps=6)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual([step['action']['tool'] for step in result['steps'][4:7]], ['invalid'] * 3)
        self.assertIn(f'目前採用 [D{revised}:L2]', result['steps'][4]['observation']['error'])
        self.assertIn(f'目前採用 [D{revised}:L2]', result['steps'][5]['observation']['error'])
        self.assertIn('calculation_source_adoptions', engine.prompts[0])
        proof = result['steps'][7]['observation'][goal_calculation.PROOF]['source_adoptions']
        self.assertEqual([(row['path'], row['user_citation']) for row in proof], [('/values/0', '[補充#4]')])
        self.assertIn('21 袋', result['result'])
        directory = artifacts.directory(self.store, self.gid)
        content = directory.joinpath('晚班.json').read_bytes()
        first, second = json.loads(content)
        self.assertEqual(first['精確結果'], '63')
        self.assertEqual(first['來源'][0]['採用依據'], '[補充#4]')
        self.assertEqual(first['來源'][1]['引用'], self.cite(2))
        self.assertEqual(second['引用'], self.cite(4))
        restored = self.restore()
        artifacts.rebuild(restored, self.sid, self.gid)
        self.assertEqual(artifacts.directory(restored, self.gid).joinpath('晚班.json').read_bytes(), content)
        with restored.connect() as db:
            obs = result['steps'][7]['observation']
            obs[goal_calculation.PROOF]['source_adoptions'][0]['user_quote_start'] += 1
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=8',
                       (json.dumps(obs, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '保存算式'):
            validate_database(restored.path)

    def test_selected_document_during_partial_recheck_survives_restore_and_independent_reply(self):
        revised = self.attach('批次量：24 袋。', '新複點.txt')
        self.create()
        question = '交班員是誰？'
        self.run_actions(self.initial(), self.formula(),
                         action('ask', question=question, calculation_report=[
                             {'label': '總件數', 'calculation_step': 2, 'unit': '件'}]))
        snapshot = goal_guidance.snapshot(self.store, self.sid, self.gid)
        revised_note = goal_guidance.save(self.store, self.sid, self.gid, f'欄位「批次量」改用 [D{revised}:L1]。',
                                          expected_fingerprint=snapshot['fingerprint'])
        calculate = json.loads(self.formula(quantity='24', citation=f'[D{revised}:L1]', quote='批次量：24 袋'))
        del calculate['note']
        engine = ScriptedEngine(
            json.dumps({'tool': 'read_ranges', 'args': {'ranges': [
                {'document_id': revised, 'start_line': 1, 'end_line': 1}]}}, ensure_ascii=False),
            json.dumps(calculate, ensure_ascii=False),
            json.dumps({'calculation_report': [{'label': '總件數', 'calculation_step': 5, 'unit': '件'}],
                        'withdrawal_source': None}, ensure_ascii=False))
        rechecked = goal_partial_recheck.run(self.store, self.sid, self.gid, engine,
                                              expected_fingerprint=revised_note['fingerprint'])
        self.assertEqual(rechecked['goal']['question'], question)
        self.assertIn('72 件', rechecked['goal']['partial_result'])
        self.assertIn('採用依據 [更正#1]', rechecked['goal']['partial_result'])
        self.store = self.restore()
        self.reply('交班員：冉希言。')
        result = self.run_actions(action('finish', calculation_report=[
            {'label': '總件數', 'recheck_calculation': 1, 'unit': '件'},
            {'label': '交班員', 'citation': '[補充#3]', 'quote': '交班員：冉希言'},
        ]))
        self.assertIn('72 件', result['result'])
        self.assertIn('採用依據 [更正#1]', result['result'])
        self.assertEqual(len(result['steps']), 4)
        validate_database(self.store.path)

    def test_unread_selection_blocks_only_named_field_until_read(self):
        revised = self.attach('批次量：18 袋。', '複點.txt')
        self.create()
        self.run_actions(self.initial(), action('ask', question='批次量要採哪一份？'))
        self.reply(f'欄位「批次量」改用 [D{revised}:L1]。')
        waiting = [{'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                   {'label': '總件數', 'pending': True}]
        result = self.run_actions(
            self.formula(),
            action('read_ranges', ranges=[{'document_id': revised, 'start_line': 1, 'end_line': 1}]),
            self.formula(quantity='18', citation=f'[D{revised}:L1]', quote='批次量：18 袋'),
            action('finish', calculation_report=[waiting[0], {'label': '總件數', 'calculation_step': 5, 'unit': '件'}]))
        self.assertIn('採用尚待核對', result['steps'][2]['observation']['error'])
        self.assertIn('窗口：北門', result['result'])
        self.assertIn('54 件', result['result'])
        validate_database(self.store.path)

    def test_competing_sources_keep_only_dependent_rows_pending_until_short_choice(self):
        revised = self.attach('批次量：19 袋。', '複點.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question='本批採哪一筆？'))
        self.reply(f'欄位「批次量」還沒決定用 {self.cite(1)} 或 [D{revised}:L1]。')
        result = self.run_actions(
            self.formula(),
            self.formula(quantity='19', citation=f'[D{revised}:L1]', quote='批次量：19 袋'),
            action('ask', question='請確認批次量採早班還是複點？', calculation_report=[
                {'label': '窗口', 'citation': self.cite(4), 'quote': '窗口：北門'},
                {'label': '總件數', 'pending': True},
            ]))
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual([step['action']['tool'] for step in result['steps'][3:5]], ['invalid'] * 2)
        self.assertEqual(result['partial_result'], f'窗口：北門 {self.cite(4)}\n總件數：待查')
        self.store = self.restore()
        self.reply(f'就用 [D{revised}:L1] 那行。')
        current = self.run_actions(
            self.formula(quantity='19', citation=f'[D{revised}:L1]', quote='批次量：19 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 7, 'unit': '件'}]))
        self.assertIn('57 件', current['result'])
        self.assertIn('[補充#6]', current['result'])
        self.assertNotIn('[補充#3]', current['result'])
        validate_database(self.store.path)

    def test_quoted_hypothetical_choice_does_not_replace_document(self):
        revised = self.attach('批次量：23 袋。', '複點.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question='是否已正式調整？'))
        self.reply(f'如果要試算，批次量改用 [D{revised}:L1]。他只說「批次量改用 [D{revised}:L1]」。')
        result = self.run_actions(self.formula(),
                                  action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 4, 'unit': '件'}]))
        self.assertIn('51 件', result['result'])
        self.assertNotIn('採用依據', result['result'])
        validate_database(self.store.path)

    def test_citation_adoption_and_later_literal_correction_each_supersede_in_order(self):
        revised = self.attach('批次量：19 袋。', '複點.txt')
        self.create()
        self.run_actions(self.initial(), action('read', document_id=revised, start_line=1, end_line=1),
                         action('ask', question='本批採哪一筆？'))
        self.reply(f'就用 [D{revised}:L1]。')
        self.run_actions(self.formula(quantity='19', citation=f'[D{revised}:L1]', quote='批次量：19 袋'),
                         action('ask', question='是否有更新的現場正式數量？'))
        self.reply('批次量正式改為 22 袋。')
        result = self.run_actions(
            self.formula(quantity='19', citation=f'[D{revised}:L1]', quote='批次量：19 袋'),
            self.formula(quantity='22', citation='[補充#5]', quote='批次量正式改為 22 袋'),
            action('finish', calculation_report=[{'label': '總件數', 'calculation_step': 7, 'unit': '件'}]))
        self.assertEqual(result['steps'][5]['action']['tool'], 'invalid')
        self.assertIn('66 件', result['result'])
        self.assertIn('[補充#5]', result['result'])
        self.assertNotIn('[補充#3]', result['result'])
        validate_database(self.store.path)

    def test_delivery_rejects_changed_saved_file_even_with_consistent_new_hash(self):
        self.create(files=True)
        rows = [{'label': '總件數', 'calculation_step': 2, 'unit': '件'}]
        self.run_actions(self.initial(), self.formula(),
                         action('write_artifact', filename='交接.txt', calculation_report=rows))
        body = '錯誤草稿：63 件'
        data = body.encode('utf-8')
        path = artifacts.directory(self.store, self.gid) / '交接.txt'
        path.write_bytes(data)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_artifacts SET content=?,sha256=?,size_bytes=? WHERE goal_id=?',
                       (body, hashlib.sha256(data).hexdigest(), len(data), self.gid))
        result = self.run_actions(action('deliver', filenames=['交接.txt'], calculation_report=rows))
        self.assertEqual(result['status'], 'paused')
        self.assertIn('檔案登記的內容', result['steps'][-1]['observation']['error'])
        self.assertEqual(path.read_bytes(), data)
        with self.assertRaisesRegex(ValueError, '保存檔案與核對成果'):
            validate_database(self.store.path)

    def test_rejects_stale_file_and_out_of_scope_step_after_revision(self):
        self.create(files=True)
        old = [{'label': '總件數', 'calculation_step': 2, 'unit': '件'}]
        self.run_actions(self.initial(), self.formula(), action('write_artifact', filename='早班.txt', calculation_report=old),
                         action('ask', question='批次量的正式複點是多少？', calculation_report=old))
        self.reply('批次量正式改為 19 袋。')
        revised = [{'label': '總件數', 'calculation_step': 7, 'unit': '件'}]
        result = self.run_actions(
            action('deliver', filenames=['早班.txt'], calculation_report=old),
            action('write_artifact', filename='後班.txt', calculation_report=revised),
            self.formula(quantity='19', citation='[補充#4]', quote='批次量正式改為 19 袋'),
            action('deliver', filenames=['早班.txt'], calculation_report=revised),
            action('write_artifact', filename='後班.txt', calculation_report=revised),
            action('deliver', filenames=['後班.txt'], calculation_report=revised))
        self.assertEqual([step['action']['tool'] for step in result['steps'][4:]], [
            'invalid', 'invalid', 'calculate_expression', 'invalid', 'write_artifact', 'deliver',
        ])
        self.assertIn('57 件', result['result'])
        validate_database(self.store.path)
