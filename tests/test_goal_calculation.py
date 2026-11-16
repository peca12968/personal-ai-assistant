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
