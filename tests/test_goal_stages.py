from assistant.goal_replies import snapshot as reply_snapshot
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistant import artifacts, goal_guidance
from assistant import autonomy as a
from assistant.answer_runs import LeaseBusy, lease
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalStageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def parent(self, report='方案甲的工作成果'):
        gid = a.create_goal(self.store, self.sid, '確認方案')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='採用哪一個方案？')))
        a.reply(self.store, self.sid, gid, '已確認採用方案甲。', expected_fingerprint=reply_snapshot(self.store, self.sid, gid)["fingerprint"])
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('finish', report=report)))
        return gid

    def attach(self, content):
        path = self.root / 'policy.txt'
        path.write_text(content, encoding='utf-8')
        return self.store.attach_document(self.sid, path)

    def test_handoff_is_explicit_immutable_and_keeps_evidence_roles(self):
        parent = self.parent()
        before = a.get_goal(self.store, self.sid, parent)
        ordinary = a.create_goal(self.store, self.sid, '計算費用')
        self.assertIsNone(a.get_goal(self.store, self.sid, ordinary)['previous_stage'])
        child = a.create_goal(self.store, self.sid, '沿用已選方案計算費用', parent_id=parent)
        result = a.get_goal(self.store, self.sid, child)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['steps'], [])
        previous = result['previous_stage']
        self.assertEqual(previous['report'], before['result'])
        self.assertIn('已確認採用方案甲', previous['user_notes'])
        self.assertEqual(previous['recorded_status'], 'report_ready')
        self.assertEqual(a.get_goal(self.store, self.sid, parent), before)
        a.update(self.store, parent, 'report_ready', result='後來的不同內容')
        self.assertEqual(a.get_goal(Store(self.store.path), self.sid, child)['previous_stage'], previous)

    def test_ancestor_tools_are_not_copied_and_citations_require_fresh_read(self):
        did = self.attach('每月120元')
        parent = a.create_goal(self.store, self.sid, '比較方案')
        self.store.remember('PRIVATE_TOOL_ONLY', 'preference')
        a.run_goal(self.store, self.sid, parent, ScriptedEngine(action('recall', query='偏好'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('finish', report=f'每月120元[D{did}:L1]')))
        child = a.create_goal(self.store, self.sid, '沿用方案計算年費', parent_id=parent)
        engine = ScriptedEngine(action('finish', report=f'年費1440元[D{did}:L1]'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('calculate', operation='product', values=['120', '12']),
            action('finish', report=f'年費1440元[D{did}:L1]'))
        result = a.run_goal(self.store, self.sid, child, engine)
        self.assertEqual(result['status'], 'report_ready')
        self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
        self.assertNotIn('PRIVATE_TOOL_ONLY', '\n'.join(engine.prompts))
        payload = json.loads(engine.prompts[0].split('目標與已觀察資料：\n')[1])
        self.assertEqual(payload['steps'], [])
        self.assertEqual(payload['documents'][0]['line_count'], 1)
        self.assertNotIn('steps', payload['previous_stage'])

    def delivered_parent(self):
        parent = a.create_goal(self.store, self.sid, '交付當期排程。', file_delivery=True)
        done = a.run_goal(self.store,self.sid,parent,ScriptedEngine(
            action('write_artifact',filename='booking.json',content='{"room":"青杉間","participants":["研秋"]}'),
            action('write_artifact',filename='draft.txt',content='未交付的內部草稿'),
            action('deliver',filenames=['booking.json'],report='已交付 booking.json。'),
        ),max_steps=3)
        self.assertEqual(done['status'],'report_ready')
        return parent

    def test_explicit_prior_file_read_uses_delivered_bytes_and_survives_restore(self):
        parent = self.delivered_parent()
        original = artifacts.directory(self.store,parent)/'booking.json'
        original.write_text('{"room":"本機另改","participants":["誤用"]}',encoding='utf-8')
        child = a.create_goal(self.store,self.sid,
                              '正式改期。用上一階段實際交付的 booking.json 內的participants 和 room。',
                              parent_id=parent)
        unauthorized = ScriptedEngine(
            action('read_previous_file',filename='draft.txt'),
            action('ask',question='請重新匯入 booking.json。'),
        )
        paused = a.run_goal(self.store,self.sid,child,unauthorized,max_steps=2)
        self.assertEqual(paused['status'],'paused')
        rejected, read = paused['steps']
        self.assertEqual(rejected['action']['tool'],'invalid')
        self.assertEqual(read['action']['tool'],'read_previous_file')
        saved = read['observation']['previous_file']
        self.assertEqual(saved['content'],'{"room":"青杉間","participants":["研秋"]}')
        self.assertEqual(saved['parent_goal_id'],parent)
        self.assertEqual(saved['delivery_sequence'],3)
        self.assertEqual(read['observation']['previous_file_recovery'],{'before_tool':'ask'})
        self.assertNotIn('未交付的內部草稿','\n'.join(unauthorized.prompts))
        self.assertNotIn('青杉間',unauthorized.prompts[0])
        self.assertIn('本機另改',original.read_text(encoding='utf-8'))

        backup = create_backup(self.store.path,self.root/'prior-read.paibackup')
        restore_backup(backup['path'],self.root/'prior-restored')
        restored = Store(self.root/'prior-restored'/'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,child),paused)
        engine = ScriptedEngine(action('finish',report='場地青杉間，研秋參加。[上一階段檔案#2]'))
        result = a.run_goal(restored,self.sid,child,engine,max_steps=1)
        self.assertEqual(result['status'],'report_ready')
        self.assertIn('青杉間',engine.prompts[0])
        self.assertNotIn('本機另改',engine.prompts[0])
        self.assertEqual(result['steps'][-1]['observation']['previous_file_citations'][0]['sha256'],saved['sha256'])
        validate_database(restored.path)
        with restored.connect() as db:
            changed = {**read['observation'],'previous_file':{**saved,'content':'換成不同內容'}}
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(changed,ensure_ascii=False),child))
        with self.assertRaisesRegex(ValueError,'上一階段檔案查讀'):
            validate_database(restored.path)

    def test_prior_file_scope_excludes_inherited_quotes_and_non_delivered_files(self):
        parent = self.delivered_parent()
        for objective, ancestor in (
            ('請核對排程摘要。',parent),
            ('範例：「請用上一階段實際交付的 booking.json」。只核對摘要。',parent),
            ('是否能讀上一階段已交付的 booking.json？',parent),
            ('若可行，請讀上一階段已交付的 booking.json。',parent),
            ('請用上一階段實際交付的 booking.json。',None),
        ):
            with self.subTest(objective=objective,ancestor=ancestor):
                child = a.create_goal(self.store,self.sid,objective,parent_id=ancestor)
                engine = ScriptedEngine(action('read_previous_file',filename='booking.json'))
                result = a.run_goal(self.store,self.sid,child,engine,max_steps=1)
                self.assertEqual(result['steps'][0]['action']['tool'],'invalid')
                self.assertNotIn('previous_stage_files',engine.prompts[0])
                self.assertNotIn('青杉間',engine.prompts[0])
        child = a.create_goal(self.store,self.sid,'請讀取上一階段已交付的「draft.txt」。',parent_id=parent)
        engine = ScriptedEngine(action('ask',question='請提供正式稿。'))
        result = a.run_goal(self.store,self.sid,child,engine,max_steps=1)
        self.assertEqual(result['status'],'needs_input')
        self.assertNotIn('未交付的內部草稿',engine.prompts[0])

    def test_saved_user_guidance_can_revoke_and_restore_previous_file_read_scope(self):
        parent = self.delivered_parent()
        child = a.create_goal(self.store,self.sid,'請讀取上一階段已交付的「booking.json」。',parent_id=parent)
        read_engine = ScriptedEngine(action('read_previous_file',filename='booking.json'))
        a.run_goal(self.store,self.sid,child,read_engine,max_steps=1)
        view = goal_guidance.snapshot(self.store,self.sid,child)
        goal_guidance.save(self.store,self.sid,child,'這次請勿再讀取上一階段已交付的 booking.json。',
                           expected_fingerprint=view['fingerprint'])
        engine = ScriptedEngine(
            action('finish',report='引用舊檔。[上一階段檔案#1]'),
            action('ask',question='請問目前場地為何？'),
        )
        result = a.run_goal(self.store,self.sid,child,engine,max_steps=2)
        self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
        self.assertNotIn('青杉間','\n'.join(engine.prompts))
        self.assertEqual(result['status'],'needs_input')
        a.reply(self.store,self.sid,child,'請用上一階段已交付的 booking.json 的資料。',
                expected_fingerprint=reply_snapshot(self.store,self.sid,child)['fingerprint'])
        result = a.run_goal(self.store,self.sid,child,ScriptedEngine(
            action('finish',report='場地為青杉間。[上一階段檔案#1]'),
        ),max_steps=1)
        self.assertEqual(result['status'],'report_ready')
        validate_database(self.store.path)

    def test_current_source_changes_are_marked_and_removed_sources_stay_unavailable(self):
        did = self.attach('舊費用120元')
        parent = self.parent()
        self.attach('新費用150元')
        child = a.create_goal(self.store, self.sid, '依目前文件重算', parent_id=parent)
        self.assertFalse(a.get_goal(self.store, self.sid, child)['previous_stage']['sources_match_current'])
        result = a.run_goal(self.store, self.sid, child, ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1)), max_steps=1)
        self.assertIn('150元', result['steps'][0]['observation']['excerpts'][0]['text'])
        self.store.detach_document(self.sid, did)
        detached = a.create_goal(self.store, self.sid, '再核對', parent_id=parent)
        result = a.run_goal(self.store, self.sid, detached, ScriptedEngine(
            action('read', document_id=did, start_line=1, end_line=1)), max_steps=1)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['steps'][0]['observation']['requested_ranges'][0]['status'], 'document_not_attached')

    def test_unfinished_wrong_session_and_active_parent_do_not_create_children(self):
        pending = a.create_goal(self.store, self.sid, '尚未完成')
        parent = self.parent()
        before = a.goals(self.store, self.sid)
        with self.assertRaises(ValueError):
            a.create_goal(self.store, self.sid, '不能跳過', parent_id=pending)
        with self.assertRaises(ValueError):
            a.create_goal(self.store, self.store.new_session(), '不同對話', parent_id=parent)
        with lease(self.store, parent), self.assertRaises(LeaseBusy):
            a.create_goal(self.store, self.sid, '仍被持有', parent_id=parent)
        self.assertEqual(a.goals(self.store, self.sid), before)

    def test_handoff_insert_failure_rolls_back_child_goal(self):
        parent = self.parent()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_handoff BEFORE INSERT ON autonomous_handoffs BEGIN SELECT RAISE(ABORT, 'test refusal'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            a.create_goal(self.store, self.sid, '不應留下半份新目標', parent_id=parent)
        self.assertEqual(len(a.goals(self.store, self.sid)), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_handoffs').fetchone()[0], 0)

    def test_backup_roundtrip_and_v019_migration_preserve_existing_goals(self):
        parent = self.parent()
        child = a.create_goal(self.store, self.sid, '下一階段', parent_id=parent)
        original = a.get_goal(self.store, self.sid, child)
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        restored = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored, self.sid, child), original)
        with restored.connect() as db:
            db.execute('DROP TABLE autonomous_handoffs')
        self.assertNotIn('autonomous_handoffs', validate_database(restored.path))
        legacy = create_backup(restored.path)
        restore_backup(legacy['path'], self.root / 'legacy')
        upgraded = Store(self.root / 'legacy' / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(upgraded, self.sid, parent)['result'], '方案甲的工作成果')
        self.assertIsNone(a.get_goal(upgraded, self.sid, child)['previous_stage'])

    def test_backup_rejects_cross_session_links(self):
        parent = self.parent()
        other = a.create_goal(self.store, self.store.new_session(), '另一個對話')
        with self.store.connect() as db:
            db.execute('INSERT INTO autonomous_handoffs(goal_id,parent_goal_id,context_json) VALUES(?,?,?)', (other, parent, '{}'))
        with self.assertRaisesRegex(ValueError, '同一對話'):
            validate_database(self.store.path)

    def test_only_immediate_stage_is_carried_and_chat_has_relationship_not_content(self):
        parent = self.parent('FIRST_STAGE_PRIVATE_REPORT')
        child = a.create_goal(self.store, self.sid, '第二階段', parent_id=parent)
        a.run_goal(self.store, self.sid, child, ScriptedEngine(action('finish', report='SECOND_STAGE_REPORT')))
        grandchild = a.create_goal(self.store, self.sid, '第三階段', parent_id=child)
        previous = a.get_goal(self.store, self.sid, grandchild)['previous_stage']
        self.assertNotIn('FIRST_STAGE_PRIVATE_REPORT', json.dumps(previous))
        self.assertEqual(previous['report'], 'SECOND_STAGE_REPORT')
        context = goal_context(self.store, self.sid)
        self.assertEqual(context['goals'][0]['parent_goal_id'], child)
        self.assertNotIn('SECOND_STAGE_REPORT', json.dumps(context))

    def test_cli_creates_followup_without_model_and_rejects_invalid_combination(self):
        parent = self.parent()
        base = [sys.executable, '-m', 'assistant', '--data-dir', str(self.root), '--session', self.sid]
        result = subprocess.run([*base, '--new-goal', '計算年費', '--from-goal', parent], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['previous_stage']['parent_goal_id'], parent)
        result = subprocess.run([*base, '--goal-status', parent, '--from-goal', parent], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
