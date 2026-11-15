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

        formal = {**row, 'field': '西棚的盤數', 'quote': '西棚的盤數正式更正為「6 盤」'}
        operand_evidence.validate([formal], '6', ())
        source = f'東棚的盤數為 6 盤。{formal["quote"]}。'
        bound, = operand_evidence.bind([formal], '6', (), operand_evidence.user_texts(source, (), (), 1))
        self.assertEqual(source[bound['field_start']:bound['field_end']], '西棚的盤數')
        self.assertEqual(bound['number_start'], source.rindex('6'))
        for invalid in ({**formal, 'field': '東棚的盤數'},
                        {**formal, 'quote': '西棚的盤數可能正式更正為 6 盤'},
                        {**formal, 'quote': '西棚的盤數正式更正為「6 盤'},
                        {**formal, 'quote': '「西棚的盤數正式更正為 6 盤」'}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '欄位名'):
                operand_evidence.validate([invalid], '6', ())

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

    def test_document_progress_combines_current_and_reusable_reads_across_restore(self):
        for name, body in [('count.txt', '批次 A\n數量 12\n已封存'), ('handoff.txt', '窗口青櫃\n交還碼 CK-35')]:
            path = self.root / name
            path.write_text(body, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '查讀兩份文件並整理出貨通知。')
        first = {'document_id': 1, 'start_line': 1, 'end_line': 1}
        missing = {'document_id': 1, 'start_line': 2, 'end_line': 3}
        second = {'document_id': 2, 'start_line': 1, 'end_line': 2}
        requirements = [{'id': 1, 'ranges': [{**first, 'end_line': 3}, second]}]
        self.run_actions(action('update_plan', items=[item(1, '查讀出貨文件')], verify_delivery=True,
                                evidence_requirements=requirements), action('read', **first), gid=gid)

        def progress(store):
            return a.get_goal(store, self.sid, gid)['work_plan']['delivery_review']['document_read_progress']

        self.assertEqual(progress(self.store), [{'id': 1, 'read_steps': [2], 'missing_ranges': [missing, second],
                                                'reuse_document_steps': []}])
        review = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '定稿請用條列。', expected_fingerprint=review['fingerprint'])
        expected = [{'id': 1, 'read_steps': [2], 'missing_ranges': [missing, second], 'reuse_document_steps': [2]}]
        self.assertEqual(progress(self.store), expected)
        self.assertIn('D1:L2-L3、D2:L1-L2', p.describe(a.get_goal(self.store, self.sid, gid)['work_plan']))

        backup = create_backup(self.store.path, self.root / 'reading.paibackup')
        restore_backup(backup['path'], self.root / 'reading')
        restored = Store(self.root / 'reading' / 'assistant.sqlite3')
        self.assertEqual(progress(restored), expected)
        engine = ScriptedEngine(action('read_ranges', ranges=[missing, second]),
                                action('update_plan', items=[item(1, '查讀出貨文件', 'done', [2, 3])],
                                       reuse_document_steps=[2], evidence_requirements=requirements),
                                action('finish', report='批次 A [D1:L1] 共 12 件 [D1:L2]，窗口青櫃 [D2:L1]，交還碼 CK-35 [D2:L2]。'))
        self.assertEqual(a.run_goal(restored, self.sid, gid, engine, max_steps=1)['status'], 'paused')
        payload = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(payload['work_plan']['delivery_review']['document_read_progress'], expected)
        self.assertEqual(progress(restored), [{'id': 1, 'read_steps': [2, 3], 'missing_ranges': [],
                                              'reuse_document_steps': [2]}])
        final = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['evidence_matches'][0]['read_steps'], [2, 3])
        validate_database(restored.path)

        other = self.root / 'unrelated.txt'
        other.write_text('不同來源', encoding='utf-8')
        restored.attach_document(self.sid, other)
        self.assertNotIn('document_read_progress', a.get_goal(restored, self.sid, gid)['work_plan']['delivery_review'])

    def test_partial_batch_keeps_only_provided_ranges_as_typed_evidence(self):
        for name, text in [('count.txt', '批次 A\n出貨數量 12 件\n' + '甲' * 8200),
                           ('handoff.txt', '窗口青櫃\n交還碼 CK-35 ' + '乙' * 2000)]:
            path = self.root / name
            path.write_text(text, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '查讀兩份出貨文件。')
        first = {'document_id': 1, 'start_line': 1, 'end_line': 3}
        second = {'document_id': 2, 'start_line': 1, 'end_line': 2}
        requirements = [{'id': 1, 'ranges': [first, second]}]
        task = item(1, '核對出貨文件')
        paused = self.run_actions(action('update_plan', items=[task], verify_delivery=True,
                                         evidence_requirements=requirements),
                                  action('read_ranges', ranges=[first, second]), gid=gid)
        batch = paused['steps'][1]
        self.assertEqual([row['status'] for row in batch['observation']['requested_ranges']],
                         ['provided', 'character_limit'])
        with self.assertRaisesRegex(ValueError, '缺少完整指定文件範圍'):
            p.observe({'items': [{**task, 'state': 'done', 'evidence_steps': [2]}]}, paused['steps'],
                      p.conditions(paused), paused['sources'])
        with self.assertRaisesRegex(ValueError, '不完整'):
            p.observe({'items': [{**task, 'state': 'done', 'evidence_steps': [2]}]}, [batch],
                      p.conditions(paused), paused['sources'])

        snapshot = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '定稿請用條列。', expected_fingerprint=snapshot['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'mixed.paibackup')
        restore_backup(backup['path'], self.root / 'mixed')
        restored = Store(self.root / 'mixed' / 'assistant.sqlite3')
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual(state['work_plan']['delivery_review']['document_read_progress'],
                         [{'id': 1, 'read_steps': [2], 'missing_ranges': [second], 'reuse_document_steps': [2],
                           'recovery': [{**second, 'action': 'split_range'}]}])
        done = {**task, 'state': 'done', 'evidence_steps': [2, 3]}
        update = {'items': [done], 'reuse_document_steps': [2]}
        engine = ScriptedEngine(action('read', **second), action('update_plan', **update),
                                action('finish', report='批次 A [D1:L1] 共 12 件 [D1:L2]，窗口青櫃 [D2:L1]。'))
        result = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(result['status'], 'report_ready')
        plan = result['work_plan']
        self.assertEqual(plan['evidence_matches'][0]['read_steps'], [2, 3])
        reuse = plan['reused_document_steps'][0]
        self.assertEqual(reuse['document_ids'], [1])
        self.assertEqual(plan['evidence_matches'][0]['partial_read_proofs'],
                         [{'sequence': 2, 'read_sha256': reuse['read_sha256']}])
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'paused')
        validate_database(restored.path)

        for changes in ({'partial_line': True}, {'sha256': '0' * 64}, {'text': 'L1: 批次 A'}):
            edited = copy.deepcopy(result['steps'][:3])
            edited[1]['observation']['excerpts'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                p.observe(update, edited, p.conditions(result), result['sources'])
        forged = copy.deepcopy(batch['observation'])
        forged['excerpts'][0]['text'] = forged['excerpts'][0]['text'].replace('12 件', '99 件')
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(forged, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作不一致'):
            validate_database(restored.path)

    def test_paged_line_evidence_requires_contiguous_pages_after_restore(self):
        path = self.root / 'approval.txt'
        path.write_text('批號 A7。' + '甲' * 8050 + '核准 CK-35。' + '乙' * 2100, encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '完整查讀核准原行後，附來源交付。')
        wanted = {'document_id': did, 'start_line': 1, 'end_line': 1}
        task = item(1, '核對核准原行')
        requirements = [{'id': 1, 'ranges': [wanted]}]
        self.run_actions(action('update_plan', items=[task], verify_delivery=True, evidence_requirements=requirements),
                         action('read', **wanted),
                         action('read_line', document_id=did, line_number=1, start_char=1), gid=gid)
        review = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '定稿用條列。', expected_fingerprint=review['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'pages.paibackup')
        restore_backup(backup['path'], self.root / 'pages')
        restored = Store(self.root / 'pages' / 'assistant.sqlite3')

        def progress():
            return a.get_goal(restored, self.sid, gid)['work_plan']['delivery_review']['document_read_progress'][0]

        self.assertEqual(progress(), {'id': 1, 'read_steps': [], 'missing_ranges': [wanted], 'reuse_document_steps': [],
                                     'recovery': [{'document_id': did, 'line_number': 1, 'action': 'read_line',
                                                   'start_char': 4001}]})
        incomplete = {**task, 'state': 'done', 'evidence_steps': [3, 4]}
        attempt = ScriptedEngine(action('read_line', document_id=did, line_number=1, start_char=8001),
                                 action('update_plan', items=[incomplete], reuse_document_steps=[3]),
                                 action('finish', report='提早交付。'))
        rejected = a.run_goal(restored, self.sid, gid, attempt, max_steps=3)
        self.assertEqual(rejected['status'], 'paused')
        self.assertIn('缺少完整指定文件範圍', rejected['steps'][4]['observation']['error'])
        self.assertIn('交付核對', rejected['steps'][5]['observation']['error'])
        self.assertEqual(progress()['recovery'][0]['start_char'], 4001)
        a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('read_line', document_id=did, line_number=1, start_char=4001)), max_steps=1)
        self.assertEqual(progress(), {'id': 1, 'read_steps': [3, 4, 7], 'missing_ranges': [],
                                     'reuse_document_steps': [3]})
        completed = {**task, 'state': 'done', 'evidence_steps': [3, 4, 7]}
        engine = ScriptedEngine(action('update_plan', items=[completed], reuse_document_steps=[3]),
                                action('finish', report=f'核准 CK-35 [D{did}:L1:C8001-C8100]。'))
        final = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(final['status'], 'report_ready')
        match, = final['work_plan']['evidence_matches']
        self.assertEqual([row['sequence'] for row in match['line_page_proofs']], [3, 4, 7])
        validate_database(restored.path)
        changed = copy.deepcopy(final['steps'][3]['observation'])
        changed['excerpts'][0]['text'] = changed['excerpts'][0]['text'].replace('CK-35', 'CK-99')
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(changed, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作不一致'):
            validate_database(restored.path)

    def test_missing_source_range_keeps_readable_evidence_and_blocks_delivery(self):
        path = self.root / 'window.txt'
        path.write_text('窗口是北庫', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口和正式門號。')
        first = {'document_id': did, 'start_line': 1, 'end_line': 1}
        absent = {**first, 'start_line': 2, 'end_line': 2}
        task = item(1, '核對兩行')
        result = self.run_actions(action('update_plan', items=[task], verify_delivery=True,
                                         evidence_requirements=[{'id': 1, 'ranges': [first, absent]}]),
                                  action('read_ranges', ranges=[first, absent]),
                                  action('finish', report='提前交付。'),
                                  action('ask', question='請提供第二行門號原文。',
                                         partial_result=f'已知窗口是北庫 [D{did}:L1]。'), gid=gid)
        self.assertEqual(result['status'], 'needs_input')
        self.assertIn('交付核對', result['steps'][2]['observation']['error'])
        self.assertEqual(result['work_plan']['delivery_review']['document_read_progress'],
                         [{'id': 1, 'read_steps': [2], 'missing_ranges': [absent], 'reuse_document_steps': [],
                           'recovery': [{**absent, 'action': 'source_missing'}]}])
        self.assertIn('北庫', result['steps'][-1]['observation']['partial_result'])
        validate_database(self.store.path)

    def test_explicit_missing_line_supplement_resumes_original_plan_after_restore(self):
        for name, body in [('window.txt', '窗口是北庫'), ('gate.txt', '交件紀錄')]:
            path = self.root / name
            path.write_text(body, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口與門號，附來源交付。')
        original, missing, replacement = [dict(document_id=did, start_line=line, end_line=line)
                                         for did, line in ((1, 1), (2, 2), (3, 1))]
        task = item(1, '核對窗口與門號')
        requirements = [{'id': 1, 'ranges': [original, missing]}]
        self.run_actions(action('update_plan', items=[task], verify_delivery=True, evidence_requirements=requirements),
                         action('read_ranges', ranges=[original, missing]),
                         action('ask', question='請提供 D2 第 2 行。', partial_result='窗口是北庫 [D1:L1]。'), gid=gid)
        old_view = goal_guidance.snapshot(self.store, self.sid, gid)
        for name, body in [('new.txt', '正式門號為 G-17'), ('other.txt', '另一批門號為 G-90')]:
            path = self.root / name
            path.write_text(body, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        command = '補件 [D2:L2] 改由 [D3:L1]\n請撤下目前問題。'
        with self.assertRaises(goal_guidance.StaleGuidance):
            goal_guidance.save(self.store, self.sid, gid, command, expected_fingerprint=old_view['fingerprint'])
        current = goal_guidance.snapshot(self.store, self.sid, gid)
        self.assertTrue(current['plan_document_supplement'])
        for bad in ('補件 [D2:L2] 改由 [D3:L1]', command.replace('D2:L2', 'D1:L1'),
                    command.replace('D3:L1', 'D3:L2'), command.replace('D3:L1', 'D2:L1')):
            with self.subTest(command=bad), self.assertRaises(ValueError):
                goal_guidance.save(self.store, self.sid, gid, bad, expected_fingerprint=current['fingerprint'])
            self.assertEqual(goal_guidance.snapshot(self.store, self.sid, gid), current)
        saved = goal_guidance.save(self.store, self.sid, gid, command, expected_fingerprint=current['fingerprint'])
        self.assertEqual(saved['goal']['status'], 'paused')
        self.assertTrue(saved['sources_match'])
        self.assertEqual(saved['goal']['withdrawn_questions'][0]['question'], '請提供 D2 第 2 行。')
        backup = create_backup(self.store.path, self.root / 'supplemented.paibackup')
        restore_backup(backup['path'], self.root / 'supplemented')
        restored = Store(self.root / 'supplemented' / 'assistant.sqlite3')
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual([row['document_id'] for row in state['sources']], [1, 2, 3])
        self.assertFalse(state.get('reply_sources'))
        self.assertEqual(state['steps'][2]['observation']['partial_result'], '窗口是北庫 [D1:L1]。')
        self.assertEqual(state['work_plan']['evidence_requirements'][0]['ranges'], [original, replacement])
        self.assertEqual(state['work_plan']['delivery_review']['document_read_progress'],
                         [{'id': 1, 'read_steps': [2, 4], 'missing_ranges': [], 'reuse_document_steps': [2]}])
        done = {**task, 'state': 'done', 'evidence_steps': [2, 4]}
        engine = ScriptedEngine(action('update_plan', items=[done], reuse_document_steps=[2], evidence_requirements=requirements),
                                action('update_plan', items=[done], reuse_document_steps=[2]),
                                action('finish', report='窗口北庫 [D1:L1]。正式門號 G-17 [D3:L1] [更正#1]。'))
        final = a.run_goal(restored, self.sid, gid, engine)
        self.assertIn('正式補件', final['steps'][4]['observation']['error'])
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['evidence_matches'][0]['supplement_read_proofs'][0]['sequence'], 4)
        validate_database(restored.path)
        with restored.connect() as db:
            db.execute('DELETE FROM autonomous_question_withdrawals WHERE goal_id=?', (gid,))
        with self.assertRaisesRegex(ValueError, '原提問撤下紀錄'):
            validate_database(restored.path)

    def test_partial_range_supplement_keeps_read_prefix_and_independent_missing_lines(self):
        for name, body in [('record.txt', '窗口是北庫\n簽收人青禾'), ('seal.txt', '封條紀錄')]:
            path = self.root / name
            path.write_text(body, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '依工作清單核對交件紀錄和封條。')
        first = {'document_id': 1, 'start_line': 1, 'end_line': 4}
        prefix = {**first, 'end_line': 2}
        shared = {**first, 'start_line': 4}
        absent = {'document_id': 2, 'start_line': 2, 'end_line': 2}
        supplied = {'document_id': 3, 'start_line': 1, 'end_line': 2}
        tasks = [item(1, '交件紀錄'), item(2, '核對期限及封條')]
        self.run_actions(action('update_plan', items=tasks, verify_delivery=True,
                                evidence_requirements=[{'id': 1, 'ranges': [first]}, {'id': 2, 'ranges': [shared, absent]}]),
                         action('read_ranges', ranges=[first, prefix, absent]),
                         action('ask', question='請提供交件紀錄第 3 至 4 行及封條第 2 行。',
                                partial_result='北庫與青禾已核對。[D1:L1-L2]'), gid=gid)
        supplement = self.root / 'supplement.txt'
        supplement.write_text('門號 G-17\n截止 16:20', encoding='utf-8')
        self.store.attach_document(self.sid, supplement)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        with self.assertRaisesRegex(ValueError, '可讀原行'):
            goal_guidance.save(self.store, self.sid, gid, '補件 [D1:L2-L3] 改由 [D3:L1-L2]\n請撤下目前問題。',
                               expected_fingerprint=view['fingerprint'])
        self.assertEqual(goal_guidance.snapshot(self.store, self.sid, gid), view)
        goal_guidance.save(self.store, self.sid, gid, '補件 [D1:L3-L4] 改由 [D3:L1-L2]\n請撤下目前問題。',
                           expected_fingerprint=view['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'partial-supplement.paibackup')
        restore_backup(backup['path'], self.root / 'partial-supplement')
        restored = Store(self.root / 'partial-supplement' / 'assistant.sqlite3')
        restored_goal = a.get_goal(restored, self.sid, gid)
        plan = restored_goal['work_plan']
        self.assertEqual([spec['ranges'] for spec in plan['evidence_requirements']],
                         [[prefix, supplied], [{**supplied, 'start_line': 2}, absent]])
        self.assertEqual(plan['delivery_review']['document_read_progress'],
                         [{'id': 1, 'read_steps': [2, 4], 'missing_ranges': [], 'reuse_document_steps': [2]},
                          {'id': 2, 'read_steps': [4], 'missing_ranges': [absent], 'reuse_document_steps': [],
                           'recovery': [{**absent, 'action': 'source_missing'}]}])
        done = {**tasks[0], 'state': 'done', 'evidence_steps': [2, 4]}
        engine = ScriptedEngine(action('update_plan', items=[done, tasks[1]], reuse_document_steps=[2]),
                                action('finish', report='提前定稿。'),
                                action('ask', question='請補封條第 2 行。',
                                       plan_questions=[{'question': '請補封條第 2 行。', 'items': [2]}],
                                       partial_result='窗口北庫、簽收人青禾 [D1:L1-L2]。門號 G-17、截止 16:20 [D3:L1-L2]。'))
        waiting = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertIn('未完成項目 2', waiting['steps'][5]['observation']['error'])
        self.assertEqual(waiting['work_plan']['items'][0]['state'], 'done')
        self.assertEqual(waiting['work_plan']['items'][1]['state'], 'pending')
        validate_database(restored.path)

        seal = self.root / 'seal-supplement.txt'
        seal.write_text('封條為 F-02', encoding='utf-8')
        self.assertEqual(restored.attach_document(self.sid, seal), 4)
        view = goal_guidance.snapshot(restored, self.sid, gid)
        self.assertTrue(view['plan_document_supplement'])
        goal_guidance.save(restored, self.sid, gid, '補件 [D2:L2] 改由 [D4:L1]\n請撤下目前問題。',
                           expected_fingerprint=view['fingerprint'])
        both_done = [done, {**tasks[1], 'state': 'done', 'evidence_steps': [4, 8]}]
        engine = ScriptedEngine(action('update_plan', items=both_done, reuse_document_steps=[2, 4]),
                                action('finish', report='窗口北庫 [D1:L1]。截止 16:20 [D3:L2]。封條 F-02 [D4:L1]。'))
        final = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual([row['ask_sequence'] for row in final['withdrawn_questions']], [3, 7])
        self.assertEqual([row['read_steps'] for row in final['work_plan']['evidence_matches']], [[2, 4], [4, 8]])
        validate_database(restored.path)

    def test_supplement_requires_matching_items_and_an_exact_multipart_selection(self):
        for name in ('record.txt', 'seal.txt'):
            path = self.root / name
            path.write_text('原文件標題', encoding='utf-8')
            self.store.attach_document(self.sid, path)
        first, second = [dict(document_id=number, start_line=2, end_line=2) for number in (1, 2)]
        tasks = [item(1, '核對門號'), item(2, '核對封條')]
        specs = [{'id': 1, 'ranges': [first]}, {'id': 2, 'ranges': [second]}]
        questions = [{'question': '請補門號原文。', 'items': [1]}, {'question': '請補封條原文。', 'items': [2]}]
        goals = []
        for parts in ([questions[1]], questions):
            gid = a.create_goal(self.store, self.sid, '核對門號與封條。')
            self.run_actions(action('update_plan', items=tasks, verify_delivery=True, evidence_requirements=specs),
                             action('read_ranges', ranges=[first, second]),
                             action('ask', question='\n'.join(row['question'] for row in parts), plan_questions=parts), gid=gid)
            goals.append(gid)
        path = self.root / 'door-supplement.txt'
        path.write_text('門號 G-17', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        command = '補件 [D1:L2] 改由 [D3:L1]\n請撤下目前問題。'
        for gid, message in zip(goals, ('未涵蓋', '多個分問'), strict=True):
            view = goal_guidance.snapshot(self.store, self.sid, gid)
            with self.assertRaisesRegex(ValueError, message):
                goal_guidance.save(self.store, self.sid, gid, command, expected_fingerprint=view['fingerprint'])
            self.assertEqual(goal_guidance.snapshot(self.store, self.sid, gid), view)
        validate_database(self.store.path)

        gid = goals[1]
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        old_reply = goal_replies.snapshot(self.store, self.sid, gid)
        selected = command.replace('目前問題', '分問「請補門號原文。」')
        wrong = selected.replace('「請補門號原文。」', '「請補封條原文。」')
        with self.assertRaisesRegex(ValueError, '未涵蓋'):
            goal_guidance.save(self.store, self.sid, gid, wrong, expected_fingerprint=view['fingerprint'])
        saved = goal_guidance.save(self.store, self.sid, gid, selected, expected_fingerprint=view['fingerprint'])
        self.assertEqual(saved['goal']['status'], 'needs_input')
        self.assertEqual(saved['goal']['question'], questions[1]['question'])
        with self.assertRaises(goal_replies.StaleReply):
            goal_replies.save(self.store, self.sid, gid, '封條為 F-02。', expected_fingerprint=old_reply['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'remaining-question.paibackup')
        restore_backup(backup['path'], self.root / 'remaining-question')
        restored = Store(self.root / 'remaining-question' / 'assistant.sqlite3')
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual(state['steps'][-1]['action']['args']['plan_questions'], [questions[1]])
        self.assertEqual(state['steps'][3]['observation']['successor']['question'], questions[1]['question'])
        self.assertEqual(state['work_plan']['delivery_review']['document_read_progress'][0]['read_steps'], [4])
        self.assertEqual(state['work_plan']['delivery_review']['document_read_progress'][1]['missing_ranges'], [second])
        with restored.connect() as db:
            db.execute('UPDATE autonomous_goals SET question=? WHERE id=?', ('變更後問題？', gid))
        with self.assertRaisesRegex(ValueError, '保留的分問'):
            validate_database(restored.path)

    def test_supplement_binds_questions_for_the_same_item_to_their_document_ranges(self):
        for name, body in [('delivery.txt', '交件標題'), ('seal.txt', '封條標題')]:
            path = self.root / name
            path.write_text(body, encoding='utf-8')
            self.store.attach_document(self.sid, path)
        delivery = {'document_id': 1, 'start_line': 2, 'end_line': 3}
        seal = {'document_id': 2, 'start_line': 2, 'end_line': 2}
        task = item(1, '核對交件及封條')
        parts = [{'question': '請提供 D1 第 2 至 3 行。', 'items': [1]},
                 {'question': '請補 D2 第 2 行。', 'items': [1]},
                 {'question': '請確認是否需要紙本？', 'items': [1]}]
        gid = a.create_goal(self.store, self.sid, '依清單核對交件及封條。')
        self.run_actions(action('update_plan', items=[task], verify_delivery=True,
                                evidence_requirements=[{'id': 1, 'ranges': [delivery, seal]}]),
                         action('read_ranges', ranges=[delivery, seal]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts), gid=gid)
        path = self.root / 'delivery-supplement.txt'
        path.write_text('門號 G-17\n截止 16:20', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        complete = '補件 [D1:L2-L3] 改由 [D3:L1-L2]'
        for mapping, target, message in ((complete, parts[1], '全部文件行'),
                                         ('補件 [D1:L2] 改由 [D3:L1]', parts[0], '全部文件行'),
                                         (complete, parts[2], '文件編號與行號')):
            with self.subTest(target=target['question']), self.assertRaisesRegex(ValueError, message):
                goal_guidance.save(self.store, self.sid, gid, mapping + f'\n請撤下分問「{target["question"]}」',
                                   expected_fingerprint=view['fingerprint'])
            self.assertEqual(goal_guidance.snapshot(self.store, self.sid, gid), view)
        saved = goal_guidance.save(self.store, self.sid, gid, complete + f'\n請撤下分問「{parts[0]["question"]}」',
                                   expected_fingerprint=view['fingerprint'])
        self.assertEqual(saved['goal']['question'], '\n'.join(part['question'] for part in parts[1:]))
        backup = create_backup(self.store.path, self.root / 'bound-range.paibackup')
        restore_backup(backup['path'], self.root / 'bound-range')
        restored = Store(self.root / 'bound-range' / 'assistant.sqlite3')
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual(state['steps'][3]['observation']['question_ranges'], [delivery])
        self.assertEqual(state['steps'][-1]['action']['args']['plan_questions'], parts[1:])
        altered = {**state['steps'][3]['observation'], 'question_ranges': [seal]}
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=4',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '保存的查讀或原提問'):
            validate_database(restored.path)

    def test_reply_after_supplement_completes_only_its_confirmation_item(self):
        path = self.root / 'delivery.txt'
        path.write_text('窗口北庫', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口、門號與接收人，齊全才交付。')
        first = {'document_id': 1, 'start_line': 1, 'end_line': 1}
        missing = {**first, 'start_line': 2, 'end_line': 2}
        tasks = [item(1, '核對交件原文'), item(2, '確認接收人')]
        questions = [{'question': '請提供 D1 第 2 行。', 'items': [1]}, {'question': '接收人是誰？', 'items': [2]}]
        self.run_actions(action('update_plan', items=tasks, verify_delivery=True,
                                evidence_requirements=[{'id': 1, 'ranges': [first, missing]}]),
                         action('read_ranges', ranges=[first, missing]),
                         action('ask', question='\n'.join(row['question'] for row in questions), plan_questions=questions,
                                partial_result='窗口北庫 [D1:L1]。'), gid=gid)
        path = self.root / 'new.txt'
        path.write_text('門號 G-17', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '補件 [D1:L2] 改由 [D2:L1]\n請撤下分問「請提供 D1 第 2 行。」',
                           expected_fingerprint=view['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'reply-after-supplement.paibackup')
        restore_backup(backup['path'], self.root / 'reply-after-supplement')
        restored = Store(self.root / 'reply-after-supplement' / 'assistant.sqlite3')
        current = goal_replies.snapshot(restored, self.sid, gid)
        self.assertEqual(current['goal']['question'], questions[1]['question'])
        goal_replies.save(restored, self.sid, gid, '接收人為林怡安。', expected_fingerprint=current['fingerprint'])
        state = a.get_goal(restored, self.sid, gid)
        proof = state['work_plan']['scope_reply']
        self.assertEqual((proof['supplement_step'], proof['ask_sequence'], proof['citation'], proof['completed_items']),
                         (4, 5, '[補充#5]', [2]))
        self.assertEqual(proof['parts'][0]['value'], '林怡安')
        self.assertEqual([row['state'] for row in state['work_plan']['items']], ['pending', 'done'])
        self.assertEqual(state['work_plan']['delivery_review']['open_items'], [1])
        validate_database(restored.path)
        done = [{**tasks[0], 'state': 'done', 'evidence_steps': [2, 4]}, state['work_plan']['items'][1]]
        final = a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('finish', report='提前定稿。'),
            action('update_plan', items=done, reuse_document_steps=[2, 4]),
            action('finish', report='窗口北庫 [D1:L1]。門號 G-17 [D2:L1]。接收人林怡安 [補充#5]。')))
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('未完成項目 1', final['steps'][6]['observation']['error'])
        self.assertEqual(final['work_plan']['items'][1]['evidence_steps'], [6])
        validate_database(restored.path)
        altered = copy.deepcopy(state['steps'][5]['observation'])
        altered['scope_reply']['supplement_step'] = 2
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作不一致'):
            validate_database(restored.path)

    def test_supplement_preserves_a_verified_reply_given_before_it(self):
        path = self.root / 'delivery.txt'
        path.write_text('窗口北庫', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口、門號和接收人。')
        first = {'document_id': 1, 'start_line': 1, 'end_line': 1}
        missing = {**first, 'start_line': 2, 'end_line': 2}
        tasks = [item(1, '核對交件原文'), item(2, '確認接收人')]
        questions = [{'question': '請提供 D1 第 2 行。', 'items': [1]}, {'question': '接收人是誰？', 'items': [2]}]
        self.run_actions(action('update_plan', items=tasks, verify_delivery=True,
                                evidence_requirements=[{'id': 1, 'ranges': [first, missing]}]),
                         action('read_ranges', ranges=[first, missing]),
                         action('ask', question='\n'.join(row['question'] for row in questions), plan_questions=questions), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual((waiting['status'], waiting['question']), ('needs_input', questions[0]['question']))
        self.assertEqual(waiting['work_plan']['scope_reply']['completed_items'], [2])
        self.assertEqual(waiting['work_plan']['items'][1]['evidence_steps'], [4])
        path = self.root / 'supplement.txt'
        path.write_text('門號 G-17', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '補件 [D1:L2] 改由 [D2:L1]\n請撤下目前問題。',
                           expected_fingerprint=view['fingerprint'])
        backup = create_backup(self.store.path, self.root / 'answered-before-supplement.paibackup')
        restore_backup(backup['path'], self.root / 'answered-before-supplement')
        restored = Store(self.root / 'answered-before-supplement' / 'assistant.sqlite3')
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual(state['work_plan']['delivery_review']['retained_reply_items'],
                         [{'id': 2, 'reply_step': 4, 'citation': '[補充#3]'}])
        self.assertNotIn(2, state['work_plan']['delivery_review']['recheck_items'])
        completed = [{**tasks[0], 'state': 'done', 'evidence_steps': [2, 6]}, state['work_plan']['items'][1]]
        engine = ScriptedEngine(action('update_plan', items=[completed[0], tasks[1]], reuse_document_steps=[2]),
                                action('update_plan', items=completed, reuse_document_steps=[2]),
                                action('update_plan', items=completed, reuse_document_steps=[2]),
                                action('finish', report='窗口北庫 [D1:L1]。門號 G-17 [D2:L1]。接收人林怡安 [補充#3]。'))
        final = a.run_goal(restored, self.sid, gid, engine)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('原答覆確認', final['steps'][6]['observation']['error'])
        self.assertEqual(final['work_plan']['items'][1]['evidence_steps'], [4])
        validate_database(restored.path)
        altered = copy.deepcopy(final['steps'][5]['observation'])
        altered['retained_reply_items'][0]['reply_step'] = 3
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=6',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '清單補件與保存'):
            validate_database(restored.path)

    def test_one_item_requires_both_source_lines_and_the_bound_confirmation(self):
        path = self.root / 'delivery.txt'
        path.write_text('窗口北庫', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        first = {'document_id': 1, 'start_line': 1, 'end_line': 1}
        missing = {**first, 'start_line': 2, 'end_line': 2}
        task = item(1, '核對交件及接收人')
        questions = [{'question': '請提供 D1 第 2 行。', 'items': [1]}, {'question': '接收人是誰？', 'items': [1]}]
        requirement = {'id': 1, 'ranges': [first, missing], 'confirmation_question': questions[1]['question']}
        for reply_first in (True, False):
            with self.subTest(reply_first=reply_first):
                gid = a.create_goal(self.store, self.sid, '核對窗口、門號及接收人。')
                self.run_actions(action('update_plan', items=[task], verify_delivery=True, evidence_requirements=[requirement]),
                                 action('read_ranges', ranges=[first, missing]),
                                 action('ask', question='\n'.join(row['question'] for row in questions),
                                        plan_questions=questions), gid=gid)
                if reply_first:
                    reply = goal_replies.snapshot(self.store, self.sid, gid)
                    goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
                    waiting = a.get_goal(self.store, self.sid, gid)
                    self.assertEqual(waiting['question'], questions[0]['question'])
                    self.assertEqual(waiting['work_plan']['items'][0]['state'], 'pending')
                supplement = self.root / f'supplement-{reply_first}.txt'
                supplement.write_text('門號 G-17', encoding='utf-8')
                did = self.store.attach_document(self.sid, supplement)
                view = goal_guidance.snapshot(self.store, self.sid, gid)
                dismissal = '請撤下目前問題。' if reply_first else '請撤下分問「請提供 D1 第 2 行。」'
                goal_guidance.save(self.store, self.sid, gid, f'補件 [D1:L2] 改由 [D{did}:L1]\n{dismissal}',
                                   expected_fingerprint=view['fingerprint'])
                if not reply_first:
                    waiting = a.get_goal(self.store, self.sid, gid)
                    self.assertEqual(waiting['question'], questions[1]['question'])
                    self.assertEqual(waiting['work_plan']['items'][0]['state'], 'pending')
                    reply = goal_replies.snapshot(self.store, self.sid, gid)
                    goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
                backup = create_backup(self.store.path, self.root / f'combined-{reply_first}.paibackup')
                destination = self.root / f'combined-{reply_first}'
                restore_backup(backup['path'], destination)
                restored = Store(destination / 'assistant.sqlite3')
                state = a.get_goal(restored, self.sid, gid)
                confirmation, = state['work_plan']['delivery_review']['verified_confirmations']
                self.assertEqual(confirmation['value'], '林怡安')
                self.assertEqual(state['work_plan']['items'][0]['state'], 'pending')
                read = 6 if reply_first else 4
                reused = [2] if reply_first else [2, read]
                ref = confirmation['reply_step']
                actions = [action('update_plan', items=[{**task, 'state': 'done', 'evidence_steps': [2, read]}],
                                  reuse_document_steps=reused),
                           action('update_plan', items=[{**task, 'state': 'done', 'evidence_steps': [ref]}]),
                           action('update_plan', items=[{**task, 'state': 'done', 'evidence_steps': [2, read, ref]}],
                                  reuse_document_steps=reused),
                           action('finish', report=f'北庫 [D1:L1]。門號 G-17 [D{did}:L1]。林怡安 {confirmation["citation"]}。')]
                final = a.run_goal(restored, self.sid, gid, ScriptedEngine(*actions))
                self.assertEqual(final['status'], 'report_ready')
                self.assertIn('已核對答覆', final['steps'][6]['observation']['error'])
                self.assertIn('完整指定文件範圍', final['steps'][7]['observation']['error'])
                self.assertEqual(final['work_plan']['evidence_matches'][0]['confirmation'], confirmation)
                validate_database(restored.path)

    def test_one_reply_uses_the_current_answer_and_revises_a_prior_verified_field(self):
        path = self.root / 'desk.txt'
        path.write_text('接收點 青檯\n窗口 段映台', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '按指定原行核對接收人、門號及稽核員。')
        one = {'document_id': did, 'start_line': 1, 'end_line': 1}
        two = {**one, 'start_line': 2, 'end_line': 2}
        rows = [item(1, '核對接收人及接收點'), item(2, '核對門號及窗口'), item(3, '核對稽核員及接收點')]
        parts = [{'question': question, 'items': [number]} for number, question in enumerate(
            ['接收人是誰？', '門號是多少？', '稽核員是誰？'], 1)]
        requirements = [{'id': number, 'ranges': [two if number == 2 else one], 'confirmation_question': part['question']}
                        for number, part in enumerate(parts, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=requirements),
                         action('read_ranges', ranges=[one, two]),
                         action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts), gid=gid)

        def reply(text):
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)

        original = reply('接收人為何佳靜。稽核員為楊正哲。')
        self.assertEqual(original['question'], parts[1]['question'])
        amended = reply('接收人正式改為許庭芳。')
        self.assertEqual(amended['question'], original['question'])
        self.assertEqual(amended['steps'][-1]['action']['args']['plan_questions'], [parts[1]])
        raw = '門號為 7 號。更正「接收人是誰？」為「周明恩」。'
        state = reply(raw)
        self.assertEqual(state['status'], 'paused')
        confirmation = {row['id']: (row['value'], row['reply_step']) for row in
                        state['work_plan']['delivery_review']['verified_confirmations']}
        self.assertEqual(confirmation, {1: ('周明恩', 8), 2: ('7 號', 8), 3: ('楊正哲', 4)})
        proof = state['steps'][7]['observation']['scope_reply']
        self.assertEqual(proof['revisions'][0]['previous']['step'], 6)
        for clause in (proof['parts'][0]['evidence'], proof['revisions'][0]['evidence']):
            self.assertEqual(raw[clause['start']:clause['end']], clause['quote'])

        archive = create_backup(self.store.path, self.root / 'combined-correction.paibackup')
        destination = self.root / 'combined-correction'
        restore_backup(archive['path'], destination)
        restored = Store(destination / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), state)
        done = [{**row, 'state': 'done', 'evidence_steps': [2, confirmation[row['id']][1]]} for row in rows]
        stale = [{**done[0], 'evidence_steps': [2, 6]}, *done[1:]]
        report = '北側青檯 [D1:L1]。窗口段映台 [D1:L2]。周明恩 [補充#7]。7 號 [補充#7]。楊正哲 [補充#3]。'
        final = a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('update_plan', items=stale, reuse_document_steps=[2]),
            action('update_plan', items=done, reuse_document_steps=[2]),
            action('finish', report=report.replace('周明恩 [補充#7]', '許庭芳 [補充#5]')),
            action('finish', report=report)), max_steps=4)
        self.assertEqual(final['status'], 'report_ready', final['steps'][-4:])
        self.assertIn('實際使用者答覆', final['steps'][8]['observation']['error'])
        self.assertIn('現行已核對的答覆', final['steps'][10]['observation']['error'])
        validate_database(restored.path)
        altered = copy.deepcopy(state['steps'][7]['observation'])
        altered['scope_reply']['revisions'][0]['previous']['step_sha256'] = 'a' * 64
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=8',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(restored.path)

    def test_scoped_correction_and_target_choice_preserve_independent_answers(self):
        path = self.root / 'handoff.txt'
        path.write_text('北廊青檯\n南廊桂台\n門號石門\n封條木箱', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對兩廊接收人、門號與封條編號。')
        rows = [item(number, task) for number, task in enumerate(
            ['核對北廊交接', '核對南廊交接', '核對門號', '核對封條'], 1)]
        questions = ['北廊的接收人是誰？', '南廊的接收人是誰？', '門號是多少？', '封條編號是什麼？']
        parts = [{'question': question, 'items': [number]} for number, question in enumerate(questions, 1)]
        ranges = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 5)]
        requirements = [{'id': n, 'ranges': [ranges[n - 1]], 'confirmation_question': question}
                        for n, question in enumerate(questions, 1)]

        def ask(*numbers):
            return action('ask', question='\n'.join(questions[n - 1] for n in numbers),
                          plan_questions=[parts[n - 1] for n in numbers])

        def reply(store, answer):
            view = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, answer, expected_fingerprint=view['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def restored(store, label):
            before = a.get_goal(store, self.sid, gid)
            backup = create_backup(store.path, self.root / f'{label}.paibackup')
            destination = self.root / label
            restore_backup(backup['path'], destination)
            cloned = Store(destination / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(cloned, self.sid, gid), before)
            return cloned

        def confirmations(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=requirements),
                         action('read_ranges', ranges=ranges), ask(1), gid=gid)
        reply(self.store, '接收人為蕭明琪。')
        self.run_actions(ask(2), gid=gid)
        previous = confirmations(reply(self.store, '接收人為林晴川。'))
        completed = [{**row, 'state': 'done', 'evidence_steps': [2, previous[row['id']]['reply_step']]}
                     for row in rows[:2]]
        before = self.run_actions(action('update_plan', items=[*completed, *rows[2:]], reuse_document_steps=[2]),
                                  ask(3, 4), gid=gid)

        qualified = restored(self.store, 'qualified')
        direct = reply(qualified, '門號為 8 號，「北廊的接收人」正式改為王昕琪。')
        direct_confirmations = confirmations(direct)
        self.assertEqual(direct['question'], questions[3])
        self.assertEqual(direct_confirmations[1]['value'], '王昕琪')
        self.assertEqual(direct_confirmations[2], previous[2])
        self.assertEqual(direct_confirmations[3]['value'], '8 號')
        self.assertEqual(direct['work_plan']['items'][1], before['work_plan']['items'][1])
        revision, = direct['steps'][-2]['observation']['scope_reply']['revisions']
        self.assertEqual(revision['recheck_items'], [1])
        self.assertEqual(revision['previous']['step'], previous[1]['reply_step'])
        validate_database(qualified.path)

        colloquial = restored(self.store, 'colloquial-target')
        proposed = reply(colloquial, '門號為 8 號。接收人改派王昕琪。')
        proposal_step = proposed['steps'][-2]
        self.assertEqual(proposal_step['action']['args']['scope_reply_version'], 29)
        proposed_values = confirmations(proposed)
        self.assertEqual({n: proposed_values[n] for n in (1, 2)}, previous)
        self.assertEqual(proposed_values[3]['value'], '8 號')
        self.assertEqual(proposed['steps'][-1]['action']['args']['plan_questions'][1], parts[3])
        colloquial = restored(colloquial, 'colloquial-choice')
        selected = reply(colloquial, '「北廊的接收人」改派王昕琪。南廊的接收人不要改派吳景辰。封條編號為Q-99。')
        selected_values = confirmations(selected)
        self.assertEqual(selected['status'], 'paused')
        self.assertEqual(selected_values[1]['value'], '王昕琪')
        self.assertEqual(selected_values[2], previous[2])
        self.assertEqual(selected_values[3], proposed_values[3])
        self.assertEqual(selected_values[4]['value'], 'Q-99')
        saved = selected['steps'][-1]['observation']['scope_reply']
        self.assertEqual(saved['confirmation_target_resolution'],
                         {'step': proposal_step['sequence'], 'decision': 'correct', 'question': questions[0]})
        self.assertEqual(saved['unapplied_directives'][0]['questions'], [questions[1]])
        validate_database(colloquial.path)

        pending = reply(self.store, '門號為 8 號。接收人改為王昕琪。')
        known = confirmations(pending)
        self.assertEqual({key: known[key] for key in (1, 2)}, previous)
        self.assertEqual(known[3]['value'], '8 號')
        first, remaining = pending['steps'][-1]['action']['args']['plan_questions']
        self.assertEqual(first['items'], [1, 2])
        self.assertTrue(all(question in first['question'] for question in questions[:2]))
        self.assertEqual(remaining, parts[3])
        with self.assertRaisesRegex(ValueError, '待選更正'):
            plan = p.latest(pending['steps'])
            p.check_delivery(plan, plan['goal_conditions_sha256'], {'tool': 'finish', 'args': {'report': '交付'}},
                             pending['steps'])
        with self.assertRaisesRegex(ValueError, '多個工作分問'):
            reply(self.store, '接收人改為王昕琪。')
        self.assertEqual(a.get_goal(self.store, self.sid, gid), pending)

        kept = restored(self.store, 'kept')
        keep_result = reply(kept, '都不改。封條編號為Q-99。')
        self.assertEqual(keep_result['status'], 'paused')
        self.assertEqual({key: confirmations(keep_result)[key] for key in known}, known)
        self.assertEqual(confirmations(keep_result)[4]['value'], 'Q-99')
        validate_database(kept.path)

        resumed = restored(self.store, 'resumed')
        partial = reply(resumed, '封條編號為Q-99。')
        self.assertEqual(partial['question'], first['question'])
        self.assertEqual({key: confirmations(partial)[key] for key in known}, known)
        current = reply(resumed, '「南廊的接收人」改為王昕琪。')
        checked = confirmations(current)
        self.assertEqual(current['status'], 'paused')
        self.assertEqual(checked[1], previous[1])
        self.assertEqual(checked[2]['value'], '王昕琪')
        self.assertEqual(checked[3], known[3])
        self.assertEqual(checked[4]['value'], 'Q-99')
        self.assertEqual(current['work_plan']['items'][0], completed[0])
        self.assertEqual(current['steps'][-1]['observation']['scope_reply']['confirmation_target_resolution'],
                         {'step': pending['steps'][-2]['sequence'], 'decision': 'correct', 'question': questions[1]})

        final_store = restored(resumed, 'resolved')
        done = [{**row, 'state': 'done', 'evidence_steps': [2, checked[row['id']]['reply_step']]} for row in rows]
        report = '\n'.join(f'{value["value"]} {value["citation"]} [D1:L{number}]' for number, value in sorted(checked.items()))
        final = a.run_goal(final_store, self.sid, gid, ScriptedEngine(
            action('update_plan', items=done, reuse_document_steps=[2]), action('finish', report=report)), max_steps=2)
        self.assertEqual(final['status'], 'report_ready', final['steps'][-2:])
        validate_database(final_store.path)

        origin = pending['steps'][-2]
        altered = copy.deepcopy(origin['observation'])
        altered['scope_reply']['confirmation_target_choice']['questions'].reverse()
        with final_store.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(altered, ensure_ascii=False), gid, origin['sequence']))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(final_store.path)

    def test_colloquial_questions_bind_counts_units_and_people_to_the_original_parts(self):
        source = self.root / 'colloquial-plan.txt'
        source.write_text('本批外殼待確認。\n每盤依本批清單。\n架號以本輪為準。\n由當輪稽核員核對。\n封條由本輪指定。',
                          encoding='utf-8')
        did = self.store.attach_document(self.sid, source)
        gid = a.create_goal(self.store, self.sid, '核對外殼、育苗盤數、架號、稽核員和封條號。')
        questions = ['外殼材料是什麼？', '育苗要準備幾盤？', '架號是多少？', '稽核員是哪一位？', '封條號是多少？']
        names = ('外殼材料', '育苗盤數', '架號', '稽核員', '封條號')
        rows = [item(n, '核對' + name) for n, name in enumerate(names, 1)]
        parts = [{'question': q, 'items': [n]} for n, q in enumerate(questions, 1)]
        ranges = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 6)]
        required = [{'id': n, 'confirmation_question': q, 'ranges': [ranges[n - 1]]} for n, q in enumerate(questions, 1)]

        def ask(*numbers):
            selected = [parts[n - 1] for n in numbers]
            return action('ask', question='\n'.join(row['question'] for row in selected), plan_questions=selected)

        def reply(store, text):
            current = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, text, expected_fingerprint=current['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def checked(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        def restore(store, label):
            snapshot = a.get_goal(store, self.sid, gid)
            target = self.root / label
            archive = create_backup(store.path, target.with_suffix('.paibackup'))
            restore_backup(archive['path'], target)
            cloned = Store(target / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(cloned, self.sid, gid), snapshot)
            return cloned

        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=ranges), ask(1), gid=gid)
        original = checked(reply(self.store, '外殼材料為不鏽鋼。'))[1]
        self.run_actions(ask(2, 3), gid=gid)
        bare = restore(self.store, 'bare-colloquial-answer')
        complete = reply(bare, '6 盤。架號為 8 號。')
        self.assertEqual(complete['status'], 'paused')
        self.assertEqual({n: row['value'] for n, row in checked(complete).items()},
                         {1: '不鏽鋼', 2: '6 盤', 3: '8 號'})
        validate_database(bare.path)

        for index, raw in enumerate(('育苗盤數為 6 盤或 7 盤。', '6 盒。', '育苗盤數為 6 盤 2 盒。'), 1):
            with self.subTest(raw=raw):
                store = restore(self.store, f'uncertain-colloquial-{index}')
                waiting = reply(store, raw + '架號為 8 號。')
                known = checked(waiting)
                self.assertEqual(known[1], original)
                self.assertEqual(known[3]['value'], '8 號')
                self.assertNotIn(2, known)
                self.assertEqual(waiting['question'], questions[1])
                self.assertEqual(waiting['steps'][-1]['action']['args']['plan_questions'], [parts[1]])
                validate_database(store.path)

        resumed = restore(Store(self.root / 'uncertain-colloquial-1' / 'assistant.sqlite3'), 'resolved-colloquial-answer')
        accepted = reply(resumed, '育苗盤數為 6 盤。')
        accepted_values = checked(accepted)
        self.assertEqual(accepted['status'], 'paused')
        self.assertEqual(accepted_values[2]['value'], '6 盤')
        a.run_goal(resumed, self.sid, gid, ScriptedEngine(ask(4)), max_steps=1)
        person = reply(resumed, '稽核員為黃未晴。')
        self.assertEqual(checked(person)[4]['value'], '黃未晴')
        a.run_goal(resumed, self.sid, gid, ScriptedEngine(ask(5)), max_steps=1)
        correction = '封條號為 9 號。育苗盤數正式改為 7 盤。'
        corrected = reply(resumed, correction)
        current = checked(corrected)
        self.assertEqual(current[1], original)
        self.assertEqual(current[3], accepted_values[3])
        self.assertEqual(current[2]['value'], '7 盤')
        self.assertEqual(current[5]['value'], '9 號')
        change, = corrected['work_plan']['scope_reply']['revisions']
        self.assertEqual(change['previous']['step'], accepted_values[2]['reply_step'])
        span = change['value_evidence']
        self.assertEqual(correction[span['start']:span['end']], '7 盤')
        validate_database(resumed.path)
        restore(resumed, 'corrected-colloquial-answer')

    def test_standalone_text_correction_reopens_only_the_edited_completed_item(self):
        document = self.root / 'closed-work-plan.txt'
        document.write_text('外殼位於甲線。\n盒數依答覆。\n集合點依答覆。', encoding='utf-8')
        did = self.store.attach_document(self.sid, document)
        gid = a.create_goal(self.store, self.sid, '定稿外殼材料、盒數及集合點。')
        questions = ['外殼材料是什麼？', '盒數是多少？', '集合點在哪裡？']
        rows = [item(n, '核對' + label) for n, label in enumerate(('外殼材料', '盒數', '集合點'), 1)]
        ranges = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 4)]
        parts = [{'question': q, 'items': [n]} for n, q in enumerate(questions, 1)]
        required = [{'id': n, 'confirmation_question': q, 'ranges': [ranges[n - 1]]} for n, q in enumerate(questions, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=ranges),
                         action('ask', question='\n'.join(questions), plan_questions=parts), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '外殼材料為鋁合金。盒數為 6 盒。集合點在未央館。',
                          expected_fingerprint=reply['fingerprint'])

        def checked(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        values = checked(a.get_goal(self.store, self.sid, gid))
        done = [{**row, 'state': 'done', 'evidence_steps': [2, values[row['id']]['reply_step']]} for row in rows]
        report = '\n'.join(f'{row["value"]} {row["citation"]} [D1:L{n}]' for n, row in sorted(values.items()))
        old = self.run_actions(action('update_plan', items=done, reuse_document_steps=[2]),
                               action('finish', report=report), gid=gid)
        self.assertEqual(old['status'], 'report_ready')
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        with self.assertRaisesRegex(ValueError, '明確的新值'):
            goal_guidance.save(self.store, self.sid, gid, '外殼材料正式改為可能是不鏽鋼。',
                               expected_fingerprint=view['fingerprint'])
        self.assertEqual(a.get_goal(self.store, self.sid, gid), old)
        raw = '外殼材料正式改為「不鏽鋼」。'
        goal_guidance.save(self.store, self.sid, gid, raw, expected_fingerprint=view['fingerprint'])
        state = a.get_goal(self.store, self.sid, gid)
        current = checked(state)
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['steps'][:-1], old['steps'])
        self.assertEqual(state['work_plan']['items'][1:], old['work_plan']['items'][1:])
        self.assertEqual(current[2], values[2])
        self.assertEqual(current[3], values[3])
        self.assertEqual(current[1]['value'], '不鏽鋼')
        step = state['steps'][-1]
        self.assertEqual(step['action']['args']['confirmation_correction_version'], 2)
        change, = step['observation']['scope_change']['revisions']
        span = change['value_evidence']
        self.assertEqual(raw[span['start']:span['end']], '不鏽鋼')
        self.assertEqual(change['previous']['value_evidence']['quote'], '鋁合金')
        self.assertEqual(change['recheck_items'], [1])

        archive = create_backup(self.store.path, self.root / 'text-guidance.paibackup')
        target = self.root / 'after-text-guidance'
        restore_backup(archive['path'], target)
        resumed = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(resumed, self.sid, gid), state)
        done[0]['evidence_steps'] = [2, current[1]['reply_step']]
        corrected_report = '\n'.join(f'{row["value"]} {row["citation"]} [D1:L{n}]' for n, row in sorted(current.items()))
        final = a.run_goal(resumed, self.sid, gid, ScriptedEngine(
            action('update_plan', items=done, reuse_document_steps=[2]),
            action('finish', report=report),
            action('finish', report=corrected_report)), max_steps=3)
        self.assertIn('現行已核對的答覆', final['steps'][-2]['observation']['error'])
        self.assertEqual(final['status'], 'report_ready')
        validate_database(resumed.path)

    def test_standalone_directives_update_only_confirmed_fields_and_replay_exact_sources(self):
        document = self.root / 'verified-work.txt'
        document.write_text('外殼依確認。\n交接依確認。\n集合依確認。\n架號依確認。', encoding='utf-8')
        did = self.store.attach_document(self.sid, document)
        gid = a.create_goal(self.store, self.sid, '定稿外殼材料、接收人、集合點與架號。')
        labels = ['外殼材料', '接收人', '集合點', '架號']
        questions = ['外殼材料是什麼？', '接收人是誰？', '集合點在哪裡？', '架號是多少？']
        rows = [item(n, '核對' + label) for n, label in enumerate(labels, 1)]
        ranges = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 5)]
        parts = [{'question': question, 'items': [n]} for n, question in enumerate(questions, 1)]
        required = [{'id': n, 'confirmation_question': question, 'ranges': [ranges[n - 1]]}
                    for n, question in enumerate(questions, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=ranges),
                         action('ask', question='\n'.join(questions), plan_questions=parts), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '外殼材料為鋁合金。接收人為林怡安。集合點在北廊。架號為 9 號。',
                          expected_fingerprint=reply['fingerprint'])

        def checked(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        def deliverable(values):
            return '\n'.join(f'{labels[n - 1]}：{row["value"]} {row["citation"]} [D1:L{n}]'
                             for n, row in sorted(values.items()))

        def completed(values):
            return [{**row, 'state': 'done', 'evidence_steps': [2, values[row['id']]['reply_step']]} for row in rows]

        initial = checked(a.get_goal(self.store, self.sid, gid))
        original = self.run_actions(action('update_plan', items=completed(initial), reuse_document_steps=[2]),
                                    action('finish', report=deliverable(initial)), gid=gid)
        self.assertEqual(original['status'], 'report_ready')
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        for text in ('外殼材料暫定改用不鏽鋼。', '接收人不要改派魏不凡。',
                     '「集合點移到未央館。」', '如果下雨，集合點移到未央館。',
                     '外殼材料改用不鏽鋼（待核對）。', '接收人改派魏不凡或吳景辰。',
                     '集合點移到未央館嗎？', '架號移到未央館。',
                     '外殼材料改用不鏽鋼；外殼材料不要改用不鏽鋼。',
                     '外殼材料改用不鏽鋼並且重新編號。'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    goal_guidance.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
                self.assertEqual(a.get_goal(self.store, self.sid, gid), original)

        text = '外殼材料改用不鏽鋼；接收人改派為「魏不凡」；請把「集合點」移至「未央館」。'
        goal_guidance.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
        state = a.get_goal(self.store, self.sid, gid)
        values = checked(state)
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['steps'][:-1], original['steps'])
        self.assertEqual(state['work_plan']['items'][3], original['work_plan']['items'][3])
        self.assertEqual(values[4], initial[4])
        step = state['steps'][-1]
        self.assertEqual(step['action']['args']['confirmation_correction_version'], 3)
        revisions = step['observation']['scope_change']['revisions']
        self.assertEqual(len(revisions), 3)
        for n, (change, value) in enumerate(zip(revisions, ['不鏽鋼', '魏不凡', '未央館'], strict=True), 1):
            start, end = (change['value_evidence'][key] for key in ('start', 'end'))
            self.assertEqual(text[start:end], value)
            self.assertEqual(change['question'], questions[n - 1])
            self.assertEqual(change['previous']['step'], initial[n]['reply_step'])
            self.assertEqual(change['recheck_items'], [n])
            self.assertEqual(values[n]['value'], value)

        archive = create_backup(self.store.path, self.root / 'directive-work.paibackup')
        destination = self.root / 'directive-work'
        restore_backup(archive['path'], destination)
        restored = Store(destination / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), state)
        report = deliverable(values)
        final = a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('update_plan', items=completed(values), reuse_document_steps=[2]),
            action('finish', report=deliverable(initial)), action('finish', report=report)), max_steps=3)
        self.assertEqual(final['result'], report)
        self.assertIn('現行已核對的答覆', final['steps'][-2]['observation']['error'])
        validate_database(restored.path)
        altered = copy.deepcopy(step['action'])
        altered['args']['confirmation_correction_version'] = 2
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET action_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(altered, ensure_ascii=False), gid, step['sequence']))
        with self.assertRaisesRegex(ValueError, '工作範圍更正與原清單或保存步驟不一致'):
            validate_database(restored.path)

    def test_unapplied_directive_in_mixed_reply_preserves_confirmation_and_exact_pending_question(self):
        doc = self.root / 'mixed-reply.txt'
        doc.write_text('材料依本輪確認。\n架號依本輪確認。\n門號依本輪確認。', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        questions = ['外殼材料是什麼？', '架號是多少？', '門號是多少？']
        parts = [{'question': question, 'items': [n]} for n, question in enumerate(questions, 1)]
        rows = [item(n, '核對' + label) for n, label in enumerate(('外殼材料', '架號', '門號'), 1)]
        ranges = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 4)]
        required = [{'id': n, 'confirmation_question': question, 'ranges': [ranges[n - 1]]}
                    for n, question in enumerate(questions, 1)]

        def reply(store, gid, text):
            view = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def checked(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        for index, (clause, reason) in enumerate((
                ('外殼材料不要改用不鏽鋼', 'nondefinitive'),
                ('外殼材料暫定改用不鏽鋼', 'nondefinitive'),
                ('「外殼材料改用不鏽鋼。」', 'quoted'),
                ('如果潮氣過高，外殼材料改用不鏽鋼', 'conditional'),
                ('外殼材料改用不鏽鋼或鈦合金', 'unconfirmed'),
                ('外殼材料改用不鏽鋼嗎', 'inquiry'))):
            with self.subTest(clause=clause):
                gid = a.create_goal(self.store, self.sid, '核對外殼材料、架號及門號。')
                self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                                 action('read_ranges', ranges=ranges),
                                 action('ask', question=questions[0], plan_questions=parts[:1]), gid=gid)
                original = checked(reply(self.store, gid, '外殼材料為鋁合金。'))[1]
                before = self.run_actions(action('ask', question='\n'.join(questions[1:]), plan_questions=parts[1:]), gid=gid)
                if index == 0:
                    with self.assertRaisesRegex(ValueError, '同一欄位'):
                        reply(self.store, gid, '架號為 9 號。外殼材料改用不鏽鋼。外殼材料不要改用不鏽鋼。')
                    self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
                punctuation = {'inquiry': '？', 'quoted': ''}.get(reason, '。')
                text = '架號為 9 號，' + clause + punctuation + '門號尚未確認。'
                current = reply(self.store, gid, text)
                values = checked(current)
                self.assertEqual(current['status'], 'needs_input')
                self.assertEqual(current['steps'][-1]['action']['args']['plan_questions'], parts[2:])
                self.assertEqual(values[1], original)
                self.assertEqual(values[2]['value'], '9 號')
                self.assertEqual(set(values), {1, 2})
                receipt = current['steps'][-2]
                self.assertEqual(receipt['action']['args']['scope_reply_version'], 29)
                self.assertEqual(values[2]['citation'], receipt['action']['args']['scope_reply'])
                proof = receipt['observation']['scope_reply']
                self.assertFalse(proof.get('revisions'))
                held, = proof['unapplied_directives']
                self.assertEqual(held['questions'], questions[:1])
                self.assertEqual(held['reason'], reason)
                span = held['evidence']
                self.assertEqual(text[span['start']:span['end']], clause)
                self.assertEqual(span['quote'], clause)

        archive = create_backup(self.store.path, self.root / 'mixed-reply.paibackup')
        destination = self.root / 'mixed-reply'
        restore_backup(archive['path'], destination)
        restored = Store(destination / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), current)
        final_values = checked(reply(restored, gid, '門號為 17 號。'))
        self.assertEqual(final_values[1], values[1])
        self.assertEqual(final_values[2], values[2])
        self.assertEqual(final_values[3]['value'], '17 號')
        altered = copy.deepcopy(receipt['observation'])
        altered['scope_reply']['unapplied_directives'][0]['reason'] = 'quoted'
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(altered, ensure_ascii=False), gid, receipt['sequence']))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(restored.path)

    def test_bound_text_reply_preserves_material_location_and_version_with_literal_sources(self):
        document = self.root / 'label-proof.txt'
        document.write_text('盒數依使用者確認。\n外殼材料待填。\n集合點待填。\n版本名稱待填。\n台號待填。',
                            encoding='utf-8')
        did = self.store.attach_document(self.sid, document)
        gid = a.create_goal(self.store, self.sid, '核對盒數、外殼材料、集合點、版本名和台號。')
        questions = ['盒數是多少？', '外殼材料是什麼？', '集合點在哪裡？', '版本名是哪個？', '台號是多少？']
        labels = ('盒數', '外殼材料', '集合點', '版本名', '台號')
        items = [item(n, '核對' + label) for n, label in enumerate(labels, 1)]
        spans = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 6)]
        parts = [{'question': q, 'items': [n]} for n, q in enumerate(questions, 1)]
        required = [{'id': n, 'confirmation_question': q, 'ranges': [spans[n - 1]]}
                    for n, q in enumerate(questions, 1)]

        def ask(*numbers):
            selected = [parts[n - 1] for n in numbers]
            return action('ask', question='\n'.join(part['question'] for part in selected), plan_questions=selected)

        def reply(store, answer):
            before = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, answer, expected_fingerprint=before['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def kept(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        def restore(store, name):
            prior = a.get_goal(store, self.sid, gid)
            target = self.root / name
            backup = create_backup(store.path, target.with_suffix('.paibackup'))
            restore_backup(backup['path'], target)
            clone = Store(target / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(clone, self.sid, gid), prior)
            return clone

        self.run_actions(action('update_plan', items=items, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=spans), ask(1), gid=gid)
        box_count = kept(reply(self.store, '盒數為 6 盒。'))[1]
        self.run_actions(ask(2, 3, 4), gid=gid)
        uncertain = ('外殼材料可能是不鏽鋼。', '外殼材料不是不鏽鋼。', '外殼材料為不鏽鋼或鋁合金。',
                     '外殼材料是不鏽鋼嗎？', '例如「外殼材料為不鏽鋼」。', '外殼材料為 TBD。',
                     '外殼材料為不鏽鋼但還要再問。')
        for index, phrase in enumerate(uncertain):
            with self.subTest(phrase=phrase):
                store = restore(self.store, f'text-uncertain-{index}')
                state = reply(store, '集合點確定在未央館，「版本名」為「R2.1 未央修訂版」。' + phrase)
                saved = kept(state)
                self.assertEqual(state['question'], questions[1])
                self.assertEqual(state['steps'][-1]['action']['args']['plan_questions'], [parts[1]])
                self.assertEqual(saved[1], box_count)
                self.assertEqual(saved[3]['value'], '未央館')
                self.assertEqual(saved[4]['value'], 'R2.1 未央修訂版')
                self.assertNotIn(2, saved)
                proof = state['work_plan']['scope_reply']['parts'][0]
                self.assertIn(proof['state'], ('ambiguous', 'deferred'))
                validate_database(store.path)

        saved_reply = Store(self.root / 'text-uncertain-0' / 'assistant.sqlite3')
        retained = kept(a.get_goal(saved_reply, self.sid, gid))
        after_restore = restore(saved_reply, 'text-answer')
        answered = reply(after_restore, '「不鏽鋼」。')
        self.assertEqual(answered['status'], 'paused')
        self.assertEqual(kept(answered)[2]['value'], '不鏽鋼')
        self.assertEqual({n: kept(answered)[n] for n in retained}, retained)

        a.run_goal(after_restore, self.sid, gid, ScriptedEngine(ask(5)), max_steps=1)
        correction = '台號為 8 號。集合點正式改為「不落亭」。'
        corrected = reply(after_restore, correction)
        current = kept(corrected)
        self.assertEqual(current[3]['value'], '不落亭')
        self.assertEqual(current[4], retained[4])
        self.assertEqual(current[5]['value'], '8 號')
        change, = corrected['work_plan']['scope_reply']['revisions']
        self.assertEqual(change['previous']['value_evidence']['quote'], '未央館')
        evidence = change['value_evidence']
        self.assertEqual(correction[evidence['start']:evidence['end']], '不落亭')

        delivery = restore(after_restore, 'text-corrected')
        done = [{**row, 'state': 'done', 'evidence_steps': [2, current[row['id']]['reply_step']]} for row in items]
        report = '\n'.join(f'{row["value"]} {row["citation"]} [D1:L{n}]' for n, row in sorted(current.items()))
        final = a.run_goal(delivery, self.sid, gid, ScriptedEngine(
            action('update_plan', items=done, reuse_document_steps=[2]),
            action('finish', report=report.replace('不落亭', '未央館')),
            action('finish', report=report)), max_steps=3)
        self.assertIn('現行已核對的答覆', final['steps'][-2]['observation']['error'])
        self.assertEqual(final['status'], 'report_ready')
        validate_database(delivery.path)

    def test_bound_text_reply_requires_the_exact_field_when_two_labels_share_a_name(self):
        doc = self.root / 'two-materials.txt'
        doc.write_text('北架外殼待確認。\n南架外殼待確認。', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        gid = a.create_goal(self.store, self.sid, '分別核對兩架材料。')
        questions = ['北架的材料要用哪種？', '南架的材料是哪種？']
        rows = [item(1, '核對北架外殼'), item(2, '核對南架外殼')]
        parts = [{'question': q, 'items': [n]} for n, q in enumerate(questions, 1)]
        req = [{'id': n, 'confirmation_question': q, 'ranges': [
            {'document_id': did, 'start_line': n, 'end_line': n}]} for n, q in enumerate(questions, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=req),
                         action('ask', question='\n'.join(questions), plan_questions=parts), gid=gid)

        def reply(answer):
            before = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, answer, expected_fingerprint=before['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)

        pending = reply('材料為不鏽鋼。')
        self.assertEqual(pending['question'], '\n'.join(questions))
        proof = pending['work_plan']['scope_reply']
        self.assertEqual(proof['verified_confirmations'], [])
        self.assertEqual(proof['unassigned_text'][0]['questions'], questions)
        final = reply('南架的材料為鋁合金。北架的材料為不鏽鋼。')
        verified = final['work_plan']['delivery_review']['verified_confirmations']
        self.assertEqual({row['question']: row['value'] for row in verified},
                         dict(zip(questions, ('不鏽鋼', '鋁合金'))))
        validate_database(self.store.path)

    def test_person_reply_distinguishes_confirmed_names_from_unresolved_wording(self):
        document = self.root / 'handover-names.txt'
        document.write_text('份數由本輪確認。\n北廊點收台在木架旁。\n門號由本輪確認。\n封條號由本輪確認。',
                            encoding='utf-8')
        did = self.store.attach_document(self.sid, document)
        gid = a.create_goal(self.store, self.sid, '核對北廊點收人、份數、門號和封條號。')
        questions = ['份數是多少？', '北廊的點收人是誰？', '門號是多少？', '封條號是多少？']
        rows = [item(n, task) for n, task in enumerate(
            ('核對份數', '核對北廊點收台及點收人', '核對門號', '核對封條號'), 1)]
        spans = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 5)]
        parts = [{'question': question, 'items': [n]} for n, question in enumerate(questions, 1)]
        required = [{'id': n, 'confirmation_question': question, 'ranges': [spans[n - 1]]}
                    for n, question in enumerate(questions, 1)]

        def ask(*numbers):
            return action('ask', question='\n'.join(questions[n - 1] for n in numbers),
                          plan_questions=[parts[n - 1] for n in numbers])

        def reply(store, answer):
            view = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, answer, expected_fingerprint=view['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def verified(state):
            return {entry['id']: entry for entry in state['work_plan']['delivery_review']['verified_confirmations']}

        def restored(store, archive):
            baseline = a.get_goal(store, self.sid, gid)
            target = self.root / archive
            backup = create_backup(store.path, target.with_suffix('.paibackup'))
            restore_backup(backup['path'], target)
            cloned = Store(target / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(cloned, self.sid, gid), baseline)
            return cloned

        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=spans), ask(1), gid=gid)
        initial = verified(reply(self.store, '份數為 4 份。'))[1]
        self.run_actions(ask(2, 3), gid=gid)
        uncertain = ('北廊的點收人可能是黃未晴。', '點收人尚未確認。', '點收人不是黃未晴。',
                     '點收人為黃未晴或林演洲。', '例如「點收人為黃未晴」。', '點收人是黃未晴嗎？',
                     '點收人為「黃未晴（暫定）」。')
        for index, phrase in enumerate(uncertain):
            with self.subTest(phrase=phrase):
                cloned = restored(self.store, f'people-{index}')
                raw = '門號為 8 號。' + phrase
                state = reply(cloned, raw)
                known = verified(state)
                self.assertEqual({1, 3}, known.keys())
                self.assertEqual(known[1], initial)
                self.assertEqual(known[3]['value'], '8 號')
                self.assertEqual(state['question'], questions[1])
                self.assertEqual(state['steps'][-1]['action']['args']['plan_questions'], [parts[1]])
                proof = state['work_plan']['scope_reply']['parts'][0]
                evidence = proof['evidence']
                self.assertIn(proof['state'], ('ambiguous', 'deferred'))
                self.assertTrue(phrase.startswith(raw[evidence['start']:evidence['end']]))
                validate_database(cloned.path)

        held = restored(self.store, 'person-in-quote')
        direct = reply(held, '「北廊的點收人」為「黃未晴」。門號為 8 號。')
        self.assertEqual(direct['status'], 'paused')
        known = verified(direct)
        self.assertEqual([row['value'] for row in known.values()], ['4 份', '黃未晴', '8 號'])
        self.assertEqual(direct['work_plan']['scope_reply']['parts'][0]['value_evidence']['quote'], '黃未晴')
        validate_database(held.path)

        restored_question = restored(Store(self.root / 'people-0' / 'assistant.sqlite3'), 'still-waiting-person')
        provisional = verified(a.get_goal(restored_question, self.sid, gid))
        accepted = reply(restored_question, '黃未晴。')
        self.assertEqual(verified(accepted)[2]['value'], '黃未晴')
        self.assertEqual(verified(accepted)[3], provisional[3])
        a.run_goal(restored_question, self.sid, gid, ScriptedEngine(ask(4)), max_steps=1)
        revision = '封條號為 5 號。更正「北廊的點收人是誰？」為「魏不凡」。'
        updated = reply(restored_question, revision)
        latest = verified(updated)
        self.assertEqual(latest[1], initial)
        self.assertEqual(latest[2]['value'], '魏不凡')
        self.assertEqual(latest[3], provisional[3])
        self.assertEqual(latest[4]['value'], '5 號')
        change, = updated['work_plan']['scope_reply']['revisions']
        self.assertEqual(change['previous']['step'], verified(accepted)[2]['reply_step'])
        span = change['value_evidence']
        self.assertEqual(revision[span['start']:span['end']], '魏不凡')

        completed_store = restored(restored_question, 'named-and-restored')
        done = [{**row, 'state': 'done', 'evidence_steps': [2, latest[row['id']]['reply_step']]} for row in rows]
        report = '\n'.join(f'{row["value"]} {row["citation"]} [D1:L{n}]' for n, row in sorted(latest.items()))
        final = a.run_goal(completed_store, self.sid, gid, ScriptedEngine(
            action('update_plan', items=done, reuse_document_steps=[2]),
            action('finish', report=report.replace('魏不凡', '黃未晴')),
            action('finish', report=report)), max_steps=3)
        self.assertIn('現行已核對的答覆', final['steps'][-2]['observation']['error'])
        self.assertEqual(final['status'], 'report_ready')
        validate_database(completed_store.path)

    def test_person_reply_keeps_same_named_roles_unassigned_until_given_exact_names(self):
        doc = self.root / 'contacts.txt'
        doc.write_text('前門在走道。\n後門在廊下。', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        gid = a.create_goal(self.store, self.sid, '核對前門與後門的接收人。')
        questions = [f'{place}的接收人是誰？' for place in ('前門', '後門')]
        rows = [item(1, '核對前門'), item(2, '核對後門')]
        parts = [{'question': q, 'items': [i]} for i, q in enumerate(questions, 1)]
        required = [{'id': i, 'confirmation_question': q, 'ranges': [{'document_id': did, 'start_line': i, 'end_line': i}]}
                    for i, q in enumerate(questions, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('ask', question='\n'.join(questions), plan_questions=parts), gid=gid)

        def reply(text):
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)

        pending = reply('接收人為林未央。')
        self.assertEqual(pending['question'], '\n'.join(questions))
        proof = pending['work_plan']['scope_reply']
        self.assertEqual(proof['verified_confirmations'], [])
        self.assertEqual(proof['unassigned_people'][0]['questions'], questions)
        state = reply('後門的接收人為魏不凡。前門的接收人為林未央。')
        checks = state['work_plan']['delivery_review']['verified_confirmations']
        self.assertEqual({row['question']: row['value'] for row in checks}, dict(zip(questions, ('林未央', '魏不凡'))))
        validate_database(self.store.path)

    def test_count_reply_keeps_other_confirmations_and_asks_only_ambiguous_quantity(self):
        path = self.root / 'tray-counts.txt'
        path.write_text('本批稽核員由使用者確認。\n每盤有 8 株。\n架號由本輪指定。\n封條由本輪指定。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對本批稽核員、育苗盤數、架號及封條。')
        questions = ['稽核員是誰？', '育苗盤數是多少？', '架號是多少？', '封條號是多少？']
        rows = [item(n, '核對' + label) for n, label in enumerate(('稽核員', '育苗盤數', '架號', '封條號'), 1)]
        parts = [{'question': q, 'items': [n]} for n, q in enumerate(questions, 1)]
        spans = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 5)]
        requirements = [{'id': n, 'confirmation_question': question, 'ranges': [spans[n - 1]]}
                        for n, question in enumerate(questions, 1)]

        def ask(*numbers):
            return action('ask', question='\n'.join(questions[n - 1] for n in numbers),
                          plan_questions=[parts[n - 1] for n in numbers])

        def reply(store, text):
            view = goal_replies.snapshot(store, self.sid, gid)
            goal_replies.save(store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
            return a.get_goal(store, self.sid, gid)

        def checked(state):
            return {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        def copy_checkpoint(store, archive):
            original = a.get_goal(store, self.sid, gid)
            path = self.root / archive
            backup = create_backup(store.path, path.with_suffix('.paibackup'))
            restore_backup(backup['path'], path)
            clone = Store(path / 'assistant.sqlite3')
            self.assertEqual(a.get_goal(clone, self.sid, gid), original)
            return clone

        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=requirements),
                         action('read_ranges', ranges=spans), ask(1), gid=gid)
        examiner = checked(reply(self.store, '稽核員為林演洲。'))[1]
        self.run_actions(ask(2, 3), gid=gid)
        for number, value in enumerate(('6 盤或 7 盤', '6 盤 2 袋', '6 盤，2 袋', '6 盒', '大約 6 盤', '六七盤'), 1):
            with self.subTest(value=value):
                clone = copy_checkpoint(self.store, f'pending-{number}')
                raw = f'育苗盤數為 {value}。架號為 8 號。'
                state = reply(clone, raw)
                kept = checked(state)
                expected_successor = state['steps'][-1]['action']['args']['plan_questions']
                self.assertEqual(state['status'], 'needs_input')
                self.assertEqual(expected_successor, [parts[1]])
                self.assertEqual(kept[1], examiner)
                self.assertEqual(kept[3]['value'], '8 號')
                self.assertNotIn(2, kept)
                proof = state['work_plan']['scope_reply']
                evidence = proof['parts'][0]['evidence']
                self.assertEqual(raw[evidence['start']:evidence['end']], f'育苗盤數為 {value}')
                self.assertEqual(proof['parts'][0]['state'], 'ambiguous')
                if number == 1:
                    restored = copy_checkpoint(clone, 'pending-count')
                    state = reply(restored, '育苗盤數為「６ 盤」。')
                    self.assertEqual(state['status'], 'paused')
                    verified = checked(state)
                    self.assertEqual(verified[2]['value'], '６ 盤')
                    self.assertEqual({n: verified[n] for n in kept}, kept)
                    a.run_goal(restored, self.sid, gid, ScriptedEngine(ask(4)), max_steps=1)
                    correction = '封條號為 6 號。育苗盤數正式改為 7 盤。'
                    adjusted = reply(restored, correction)
                    revised = checked(adjusted)
                    self.assertEqual(revised[1], examiner)
                    self.assertEqual(revised[3], kept[3])
                    self.assertEqual(revised[2]['value'], '7 盤')
                    self.assertEqual(revised[4]['value'], '6 號')
                    change, = adjusted['work_plan']['scope_reply']['revisions']
                    span = change['value_evidence']
                    self.assertEqual(correction[span['start']:span['end']], '7 盤')
                    self.assertEqual(change['previous']['step'], verified[2]['reply_step'])
                    self.assertEqual(change['previous']['value_evidence']['quote'], '６ 盤')
                    validate_database(restored.path)
                    copy_checkpoint(restored, 'resolved-count')
                validate_database(clone.path)

    def test_count_reply_binds_complete_scoped_names_and_preserves_shared_alias_ambiguity(self):
        path = self.root / 'box-counts.txt'
        path.write_text('北架有紅盒。\n南架有藍盒。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對兩架各自盒數。')
        rows = [item(1, '核對北架紅盒'), item(2, '核對南架藍盒')]
        questions = [f'{side}的盒數是多少？' for side in ('北架', '南架')]
        parts = [{'question': question, 'items': [n]} for n, question in enumerate(questions, 1)]
        required = [{'id': n, 'confirmation_question': q, 'ranges': [{'document_id': did, 'start_line': n, 'end_line': n}]}
                    for n, q in enumerate(questions, 1)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('ask', question='\n'.join(questions), plan_questions=parts), gid=gid)

        def reply(text):
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])
            return a.get_goal(self.store, self.sid, gid)

        ambiguous = reply('盒數為 6 盒。')
        self.assertEqual(ambiguous['question'], '\n'.join(questions))
        choice, = ambiguous['work_plan']['scope_reply']['unassigned_counts']
        self.assertEqual(choice['questions'], questions)
        self.assertEqual(ambiguous['work_plan']['scope_reply']['verified_confirmations'], [])
        state = reply('北架的盒數為 ６ 盒。北架的盒數為 6盒。南架的盒數為八盒。')
        self.assertEqual(state['status'], 'paused')
        self.assertEqual({row['question']: row['value'] for row in state['work_plan']['delivery_review']['verified_confirmations']},
                         {questions[0]: '６ 盒', questions[1]: '八盒'})
        validate_database(self.store.path)

    def test_formal_quantity_correction_recalculates_only_the_bound_items_and_total(self):
        path = self.root / 'packing.txt'
        path.write_text('北4廊每份為 2 件。\n南廊每份為 3 件。\n門號由使用者提供。', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對兩廊件數、加總及本輪門號。')
        rows = [item(n, task) for n, task in enumerate(
            ('核對北4廊份數', '核對南廊份數', '核對門號', '算北4廊件數', '算南廊件數', '加總兩廊件數'), 1)]
        questions = ['北4廊的份數是多少？', '南廊的份數是多少？', '門號是多少？']
        spans = [{'document_id': did, 'start_line': n, 'end_line': n} for n in range(1, 4)]
        required = [{'id': n, 'ranges': [spans[n - 1]], 'confirmation_question': q}
                    for n, q in enumerate(questions, 1)]

        def ask(n):
            return action('ask', question=questions[n - 1], plan_questions=[{'question': questions[n - 1], 'items': [n]}])

        def reply(answer):
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, answer, expected_fingerprint=view['fingerprint'])
            state = a.get_goal(self.store, self.sid, gid)
            return state, {row['id']: row for row in state['work_plan']['delivery_review']['verified_confirmations']}

        def completed(number, refs):
            return {**rows[number - 1], 'state': 'done', 'evidence_steps': refs}

        def calculations(north, south, nsource, ssource):
            north_formula = {'operation': 'product', 'values': [north, '2']}
            south_formula = {'operation': 'product', 'values': [south, '3']}
            north_leaves = [{'path': '/values/0', **nsource},
                            {'path': '/values/1', 'citation': '[D1:L1]', 'quote': '北4廊每份為 2 件'}]
            south_leaves = [{'path': '/values/0', **ssource},
                            {'path': '/values/1', 'citation': '[D1:L2]', 'quote': '南廊每份為 3 件'}]
            return [
                {'id': 4, 'ranges': [spans[0]], 'expression': north_formula, 'operands': north_leaves},
                {'id': 5, 'ranges': [spans[1]], 'expression': south_formula, 'operands': south_leaves},
                {'id': 6, 'ranges': spans[:2], 'expression': {'operation': 'sum', 'values': [north_formula, south_formula]},
                 'operands': [{**op, 'path': f'/values/{index}{op["path"]}'}
                              for index, ops in enumerate((north_leaves, south_leaves)) for op in ops]},
            ]

        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=required),
                         action('read_ranges', ranges=spans), ask(1), gid=gid)
        reply('份數為 4 份。')
        self.run_actions(ask(2), gid=gid)
        _, original = reply('份數為 6 份。')
        south = {'citation': original[2]['citation'], 'quote': '份數為 6 份'}
        earlier = calculations('4', '6', {'citation': original[1]['citation'], 'quote': '份數為 4 份'}, south)
        initial_calculated = self.run_actions(*(action('calculate_expression', expression=spec['expression'])
                                               for spec in earlier), gid=gid)
        old_steps = [step['sequence'] for step in initial_calculated['steps'][-3:]]
        old_items = [completed(1, [2, original[1]['reply_step']]), completed(2, [2, original[2]['reply_step']]),
                     rows[2], *(completed(n, [2, ref]) for n, ref in enumerate(old_steps, 4))]
        before = self.run_actions(action('update_plan', items=old_items, evidence_requirements=earlier,
                                         reuse_document_steps=[2]), ask(3), gid=gid)
        south_match = next(row for row in before['work_plan']['evidence_matches'] if row['id'] == 5)
        revision = '「北4廊的份數」正式改為「6 份」'
        answer = f'門號為 6 號。{revision}。'
        state, known = reply(answer)
        edited, = state['work_plan']['scope_reply']['revisions']
        value_evidence = edited['value_evidence']
        self.assertEqual(answer[value_evidence['start']:value_evidence['end']], '6 份')
        self.assertEqual(edited['recheck_items'], [1, 4, 6])
        self.assertEqual(edited['operand_paths'], [
            {'id': 4, 'paths': ['/values/0']}, {'id': 6, 'paths': ['/values/0/values/0']}])
        self.assertEqual(known[2], original[2])
        self.assertEqual(state['work_plan']['items'][4], old_items[4])

        archive = create_backup(self.store.path, self.root / 'quantity.paibackup')
        restore_backup(archive['path'], self.root / 'quantity')
        saved = Store(self.root / 'quantity' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(saved, self.sid, gid), state)
        updated = calculations('6', '6', {'citation': known[1]['citation'], 'quote': revision, 'field': '北4廊的份數'}, south)
        valid_specs = [updated[0], updated[2]]
        wrong_specs = copy.deepcopy(valid_specs)
        wrong_specs[0]['operands'][0] = {'path': '/values/0', 'citation': known[3]['citation'], 'quote': '門號為 6 號'}
        first_new = len(state['steps']) + 1
        current = [completed(n, [2, known[n]['reply_step']]) for n in (1, 2, 3)] + [
            completed(4, [2, first_new]), old_items[4], completed(6, [2, first_new + 1])]
        report = (f'北4廊 6 份 {known[1]["citation"]}，每份 2 件 [D1:L1]，共 12 件。\n'
                  f'南廊 6 份 {known[2]["citation"]}，每份 3 件 [D1:L2]，共 18 件。\n'
                  f'兩廊共 30 件。門號 6 號 {known[3]["citation"]}。')
        field_digit = copy.deepcopy(earlier[0])
        field_digit['operands'][0] = {'path': '/values/0', 'citation': known[1]['citation'], 'quote': '北4廊的份數'}
        stale = [*current[:3], completed(4, [2, first_new + 2]), *current[4:]]
        final = a.run_goal(saved, self.sid, gid, ScriptedEngine(
            *(action('calculate_expression', expression=row['expression']) for row in valid_specs),
            action('calculate_expression', expression=field_digit['expression']),
            action('update_plan', items=stale, evidence_requirements=[field_digit, updated[2]], reuse_document_steps=[2]),
            action('update_plan', items=current, evidence_requirements=wrong_specs, reuse_document_steps=[2]),
            action('update_plan', items=current, evidence_requirements=valid_specs, reuse_document_steps=[2]),
            action('finish', report=report)), max_steps=7)
        for attempted in final['steps'][-4:-2]:
            self.assertIn('新值重算並核對運算元', attempted['observation'].get('error', '錯誤依據竟被接受'))
        self.assertEqual(final['status'], 'report_ready', final['steps'][-3:])
        matches = {row['id']: row for row in final['work_plan']['evidence_matches']}
        self.assertEqual(matches[5], south_match)
        for n in (4, 6):
            operand = matches[n]['operands'][0]
            self.assertEqual(answer[operand['field_start']:operand['field_end']], '北4廊的份數')
            self.assertEqual(operand['number_start'], answer.rindex('6'))
        validate_database(saved.path)
        final_archive = create_backup(saved.path, self.root / 'quantity-final.paibackup')
        restore_backup(final_archive['path'], self.root / 'quantity-final')
        self.assertEqual(a.get_goal(Store(self.root / 'quantity-final' / 'assistant.sqlite3'), self.sid, gid), final)
        revision_step = state['steps'][-1]
        modified = copy.deepcopy(revision_step['observation'])
        modified['scope_reply']['revisions'][0]['value_evidence']['start'] -= 1
        with saved.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(modified, ensure_ascii=False), gid, revision_step['sequence']))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(saved.path)

    def test_corrected_confirmation_reopens_delivered_item_and_reuses_unchanged_evidence(self):
        path = self.root / 'delivery.txt'
        path.write_text('窗口 北庫\n門號 G-17', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口、門號與目前接收人。')
        lines = {'document_id': did, 'start_line': 1, 'end_line': 2}
        person, independent = item(1, '核對窗口及接收人'), item(2, '核對門號')
        question = '接收人是誰？'
        self.run_actions(action('update_plan', items=[person, independent], verify_delivery=True, evidence_requirements=[
            {'id': 1, 'ranges': [lines], 'confirmation_question': question}, {'id': 2, 'ranges': [lines]}]),
            action('read', **lines), action('ask', question=question, plan_questions=[{'question': question, 'items': [1]}]),
            gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
        done_person = {**person, 'state': 'done', 'evidence_steps': [2, 4]}
        done_independent = {**independent, 'state': 'done', 'evidence_steps': [2]}
        original = self.run_actions(action('update_plan', items=[done_person, done_independent], reuse_document_steps=[2]),
                                    action('finish', report='北庫 [D1:L1]。G-17 [D1:L2]。林怡安 [補充#3]。'), gid=gid)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        self.assertTrue(goal_guidance.eligibility(view)[0])
        for bad in ('其他人正式改為周明恩。', '接收人正式改為周明恩或王中和。', '「接收人正式改為周明恩」。'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                goal_guidance.save(self.store, self.sid, gid, bad, expected_fingerprint=view['fingerprint'])
        self.assertEqual(a.get_goal(self.store, self.sid, gid), original)
        goal_guidance.save(self.store, self.sid, gid, '接收人正式改為周明恩。', expected_fingerprint=view['fingerprint'])
        with self.assertRaises(goal_guidance.StaleGuidance):
            goal_guidance.save(self.store, self.sid, gid, '接收人改為王中和。', expected_fingerprint=view['fingerprint'])
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '更正「接收人是誰？」為「王中和」。', expected_fingerprint=view['fingerprint'])
        expected = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(expected['status'], 'paused')
        self.assertEqual(expected['result'], '')
        self.assertEqual(expected['steps'][:6], original['steps'])
        self.assertEqual(expected['work_plan']['items'][1], done_independent)
        self.assertEqual(expected['work_plan']['items'][0]['state'], 'pending')
        self.assertEqual(expected['steps'][-1]['observation']['scope_change']['revisions'][0]['previous']['step'], 7)
        confirmation, = expected['work_plan']['delivery_review']['verified_confirmations']
        self.assertEqual((confirmation['value'], confirmation['reply_step'], confirmation['citation']), ('王中和', 8, '[更正#2]'))
        self.assertEqual(expected['work_plan']['delivery_review']['document_read_progress'][0]['reuse_document_steps'], [2])
        backup = create_backup(self.store.path, self.root / 'correction.paibackup')
        destination = self.root / 'correction'
        restore_backup(backup['path'], destination)
        restored = Store(destination / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), expected)
        final = a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('finish', report='提前交付'),
            action('update_plan', items=[done_person, done_independent], reuse_document_steps=[2]),
            action('update_plan', items=[{**done_person, 'evidence_steps': [2, 8]}, done_independent], reuse_document_steps=[2]),
            action('finish', report='北庫 [D1:L1]。G-17 [D1:L2]。林怡安 [更正#2]。'),
            action('finish', report='北庫 [D1:L1]。G-17 [D1:L2]。王中和 [更正#2]。')), max_steps=5)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('未完成', final['steps'][8]['observation']['error'])
        self.assertIn('實際使用者答覆', final['steps'][9]['observation']['error'])
        self.assertIn('現行已核對', final['steps'][11]['observation']['error'])
        self.assertEqual(final['work_plan']['evidence_matches'][0]['confirmation'], confirmation)
        validate_database(restored.path)
        altered = copy.deepcopy(final['steps'][7]['observation'])
        altered['scope_change']['revisions'][0]['previous']['step'] = 4
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=8',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(restored.path)

    def test_corrected_confirmation_survives_an_independent_reply_and_document_supplement(self):
        path = self.root / 'dispatch.txt'
        path.write_text('窗口 北庫', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口、門號、接收人、班別。')
        first = {'document_id': did, 'start_line': 1, 'end_line': 1}
        missing = {**first, 'start_line': 2, 'end_line': 2}
        tasks = [item(1, '核對交件及接收人'), item(2, '確認班別')]
        questions = [{'question': '接收人是誰？', 'items': [1]}, {'question': '班別是哪個？', 'items': [2]},
                     {'question': '請提供 D1 第 2 行。', 'items': [1]}]
        self.run_actions(action('update_plan', items=tasks, verify_delivery=True, evidence_requirements=[
            {'id': 1, 'ranges': [first, missing], 'confirmation_question': questions[0]['question']}]),
            action('read_ranges', ranges=[first, missing]),
            action('ask', question='\n'.join(row['question'] for row in questions), plan_questions=questions), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
        prior = a.get_goal(self.store, self.sid, gid)
        stale = goal_replies.snapshot(self.store, self.sid, gid)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '接收人改為周明恩。', expected_fingerprint=view['fingerprint'])
        amended = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(amended['question'], prior['question'])
        self.assertEqual(amended['steps'][-1]['action']['args'], prior['steps'][-1]['action']['args'])
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        self.assertNotEqual(stale['fingerprint'], reply['fingerprint'])
        goal_replies.save(self.store, self.sid, gid, '班別為早班。', expected_fingerprint=reply['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(waiting['question'], questions[2]['question'])
        self.assertEqual(waiting['work_plan']['items'][1]['state'], 'done')
        self.assertEqual(waiting['work_plan']['delivery_review']['verified_confirmations'][0]['value'], '周明恩')
        supplement = self.root / 'supplement.txt'
        supplement.write_text('門號 G-17', encoding='utf-8')
        self.store.attach_document(self.sid, supplement)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '補件 [D1:L2] 改由 [D2:L1]\n請撤下目前問題。',
                           expected_fingerprint=view['fingerprint'])
        amended = a.get_goal(self.store, self.sid, gid)
        review = amended['work_plan']['delivery_review']
        self.assertEqual(review['verified_confirmations'][0]['reply_step'], 6)
        self.assertEqual(review['document_read_progress'][0]['read_steps'], [2, 10])
        final = self.run_actions(action('update_plan', items=[
            {**tasks[0], 'state': 'done', 'evidence_steps': [2, 6, 10]}, amended['work_plan']['items'][1]],
            reuse_document_steps=[2]),
            action('finish', report='北庫 [D1:L1]。G-17 [D2:L1]。周明恩 [更正#1]。早班 [補充#7]。'), gid=gid)
        self.assertEqual(final['status'], 'report_ready', final['steps'][-2:])
        validate_database(self.store.path)

    def test_corrected_confirmation_survives_renaming_a_completed_file(self):
        path = self.root / 'dispatch.txt'
        path.write_text('窗口 北庫', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '核對窗口及接收人。定稿檔名為「交件.md」。', file_delivery=True)
        lines = {'document_id': did, 'start_line': 1, 'end_line': 1}
        person = item(1, '核對窗口及接收人')
        question = '接收人是誰？'
        self.run_actions(action('update_plan', items=[person], verify_delivery=True, evidence_requirements=[
            {'id': 1, 'ranges': [lines], 'confirmation_question': question}]), action('read', **lines),
            action('ask', question=question, plan_questions=[{'question': question, 'items': [1]}]), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
        self.run_actions(action('update_plan', items=[{**person, 'state': 'done', 'evidence_steps': [2, 4]}],
                                reuse_document_steps=[2]),
                         action('write_artifact', filename='交件.md', content='北庫 [D1:L1]。林怡安 [補充#3]。'),
                         action('deliver', filenames=['交件.md'], report='已交付。'), gid=gid)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        self.assertEqual(view['goal']['status'], 'report_ready')
        self.assertTrue(goal_guidance.eligibility(view)[0])
        goal_guidance.save(self.store, self.sid, gid, '接收人改為周明恩。', expected_fingerprint=view['fingerprint'])
        waiting = self.run_actions(action('update_plan', items=[{**person, 'state': 'done', 'evidence_steps': [2, 8]}],
                                          reuse_document_steps=[2]), action('finish', report='多餘'), gid=gid)
        self.assertEqual(waiting['status'], 'needs_input')
        self.assertIn('定稿檔名', waiting['question'])
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '交件-final.md', expected_fingerprint=reply['fingerprint'])
        final = self.run_actions(
            action('write_artifact', filename='交件-final.md', content='北庫 [D1:L1]。周明恩 [更正#1]。'),
            action('deliver', filenames=['交件-final.md'], report='已交付更新檔案。'), gid=gid)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['delivery_review']['verified_confirmations'][0]['value'], '周明恩')
        self.assertEqual([row['name'] for row in final['artifacts']], ['交件.md', '交件-final.md'])
        self.assertEqual(final['steps'][5]['action']['args']['content'], '北庫 [D1:L1]。林怡安 [補充#3]。')
        validate_database(self.store.path)

    def test_withdrawal_reopens_only_the_needed_confirmation_in_a_delivered_goal(self):
        doc = self.root / 'register.txt'
        doc.write_text('東站 午間交接\n北站 早班交接', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        gid = a.create_goal(self.store, self.sid, '核對兩站原文、接收人與聯絡人，逐列引用。')
        all_lines = {'document_id': did, 'start_line': 1, 'end_line': 2}
        rows = [item(1, '核對東站交接'), item(2, '核對北站交接')]
        questions = [{'question': '接收人是誰？', 'items': [1]}, {'question': '聯絡人是誰？', 'items': [2]}]
        requirements = [{'id': n, 'ranges': [{**all_lines, 'start_line': n, 'end_line': n}],
                         'confirmation_question': questions[n - 1]['question']} for n in (1, 2)]
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=requirements),
                         action('read', **all_lines),
                         action('ask', question='\n'.join(row['question'] for row in questions), plan_questions=questions), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '接收人為林怡安。聯絡人為方志文。', expected_fingerprint=reply['fingerprint'])
        done = [{**row, 'state': 'done', 'evidence_steps': [2, 4]} for row in rows]
        original = self.run_actions(action('update_plan', items=done, reuse_document_steps=[2]),
            action('finish', report='東站 [D1:L1] 林怡安 [補充#3]。北站 [D1:L2] 方志文 [補充#3]。'), gid=gid)
        self.assertEqual(original['status'], 'report_ready')
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '撤回「接收人是誰？」的答覆。', expected_fingerprint=view['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(waiting['question'], '接收人是誰？')
        self.assertEqual(waiting['steps'][:6], original['steps'])
        self.assertEqual(waiting['result'], '')
        review = waiting['work_plan']['delivery_review']
        self.assertEqual([row['value'] for row in review['verified_confirmations']], ['方志文'])
        self.assertEqual(review['pending_reconfirmations'], [
            {'id': 1, 'question': '接收人是誰？', 'citation': '[更正#1]', 'withdrawal_step': 7}])
        self.assertFalse(review['ready'])
        with self.assertRaisesRegex(ValueError, '等待實際重新確認'):
            p.observe({'items': [{**rows[0], 'state': 'superseded', 'note': '跳過'}, done[1]]},
                      waiting['steps'], p.conditions(waiting), waiting['sources'])
        with self.assertRaisesRegex(ValueError, '須重新確認'):
            p.check_delivery(waiting['work_plan'], p.conditions(waiting), {'tool': 'finish', 'args': {}}, waiting['steps'])
        backup = create_backup(self.store.path, self.root / 'waiting-reconfirmation.paibackup')
        destination = self.root / 'reconfirmed'
        restore_backup(backup['path'], destination)
        restored = Store(destination / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, gid), waiting)
        reply = goal_replies.snapshot(restored, self.sid, gid)
        with self.assertRaisesRegex(ValueError, '仍待確認'):
            goal_replies.save(restored, self.sid, gid, '接收人可能是林怡安。', expected_fingerprint=reply['fingerprint'])
        self.assertEqual(a.get_goal(restored, self.sid, gid), waiting)
        goal_replies.save(restored, self.sid, gid, '接收人為林怡安。', expected_fingerprint=reply['fingerprint'])
        state = a.get_goal(restored, self.sid, gid)
        self.assertEqual(state['status'], 'paused')
        self.assertEqual({row['id']: row['reply_step'] for row in state['work_plan']['delivery_review']['verified_confirmations']},
                         {1: 9, 2: 4})
        final = a.run_goal(restored, self.sid, gid, ScriptedEngine(
            action('update_plan', items=[{**done[0], 'evidence_steps': [2, 9]}, done[1]], reuse_document_steps=[2]),
            action('finish', report='東站 [D1:L1] 林怡安 [補充#3]。北站 [D1:L2] 方志文 [補充#3]。'),
            action('finish', report='東站 [D1:L1] 林怡安 [補充#8]。北站 [D1:L2] 方志文 [補充#3]。')), max_steps=3)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('現行已核對', final['steps'][10]['observation']['error'])
        validate_database(restored.path)
        altered = copy.deepcopy(final['steps'][8]['observation'])
        altered['scope_reply']['reconfirmed_withdrawals'][0]['withdrawal_step'] = 6
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=9',
                       (json.dumps(altered, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '工作清單與保存動作'):
            validate_database(restored.path)

    def test_withdrawal_keeps_an_unrelated_missing_document_question(self):
        doc = self.root / 'handoff.txt'
        doc.write_text('南站承辦', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        gid = a.create_goal(self.store, self.sid, '核對交接原行及值班組。')
        first = {'document_id': did, 'start_line': 1, 'end_line': 1}
        missing = {**first, 'start_line': 2, 'end_line': 2}
        task = item(1, '核對交接')
        target = '值班組是哪個？'
        missing_question = '請提供 D1 第 2 行。'
        questions = [{'question': target, 'items': [1]}, {'question': missing_question, 'items': [1]}]
        self.run_actions(action('update_plan', items=[task], verify_delivery=True, evidence_requirements=[
            {'id': 1, 'ranges': [first, missing], 'confirmation_question': target}]),
            action('read_ranges', ranges=[first, missing]),
            action('ask', question='\n'.join(row['question'] for row in questions), plan_questions=questions), gid=gid)
        reply = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '值班組為甲組。', expected_fingerprint=reply['fingerprint'])
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '撤回值班組的確認。', expected_fingerprint=view['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(waiting['question'], missing_question + '\n' + target)
        supplemental = self.root / 'addendum.txt'
        supplemental.write_text('交接代號 J-25', encoding='utf-8')
        self.store.attach_document(self.sid, supplemental)
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, f'補件 [D1:L2] 改由 [D2:L1]\n請撤下分問「{missing_question}」',
                           expected_fingerprint=view['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(waiting['question'], target)
        self.assertNotIn('verified_confirmations', waiting['work_plan']['delivery_review'])
        view = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '值班組為乙組。', expected_fingerprint=view['fingerprint'])
        final = self.run_actions(action('update_plan', items=[{**task, 'state': 'done', 'evidence_steps': [2, 8, 10]}],
                                        reuse_document_steps=[2, 8]),
                                 action('finish', report='南站 [D1:L1]。J-25 [D2:L1]。乙組 [補充#9]。'), gid=gid)
        self.assertEqual(final['status'], 'report_ready', final['steps'][-2:])
        validate_database(self.store.path)

    def test_withdrawn_number_requires_a_new_operand_source(self):
        doc = self.root / 'packing.txt'
        doc.write_text('每袋 2 盒', encoding='utf-8')
        did = self.store.attach_document(self.sid, doc)
        gid = a.create_goal(self.store, self.sid, '按文件及使用者確認計算盒數。')
        lines = {'document_id': did, 'start_line': 1, 'end_line': 1}
        rows = [item(1, '核對每袋盒數及袋數'), item(2, '計算本批總盒數')]
        question = '袋數是多少？'
        self.run_actions(action('update_plan', items=rows, verify_delivery=True, evidence_requirements=[
            {'id': 1, 'ranges': [lines], 'confirmation_question': question}]),
            action('read', **lines), action('ask', question=question, plan_questions=[{'question': question, 'items': [1]}]), gid=gid)
        view = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '袋數為 3 袋。', expected_fingerprint=view['fingerprint'])
        calculation = {'operation': 'product', 'values': ['3', '2']}
        spec = {'id': 2, 'ranges': [lines], 'expression': calculation, 'operands': [
            {'path': '/values/0', 'citation': '[補充#3]', 'quote': '袋數為 3 袋'},
            {'path': '/values/1', 'citation': '[D1:L1]', 'quote': '每袋 2 盒'}]}
        done = [{**rows[0], 'state': 'done', 'evidence_steps': [2, 4]},
                {**rows[1], 'state': 'done', 'evidence_steps': [2, 5]}]
        original = self.run_actions(action('calculate_expression', expression=calculation),
            action('update_plan', items=done, reuse_document_steps=[2], evidence_requirements=[spec]), gid=gid)
        self.assertEqual(original['work_plan']['items'], done, original['steps'][-2:])
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '撤回「袋數是多少？」的答覆。', expected_fingerprint=view['fingerprint'])
        waiting = a.get_goal(self.store, self.sid, gid)
        self.assertEqual([row['state'] for row in waiting['work_plan']['items']], ['pending', 'pending'])
        view = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '袋數為 3 袋。', expected_fingerprint=view['fingerprint'])
        target = [{**done[0], 'evidence_steps': [2, 9]}, {**done[1], 'evidence_steps': [2, 10]}]
        updated = {**spec, 'operands': [{**spec['operands'][0], 'citation': '[補充#8]'}, spec['operands'][1]]}
        final = self.run_actions(action('calculate_expression', expression=calculation),
            action('update_plan', items=target, reuse_document_steps=[2], evidence_requirements=[spec]),
            action('update_plan', items=target, reuse_document_steps=[2], evidence_requirements=[updated]),
            action('finish', report='每袋 2 盒 [D1:L1]，3 袋 [補充#8]，共 6 盒。'), gid=gid)
        self.assertEqual(final['status'], 'report_ready', final['steps'][-3:])
        self.assertIn('[補充#8]', final['steps'][10]['observation']['error'])
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

    def _wait_for_filename_with_bound_calculation(self):
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
        return waiting, calculation, requirement

    def test_filename_reply_rechecks_calculation_with_the_actual_revised_operand(self):
        _, calculation, requirement = self._wait_for_filename_with_bound_calculation()
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

    def test_filename_remains_pending_until_recalculation_uses_the_new_operand(self):
        waiting, _, requirement = self._wait_for_filename_with_bound_calculation()
        state = self.save_scope_reply('留存份數改為 5 份。')
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['artifacts'], waiting['artifacts'])
        plan = state['work_plan']
        proof = plan['scope_reply']
        self.assertEqual(proof['filename_pending']['filename'], 'report.md')
        self.assertEqual(proof['revisions'][0]['recheck_items'], [1])
        self.assertEqual(proof['retained_items'], [2])
        validate_database(self.store.path)
        expression = {'operation': 'product', 'values': ['5', '3']}
        new_requirement = {**requirement, 'expression': expression, 'operands': [
            {**requirement['operands'][0], 'citation': proof['citation'], 'quote': '留存份數改為 5 份'},
            requirement['operands'][1]]}
        engine = ScriptedEngine(
            action('write_artifact', filename='result.md', content='尚未核對'),
            action('calculate_expression', expression=expression),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [len(state['steps']) + 2]},
                                         *plan['items'][1:]], evidence_requirements=[new_requirement]))
        refreshed = a.run_goal(self.store, self.sid, self.gid, engine, max_steps=4)
        self.assertEqual([step['action']['tool'] for step in refreshed['steps'][-4:]],
                         ['invalid', 'calculate_expression', 'update_plan', 'ask'])
        self.assertIn('定稿檔名仍待確認', refreshed['steps'][-4]['observation']['error'])
        self.assertEqual(refreshed['question'], waiting['question'])
        self.assertEqual(refreshed['artifacts'], waiting['artifacts'])
        self.assertEqual(refreshed['work_plan']['items'][1]['evidence_steps'], [3])
        self.save_scope_reply('result.md。')
        final = self.run_actions(action('write_artifact', filename='result.md', content='總份數 15，獨立數量 5。'),
                                 action('deliver', report='已交付。', filenames=['result.md']))
        self.assertEqual(final['status'], 'report_ready')
        validate_database(self.store.path)

    def test_field_choice_with_filename_rechecks_the_selected_operand_and_delivers_without_another_question(self):
        waiting, _, requirement = self._wait_for_filename_with_bound_calculation()
        pending = self.save_scope_reply('留存份數改為 5 份或 7 份。')
        self.assertIn('留存份數要改為', pending['question'])
        state = self.save_scope_reply('用 7 份，檔名用 result.md。')
        plan = state['work_plan']
        proof = plan['scope_reply']
        self.assertEqual(proof['retained_items'], [2])
        self.assertEqual(proof['revisions'][0]['recheck_items'], [1])
        self.assertEqual(proof['filename_choice']['question_step'], waiting['steps'][-1]['sequence'])
        expression = {'operation': 'product', 'values': ['7', '3']}
        new_requirement = {**requirement, 'expression': expression, 'operands': [
            {'path': '/values/0', 'citation': proof['citation'], 'quote': '用 7 份'},
            requirement['operands'][1]]}
        final = self.run_actions(action('calculate_expression', expression=expression),
            action('update_plan', items=[{**plan['items'][0], 'state': 'done', 'evidence_steps': [len(state['steps']) + 1]},
                                         *plan['items'][1:]], evidence_requirements=[new_requirement]),
            action('write_artifact', filename='result.md', content='總份數 21，獨立數量 5。'),
            action('deliver', report='已交付。', filenames=['result.md']))
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['work_plan']['items'][1]['evidence_steps'], [3])
        self.assertEqual([step['action']['tool'] for step in final['steps'][-4:]],
                         ['calculate_expression', 'update_plan', 'write_artifact', 'deliver'])
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
