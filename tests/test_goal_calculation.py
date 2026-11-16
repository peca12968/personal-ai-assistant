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
