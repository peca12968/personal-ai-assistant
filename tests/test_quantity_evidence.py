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
    def test_quantity_branches_keep_independent_results_and_current_source_proofs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '本批退回'
            base = '本批原備 30 件。'
            first = f'版本「甲」欄位「{field}」為 4 件。'
            second = f'版本「乙」欄位「{field}」為 0 件。'
            approval = f'欄位「{field}」｜採用版本｜甲'
            other = f'欄位「{field}」｜採用版本｜丙'
            path = root / 'counts.txt'
            path.write_text('\n'.join((base, first, second, approval, other)), encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, f'數量採用範圍：欄位「{field}」｜[D{did}:L4-L5]\n核對各版本。')
            args = {'unit': '件', 'candidates': [
                {'label': name, 'citation': f'[D{did}:L{i}]', 'quote': quote}
                for i, (name, quote) in enumerate((('甲', first), ('乙', second)), 2)]}
            shared = {'path': '/values/0', 'citation': f'[D{did}:L1]', 'quote': base}
            expression = {'operation': 'quotient', 'values': ['30', '$candidate']}
            request = {'step': 2, 'expression': expression, 'operands': [shared]}
            self.assertRaisesRegex(ValueError, '一次', quantity_evidence.validate_branches,
                                   {**request, 'expression': {'operation': 'sum', 'values': ['$candidate', '$candidate']}})
            engine = ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=4),
                action('check_quantities', **args),
                action('calculate_quantity_branches', **{**request, 'operands': []}),
                action('calculate_quantity_branches', **request),
                action('read', document_id=did, start_line=5, end_line=5),
                action('calculate_quantity_branches', **request),
                action('check_quantities', **args),
                action('calculate_quantity_branches', **{**request, 'step': 7}),
                action('finish', report='甲可核算，丙缺資料。'))
            final = autonomy.run_goal(store, sid, gid, engine, max_steps=9)
            saved = [step['observation'] for step in final['steps']]
            self.assertIn('每個數字葉節點', saved[2]['error'])
            partial = saved[3]
            self.assertNotIn('exact', partial)
            self.assertEqual(partial['adoption']['missing'], [f'[D{did}:L5]'])
            self.assertEqual(partial['operands'][0]['number'], '30')
            self.assertEqual(partial['branches'][0]['calculation']['exact'], '15/2')
            failure = partial['branches'][1]
            self.assertEqual((failure['status'], failure['calculation_failure']['path']), ('calculation_failed', ''))
            self.assertNotIn('calculation', failure)
            self.assertIn('重新核對', saved[5]['error'])
            selected = saved[7]['branches']
            self.assertEqual([(row['version'], row['status']) for row in selected],
                             [('甲', 'calculated'), ('丙', 'candidate_unavailable')])
            self.assertEqual(selected[1]['adoption_citations'], [f'[D{did}:L5]'])
            self.assertNotIn('quantity', selected[1])
            self.assertIn('calculate_quantity_branches一次', engine.prompts[2])
            handoff = autonomy.create_goal(store, sid, '下一階段', parent_id=gid)
            carry = autonomy.get_goal(store, sid, handoff)['previous_stage']
            self.assertNotIn('"branches"', json.dumps(carry))
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), final)
            tampered = copy.deepcopy(saved[7])
            tampered['branches'][0]['calculation']['exact'] = '30'
            with restored.connect() as db:
                db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=8',
                           (json.dumps(tampered, ensure_ascii=False), gid))
            with self.assertRaisesRegex(ValueError, '保存的分版算式'):
                validate_database(restored.path)

    def test_quantity_branches_preserve_missing_unit_and_reject_unread_shared_operand(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '溪岸本批扣除'
            first = f'版本「現場」欄位「{field}」為 500 公克。'
            second = f'版本「重驗」欄位「{field}」為 750。'
            shared = '溪岸本批原備 3 公斤。'
            path = root / 'weights.txt'
            path.write_text('\n'.join((first, second, shared, '欄位「另批扣除」｜採用版本｜現場')), encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, f'數量採用範圍：欄位「{field}」｜[D{did}:L4]\n核對各版本。')
            args = {'step': 2, 'expression': {'operation': 'difference', 'values': ['3', '$candidate']},
                    'operands': [{'path': '/values/0', 'citation': f'[D{did}:L3]', 'quote': shared}]}
            result = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read_ranges', ranges=[{'document_id': did, 'start_line': start, 'end_line': end}
                                              for start, end in ((1, 2), (4, 4))]),
                action('check_quantities', unit='公斤', candidates=[
                    {'label': name, 'citation': f'[D{did}:L{i}]', 'quote': quote}
                    for i, (name, quote) in enumerate((('現場', first), ('重驗', second)), 1)]),
                action('calculate_quantity_branches', **args),
                action('read', document_id=did, start_line=3, end_line=3),
                action('calculate_quantity_branches', **args)), max_steps=5)
            self.assertIn('完整的查讀文字', result['steps'][2]['observation']['error'])
            evidence = result['steps'][4]['observation']
            self.assertEqual(evidence['adoption']['status'], 'unrecorded')
            self.assertEqual(evidence['adoption']['records'][0]['field'], '另批扣除')
            verified, unknown = evidence['branches']
            self.assertEqual((verified['quantity']['converted_exact'], verified['calculation']['exact']), ('1/2', '5/2'))
            self.assertEqual((unknown['status'], unknown['quantity']['number']), ('unit_unconfirmed', '750'))
            self.assertNotIn('calculation', unknown)
            snap = goal_guidance.snapshot(store, sid, gid)
            goal_guidance.save(store, sid, gid, '改用公斤再確認。', expected_fingerprint=snap['fingerprint'])
            changed = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_quantity_branches', **args)), max_steps=1)
            self.assertIn('重新核對', changed['steps'][-1]['observation']['error'])
            validate_database(store.path)

    def test_declared_scope_rejects_subsets_and_keeps_conflict_across_documents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '冷杉本批退回'
            lines = [f'版本「甲版」欄位「{field}」為 6 件。', f'版本「乙版」欄位「{field}」為 9 件。',
                     f'欄位「{field}」｜採用版本｜甲版', f'欄位「{field}」｜採用版本｜乙版']
            ranges = []
            for i, line in enumerate(lines, 1):
                path = root / f'scope-{i}.txt'
                path.write_text(line, encoding='utf-8')
                ranges.append({'document_id': store.attach_document(sid, path), 'start_line': 1, 'end_line': 1})
            refs = [f'[D{row["document_id"]}:L1]' for row in ranges]
            prefix = f'數量採用範圍：欄位「{field}」｜'
            with self.assertRaisesRegex(ValueError, '目標首行'):
                autonomy.create_goal(store, sid, prefix + 'D3:L1')
            with self.assertRaisesRegex(ValueError, '重複'):
                autonomy.create_goal(store, sid, prefix + '、'.join([refs[2]] * 2))
            with self.assertRaisesRegex(ValueError, '目前對話'):
                autonomy.create_goal(store, store.new_session(), prefix + refs[2])
            self.assertEqual(autonomy.goals(store, sid), [])
            gid = autonomy.create_goal(store, sid, prefix + '、'.join(refs[2:]) + '\r\n核對本批。')
            declared = autonomy.get_goal(store, sid, gid)['quantity_adoption_scope']
            self.assertEqual(declared, {'field': field, 'ranges': ranges[2:], 'missing_ranges': ranges[2:]})
            args = {'unit': '件', 'candidates': [
                {'label': label, 'citation': refs[i], 'quote': lines[i]} for i, label in enumerate(('甲版', '乙版'))]}
            def bind(step):
                return action('calculate_expression', expression='6',
                              quantity_inputs=[{'path': '', 'step': step, 'candidate': 1}])
            engine = ScriptedEngine(
                action('read_ranges', ranges=ranges[:3]),
                action('check_quantities', **args, field=field, adoption_ranges=ranges[2:3]),
                action('check_quantities', **args),
                bind(3), action('read_ranges', ranges=ranges[3:]),
                action('check_quantities', **args, field=field, adoption_ranges=ranges[2:][::-1]),
                bind(6), action('finish', report='兩份採用原文衝突，兩個候選各自保留。'))
            result = autonomy.run_goal(store, sid, gid, engine, max_steps=8)
            steps = [step['observation'] for step in result['steps']]
            self.assertIn('宣告的全部區段', steps[1]['error'])
            self.assertEqual(steps[2]['field'], field)
            partial = steps[2]['adoption']
            self.assertEqual(partial['declared_ranges'], ranges[2:])
            self.assertEqual((partial['status'], partial['missing']), ('incomplete', refs[3:]))
            self.assertEqual([row['quote'] for row in partial['records']], lines[2:3])
            self.assertIn('採用缺漏或有衝突', steps[3]['error'])
            self.assertEqual(steps[5]['adoption']['status'], 'conflicting')
            self.assertEqual([row['quote'] for row in steps[5]['adoption']['records']], lines[2:])
            self.assertIn('採用缺漏或有衝突', steps[6]['error'])
            context = json.loads(engine.prompts[2].split('目標與已觀察資料：\n')[-1])
            self.assertEqual(context['quantity_adoption_scope']['missing_ranges'], ranges[3:])
            self.assertEqual(result['quantity_adoption_scope']['missing_ranges'], [])
            archive = create_backup(store.path)
            restore_backup(archive['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), result)
            with restored.connect() as db:
                db.execute('DELETE FROM autonomous_quantity_scopes WHERE goal_id=?', (gid,))
            with self.assertRaisesRegex(ValueError, '保存的數量核對'):
                validate_database(restored.path)

    def test_declared_scope_adoption_binds_value_with_later_unit_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            field = '霧松本批退回'
            first = f'版本「早班」欄位「{field}」為 5 件。'
            second = f'版本「複驗」欄位「{field}」為 8。'
            approval = f'欄位「{field}」｜採用版本｜複驗'
            other = '欄位「霧松下批退回」｜採用版本｜早班'
            path = root / 'counts.txt'
            path.write_text('\n'.join((first, second, approval, other)), encoding='utf-8')
            did = store.attach_document(sid, path)
            gid = autonomy.create_goal(store, sid, f'數量採用範圍：欄位「{field}」｜[D{did}:L3-L4]\n核對可交量。')
            waiting = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('read', document_id=did, start_line=1, end_line=4),
                action('check_quantities', unit='件', candidates=[
                    {'label': name, 'citation': f'[D{did}:L{i}]', 'quote': quote}
                    for i, (name, quote) in enumerate((('早班', first), ('複驗', second)), 1)]),
                action('ask', question='複驗版退回的單位是什麼？')))
            checked = waiting['steps'][1]['observation']['adoption']
            self.assertEqual((checked['status'], checked['candidate']), ('adopted', 2))
            self.assertEqual([row['quote'] for row in checked['records']], [approval, other])
            reply = f'{field}的單位是件。'
            snap = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, reply, expected_fingerprint=snap['fingerprint'])
            final = autonomy.run_goal(store, sid, gid, ScriptedEngine(
                action('calculate_expression', expression='8', quantity_inputs=[{
                    'path': '', 'step': 2, 'candidate': 2,
                    'unit_evidence': {'citation': '[補充#3]', 'quote': reply, 'unit': '件'}}]),
                action('finish', report='確認複驗版退回8件。')))
            result = final['steps'][3]['observation']
            self.assertEqual(result['exact'], '8')
            self.assertEqual(result['quantity_inputs'][0]['adoption'], checked)
            validate_database(store.path)

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
