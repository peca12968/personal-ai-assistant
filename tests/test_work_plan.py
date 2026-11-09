import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, work_plan as p, goal_guidance
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
