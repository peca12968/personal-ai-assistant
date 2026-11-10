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


if __name__ == '__main__':
    unittest.main()
