import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy, goal_guidance, goal_replies, quantity_evidence
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class QuantityEvidenceTests(unittest.TestCase):
    def test_document_adoption_binds_exact_version_and_rejects_stale_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '岩洲本批退回'
            first = '版本「初填版」欄位「岩洲本批退回」為 5 件。'
            second = '版本「複核版」欄位「岩洲本批退回」為 8 件。'
            approved = '欄位「岩洲本批退回」｜採用版本｜複核版'
            note = approved + '\n欄位「岩洲另批退回」｜採用版本｜初填版\n草稿引用欄位「岩洲本批退回」｜採用版本｜初填版'
            ranges = []
            for number, text in enumerate((first, second, note), 1):
                path = root / f'adoption-{number}.txt'
                path.write_text(text, encoding='utf-8')
                did = store.attach_document(sid, path)
                ranges.append({'document_id': did, 'start_line': 1, 'end_line': len(text.splitlines())})
            gid = autonomy.create_goal(store, sid, '岩洲本批原備40件，核對可交量。')
            candidates = [{'label': '初填版', 'citation': f'[D{ranges[0]["document_id"]}:L1]', 'quote': first},
                          {'label': '複核版', 'citation': f'[D{ranges[1]["document_id"]}:L1]', 'quote': second}]
            args = {'unit': '件', 'field': field, 'candidates': candidates, 'adoption_ranges': [ranges[2]]}
            invalid = [{**candidates[0], 'label': '舊版'}, candidates[1]]
            binding = {'path': '/values/1', 'step': 3, 'candidate': 2}
            expr = {'operation': 'difference', 'values': ['40', '8']}
            checked = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read_ranges', ranges=ranges), action('check_quantities', **{**args, 'candidates': invalid}),
                action('check_quantities', **args),
                action('calculate_expression', expression={**expr, 'values': ['40', '5']},
                       quantity_inputs=[{**binding, 'candidate': 1}]),
                action('calculate_expression', expression=expr, quantity_inputs=[binding])), max_steps=5)
            self.assertIn('候選版名須逐字', checked['steps'][1]['observation']['error'])
            adoption = checked['steps'][2]['observation']['adoption']
            self.assertEqual((adoption['status'], adoption['version'], adoption['candidate']), ('adopted', '複核版', 2))
            self.assertEqual([row['field'] for row in adoption['records']], [field, '岩洲另批退回'])
            self.assertEqual(adoption['records'][0]['quote'], approved)
            self.assertIn('文件明列採用的版本', checked['steps'][3]['observation']['error'])
            calculated = checked['steps'][4]['observation']
            self.assertEqual(calculated['exact'], '32')
            proof, = calculated['quantity_inputs']
            self.assertEqual(proof['adoption'], adoption)
            self.assertEqual(second[proof['quantity']['version_start']:proof['quantity']['version_end']], '複核版')
            snap = goal_guidance.snapshot(store, sid, gid)
            later = '本次採用初填版。'
            goal_guidance.save(store, sid, gid, later, expected_fingerprint=snap['fingerprint'])
            override = {**binding, 'candidate': 1, 'selection': {'citation': '[更正#1]', 'quote': later}}
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_expression', expression=expr, quantity_inputs=[binding]),
                action('calculate_expression', expression={**expr, 'values': ['40', '5']}, quantity_inputs=[override]),
                action('finish', report='依正式補充採初填，可交35件。')))
            self.assertIn('目前條件', final['steps'][5]['observation']['error'])
            self.assertEqual(final['steps'][6]['observation']['exact'], '35')
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            altered = copy.deepcopy(calculated)
            altered['quantity_inputs'][0]['adoption']['records'][0]['version'] = '初填版'
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                           (json.dumps(altered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '保存的算式與數量核對'):
                validate_database(restored.path)

    def test_document_adoption_does_not_select_from_incomplete_conflicting_or_missing_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            path = root / 'versions.txt'
            lines = ['版本「甲版」欄位「本批退回」為 3 件。', '版本「乙版」欄位「本批退回」為 4 件。',
                     '欄位「本批退回」｜採用版本｜甲版', '欄位「本批退回」｜採用版本｜乙版',
                     '欄位「本批退回」｜採用版本｜定稿版']
            path.write_text('\n'.join(lines), encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, '核對正式版本')
            candidates = [{'label': name, 'citation': f'[D{did}:L{line}]', 'quote': lines[line - 1]}
                          for line, name in ((1, '甲版'), (2, '乙版'))]
            def compare(start, end):
                return action('check_quantities', unit='件', field='本批退回', candidates=candidates,
                              adoption_ranges=[{'document_id': did, 'start_line': start, 'end_line': end}])
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=3), compare(3, 4),
                action('read', document_id=did, start_line=4, end_line=5), compare(3, 4), compare(5, 5),
                action('calculate_expression', expression='3', quantity_inputs=[{'path': '', 'step': 5, 'candidate': 1}]),
                action('finish', report='明列採定稿版，但該版數值待確認。')), max_steps=7)
            self.assertEqual(result['steps'][1]['observation']['adoption']['missing'], [f'[D{did}:L4]'])
            self.assertEqual(result['steps'][1]['observation']['adoption']['status'], 'incomplete')
            self.assertEqual(result['steps'][3]['observation']['adoption']['status'], 'conflicting')
            missing = result['steps'][4]['observation']['adoption']
            self.assertEqual((missing['status'], missing['version']), ('candidate_unavailable', '定稿版'))
            self.assertIn('採用缺漏或有衝突', result['steps'][5]['observation']['error'])
            validate_database(store.path)

    def test_named_versions_accept_only_current_same_field_replacement_and_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '第 2 批配料'
            docs = ['欄位「第 2 批配料」為 400 公克。欄位「第 3 批配料」為 600 公克。',
                    '欄位「第 2 批配料」為 0.6 公斤。']
            ranges, versions = [], []
            for i, (name, text) in enumerate(zip(('初填版', '複核版'), docs, strict=True), 1):
                path = root / f'version-{i}.txt'
                path.write_text(text, encoding='utf-8')
                did = store.attach_document(sid, path)
                ranges.append({'document_id': did, 'start_line': 1, 'end_line': 1})
                versions.append({'label': name, 'citation': f'[D{did}:L1]', 'quote': text.split('。')[0]})
            gid = autonomy.create_goal(store, sid, '第 2 批主料 2 公斤，核對總重。')
            wrong = {**versions[0], 'quote': '欄位「第 3 批配料」為 600 公克'}
            expr = {'operation': 'sum', 'values': ['2', '0.6']}
            waiting = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read_ranges', ranges=ranges),
                action('check_quantities', unit='公斤', field=field, candidates=[wrong, versions[1]]),
                action('check_quantities', unit='公斤', field=field, candidates=versions),
                action('calculate_expression', expression=expr, quantity_inputs=[{'path': '/values/1', 'step': 3, 'candidate': 2}]),
                action('ask', question='採哪版或正式配料數？')))
            self.assertIn('唯一明列欄位', waiting['steps'][1]['observation']['error'])
            checked = waiting['steps'][2]['observation']
            self.assertEqual((checked['field'], checked['possible_exact'], checked['status']), (field, ['2/5', '3/5'], 'different'))
            for quantity, text in zip(checked['quantities'], docs, strict=True):
                self.assertEqual(text[quantity['field_start']:quantity['field_end']], field)
            self.assertIn('候選數量尚有差異', waiting['steps'][3]['observation']['error'])
            snapshot = goal_replies.snapshot(store, sid, gid)
            reply = '正式更正欄位「第 2 批配料」為 0.75 公斤。'
            goal_replies.save(store, sid, gid, reply, expected_fingerprint=snapshot['fingerprint'])
            binding = {'path': '/values/1', 'step': 3, 'replacement': {'citation': '[補充#5]', 'quote': reply}}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_expression', expression=expr, quantity_inputs=[binding]),
                action('calculate_expression', expression={**expr, 'values': ['2', '0.75']}, quantity_inputs=[binding]),
                action('finish', report='正式總重 2.75 公斤。')))
            self.assertIn('與已確認的精確數量不符', result['steps'][5]['observation']['error'])
            calculated = result['steps'][6]['observation']
            self.assertEqual(calculated['exact'], '11/4')
            proof, = calculated['quantity_inputs']
            self.assertEqual(proof['step'], 3)
            self.assertEqual(proof['replacement']['quote'], reply)
            quantity = proof['quantity']
            self.assertEqual((quantity['field'], quantity['converted_exact']), (field, '3/4'))
            self.assertEqual(reply[quantity['field_start']:quantity['field_end']], field)
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            altered = copy.deepcopy(calculated)
            altered['quantity_inputs'][0]['quantity']['field_end'] -= 1
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=7',
                           (json.dumps(altered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '保存的算式與數量核對'):
                validate_database(restored.path)

    def test_replacement_rejects_conditional_wrong_field_and_incomplete_quantity(self):
        good = '正式更正欄位「本批退回」為 7 件。'
        for text, quote in (('如果核對通過。' + good, good), (good + '仍待確認。', good),
                            (good.replace('本批', '另批'), good.replace('本批', '另批')),
                            (good.replace(' 件', ''), good.replace(' 件', ''))):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '同一欄位'):
                quantity_evidence._replacement_quantity({'citation': '[補充#3]', 'quote': quote},
                                                        '本批退回', '件', (text, 'a' * 64, 3))

    def test_calculation_reuses_pending_quantity_with_later_unit_and_binds_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            path = root / 'shipping.txt'
            path.write_text('本次襯材 750（單位待填）。', encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, '核對本次公斤總重')
            row = {'label': '本次襯材', 'citation': f'[D{did}:L1]', 'quote': '本次襯材 750'}
            autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=1),
                action('check_quantities', unit='公斤', candidates=[row]),
                action('ask', question='襯材單位？')))
            waiting = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '本次襯材的單位是公克。', expected_fingerprint=waiting['fingerprint'])
            binding = {'path': '/values/1', 'step': 2, 'candidate': 1,
                       'unit_evidence': {'citation': '[補充#3]', 'quote': '本次襯材的單位是公克', 'unit': '公克'}}
            expr = {'operation': 'sum', 'values': ['2.4', '0.75']}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_expression', expression={**expr, 'values': ['2.4', '0.00075']}, quantity_inputs=[binding]),
                action('calculate_expression', expression=expr, quantity_inputs=[binding]),
                action('update_plan', items=[{'id': 1, 'task': '核對總重', 'state': 'done',
                                              'evidence_steps': [5], 'note': ''}], verify_delivery=True,
                       evidence_requirements=[{'id': 1, 'expression': expr}]),
                action('finish', report='3.15 公斤。')))
            self.assertIn('與已確認的精確數量不符', result['steps'][3]['observation']['error'])
            calculated = result['steps'][4]['observation']
            self.assertEqual(calculated['exact'], '63/20')
            proof, = calculated['quantity_inputs']
            self.assertEqual(proof['quantity']['converted_exact'], '3/4')
            self.assertEqual(proof['quantity']['unit_evidence']['source_sha256'], result['reply_sources'][0]['sha256'])
            self.assertEqual(result['work_plan']['evidence_matches'], [{'id': 1, 'expression_step': 5}])
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            altered = copy.deepcopy(calculated)
            altered['quantity_inputs'][0]['quantity']['factor_exact'] = '1/1000000'
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                           (json.dumps(altered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '保存的算式與數量核對'):
                validate_database(restored.path)

    def test_calculation_requires_explicit_matching_selection_for_different_values(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '本批進件 240 件，完成查驗 180 件。')
            candidates = [
                {'label': '進件件數', 'citation': '[目標]', 'quote': '進件 240 件'},
                {'label': '完成查驗件數', 'citation': '[目標]', 'quote': '完成查驗 180 件'}]
            base = {'path': '/values/0', 'step': 1, 'candidate': 2}
            expr = {'operation': 'product', 'values': ['180', '0.05']}
            waiting = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_quantities', unit='件', candidates=candidates),
                action('calculate_expression', expression=expr, quantity_inputs=[base]),
                action('ask', question='採哪個基數？')))
            self.assertIn('候選數量尚有差異', waiting['steps'][1]['observation']['error'])
            snapshot = goal_replies.snapshot(store, sid, gid)
            reply = '本次採完成查驗件數為基數。'
            goal_replies.save(store, sid, gid, reply, expected_fingerprint=snapshot['fingerprint'])
            selected = {**base, 'selection': {'citation': '[補充#3]', 'quote': reply}}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_expression', expression=expr, quantity_inputs=[{**selected, 'candidate': 1}]),
                action('calculate_expression', expression=expr, quantity_inputs=[selected]),
                action('finish', report='9 件。')))
            self.assertIn('唯一確認原候選', result['steps'][3]['observation']['error'])
            calculated = result['steps'][4]['observation']
            self.assertEqual(calculated['exact'], '9')
            self.assertEqual(calculated['quantity_inputs'][0]['selection']['quote'], reply)
            validate_database(store.path)

    def test_missing_unit_keeps_original_number_and_binds_later_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            path = root / 'weights.txt'
            path.write_text('本批主料 1.20 公斤，配料 450（單位待填）。', encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, '核對本批重量')
            original = {'label': '本批配料', 'citation': f'[D{did}:L1]', 'quote': '配料 450'}
            main = {'label': '主料', 'citation': f'[D{did}:L1]', 'quote': '本批主料 1.20 公斤'}
            initial = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=1),
                action('check_quantities', unit='公斤', candidates=[main, original]),
                action('ask', question='本批配料的單位？', partial_result='主料 1.20 公斤，配料 450 待補單位。')))
            first = initial['steps'][1]['observation']
            self.assertEqual(first['status'], 'needs_evidence')
            self.assertEqual(first['possible_exact'], ['6/5'])
            self.assertEqual((first['quantities'][1]['source_exact'], first['quantities'][1]['status']),
                             ('450', 'unit_unconfirmed'))
            snapshot = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '本批配料的單位是公克。', expected_fingerprint=snapshot['fingerprint'])
            resolved = {**original, 'unit_evidence': {'citation': '[補充#3]', 'quote': '本批配料的單位是公克',
                                                      'unit': '公克'}}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('check_quantities', unit='公斤', candidates=[resolved]),
                action('calculate_expression', expression={'operation': 'sum', 'values': ['1.2', '0.45']}),
                action('finish', report='本批 1.65 公斤。')))
            checked = result['steps'][3]['observation']
            self.assertEqual((checked['status'], checked['possible_exact']), ('confirmed', ['9/20']))
            proof = checked['quantities'][0]
            self.assertIsNone(proof['source_unit'])
            self.assertEqual(proof['effective_unit'], '公克')
            self.assertEqual(proof['source_sha256'], first['quantities'][1]['source_sha256'])
            self.assertEqual(proof['unit_evidence']['source_sha256'], result['reply_sources'][0]['sha256'])
            self.assertEqual(result['steps'][4]['observation']['exact'], '33/20')
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            altered = copy.deepcopy(checked)
            altered['quantities'][0]['unit_evidence']['quote_start'] = 1
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                           (json.dumps(altered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '數量核對與原話'):
                validate_database(restored.path)

    def test_exact_equivalence_conflict_and_rejects_a_clipped_unit(self):
        specs = [
            {'label': '第一份', 'citation': '[目標]', 'quote': '前段容積 0.7 公升'},
            {'label': '第二份', 'citation': '[補充#1]', 'quote': '前段容積 700 mL'}]
        steps = [{'sequence': 1, 'action': {'tool': 'ask'}, 'observation': {}}]
        reply = {'citation': '[補充#1]', 'ask_sequence': 1, 'answer': '前段容積 700 mL。', 'sha256': 'a' * 64}
        args = {'unit': '公升', 'candidates': specs}
        equal = quantity_evidence.observe(args, steps, [], '前段容積 0.7 公升。', [reply])
        self.assertEqual((equal['status'], equal['possible_exact']), ('equivalent', ['7/10']))
        changed = {**reply, 'answer': '前段容積 750 mL。'}
        args['candidates'] = [specs[0], {**specs[1], 'quote': '前段容積 750 mL'}]
        unequal = quantity_evidence.observe(args, steps, [], '前段容積 0.7 公升。', [changed])
        self.assertEqual((unequal['status'], unequal['possible_exact']), ('different', ['7/10', '3/4']))
        with self.assertRaisesRegex(ValueError, '截斷'):
            quantity_evidence.observe({'unit': '公升', 'candidates': [
                {**specs[0], 'quote': '前段容積 0.7'}]}, steps, [], '前段容積 0.7 公升。')
        for text in ('不是公克。', '如果是A批，本批單位是公克。'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '完整且明確'):
                quantity_evidence.observe({'unit': '公斤', 'candidates': [
                    {'label': '本批配料', 'citation': '[目標]', 'quote': '配料 450',
                     'unit_evidence': {'citation': '[補充#1]', 'unit': '公克',
                                       'quote': '公克' if text.startswith('不是') else '本批單位是公克'}}]},
                    steps, [], '本批配料 450。', [{**reply, 'answer': text}])
