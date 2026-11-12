import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, work_plan as p, goal_guidance, goal_replies, goal_stages, operand_evidence
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def item(number=1, task='計算總費用', state='pending', refs=None, note=''):
    return {'id': number, 'task': task, 'state': state, 'evidence_steps': refs or [], 'note': note}


class WorkPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.gid = a.create_goal(self.store, self.sid, '計算12個月費用')

    def run_actions(self, *actions, gid=None):
        return a.run_goal(self.store, self.sid, gid or self.gid, ScriptedEngine(*actions), max_steps=len(actions))

    def test_real_loop_stores_versions_and_planning_is_not_delivery(self):
        goal = self.run_actions(action('update_plan', items=[item(state='working')]),
            action('calculate', operation='product', values=['120', '12']),
            action('update_plan', items=[item(state='done', refs=[2], note='1440元')]))
        self.assertEqual(goal['status'], 'paused')
        self.assertEqual(goal['result'], '')
        self.assertEqual(goal['work_plan']['items'][0]['state'], 'done')
        self.assertTrue(goal['work_plan']['conditions_current'])
        self.assertEqual(goal['steps'][0]['observation']['items'][0]['state'], 'working')
        engine = ScriptedEngine(action('finish', report='12個月為1440元。'))
        result = a.run_goal(self.store, self.sid, self.gid, engine)
        payload = json.loads(engine.prompts[0].split('目標與已觀察資料：\n')[1])
        self.assertEqual(payload['work_plan'], goal['work_plan'])
        self.assertEqual(result['status'], 'report_ready')

    def test_schema_bounds_reject_duplicate_ids_unsupported_fields_and_multiple_working(self):
        bad = [{'items': []}, {'items': [item()] * 2}, {'items': [item(i) for i in range(1, 10)]},
               {'items': [item(state='finished')]}, {'items': [item(state='done')]},
               {'items': [item(state='blocked')]}, {'items': [item(state='superseded')]},
               {'items': [item(1, state='working'), item(2, state='working')]},
               {'items': [item(refs=[True])]}, {'items': [item(refs=[1, 1])]},
               {'items': [item(refs=list(range(1, 8)))]}, {'items': [item(task='x'*301)]},
               {'items': [item(note='x'*401)]}, {'items': [dict(item(), command='run')]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.validate(value)

    def test_done_cannot_use_missing_plan_failed_partial_or_legacy_evidence(self):
        scope = 'a'*64
        examples = [[], [{'sequence': 1, 'action': {'tool': 'update_plan'}, 'observation': {'status':'plan_recorded','items':[item()], 'goal_conditions_sha256':scope}}],
            [{'sequence': 1, 'action': {'tool':'calculate'}, 'observation': {'exact':'1'}}],
            [{'sequence': 1, 'action': {'tool':'execute_csv'}, 'observation': {'status':'failed','goal_conditions_sha256':scope}}],
            [{'sequence': 1, 'action': {'tool':'read'}, 'observation': {'requested_ranges':[{'status':'partial'}], 'goal_conditions_sha256':scope}}]]
        for steps in examples:
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                p.observe({'items':[item(state='done',refs=[1])]}, steps, scope)
        goal = self.run_actions(action('calculate', operation='quotient', values=['1','0']),
            action('update_plan', items=[item(state='blocked',refs=[1],note='計算除以零，需修正分母。')]))
        self.assertEqual(goal['work_plan']['items'][0]['state'], 'blocked')
        self.assertEqual(goal['status'], 'paused')  # A blocked item does not ask or execute anything by itself.

    def test_items_cannot_disappear_or_change_identity_but_can_be_superseded(self):
        self.run_actions(action('update_plan', items=[item(),item(2,'整理報告')]))
        for rows in ([item()], [item(task='悄悄改成別件工作'),item(2,'整理報告')]):
            result = self.run_actions(action('update_plan', items=rows))
            self.assertEqual(result['steps'][-1]['action']['tool'], 'invalid')
        rows = [item(state='superseded',note='新的補充要求改算6個月'),item(2,'整理報告'),item(3,'改算6個月')]
        result = self.run_actions(action('update_plan', items=rows))
        self.assertEqual(len(result['work_plan']['items']), 3)
        self.assertEqual(result['work_plan']['items'][0]['task'], '計算總費用')

    def test_guidance_invalidates_old_completion_and_requires_new_tool_evidence(self):
        self.run_actions(action('calculate', operation='product', values=['120','12']),
            action('update_plan', items=[item(state='done', refs=[1])]))
        original = a.get_goal(self.store, self.sid, self.gid)
        snapshot = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, '更正：改算6個月。', expected_fingerprint=snapshot['fingerprint'])
        current = a.get_goal(self.store, self.sid, self.gid)
        self.assertFalse(current['work_plan']['conditions_current'])
        self.assertEqual(current['steps'], original['steps'])
        result = self.run_actions(action('update_plan', items=[item(state='done', refs=[1])]),
            action('calculate', operation='product', values=['120','6']),
            action('update_plan', items=[item(state='done', refs=[4], note='依新條件重算720元。')]))
        self.assertEqual(result['steps'][2]['action']['tool'], 'invalid')
        self.assertTrue(result['work_plan']['conditions_current'])
        self.assertEqual(result['work_plan']['items'][0]['evidence_steps'], [4])

    def test_unchanged_read_is_reused_while_corrected_calculation_requires_fresh_step(self):
        file = self.root / 'stock.txt'
        file.write_text('甲批30件，原記3件不可用。\n乙批24件。', encoding='utf-8')
        did = self.store.attach_document(self.sid, file)
        gid = a.create_goal(self.store, self.sid, '核對原文件及甲批可出數量')
        read_item = item(1, '核對文件原值', 'done', [1])
        calculation = item(2, '計算甲批數量', 'done', [2])
        initial = self.run_actions(
            action('read_ranges', ranges=[{'document_id': did, 'start_line': 1, 'end_line': 2}]),
            action('calculate', operation='difference', values=['30', '3']),
            action('update_plan', items=[read_item, calculation]), gid=gid)
        observed = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '更正甲批不可用為4件。',
                           expected_fingerprint=observed['fingerprint'])
        pending = a.get_goal(self.store, self.sid, gid)
        self.assertFalse(pending['work_plan']['conditions_current'])
        self.assertEqual(pending['steps'], initial['steps'])
        result = self.run_actions(
            action('update_plan', items=[read_item, calculation], reuse_document_steps=[1, 2]),
            action('calculate', operation='difference', values=['30', '4']),
            action('update_plan', items=[read_item, item(2, '計算甲批數量', 'done', [5])],
                   reuse_document_steps=[1]), gid=gid)
        self.assertEqual(result['steps'][3]['action']['tool'], 'invalid')
        self.assertEqual(result['steps'][4]['observation']['exact'], '26')
        self.assertEqual(result['steps'][:3], initial['steps'])
        self.assertTrue(result['work_plan']['conditions_current'])
        proof, = result['work_plan']['reused_document_steps']
        self.assertEqual((proof['sequence'], proof['document_ids']), (1, [did]))
        self.assertIn('沿用同版本文件查讀 · 步驟 1', p.describe(result['work_plan']))
        self.assertNotIn('excerpts', result['steps'][-1]['observation'])

        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        with restored.connect() as db:
            changed = copy.deepcopy(result['steps'][0]['observation'])
            changed['excerpts'][0]['text'] += '篡改'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=1',
                       (json.dumps(changed, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_guarded_finish_blocks_stale_plan_then_links_current_evidence(self):
        file = self.root / 'count.txt'
        file.write_text('原數32件，報廢5件。', encoding='utf-8')
        did = self.store.attach_document(self.sid, file)
        gid = a.create_goal(self.store, self.sid, '核對可交件數')
        reading = item(1, '核對原文', 'done', [1])
        old = item(2, '計算可交件數', 'done', [2])
        self.run_actions(
            action('read', document_id=did, start_line=1, end_line=1),
            action('calculate', operation='difference', values=['32', '5']),
            action('update_plan', items=[reading, old], verify_delivery=True), gid=gid)
        snapshot = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '本批報廢改為7件。', expected_fingerprint=snapshot['fingerprint'])
        pending = a.get_goal(self.store, self.sid, gid)['work_plan']
        self.assertEqual(pending['delivery_review'], {'ready': False, 'open_items': [], 'recheck_items': [2],
            'reusable_documents': [{'id': 1, 'evidence_steps': [1]}]})
        result = self.run_actions(
            action('finish', report='27件。'),
            action('calculate', operation='difference', values=['32', '7']),
            action('update_plan', items=[reading, item(2, '計算可交件數', 'done', [5])], reuse_document_steps=[1]),
            action('finish', report='25件。'), gid=gid)
        self.assertEqual(result['steps'][3]['action']['tool'], 'invalid')
        self.assertIn('仍依舊條件', result['steps'][3]['observation']['error'])
        self.assertEqual(result['steps'][4]['observation']['exact'], '25')
        self.assertTrue(result['work_plan']['verify_delivery'])
        self.assertTrue(result['work_plan']['delivery_review']['ready'])
        self.assertEqual(result['steps'][-1]['observation']['plan_check_step'], 6)
        self.assertEqual(result['result'], '25件。')
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = {**result['steps'][-1]['observation'], 'plan_check_step': 3}
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=7',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '交付步驟與保存的清單核對'):
            validate_database(restored.path)

    def test_guarded_file_delivery_rejects_open_plan_and_old_file(self):
        gid = a.create_goal(self.store, self.sid, '依清單交付月費', file_delivery=True)
        initial = self.run_actions(
            action('update_plan', items=[item()], verify_delivery=True),
            action('write_artifact', filename='premature.txt', content='不應建立'),
            action('calculate', operation='product', values=['120', '12']),
            action('update_plan', items=[item(state='done', refs=[3])]),
            action('write_artifact', filename='old.txt', content='1440元'), gid=gid)
        self.assertIn('未完成項目 1', initial['steps'][1]['observation']['error'])
        self.assertEqual([file['name'] for file in initial['artifacts']], ['old.txt'])
        snapshot = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '改算6個月。', expected_fingerprint=snapshot['fingerprint'])
        result = self.run_actions(
            action('calculate', operation='product', values=['120', '6']),
            action('update_plan', items=[item(state='done', refs=[6])]),
            action('deliver', report='舊版', filenames=['old.txt']),
            action('write_artifact', filename='new.txt', content='720元'),
            action('deliver', report='本次720元', filenames=['new.txt']), gid=gid)
        self.assertIn('缺少目前條件下的清單核對', result['steps'][7]['observation']['error'])
        self.assertEqual(result['steps'][8]['observation']['plan_check_step'], 7)
        self.assertEqual(result['steps'][9]['observation']['plan_check_step'], 7)
        self.assertEqual(result['result'], '本次720元')
        validate_database(self.store.path)
        with self.assertRaisesRegex(ValueError, '只能明確開啟'):
            p.validate({'items': [item()], 'verify_delivery': False})

    def _two_document_calculations(self):
        requirements = []
        tasks = []
        for number, name, total, damage in ((1, '東架', '36', '8'), (2, '西架', '25', '6')):
            path = self.root / f'shelf-{number}.txt'
            path.write_text(f'{name}原有 {total} 盒，破損 {damage} 盒。', encoding='utf-8')
            did = self.store.attach_document(self.sid, path)
            source = {'document_id': did, 'start_line': 1, 'end_line': 1}
            citation = f'[D{did}:L1]'
            requirements.append({'id': number, 'ranges': [source],
                                 'expression': {'operation': 'difference', 'values': [total, damage]},
                                 'operands': [{'path': '/values/0', 'citation': citation, 'quote': f'{name}原有 {total} 盒'},
                                              {'path': '/values/1', 'citation': citation, 'quote': f'破損 {damage} 盒'}]})
            tasks.append(item(number, name + '可交量', 'done', [1, number + 1]))
        gid = a.create_goal(self.store, self.sid, '核對東西架的可交盒數後交檔。', file_delivery=True)
        self.run_actions(action('read_ranges', ranges=[row['ranges'][0] for row in requirements]),
                         *(action('calculate_expression', expression=row['expression']) for row in requirements),
                         action('update_plan', items=tasks, verify_delivery=True, evidence_requirements=requirements), gid=gid)
        return gid, tasks, requirements

    def test_confirmed_calculation_reuse_keeps_operand_proof_and_requires_fresh_artifact(self):
        gid, tasks, requirements = self._two_document_calculations()
        initial = self.run_actions(action('write_artifact', filename='draft.md', content='東28盒，西19盒。'),
                                   action('ask', question='最後有更正嗎？', partial_result='暫表東28盒，西19盒。'), gid=gid)
        self.assertEqual(initial['status'], 'needs_input')
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '東架破損更正為 9 盒。\n「西架可交量」的數量與算式均照舊。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snap['fingerprint'])
        pending = a.get_goal(self.store, self.sid, gid)
        proof = {'id': 2, 'expression_step': 3, 'prior_plan_step': 4, 'citation': '[補充#6]',
                 'source_sha256': pending['reply_sources'][0]['sha256']}
        review = pending['work_plan']['delivery_review']
        self.assertEqual(review['recheck_items'], [1])
        self.assertEqual(review['reusable_calculations'], [{**proof, 'evidence_steps': [1, 3]}])
        east = copy.deepcopy(requirements[0])
        east['expression']['values'][1] = '9'
        east['operands'][1].update(citation='[補充#6]', quote='東架破損更正為 9 盒')
        current = [{**tasks[0], 'evidence_steps': [1, 8]}, tasks[1]]
        final = self.run_actions(action('deliver', report='舊檔', filenames=['draft.md']),
                                 action('calculate_expression', expression=east['expression']),
                                 action('update_plan', items=current, reuse_document_steps=[1], evidence_requirements=[east]),
                                 action('update_plan', items=current, reuse_document_steps=[1]),
                                 action('deliver', report='舊檔', filenames=['draft.md']),
                                 action('write_artifact', filename='final.md', content='東27盒 [補充#6]，西19盒。'),
                                 action('deliver', report='交付目前結果。', filenames=['final.md']), gid=gid)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('仍依舊條件', final['steps'][6]['observation']['error'])
        self.assertIn('重新產檔', final['steps'][10]['observation']['error'])
        self.assertEqual(final['steps'][-1]['observation']['plan_check_step'], 10)
        self.assertEqual(final['work_plan']['reused_calculations'], [proof])
        self.assertEqual(final['work_plan']['evidence_matches'][1], initial['work_plan']['evidence_matches'][1])
        self.assertIn('依使用者確認沿用算式 · 項目 2 [補充#6]', p.describe(final['work_plan']))
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), final)
        altered = copy.deepcopy(final['steps'][8]['observation'])
        altered['reused_calculations'][0]['source_sha256'] = 'a' * 64
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=9',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_blocked_item_partial_requirements_preserve_original_scope_and_delivery_guard(self):
        gid, tasks, requirements = self._two_document_calculations()
        original = a.get_goal(self.store, self.sid, gid)['work_plan']['evidence_requirements'][1]
        amendment = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '東架破損更正為 9 盒，西架仍待確認。',
                           expected_fingerprint=amendment['fingerprint'])
        east, west = copy.deepcopy(requirements)
        east['expression']['values'][1] = '9'
        east['operands'][1].update(citation='[更正#1]', quote='東架破損更正為 9 盒')
        partial = {'id': 2, 'ranges': west['ranges']}
        waiting_items = [{**tasks[0], 'evidence_steps': [1, 5]},
                         item(2, tasks[1]['task'], 'blocked', [1], '等待西架正式破損數')]
        waiting = self.run_actions(
            action('calculate_expression', expression=east['expression']),
            action('update_plan', items=waiting_items, reuse_document_steps=[1], evidence_requirements=[east, partial]),
            action('write_artifact', filename='early.md', content='未定稿'),
            action('ask', question='西架正式破損幾盒？', partial_result='東架27盒，西架待確認。'), gid=gid)
        waiting_plan = waiting['work_plan']
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertEqual(waiting_plan['evidence_requirements'][1], original)
        self.assertNotEqual(waiting_plan['goal_conditions_sha256'], original['defined_scope'])
        self.assertEqual([row['id'] for row in waiting_plan['evidence_matches']], [1])
        self.assertIn('未完成項目 2', waiting['steps'][6]['observation']['error'])
        self.assertFalse(waiting['artifacts'])
        changed = {**partial, 'ranges': east['ranges']}
        with self.assertRaisesRegex(ValueError, '已保存指定證據'):
            p.observe({'items': waiting_items, 'evidence_requirements': [changed]},
                      waiting['steps'], waiting_plan['goal_conditions_sha256'], waiting['sources'])

        snap = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '西架正式破損 8 盒。', expected_fingerprint=snap['fingerprint'])
        west['expression']['values'][1] = '8'
        west['operands'][1].update(citation='[補充#8]', quote='西架正式破損 8 盒')
        complete = [{**task, 'evidence_steps': [1, ref]} for task, ref in zip(tasks, [9, 10], strict=True)]
        result = self.run_actions(
            action('calculate_expression', expression=east['expression']),
            action('calculate_expression', expression=west['expression']),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[east, partial]),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[east]),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[east, west]),
            action('write_artifact', filename='final.md', content='東27盒，西17盒。'),
            action('deliver', report='正式結果已核對。', filenames=['final.md']), gid=gid)
        self.assertIn('已保存指定證據', result['steps'][10]['observation']['error'])
        self.assertIn('算式要求來自舊條件', result['steps'][11]['observation']['error'])
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['steps'][-1]['observation']['plan_check_step'], 13)
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = copy.deepcopy(result['steps'][5]['observation'])
        altered['evidence_requirements'][1]['defined_scope'] = altered['goal_conditions_sha256']
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_group_confirmation_reuses_an_independent_prior_round_and_rechecks_new_sources(self):
        gid, tasks, requirements = self._two_document_calculations()
        self.run_actions(action('ask', question='最後有更正嗎？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '東架破損更正為 9 盒，西架待確認。',
                          expected_fingerprint=snap['fingerprint'])
        east, west = copy.deepcopy(requirements)
        east['expression']['values'][1] = '9'
        east['operands'][1].update(citation='[補充#5]', quote='東架破損更正為 9 盒')
        waiting_items = [{**tasks[0], 'evidence_steps': [1, 6]},
                         item(2, tasks[1]['task'], 'blocked', [1], '等待西架正式破損數')]
        self.run_actions(action('calculate_expression', expression=east['expression']),
                         action('update_plan', items=waiting_items, reuse_document_steps=[1], evidence_requirements=[east]),
                         action('ask', question='西架正式破損幾盒？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '西架正式破損 8 盒。\n本次只更正「西架可交量」，其餘項目的數量與算式均照舊。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snap['fingerprint'])
        pending = a.get_goal(self.store, self.sid, gid)
        review = pending['work_plan']['delivery_review']
        proof = {'id': 1, 'expression_step': 6, 'prior_plan_step': 7, 'changed_item': 2,
                 'citation': '[補充#8]', 'source_sha256': pending['reply_sources'][-1]['sha256']}
        self.assertEqual(review['open_items'], [2])
        self.assertEqual(review['recheck_items'], [])
        self.assertEqual(review['reusable_calculations'], [{**proof, 'evidence_steps': [1, 6]}])
        west['expression']['values'][1] = '8'
        west['operands'][1].update(citation='[補充#8]', quote='西架正式破損 8 盒')
        complete = [waiting_items[0], {**tasks[1], 'evidence_steps': [1, 9]}]
        overlap = {**west, 'ranges': west['ranges'] + east['ranges']}
        result = self.run_actions(
            action('calculate_expression', expression=west['expression']),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[overlap]),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[west]),
            action('write_artifact', filename='final.md', content='東27盒，西17盒。'),
            action('deliver', report='正式結果已核對。', filenames=['final.md']), gid=gid)
        self.assertIn('與更正項的指定文件行範圍重疊', result['steps'][9]['observation']['error'])
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['work_plan']['reused_calculations'], [proof])
        self.assertEqual(result['work_plan']['evidence_matches'][0], pending['work_plan']['evidence_matches'][0])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = copy.deepcopy(result['steps'][10]['observation'])
        altered['reused_calculations'][0]['changed_item'] = 1
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=11',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_group_confirmation_does_not_cover_overlapping_old_ranges_or_operand_citations(self):
        west_range = {'document_id': 1, 'start_line': 2, 'end_line': 4}
        east = {'ranges': [{'document_id': 1, 'start_line': 1, 'end_line': 2}],
                'operands': [{'citation': '[補充#1]'}]}
        west = {'ranges': [west_range], 'operands': [{'citation': '[補充#2]'}]}
        self.assertEqual(p._group_reuse_blocker(east, west), 'shared_document_ranges')
        west_range['start_line'] = 3
        self.assertIsNone(p._group_reuse_blocker(east, west))
        west['operands'][0]['citation'] = '[補充#1]'
        self.assertEqual(p._group_reuse_blocker(east, west), 'shared_operand_citations')
        self.assertEqual(p._group_reuse_blocker(east, {}), 'missing_source_requirements')

    def test_group_recheck_previews_shared_reply_and_preserves_original_read_coverage(self):
        gid, tasks, requirements = self._two_document_calculations()
        self.run_actions(action('ask', question='早班破損數更正？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '兩架共用扣除 7 盒。',
                          expected_fingerprint=snap['fingerprint'])
        east, west = copy.deepcopy(requirements)
        for spec in (east, west):
            spec['expression']['values'][1] = '7'
            spec['operands'][1].update(citation='[補充#5]', quote='兩架共用扣除 7 盒')
        early = [{**tasks[0], 'evidence_steps': [1, 6]}, {**tasks[1], 'evidence_steps': [1, 7]}]
        self.run_actions(action('calculate_expression', expression=east['expression']),
                         action('calculate_expression', expression=west['expression']),
                         action('update_plan', items=early, reuse_document_steps=[1], evidence_requirements=[east, west]),
                         action('ask', question='晚班有更正嗎？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '東架正式破損 10 盒。\n本次只更正「東架可交量」，其餘項目的數量與算式均照舊。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snap['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        plan = waiting['work_plan']
        review = plan['delivery_review']
        self.assertEqual(review['calculation_rechecks'], [
            {'id': 2, 'changed_item': 1, 'reason': 'shared_operand_citations'}])
        self.assertNotIn('reusable_calculations', review)
        self.assertEqual(review['reusable_documents'], [
            {'id': 1, 'evidence_steps': [1]}, {'id': 2, 'evidence_steps': [1]}])
        self.assertIn('項目 2 無法依整組確認沿用算式，與更正項的運算元引用同一來源（更正項 1）', p.describe(plan))
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), waiting)
        east['expression']['values'][1] = '10'
        east['operands'][1].update(citation='[補充#9]', quote='東架正式破損 10 盒')
        fresh_east = {**tasks[0], 'evidence_steps': [1, 10]}
        final = self.run_actions(
            action('calculate_expression', expression=east['expression']),
            action('update_plan', items=[fresh_east, early[1]], reuse_document_steps=[1], evidence_requirements=[east, west]),
            action('calculate_expression', expression=west['expression']),
            action('update_plan', items=[fresh_east, {**tasks[1], 'evidence_steps': [1, 12]}],
                   reuse_document_steps=[1], evidence_requirements=[east, west]), gid=gid)
        self.assertIn('與更正項的運算元引用同一來源', final['steps'][10]['observation']['error'])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        self.assertNotIn('calculation_rechecks', final['work_plan']['delivery_review'])
        validate_database(self.store.path)

    def test_group_reuse_separates_exact_excerpts_from_same_line_and_reply(self):
        line = '青洲原有 26 包，原列破損 4 包。竹洲原有 19 包，原列破損 3 包。'
        path = self.root / 'stock.txt'
        path.write_text(line, encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '交付兩批正式可交量')
        source = {'document_id': did, 'start_line': 1, 'end_line': 1}
        self.run_actions(action('read', **source), action('ask', question='早班數字？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '青洲破損 5 包。竹洲破損 6 包。',
                          expected_fingerprint=snap['fingerprint'])
        specs = [{'id': number, 'ranges': [source], 'expression': {'operation': 'difference', 'values': [base, damage]},
                  'operands': [{'path': '/values/0', 'citation': f'[D{did}:L1]', 'quote': f'{name}原有 {base} 包'},
                               {'path': '/values/1', 'citation': '[補充#2]', 'quote': f'{name}破損 {damage} 包'}]}
                 for number, name, base, damage in ((1, '青洲', '26', '5'), (2, '竹洲', '19', '6'))]
        early = [item(1, '青洲可交量', 'done', [1, 3]), item(2, '竹洲可交量', 'done', [1, 4])]
        self.run_actions(*(action('calculate_expression', expression=spec['expression']) for spec in specs),
                         action('update_plan', items=early, verify_delivery=True, reuse_document_steps=[1],
                                evidence_requirements=specs), action('ask', question='晚班數字？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '青洲正式破損 7 包。\n本次只更正「青洲可交量」，其餘項目的數量與算式均照舊。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snap['fingerprint'])
        plan = a.get_goal(self.store, self.sid, gid)['work_plan']
        preview, = plan['delivery_review']['reusable_calculations']
        self.assertTrue(preview['separate_operand_excerpts'])
        self.assertEqual((preview['id'], preview['changed_item'], preview['evidence_steps']), (2, 1, [1, 4]))
        current = copy.deepcopy(specs[0])
        current['expression']['values'][1] = '7'
        current['operands'][1].update(citation='[補充#6]', quote='青洲正式破損 7 包')
        broad = copy.deepcopy(current)
        broad['operands'][0]['quote'] = line
        complete = [{**early[0], 'evidence_steps': [1, 7]}, early[1]]
        result = self.run_actions(
            action('calculate_expression', expression=current['expression']),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[broad]),
            action('update_plan', items=complete, reuse_document_steps=[1], evidence_requirements=[current]),
            action('finish', report='青洲19包、竹洲13包。'), gid=gid)
        self.assertIn('指定文件行範圍重疊', result['steps'][7]['observation']['error'])
        self.assertEqual(result['status'], 'report_ready')
        receipt, = result['work_plan']['reused_calculations']
        self.assertTrue(receipt['separate_operand_excerpts'])
        self.assertEqual(result['work_plan']['evidence_matches'][1], plan['evidence_matches'][1])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = copy.deepcopy(result['steps'][8]['observation'])
        del altered['reused_calculations'][0]['separate_operand_excerpts']
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=9',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_group_reuse_blocks_copied_field_across_different_documents(self):
        ranges = []
        specs = []
        for number, name, count in ((1, '琥架', '4'), (2, '石架', '7')):
            path = self.root / f'field-{number}.txt'
            box_quote = f'欄位「{name}盒數」為 {count} 盒'
            shared_quote = '欄位「共用規格」為 12 片'
            path.write_text(box_quote + '。' + shared_quote + '。', encoding='utf-8')
            did = self.store.attach_document(self.sid, path)
            source = {'document_id': did, 'start_line': 1, 'end_line': 1}
            ranges.append(source)
            specs.append({'id': number, 'ranges': [source],
                          'expression': {'operation': 'product', 'values': [count, '12']},
                          'operands': [{'path': '/values/0', 'citation': f'[D{did}:L1]', 'quote': box_quote,
                                        'field': f'{name}盒數'},
                                       {'path': '/values/1', 'citation': f'[D{did}:L1]', 'quote': shared_quote,
                                        'field': '共用規格'}]})
        gid = a.create_goal(self.store, self.sid, '核對兩架片數')
        initial = [item(1, '琥架片數', 'done', [1, 2]), item(2, '石架片數', 'done', [1, 3])]
        self.run_actions(action('read_ranges', ranges=ranges),
                         *(action('calculate_expression', expression=spec['expression']) for spec in specs),
                         action('update_plan', items=initial, evidence_requirements=specs, verify_delivery=True),
                         action('ask', question='最後更正？'), gid=gid)
        snap = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '欄位「琥架盒數」為 5 盒。\n本次只更正「琥架片數」，其餘項目的數量與算式均照舊。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snap['fingerprint'])
        review = a.get_goal(self.store, self.sid, gid)['work_plan']['delivery_review']
        self.assertEqual(review['calculation_rechecks'], [{'id': 2, 'changed_item': 1, 'reason': 'shared_operand_fields'}])
        self.assertNotIn('reusable_calculations', review)
        specs[0]['expression']['values'][0] = '5'
        specs[0]['operands'][0].update(citation='[補充#5]', quote='欄位「琥架盒數」為 5 盒')
        current = {**initial[0], 'evidence_steps': [1, 6]}
        final = self.run_actions(action('calculate_expression', expression=specs[0]['expression']),
            action('update_plan', items=[current, initial[1]], reuse_document_steps=[1], evidence_requirements=specs),
            action('calculate_expression', expression=specs[1]['expression']),
            action('update_plan', items=[current, {**initial[1], 'evidence_steps': [1, 8]}],
                   reuse_document_steps=[1], evidence_requirements=specs),
            action('finish', report='琥60片、石84片。'), gid=gid)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('運算元使用同一個明列欄位', final['steps'][6]['observation']['error'])
        self.assertEqual(final['work_plan']['evidence_matches'][0]['operands'][0]['field'], '琥架盒數')
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), final)
        observation = copy.deepcopy(final['steps'][8]['observation'])
        observation['evidence_matches'][0]['operands'][0]['field_start'] += 1
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=9',
                       (json.dumps(observation, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_operand_field_anchors_to_the_matching_number_and_composes_with_conversion(self):
        source = '🧾欄位「原備」為 ５ 件。欄位「退回」為 ５ 件。欄位「重量」：50 公克。'
        row = {'path': '', 'citation': '[目標]', 'quote': '欄位「退回」為 ５ 件', 'field': '退回'}
        operand_evidence.validate([row], '5', ())
        texts = operand_evidence.user_texts(source, (), (), 1)
        proof, = operand_evidence.bind([row], '5', (), texts)
        self.assertEqual(source[proof['field_start']:proof['field_end']], '退回')
        self.assertEqual(source[proof['number_start']:proof['number_end']], '５')
        for invalid in ({**row, 'field': '原備'}, {**row, 'quote': '退回」為 ５ 件'},
                        {**row, 'quote': '欄位「退回」為 ５ 件', 'field': '退回 '}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '欄位名'):
                operand_evidence.validate([invalid], '5', ())
        converted = {**row, 'quote': '欄位「重量」：50 公克', 'field': '重量',
                     'conversion': {'from': '公克', 'to': '公斤'}}
        operand_evidence.validate([converted], '0.05', ())
        proof, = operand_evidence.bind([converted], '0.05', (), texts)
        self.assertEqual((source[proof['field_start']:proof['field_end']], proof['conversion']['converted_exact']),
                         ('重量', '1/20'))

    def test_reuse_requires_latest_unconditional_named_confirmation_and_unchanged_evidence(self):
        gid, tasks, requirements = self._two_document_calculations()
        affirmation = '「西架可交量」的數量與算式均照舊。'

        def amend(text):
            snap = goal_guidance.snapshot(self.store, self.sid, gid)
            goal_guidance.save(self.store, self.sid, gid, text, expected_fingerprint=snap['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)['work_plan']['delivery_review']

        self.assertEqual(amend(affirmation)['reusable_calculations'][0]['citation'], '[更正#1]')
        for uncertainty in ('其他照舊。', '如果覆核通過：\n' + affirmation,
                            '```\n' + affirmation + '\n```', affirmation + '\n西架可交量改用新數。'):
            with self.subTest(uncertainty=uncertainty):
                self.assertNotIn('reusable_calculations', amend(uncertainty))
        self.assertEqual(amend(affirmation)['recheck_items'], [1])
        changed = copy.deepcopy(requirements[1])
        changed['expression']['values'][0] = '25.0'
        east_pending = item(1, tasks[0]['task'])
        result = self.run_actions(action('update_plan', items=[east_pending, tasks[1]],
                                         reuse_document_steps=[1], evidence_requirements=[changed]),
                                  action('update_plan', items=[east_pending, {**tasks[1], 'evidence_steps': [1, 2]}],
                                         reuse_document_steps=[1]),
                                  action('update_plan', items=[east_pending, tasks[1]], reuse_document_steps=[1]), gid=gid)
        self.assertEqual([step['action']['tool'] for step in result['steps'][-3:]], ['invalid', 'invalid', 'update_plan'])
        self.assertEqual(result['work_plan']['reused_calculations'][0]['citation'], '[更正#6]')
        self.assertEqual(result['work_plan']['delivery_review']['open_items'], [1])
        validate_database(self.store.path)

    def test_equal_results_cannot_supply_the_other_items_expression(self):
        first = {'operation': 'difference', 'values': ['20', '4']}
        second = {'operation': 'difference', 'values': ['19', '3']}
        pending = [item(1, '計算竹穗件數'), item(2, '計算泉角件數')]
        requirements = [{'id': 1, 'expression': first}, {'id': 2, 'expression': second}]
        wrong = [item(1, '計算竹穗件數', 'done', [3]), item(2, '計算泉角件數', 'done', [2])]
        right = [item(1, '計算竹穗件數', 'done', [2]), item(2, '計算泉角件數', 'done', [3])]
        result = self.run_actions(
            action('update_plan', items=pending, evidence_requirements=requirements, verify_delivery=True),
            action('calculate_expression', expression=first),
            action('calculate_expression', expression=second),
            action('update_plan', items=wrong),
            action('update_plan', items=right),
            action('finish', report='兩批各16件。'))
        self.assertEqual([s['observation']['exact'] for s in result['steps'][1:3]], ['16', '16'])
        self.assertIn('項目 1 的步驟 3 與指定算式不符', result['steps'][3]['observation']['error'])
        self.assertEqual(result['work_plan']['evidence_matches'],
                         [{'id': 1, 'expression_step': 2}, {'id': 2, 'expression_step': 3}])
        self.assertIn('指定算式或原文已核對 · 項目 1、2', p.describe(result['work_plan']))
        self.assertEqual(result['steps'][-1]['observation']['plan_check_step'], 5)
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), result)
        altered = copy.deepcopy(result['steps'][4]['observation'])
        altered['evidence_matches'][0]['expression_step'] = 3
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=5',
                       (json.dumps(altered, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_typed_mixed_item_reuses_its_read_and_requires_current_expression(self):
        path = self.root / 'boxes.txt'
        path.write_text('原有3組，每組6個。\n窗口金同仁。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對紙盒及窗口')
        quantity = {'id': 1, 'ranges': [{'document_id': did, 'start_line': 1, 'end_line': 1}],
                    'expression': {'operation': 'product', 'values': ['3', '6']}}
        contact = {'id': 2, 'ranges': [{'document_id': did, 'start_line': 2, 'end_line': 2}]}
        self.run_actions(action('read', **quantity['ranges'][0]), action('read', **contact['ranges'][0]),
            action('update_plan', items=[item(1, '核對紙盒', refs=[1]), item(2, '核對窗口', 'done', [2])],
                   verify_delivery=True, evidence_requirements=[quantity, contact]), gid=gid)
        snapshot = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '本次改為4組。', expected_fingerprint=snapshot['fingerprint'])
        review = a.get_goal(self.store, self.sid, gid)['work_plan']['delivery_review']
        self.assertEqual(review['open_items'], [1])
        self.assertEqual(review['reusable_documents'], [
            {'id': 1, 'evidence_steps': [1]}, {'id': 2, 'evidence_steps': [2]}])
        updated = {**quantity, 'expression': {'operation': 'product', 'values': ['4', '6']}}
        wrong = [item(1, '核對紙盒', 'done', [2, 4]), item(2, '核對窗口', 'done', [2])]
        right = [item(1, '核對紙盒', 'done', [1, 4]), item(2, '核對窗口', 'done', [2])]
        result = self.run_actions(
            action('calculate_expression', expression=updated['expression']),
            action('update_plan', items=right, reuse_document_steps=[1, 2]),
            action('update_plan', items=wrong, reuse_document_steps=[2], evidence_requirements=[updated]),
            action('update_plan', items=right, reuse_document_steps=[1, 2], evidence_requirements=[updated]),
            action('finish', report='24個，窗口金同仁。'), gid=gid)
        self.assertIn('算式要求來自舊條件', result['steps'][4]['observation']['error'])
        self.assertIn('未查讀指定文件範圍', result['steps'][5]['observation']['error'])
        self.assertEqual(result['work_plan']['evidence_matches'],
                         [{'id': 1, 'read_steps': [1], 'expression_step': 4}, {'id': 2, 'read_steps': [2]}])
        self.assertEqual([row['sequence'] for row in result['work_plan']['reused_document_steps']], [1, 2])
        self.assertEqual(result['steps'][-1]['observation']['plan_check_step'], 7)
        validate_database(self.store.path)

    def test_operand_proofs_bind_document_reply_and_objective_and_restore(self):
        path = self.root / 'lengths.txt'
        path.write_text('📍本批有 8 片，每片長 1/2 公尺。\n另批每片長 0.5 公尺。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '本批倍數為 2，核對總長。')
        source_range = {'document_id': did, 'start_line': 1, 'end_line': 1}
        self.run_actions(action('read', **source_range), action('ask', question='另加長度？'), gid=gid)
        snapshot = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '本批另加 7 公尺。', expected_fingerprint=snapshot['fingerprint'])
        expression = {'operation': 'sum', 'values': [
            {'operation': 'product', 'values': ['8', '0.5', '2']}, '7']}
        operands = [
            {'path': '/values/0/values/0', 'citation': f'[D{did}:L1]', 'quote': '本批有 8 片'},
            {'path': '/values/0/values/1', 'citation': f'[D{did}:L1]', 'quote': '每片長 1/2 公尺'},
            {'path': '/values/0/values/2', 'citation': '[目標]', 'quote': '倍數為 2'},
            {'path': '/values/1', 'citation': '[補充#2]', 'quote': '本批另加 7 公尺'}]
        requirement = {'id': 1, 'ranges': [source_range], 'expression': expression, 'operands': operands}
        ready = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', items=[item(task='核對總長', state='done', refs=[1, 3])],
                   verify_delivery=True, reuse_document_steps=[1], evidence_requirements=[requirement]), gid=gid)
        self.assertEqual(ready['steps'][2]['observation']['exact'], '15')
        bound = ready['work_plan']['evidence_matches'][0]['operands']
        self.assertEqual([row['number'] for row in bound], ['8', '1/2', '2', '7'])
        self.assertEqual(bound[0]['quote_start'], 1)
        self.assertEqual(bound[-1]['source_sha256'], ready['reply_sources'][0]['sha256'])
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
            checkpoint = goal_stages.checkpoint(db, row)['work_plan']
        self.assertTrue(checkpoint['operand_evidence_omitted'])
        self.assertNotIn('本批另加 7 公尺', json.dumps(checkpoint, ensure_ascii=False))
        result = self.run_actions(action('finish', report='15 公尺。'), gid=gid)
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = copy.deepcopy(result['steps'][3]['observation'])
        altered['evidence_matches'][0]['operands'][1]['number_start'] += 1
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_operand_literal_requires_whole_number_unique_quote_and_available_source(self):
        row = {'path': '', 'citation': '[目標]', 'quote': '7'}
        operand_evidence.validate([row], '7', ())
        for text, reason in (('本批為17', '截斷'), ('本批是7，另批也是7', '唯一定位'),
                             ('備用長度為7/2', '截斷'), ('本批有7%', '截斷')):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, reason):
                operand_evidence.bind([row], '7', [], {'[目標]': (text, 'a' * 64)})
        with self.assertRaisesRegex(ValueError, '每個數字'):
            operand_evidence.validate([row], {'operation': 'sum', 'values': ['7', '7']}, ())
        with self.assertRaisesRegex(ValueError, '當時已保存'):
            operand_evidence.bind([{**row, 'citation': '[補充#9]'}], '7', [], {})
        quoted = {**row, 'quote': '原值 −７．０ 公尺'}
        operand_evidence.validate([quoted], '-7', ())
        bound, = operand_evidence.bind([quoted], '-7', [], {'[目標]': ('📍' + quoted['quote'], 'a' * 64)})
        self.assertEqual(bound['number'], '−７．０')
        self.assertEqual(bound['quote_start'], 1)

    def test_plan_operand_conversion_rechecks_mass_percent_and_restored_factor(self):
        path = self.root / 'mass.txt'
        path.write_text('本批主料 1.25 公斤。\n另批配料 450 毫克。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對本批抽取重量')
        source_range = {'document_id': did, 'start_line': 1, 'end_line': 1}
        self.run_actions(action('read', **source_range), action('ask', question='本批配料及抽取率？'), gid=gid)
        snapshot = goal_replies.snapshot(self.store, self.sid, gid)
        reply = '本批配料 450 公克，抽取率 8％。'
        goal_replies.save(self.store, self.sid, gid, reply, expected_fingerprint=snapshot['fingerprint'])
        expression = {'operation': 'product', 'values': [
            {'operation': 'sum', 'values': ['1.25', '0.45']}, '0.08']}
        operands = [
            {'path': '/values/0/values/0', 'citation': f'[D{did}:L1]', 'quote': '本批主料 1.25 公斤'},
            {'path': '/values/0/values/1', 'citation': '[補充#2]', 'quote': '本批配料 450 公克',
             'conversion': {'from': '公克', 'to': '公斤'}},
            {'path': '/values/1', 'citation': '[補充#2]', 'quote': '抽取率 8％',
             'conversion': {'from': '%', 'to': 'ratio'}}]
        requirement = {'id': 1, 'ranges': [source_range], 'expression': expression, 'operands': operands}
        result = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', items=[item(task='核對抽取重量', state='done', refs=[1, 3])],
                   verify_delivery=True, reuse_document_steps=[1], evidence_requirements=[requirement]),
            action('finish', report='0.136 公斤。'), gid=gid)
        self.assertEqual(result['steps'][2]['observation']['exact'], '17/125')
        _, weight, rate = result['work_plan']['evidence_matches'][0]['operands']
        self.assertEqual(weight['number'], '450')
        self.assertEqual((weight['conversion']['factor_exact'], weight['conversion']['converted_exact']), ('1/1000', '9/20'))
        self.assertEqual((rate['conversion']['unit'], rate['conversion']['converted_exact']), ('％', '2/25'))
        for receipt in (weight, rate):
            conversion = receipt['conversion']
            self.assertEqual(reply[conversion['unit_start']:conversion['unit_end']], conversion['unit'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), result)
        altered = copy.deepcopy(result['steps'][3]['observation'])
        altered['evidence_matches'][0]['operands'][1]['conversion']['factor_exact'] = '1/1000000'
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_conversion_rejects_clipped_units_and_incompatible_dimensions(self):
        row = {'path': '', 'citation': '[目標]', 'quote': '500g',
               'conversion': {'from': 'g', 'to': 'kg'}}
        operand_evidence.validate([row], '0.5', ())
        bound, = operand_evidence.bind([row], '1/2', [], {'[目標]': ('本批500g。', 'a' * 64)})
        self.assertEqual(bound['conversion']['source_exact'], '500')
        for text in ('本批1500g。', '本批500g/m。', '本批500grams。'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '截斷'):
                operand_evidence.bind([row], '0.5', [], {'[目標]': (text, 'a' * 64)})
        with self.assertRaisesRegex(ValueError, '相同量綱'):
            operand_evidence.validate([{**row, 'conversion': {'from': 'g', 'to': 'm'}}], '0.5', ())
        with self.assertRaisesRegex(ValueError, '原數量及單位'):
            operand_evidence.validate([{**row, 'quote': '500mg'}], '0.5', ())
        with self.assertRaisesRegex(ValueError, '原數量及單位'):
            operand_evidence.validate([row], '0.0005', ())

    def test_reuse_checks_original_evidence_set_complete_reads_and_version(self):
        file = self.root / 'sources.txt'
        file.write_text('甲批原值。\n乙批原值。', encoding='utf-8')
        did = self.store.attach_document(self.sid, file)
        gid = a.create_goal(self.store, self.sid, '核對兩行')
        task = item(task='核對兩行', state='done', refs=[1, 2])
        goal = self.run_actions(action('read', document_id=did, start_line=1, end_line=1),
                                action('read', document_id=did, start_line=2, end_line=2),
                                action('update_plan', items=[task]), gid=gid)
        scope = 'a' * 64
        args = {'items': [task], 'reuse_document_steps': [1, 2]}
        p.observe(args, goal['steps'], scope, goal['sources'])
        dropped = {'items': [item(task='核對兩行', state='done', refs=[1])],
                   'reuse_document_steps': [1]}
        with self.assertRaisesRegex(ValueError, '同一個原已完成'):
            p.observe(dropped, goal['steps'], scope, goal['sources'])
        invalid = [[], [1, 1], [True], [33]]
        for refs in invalid:
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                p.validate({**args, 'reuse_document_steps': refs})
        with self.assertRaisesRegex(ValueError, '本次完成項目'):
            p.observe({**args, 'reuse_document_steps': [1, 2, 32]}, goal['steps'], scope, goal['sources'])
        mismatched = [{**goal['sources'][0], 'sha256': '0' * 64}]
        with self.assertRaisesRegex(ValueError, '本目標文件版本或範圍不一致'):
            p.observe(args, goal['steps'], scope, mismatched)
        partial = copy.deepcopy(goal['steps'])
        partial[1]['observation']['requested_ranges'][0]['status'] = 'line_out_of_bounds'
        with self.assertRaisesRegex(ValueError, '不完整'):
            p.observe(args, partial, scope, goal['sources'])

    def test_changed_sources_are_stale_and_parent_plan_confers_no_child_evidence(self):
        file = self.root / 'fee.txt';file.write_text('每月120元', encoding='utf-8')
        self.store.attach_document(self.sid, file)
        parent = a.create_goal(self.store, self.sid, '計算費用')
        self.run_actions(action('read', document_id=1, start_line=1, end_line=1),
            action('update_plan', items=[item(task='讀取費用',state='done',refs=[1])]), gid=parent)
        file.write_text('每月150元', encoding='utf-8');self.store.attach_document(self.sid, file)
        self.assertFalse(a.get_goal(self.store,self.sid,parent)['work_plan']['conditions_current'])
        child = a.create_goal(self.store,self.sid,'接續剩下事項',parent_id=parent)
        saved = a.get_goal(self.store,self.sid,child)
        self.assertIsNone(saved['work_plan'])
        self.assertIn('work_plan',saved['previous_stage']['checkpoint'])
        self.assertNotIn('work_plan',json.dumps(goal_context(self.store,self.sid)))
        result = self.run_actions(action('update_plan', items=[item(state='done',refs=[1])]),gid=child)
        self.assertEqual(result['steps'][0]['action']['tool'],'invalid')

    def test_plan_has_no_citation_coverage_or_file_permission(self):
        file = self.root/'doc.txt';file.write_text('尚未讀取的原文',encoding='utf-8');self.store.attach_document(self.sid,file)
        gid = a.create_goal(self.store,self.sid,'整理')
        result = self.run_actions(action('update_plan',items=[item(task='未來讀取[D1:L1]')]),
            action('finish',report='擅自引用[D1:L1]'),action('write_artifact',filename='a.txt',content='內容'),gid=gid)
        self.assertEqual([s['action']['tool'] for s in result['steps']],['update_plan','invalid','invalid'])
        self.assertFalse(result['file_delivery'])

    def test_cancel_and_cumulative_budget_still_apply_to_plan_actions(self):
        event = threading.Event()
        class CancelEngine:
            def answer(self,prompt,cancel=None):
                event.set();return action('update_plan',items=[item()])
        goal = a.run_goal(self.store,self.sid,self.gid,CancelEngine(),cancel=event)
        self.assertEqual(goal['steps'],[])
        for count in (12,12,8):self.run_actions(*([action('update_plan',items=[item()])]*count))
        engine=ScriptedEngine()
        goal=a.run_goal(self.store,self.sid,self.gid,engine)
        self.assertEqual(len(goal['steps']),32)
        self.assertEqual(engine.prompts,[])
        self.assertEqual(goal['status'],'paused')

    def test_backup_keeps_history_and_rejects_inconsistent_plan(self):
        self.run_actions(action('calculate',operation='sum',values=['1']),
            action('update_plan',items=[item(state='done',refs=[1])]))
        original=a.get_goal(self.store,self.sid,self.gid)
        backup=create_backup(self.store.path);restore_backup(backup['path'],self.root/'restored')
        self.assertEqual(a.get_goal(Store(self.root/'restored'/'assistant.sqlite3'),self.sid,self.gid),original)
        with self.store.connect() as db:
            value=copy.deepcopy(original['steps'][1]['observation'])
            value['items'][0]['task']='被改掉的事項'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',(json.dumps(value),self.gid))
        with self.assertRaisesRegex(ValueError,'清單與保存動作不一致'):
            validate_database(self.store.path)

    def test_cli_status_returns_saved_progress_without_execution(self):
        self.run_actions(action('update_plan', items=[item(state='blocked', note='尚缺費用文件。')]))
        before = a.get_goal(self.store, self.sid, self.gid)
        result = subprocess.run([sys.executable, '-m', 'assistant', '--data-dir', str(self.root),
            '--session', self.sid, '--goal-status', self.gid], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['work_plan'], before['work_plan'])
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), before)

    def save_scope_guidance(self, content):
        current = goal_guidance.snapshot(self.store, self.sid, self.gid)
        goal_guidance.save(self.store, self.sid, self.gid, content, expected_fingerprint=current['fingerprint'])
        return a.get_goal(self.store, self.sid, self.gid)

    def test_cancel_single_legacy_question_and_carry_verified_calculation(self):
        expression = {'operation': 'product', 'values': ['120', '12']}
        before = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', verify_delivery=True, items=[item(state='done', refs=[1]), item(2, '安排收貨窗口')],
                   evidence_requirements=[{'id': 1, 'expression': expression}]),
            action('ask', question='正式收貨窗口是誰？'))
        state = self.save_scope_guidance('取消「安排收貨窗口」，只交已核對的總費用。')
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['steps'][:-1], before['steps'])
        self.assertNotIn('reply_sources', state)
        self.assertEqual(state['withdrawn_questions'][-1]['ask_sequence'], 3)
        plan = state['work_plan']
        self.assertTrue(plan['delivery_review']['ready'])
        self.assertEqual(plan['items'][0]['evidence_steps'], [1])
        self.assertEqual(plan['evidence_requirements'][0]['defined_scope'], plan['goal_conditions_sha256'])
        self.assertEqual(plan['scope_change']['retained_items'], [1])
        reactivated = self.run_actions(action('update_plan', items=[plan['items'][0], item(2, '安排收貨窗口')]))
        self.assertEqual(reactivated['steps'][-1]['action']['tool'], 'invalid')
        final = self.run_actions(action('update_plan', items=plan['items']), action('finish', report='合計 1440 元。'))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['items'][0]['evidence_steps'], [1])
        validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('DELETE FROM autonomous_question_withdrawals WHERE goal_id=?', (self.gid,))
        with self.assertRaisesRegex(ValueError, '缺少相符的問題撤下紀錄'):
            validate_database(self.store.path)

    def test_cancel_one_bound_question_and_restore_the_remaining_question(self):
        parts = [{'question': '收貨窗口是誰？', 'items': [1]}, {'question': '入場門號是多少？', 'items': [2]}]
        before = self.run_actions(action('update_plan', items=[item(1, '安排收貨窗口'), item(2, '確認入場門號')]),
            action('ask', question='收貨窗口是誰？\n入場門號是多少？', plan_questions=parts, partial_result='部分內容待核對。'))
        stale = goal_replies.snapshot(self.store, self.sid, self.gid)
        state = self.save_scope_guidance('先不做「安排收貨窗口」。')
        self.assertEqual(state['steps'][:len(before['steps'])], before['steps'])
        self.assertEqual(state['question'], '入場門號是多少？')
        self.assertNotIn('partial_result', state)
        binding = state['steps'][-1]['observation']['work_plan_question']
        self.assertEqual(binding['parts'], [parts[1]])
        self.assertEqual(binding['plan_step'], 3)
        self.assertEqual(state['withdrawn_questions'][-1]['ask_sequence'], 2)
        with self.assertRaisesRegex(ValueError, '重新載入'):
            goal_replies.save(self.store, self.sid, self.gid, '窗口是廖同仁。', expected_fingerprint=stale['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'scope.paibackup')
        restore_backup(backup['path'], self.root / 'restored-scope')
        restored = Store(self.root / 'restored-scope/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), state)
        view = goal_replies.snapshot(restored, self.sid, self.gid)
        goal_replies.save(restored, self.sid, self.gid, '入場門號為 3。', expected_fingerprint=view['fingerprint'])
        answered = a.get_goal(restored, self.sid, self.gid)
        self.assertEqual(answered['reply_sources'][-1]['ask_sequence'], 4)
        validate_database(restored.path)

    def test_shared_question_remains_until_all_linked_items_are_cancelled(self):
        question = '正式收貨窗口是誰？'
        self.run_actions(action('update_plan', items=[item(1, '安排收貨窗口'), item(2, '準備通知名單')]),
            action('ask', question=question, plan_questions=[{'question': question, 'items': [1, 2]}]))
        first = self.save_scope_guidance('取消「安排收貨窗口」。')
        self.assertEqual(first['question'], question)
        self.assertEqual(first['steps'][-1]['action']['args']['plan_questions'], [{'question': question, 'items': [2]}])
        second = self.save_scope_guidance('取消「準備通知名單」。')
        self.assertEqual(second['status'], 'paused')
        self.assertEqual(second['work_plan']['scope_cancelled'], [1, 2])
        self.assertEqual([row['ask_sequence'] for row in second['withdrawn_questions']], [2, 4])
        validate_database(self.store.path)

    def test_partial_quoted_or_conflicting_scope_preserves_an_unconfirmed_question(self):
        before = self.run_actions(action('update_plan', items=[item(1, '安排正式窗口'), item(2, '安排備援窗口')]),
            action('ask', question='正式窗口是誰？'))
        for content in ('取消「窗口」。', '有人提議「取消安排正式窗口」，未採用。',
                        '取消「安排正式窗口」？', '取消「安排正式窗口」。只交正式窗口。'):
            state = self.save_scope_guidance(content)
            self.assertEqual(state['steps'], before['steps'])
            self.assertEqual(state['question'], before['question'])
            self.assertNotIn('withdrawn_questions', state)

    def test_scope_change_cannot_revalidate_evidence_made_stale_by_an_earlier_correction(self):
        self.run_actions(action('calculate', operation='product', values=['120', '12']),
            action('update_plan', items=[item(state='done', refs=[1]), item(2, '排通知時間')]))
        self.save_scope_guidance('更正為 6 個月。')
        state = self.save_scope_guidance('取消「排通知時間」。')
        self.assertEqual(state['work_plan']['items'][0]['state'], 'pending')
        self.assertEqual(state['work_plan']['items'][0]['evidence_steps'], [])
        self.assertEqual(state['work_plan']['scope_change']['retained_items'], [])
        self.assertEqual(state['steps'][1]['observation']['items'][0]['state'], 'done')
        validate_database(self.store.path)

    def save_scope_reply(self, answer, *, store=None):
        store = store or self.store
        view = goal_replies.snapshot(store, self.sid, self.gid)
        goal_replies.save(store, self.sid, self.gid, answer, expected_fingerprint=view['fingerprint'])
        return a.get_goal(store, self.sid, self.gid)

    def test_reply_cancels_a_task_and_supplies_exact_completion_evidence(self):
        question = '收貨窗口是誰？\n入場門號是多少？'
        before = self.run_actions(action('calculate', operation='sum', values=['11']),
            action('update_plan', verify_delivery=True, items=[item(1, '核對件數', state='done', refs=[1]),
                   item(2, '安排收貨窗口'), item(3, '確認入場門號')]),
            action('ask', question=question, plan_questions=[{'question': line, 'items': [number]}
                                                           for number, line in enumerate(question.splitlines(), 2)]))
        answer = '入場門號是 3 號，這次取消「安排收貨窗口」。'
        state = self.save_scope_reply(answer)
        self.assertEqual(state['steps'][:-1], before['steps'])
        self.assertEqual(state['reply_sources'][-1]['answer'], answer)
        self.assertNotIn('withdrawn_questions', state)
        plan = state['work_plan']
        self.assertTrue(plan['delivery_review']['ready'])
        proof = plan['scope_reply']
        self.assertEqual(proof['completed_items'], [3])
        self.assertEqual(proof['retained_items'], [1])
        evidence = proof['parts'][1]['evidence']
        self.assertEqual(answer[evidence['start']:evidence['end']], '入場門號是 3 號')
        change = proof['changes'][0]
        self.assertEqual(answer[change['start']:change['end']], '這次取消「安排收貨窗口」')
        self.assertEqual(plan['items'][2]['evidence_steps'], [4])
        with self.assertRaisesRegex(ValueError, '完成同一項'):
            p.observe({'items': [{**plan['items'][0], 'evidence_steps': [4]}, *plan['items'][1:]]}, state['steps'],
                      plan['goal_conditions_sha256'])
        final = self.run_actions(action('update_plan', items=plan['items']), action('finish', report='11 件，門號 3。[補充#3]'))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['steps'][-1]['observation']['plan_check_step'], 5)
        validate_database(self.store.path)
        with self.store.connect() as db:
            observation = copy.deepcopy(state['steps'][3]['observation'])
            observation['scope_reply']['parts'][1]['value'] = '4 號'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(observation), self.gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(self.store.path)

    def test_scope_reply_restores_and_preserves_first_confirmation_when_other_answer_arrives(self):
        parts = [{'question': '領用人是誰？', 'items': [1]}, {'question': '窗口是誰？', 'items': [2]},
                 {'question': '入場門號是多少？', 'items': [3]}]
        self.run_actions(action('update_plan', verify_delivery=True,
                                items=[item(1, '確認領用人'), item(2, '安排窗口'), item(3, '確認入場門號')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        waiting = self.save_scope_reply('取消「安排窗口」。領用人為「陳怡君」。入場門號目前還沒確認。')
        self.assertEqual(waiting['question'], parts[2]['question'])
        self.assertEqual([part['state'] for part in waiting['work_plan']['scope_reply']['parts']],
                         ['confirmed', 'cancelled', 'deferred'])
        backup = create_backup(self.store.path, self.root / 'mixed-scope.paibackup')
        restore_backup(backup['path'], self.root / 'restored-mixed-scope')
        restored = Store(self.root / 'restored-mixed-scope/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), waiting)
        final = self.save_scope_reply('７ 號', store=restored)
        self.assertEqual([row['ask_sequence'] for row in final['reply_sources']], [2, 4])
        self.assertEqual(final['work_plan']['scope_reply']['retained_items'], [1])
        self.assertEqual(final['work_plan']['scope_reply']['completed_items'], [3])
        self.assertEqual([row['evidence_steps'] for row in final['work_plan']['items']], [[3], [], [5]])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(restored.path)

    def test_confirmed_reply_cannot_replace_an_item_specific_tool_requirement(self):
        question = '入場門號是多少？'
        requirement = {'id': 2, 'expression': {'operation': 'sum', 'values': ['1', '2']}}
        self.run_actions(action('update_plan', verify_delivery=True, items=[item(1, '安排窗口'), item(2, '確認入場門號')],
                                evidence_requirements=[requirement]),
                         action('ask', question=question, plan_questions=[{'question': question, 'items': [2]}]))
        state = self.save_scope_reply('取消「安排窗口」。入場門號為 3 號。')
        plan = state['work_plan']
        self.assertEqual(plan['scope_reply']['parts'][0]['state'], 'confirmed')
        self.assertEqual(plan['scope_reply']['completed_items'], [])
        self.assertEqual(plan['items'][1]['state'], 'pending')
        self.assertEqual(state['question'], '')
        blocked = self.run_actions(action('finish', report='門號 3。'))
        self.assertEqual(blocked['steps'][-1]['action']['tool'], 'invalid')
        validate_database(self.store.path)

    def test_unresolved_scope_sentences_do_not_change_plan(self):
        question = '入場門號是多少？'
        for answer in ('有人提議「取消安排窗口」，還沒有採用。入場門號是 3 號。',
                       '取消「安排窗口」。入場門號是我晚點回覆。',
                       '取消「安排窗口」。其實窗口還是要做。入場門號是 3 號。'):
            with self.subTest(answer=answer):
                self.gid = a.create_goal(self.store, self.sid, '整理交接清單')
                original = self.run_actions(action('update_plan', items=[item(1, '安排窗口'), item(2, '確認入場門號')]),
                    action('ask', question=question, plan_questions=[{'question': question, 'items': [2]}]))
                state = self.save_scope_reply(answer)
                self.assertEqual(state['steps'], original['steps'])
                self.assertEqual(state['reply_sources'][-1]['answer'], answer)
                self.assertNotIn('scope_cancelled', state['work_plan'])
        validate_database(self.store.path)

    def test_same_message_task_mention_cannot_answer_a_scope_question_not_yet_asked(self):
        parts = [{'question': '收貨窗口是誰？', 'items': [1]}, {'question': '覆核窗口是誰？', 'items': [2]}]
        original = self.run_actions(action('update_plan', items=[item(1, '確認收貨窗口'), item(2, '確認覆核窗口')]),
            action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        state = self.save_scope_reply('取消窗口。確認覆核窗口。')
        self.assertEqual(state['steps'], original['steps'])
        self.assertNotIn('scope_cancelled', state['work_plan'])
        validate_database(self.store.path)

    def test_conflicting_field_keeps_independent_confirmations_and_both_original_values(self):
        parts = [{'question': '窗口是誰？', 'items': [1]}, {'question': '入場門號是多少？', 'items': [2]},
                 {'question': '領用人是誰？', 'items': [3]}]
        self.run_actions(action('update_plan', verify_delivery=True,
                                items=[item(1, '安排窗口'), item(2, '確認入場門號'), item(3, '確認領用人')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        answer = '取消「安排窗口」。入場門號是 3 號。領用人為葉舒禾。入場門號是 4 號。'
        waiting = self.save_scope_reply(answer)
        proof = waiting['work_plan']['scope_reply']
        self.assertEqual(proof['completed_items'], [3])
        field = proof['parts'][1]
        self.assertEqual(field['state'], 'conflicting')
        self.assertEqual([row['value'] for row in field['statements']], ['3 號', '4 號'])
        for row in field['statements']:
            evidence = row['evidence']
            self.assertEqual(answer[evidence['start']:evidence['end']], evidence['quote'])
        self.assertEqual(waiting['question'], '入場門號要以「3 號」還是「4 號」為準？')
        final = self.save_scope_reply('就用 4 號。')
        self.assertEqual(final['reply_sources'][-1]['question'], waiting['question'])
        self.assertEqual(final['work_plan']['scope_reply']['retained_items'], [3])
        self.assertEqual(final['work_plan']['scope_reply']['completed_items'], [2])
        self.assertEqual(final['work_plan']['scope_reply']['parts'][0]['value'], '4 號')
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def wait_on_two_windows(self):
        parts = [{'question': '收貨窗口是誰？', 'items': [1]}, {'question': '覆核窗口是誰？', 'items': [2]},
                 {'question': '入場門號是多少？', 'items': [3]}]
        self.run_actions(action('update_plan', verify_delivery=True,
                                items=[item(1, '確認收貨窗口'), item(2, '確認覆核窗口'), item(3, '確認入場門號')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        return self.save_scope_reply('取消窗口。收貨窗口是葉舒禾。入場門號是 6 號。')

    def test_short_cancellation_can_be_declined_after_restore_and_preserves_confirmed_fields(self):
        state = self.wait_on_two_windows()
        self.assertEqual(state['work_plan']['scope_reply']['completed_items'], [1, 3])
        self.assertEqual(state['work_plan']['scope_reply']['language']['cancellation']['items'], [1, 2])
        self.assertEqual(state['work_plan']['scope_cancelled'], [])
        self.assertEqual(state['steps'][-1]['observation']['work_plan_question']['parts'][0]['items'], [1, 2])
        backup = create_backup(self.store.path, self.root / 'ambiguous-scope.paibackup')
        restore_backup(backup['path'], self.root / 'restored-ambiguous-scope')
        restored = Store(self.root / 'restored-ambiguous-scope/assistant.sqlite3')
        after = self.save_scope_reply('都保留。', store=restored)
        self.assertEqual(after['question'], '覆核窗口是誰？')
        self.assertEqual(after['work_plan']['scope_reply']['language']['resolution']['items'], [])
        final = self.save_scope_reply('「林語安」。', store=restored)
        self.assertEqual(final['work_plan']['scope_reply']['retained_items'], [1, 3])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(restored.path)
        with restored.connect() as db:
            broken = copy.deepcopy(state['steps'][2]['observation'])
            broken['scope_reply']['language']['cancellation']['items'] = [2, 3]
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=3',
                       (json.dumps(broken), self.gid))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_separate_exact_correction_can_resolve_a_pending_short_cancellation(self):
        self.wait_on_two_windows()
        after = self.save_scope_guidance('取消「確認覆核窗口」。')
        self.assertEqual(after['work_plan']['scope_cancelled'], [2])
        self.assertEqual(after['work_plan']['scope_change']['retained_items'], [1, 3])
        self.assertEqual(after['status'], 'paused')
        self.assertTrue(after['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def test_repeated_equivalent_numbers_retain_the_literal_but_need_no_extra_question(self):
        question = '入場門號是多少？'
        self.run_actions(action('update_plan', verify_delivery=True, items=[item(1, '安排窗口'), item(2, '確認入場門號')]),
                         action('ask', question=question, plan_questions=[{'question': question, 'items': [2]}]))
        saved = self.save_scope_reply('窗口先不用了，入場門號是 ３ 號，入場門號是 3號。')
        proof = saved['work_plan']['scope_reply']
        self.assertEqual(proof['parts'][0]['value'], '３ 號')
        self.assertEqual(len(proof['parts'][0]['statements']), 2)
        self.assertEqual(proof['completed_items'], [2])
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def test_revision_reopens_only_the_calculation_that_depends_on_the_corrected_field(self):
        self.gid = a.create_goal(self.store, self.sid, '欄位「留存份數」為 2 份。每組倍率為 3。核對留存份數與獨立數量。')
        calculation = {'operation': 'product', 'values': ['2', '3']}
        unrelated = {'operation': 'sum', 'values': ['1', '4']}
        requirement = {'id': 1, 'expression': calculation, 'operands': [
            {'path': '/values/0', 'citation': '[目標]', 'quote': '欄位「留存份數」為 2 份', 'field': '留存份數'},
            {'path': '/values/1', 'citation': '[目標]', 'quote': '每組倍率為 3'}]}
        parts = [{'question': '窗口是誰？', 'items': [3]}, {'question': '入場門號是多少？', 'items': [4]},
                 {'question': '留存份數是多少？', 'items': [5]}]
        self.run_actions(action('calculate_expression', expression=calculation),
                         action('calculate_expression', expression=unrelated),
                         action('update_plan', verify_delivery=True, items=[item(1, '計算總份數', state='done', refs=[1]),
                                item(2, '計算獨立數量', state='done', refs=[2]), item(3, '安排窗口'),
                                item(4, '確認入場門號'), item(5, '確認留存份數')],
                                evidence_requirements=[requirement, {'id': 2, 'expression': unrelated}]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        first = self.save_scope_reply('取消「安排窗口」。入場門號還沒確認。留存份數是 2 份。')
        state = self.save_scope_reply('7 號。留存份數改為 5 份。')
        plan = state['work_plan']
        proof = plan['scope_reply']
        self.assertEqual(proof['retained_items'], [2])
        self.assertEqual(proof['completed_items'], [4, 5])
        self.assertEqual(proof['revisions'][0]['recheck_items'], [1])
        self.assertEqual(proof['revisions'][0]['previous']['citation'], first['reply_sources'][-1]['citation'])
        self.assertEqual(plan['items'][0]['state'], 'pending')
        self.assertEqual(plan['items'][1]['evidence_steps'], [2])
        self.assertFalse(plan['delivery_review']['ready'])
        current_ref = len(state['steps']) + 1
        rejected = self.run_actions(action('calculate_expression', expression=calculation),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [current_ref]},
                                         *plan['items'][1:]], evidence_requirements=[requirement]))
        self.assertIn('的「留存份數」已更正', rejected['steps'][-1]['observation']['error'])
        expression = {'operation': 'product', 'values': ['5', '3']}
        new_requirement = {**requirement, 'expression': expression, 'operands': [
            {**requirement['operands'][0], 'citation': proof['citation'], 'quote': '留存份數改為 5 份'},
            requirement['operands'][1]]}
        new_ref = len(rejected['steps']) + 1
        final = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [new_ref]},
                                         *plan['items'][1:]], evidence_requirements=[new_requirement]),
            action('finish', report='總份數 15，獨立數量 5，入場門號 7。'))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['items'][1]['evidence_steps'], [2])
        validate_database(self.store.path)

    def test_successive_revisions_replay_from_latest_actual_answer_after_backup(self):
        parts = [{'question': '窗口是誰？', 'items': [1]}, {'question': '進場門號是多少？', 'items': [2]},
                 {'question': '領用人是誰？', 'items': [3]}]
        self.run_actions(action('update_plan', verify_delivery=True,
                                items=[item(1, '安排窗口'), item(2, '確認進場門號'), item(3, '確認領用人')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        before = self.save_scope_reply('取消「安排窗口」。進場門號還沒確認。領用人是林語安。')
        revised = self.save_scope_reply('領用人剛才說錯，改成周雅文。')
        self.assertEqual(revised['question'], before['question'])
        self.assertEqual(revised['work_plan']['scope_reply']['revisions'][0]['previous']['value'], '林語安')
        backup = create_backup(self.store.path, self.root / 'successive-replies.paibackup')
        restore_backup(backup['path'], self.root / 'restored-replies')
        restored = Store(self.root / 'restored-replies/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), revised)
        final = self.save_scope_reply('領用人更正為江明洋。6 號。', store=restored)
        proof = final['work_plan']['scope_reply']
        self.assertEqual(proof['revisions'][0]['previous']['value'], '周雅文')
        self.assertEqual(proof['revisions'][0]['previous']['step'], revised['work_plan']['sequence'])
        self.assertEqual(proof['completed_items'], [2, 3])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(restored.path)
        with restored.connect() as db:
            host = final['steps'][-1]
            bad = copy.deepcopy(host['observation'])
            bad['scope_reply']['revisions'][0]['previous']['source_sha256'] = '0' * 64
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(bad), self.gid, host['sequence']))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_filename_reply_rechecks_calculation_with_the_actual_revised_operand(self):
        self.gid = a.create_goal(self.store, self.sid,
            '欄位「留存份數」為 2 份。每組倍率為 3。核對留存份數與獨立數量。請交 report.md。', file_delivery=True)
        calculation = {'operation': 'product', 'values': ['2', '3']}
        unrelated = {'operation': 'sum', 'values': ['1', '4']}
        requirement = {'id': 1, 'expression': calculation, 'operands': [
            {'path': '/values/0', 'citation': '[目標]', 'quote': '欄位「留存份數」為 2 份', 'field': '留存份數'},
            {'path': '/values/1', 'citation': '[目標]', 'quote': '每組倍率為 3'}]}
        parts = [{'question': '窗口是誰？', 'items': [3]}, {'question': '留存份數是多少？', 'items': [4]}]
        self.run_actions(action('write_artifact', filename='report.md', content='草稿。總份數 6。'),
                         action('calculate_expression', expression=calculation),
                         action('calculate_expression', expression=unrelated),
                         action('update_plan', verify_delivery=True, items=[item(1, '計算總份數', state='done', refs=[2]),
                                item(2, '計算獨立數量', state='done', refs=[3]), item(3, '安排窗口'), item(4, '確認留存份數')],
                                evidence_requirements=[requirement, {'id': 2, 'expression': unrelated}]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        self.save_scope_reply('取消「安排窗口」。留存份數是 2 份。')
        engine = ScriptedEngine()
        waiting = a.run_goal(self.store, self.sid, self.gid, engine)
        self.assertFalse(engine.prompts)
        self.assertIn('定稿檔名', waiting['question'])
        state = self.save_scope_reply('定稿用 result.md。留存份數改為 5 份。')
        plan = state['work_plan']
        proof = plan['scope_reply']
        self.assertEqual(proof['retained_items'], [2])
        self.assertEqual(proof['revisions'][0]['recheck_items'], [1])
        self.assertEqual(plan['items'][0]['state'], 'pending')
        self.assertFalse(plan['delivery_review']['ready'])
        current_ref = len(state['steps']) + 1
        rejected = self.run_actions(action('calculate_expression', expression=calculation),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [current_ref]},
                                         *plan['items'][1:]], evidence_requirements=[requirement]))
        self.assertIn('的「留存份數」已更正', rejected['steps'][-1]['observation']['error'])
        expression = {'operation': 'product', 'values': ['5', '3']}
        new_requirement = {**requirement, 'expression': expression, 'operands': [
            {**requirement['operands'][0], 'citation': proof['citation'], 'quote': '留存份數改為 5 份'},
            requirement['operands'][1]]}
        new_ref = len(rejected['steps']) + 1
        final = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [new_ref]},
                                         *plan['items'][1:]], evidence_requirements=[new_requirement]),
            action('write_artifact', filename='result.md', content='總份數 15，獨立數量 5。'),
            action('deliver', report='已交付。', filenames=['result.md']))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['items'][1]['evidence_steps'], [3])
        validate_database(self.store.path)

    def test_reply_cannot_revive_prior_confirmation_after_unverified_scope_change(self):
        parts = [{'question': '窗口是誰？', 'items': [1]}, {'question': '門號是多少？', 'items': [2]},
                 {'question': '領用人是誰？', 'items': [3]}]
        self.run_actions(action('update_plan', items=[item(1, '安排窗口'), item(2, '確認門號'), item(3, '確認領用人')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        self.save_scope_reply('取消「安排窗口」。門號還沒確認。領用人為林語安。')
        before = self.save_scope_guidance('更正為不同批次。')
        self.assertFalse(before['work_plan']['conditions_current'])
        after = self.save_scope_reply('門號是 9 號。領用人改為周雅文。')
        self.assertEqual(after['steps'], before['steps'])
        self.assertFalse(after['work_plan']['conditions_current'])
        self.assertEqual(after['reply_sources'][-1]['answer'], '門號是 9 號。領用人改為周雅文。')
        validate_database(self.store.path)

    def _wait_for_gate_with_confirmed_fields(self):
        parts = [{'question': '窗口是誰？', 'items': [1]}, {'question': '門號是多少？', 'items': [2]},
                 {'question': '領用人是誰？', 'items': [3]}, {'question': '留存份數是多少？', 'items': [4]}]
        self.run_actions(action('update_plan', verify_delivery=True, items=[item(1, '安排窗口'), item(2, '確認門號'),
                                item(3, '確認領用人'), item(4, '確認留存份數')]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
        return self.save_scope_reply('取消「安排窗口」。門號還沒確認。領用人是林語安。留存份數是 3 份。')

    def test_declined_quoted_proposal_is_saved_but_leaves_the_confirmed_value_intact(self):
        before = self._wait_for_gate_with_confirmed_fields()
        answer = '6 號。主管建議『領用人改成周雅文？』，本輪不採用。'
        saved = self.save_scope_reply(answer)
        proof = saved['work_plan']['scope_reply']
        span = proof['language']['declined_proposals'][0]
        self.assertEqual(answer[span['start']:span['end']], '主管建議『領用人改成周雅文？』，本輪不採用')
        self.assertEqual(saved['work_plan']['items'][2], before['work_plan']['items'][2])
        self.assertEqual(proof['completed_items'], [2])
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def test_two_disputes_bind_to_successive_asks_and_preserve_a_draft_on_invalid_choice(self):
        before = self._wait_for_gate_with_confirmed_fields()
        waiting = self.save_scope_reply('6 號。領用人改成周雅文。領用人改為江明洋。留存份數改為 4 份或 6 份。')
        plan = waiting['work_plan']
        self.assertEqual(plan['items'][2:], before['work_plan']['items'][2:])
        self.assertEqual(plan['delivery_review']['pending_revision_fields'], ['領用人', '留存份數'])
        self.assertFalse(plan['delivery_review']['ready'])
        self.assertEqual(waiting['question'], '領用人要改為「周雅文」或「江明洋」，還是沿用「林語安」？')
        with self.assertRaisesRegex(ValueError, '待選更正'):
            p.check_delivery(plan, plan['goal_conditions_sha256'], {'tool': 'finish', 'args': {}}, waiting['steps'])
        with self.assertRaisesRegex(ValueError, '待選更正仍未確認'):
            self.save_scope_reply('8 份。')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), waiting)
        backup = create_backup(self.store.path, self.root / 'revision-choices.paibackup')
        restore_backup(backup['path'], self.root / 'restored-choices')
        restored = Store(self.root / 'restored-choices/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), waiting)
        second = self.save_scope_reply('就用江明洋。', store=restored)
        self.assertEqual(second['question'], '留存份數要改為「4 份」或「6 份」，還是沿用「3 份」？')
        proof = second['work_plan']['scope_reply']
        self.assertEqual(proof['language']['revision_resolution']['from'], waiting['reply_sources'][-1]['citation'])
        self.assertEqual(proof['revisions'][0]['value'], '江明洋')
        self.assertIn('江明洋', second['work_plan']['items'][2]['note'])
        final = self.save_scope_reply('維持原值。', store=restored)
        self.assertEqual(final['work_plan']['items'][3], before['work_plan']['items'][3])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(restored.path)
        with restored.connect() as db:
            host = waiting['steps'][-2]
            bad = copy.deepcopy(host['observation'])
            bad['scope_reply']['language']['pending_revisions'][0]['options'][0]['value'] = '另一人'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(bad), self.gid, host['sequence']))
        with self.assertRaisesRegex(ValueError, '清單與保存動作不一致'):
            validate_database(restored.path)

    def test_scope_removal_eliminates_a_choice_that_no_longer_affects_delivery(self):
        before = self._wait_for_gate_with_confirmed_fields()
        waiting = self.save_scope_reply('6 號。領用人改為周雅文或江明洋。')
        self.assertIn('領用人要改為', waiting['question'])
        after = self.save_scope_guidance('取消「確認領用人」。')
        self.assertEqual(after['status'], 'paused')
        self.assertFalse(after['question'])
        self.assertEqual(after['work_plan']['items'][3], before['work_plan']['items'][3])
        self.assertTrue(after['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def _wait_for_file_plan_choice(self):
        self.gid = a.create_goal(self.store, self.sid, '核對工作清單並依目前方式交付', file_delivery=True)
        self._wait_for_gate_with_confirmed_fields()
        return self.save_scope_reply('6 號。領用人改為周雅文或江明洋。')

    def test_mode_and_choice_share_an_actual_reply_and_backup_requires_both_receipts(self):
        waiting = self._wait_for_file_plan_choice()
        answer = '改成文字回覆，用江明洋。'
        saved = self.save_scope_reply(answer)
        plan = saved['work_plan']
        mode = saved['delivery_mode_change']
        self.assertFalse(saved['file_delivery'])
        self.assertEqual(saved['reply_sources'][-1]['question'], waiting['question'])
        self.assertEqual(plan['scope_reply']['delivery_mode'], {'receipt': mode, 'changed': True})
        self.assertEqual(answer[mode['start']:mode['end']], '改成文字回覆')
        choice = plan['scope_reply']['revisions'][0]['evidence']
        self.assertEqual(answer[choice['start']:choice['end']], '用江明洋')
        self.assertEqual(plan['items'][2]['note'], f'依 {mode["citation"]} 確認：江明洋。')
        self.assertTrue(plan['delivery_review']['ready'])
        backup = create_backup(self.store.path, self.root / 'joined-delivery.paibackup')
        restore_backup(backup['path'], self.root / 'restored-joined-delivery')
        restored = Store(self.root / 'restored-joined-delivery/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, self.gid), saved)
        with restored.connect() as db:
            db.execute('DELETE FROM autonomous_delivery_changes WHERE goal_id=?', (self.gid,))
        with self.assertRaisesRegex(ValueError, '缺少相符的交付方式紀錄'):
            validate_database(restored.path)
        result = self.run_actions(action('write_artifact', filename='unexpected.txt', content='舊內容'),
                                  action('finish', report='領用人江明洋，門號 6 號。'))
        self.assertEqual([step['action']['tool'] for step in result['steps'][-2:]], ['invalid', 'finish'])
        self.assertEqual(result['artifacts'], [])
        validate_database(self.store.path)

    def test_mode_only_reply_keeps_pending_choice_and_noop_mode_can_accompany_selection(self):
        waiting = self._wait_for_file_plan_choice()
        with self.assertRaisesRegex(ValueError, '待選更正仍未確認'):
            self.save_scope_reply('如果選周雅文，改成文字回覆。')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), waiting)
        saved = self.save_scope_reply('不用出檔，直接交文字。')
        self.assertEqual(saved['question'], waiting['question'])
        self.assertFalse(saved['file_delivery'])
        self.assertTrue(saved['work_plan']['conditions_current'])
        self.assertEqual(saved['work_plan']['items'], waiting['work_plan']['items'])
        self.assertNotEqual(saved['steps'][-1]['sequence'], waiting['steps'][-1]['sequence'])
        self.assertEqual(saved['delivery_mode_change']['quote'], '不用出檔，直接交文字')
        final = self.save_scope_reply('沿用原值。改成文字回覆。')
        self.assertEqual(final['delivery_mode_change'], saved['delivery_mode_change'])
        self.assertFalse(final['work_plan']['scope_reply']['delivery_mode']['changed'])
        self.assertEqual(final['work_plan']['items'][2], waiting['work_plan']['items'][2])
        self.assertTrue(final['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)

    def test_mode_guidance_reanchors_pending_ask_and_reply_can_restore_file_delivery(self):
        waiting = self._wait_for_file_plan_choice()
        snapshot = goal_replies.snapshot(self.store, self.sid, self.gid)
        saved = self.save_scope_guidance('交付方式改為文字。')
        self.assertEqual(saved['question'], waiting['question'])
        self.assertTrue(saved['work_plan']['conditions_current'])
        self.assertFalse(saved['file_delivery'])
        self.assertEqual(saved['withdrawn_questions'][-1]['ask_sequence'], waiting['steps'][-1]['sequence'])
        self.assertEqual(saved['work_plan']['scope_change']['delivery_mode'], saved['delivery_mode_change'])
        with self.assertRaisesRegex(ValueError, '重新載入'):
            goal_replies.save(self.store, self.sid, self.gid, '用周雅文。', expected_fingerprint=snapshot['fingerprint'])
        selected = self.save_scope_reply('用周雅文，改回交檔。')
        self.assertTrue(selected['file_delivery'])
        self.assertEqual(selected['reply_sources'][-1]['ask_sequence'], saved['steps'][-1]['sequence'])
        content = '領用人周雅文，門號 6 號。'
        result = self.run_actions(action('write_artifact', filename='report.md', content=content),
                                  action('deliver', report='已完成', filenames=['report.md']))
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual((self.root / 'autonomy-artifacts' / self.gid / 'report.md').read_text(encoding='utf-8'), content)
        validate_database(self.store.path)

    def test_original_plan_question_accepts_confirmed_value_with_mode_change(self):
        self.gid = a.create_goal(self.store, self.sid, '確認門號後交付', file_delivery=True)
        question = '入場門號是多少？'
        self.run_actions(action('update_plan', verify_delivery=True, items=[item(1, '確認入場門號')]),
                         action('ask', question=question, plan_questions=[{'question': question, 'items': [1]}]))
        saved = self.save_scope_reply('入場門號是 7 號，改成文字回覆。')
        self.assertFalse(saved['file_delivery'])
        self.assertEqual(saved['work_plan']['scope_reply']['completed_items'], [1])
        self.assertTrue(saved['work_plan']['delivery_review']['ready'])
        validate_database(self.store.path)


if __name__ == '__main__':
    unittest.main()
