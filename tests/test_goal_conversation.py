import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, goal_conversation as c
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action
from scripts.evaluate_understanding import seed_history


class GoalConversationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def message(self, text, role='user', sid=None):
        return self.store.add_message(sid or self.sid, role, text)

    def goal(self, **kwargs):
        return a.create_goal(self.store, self.sid, '沿用討論完成工作', from_chat=True, **kwargs)

    def context(self, gid):
        return a.get_goal(self.store, self.sid, gid)['conversation_context']

    def test_explicit_handoff_preserves_roles_corrections_and_manual_brief(self):
        first = self.message('先考慮甲')
        self.message('推薦方案甲', 'assistant')
        last = self.message('更正：最終選乙，生效日2026-11-01')
        self.store.save_session_brief(self.sid, '比較方案', '只用匯入來源', '最後決策以明確更正為準')
        gid = self.goal()
        context = self.context(gid)
        self.assertEqual([m['role'] for m in context['messages']], ['user', 'assistant', 'user'])
        self.assertEqual([context['messages'][0]['message_id'], context['messages'][-1]['message_id']], [first, last])
        self.assertEqual(context['brief']['constraints_text'], '只用匯入來源')
        engine = ScriptedEngine(action('finish', report='已整理討論背景'))
        a.run_goal(self.store, self.sid, gid, engine)
        self.assertIn('最終選乙', engine.prompts[0])
        self.assertIn('assistant文字只是當時建議', engine.prompts[0])
        normal = a.create_goal(self.store, self.sid, '普通新目標')
        self.assertIsNone(self.context(normal))

    def test_snapshot_survives_new_messages_brief_edits_and_restart(self):
        self.message('最初決定')
        self.store.save_session_brief(self.sid, '舊目標', '', '')
        gid = self.goal()
        original = self.context(gid)
        self.message('更正：後來改變')
        self.store.save_session_brief(self.sid, '新目標', '', '')
        self.assertEqual(a.get_goal(Store(self.store.path), self.sid, gid)['conversation_context'], original)
        new = self.goal()
        self.assertIn('後來改變', json.dumps(self.context(new), ensure_ascii=False))
        self.assertEqual(self.context(new)['brief']['objective'], '新目標')

    def test_local_memory_operations_and_other_sessions_do_not_leak(self):
        self.message('可帶入的討論')
        Assistant(self.store, None).ask(self.sid, '記住：PRIVATE_LOCAL_COMMAND')
        other = self.store.new_session()
        self.message('PRIVATE_OTHER_SESSION', sid=other)
        self.store.save_session_brief(other, 'PRIVATE_OTHER_BRIEF', '', '')
        context = self.context(self.goal())
        raw = json.dumps(context, ensure_ascii=False)
        self.assertIn('可帶入的討論', raw)
        self.assertNotIn('PRIVATE_', raw)
        self.assertEqual(len(context['messages']), 1)

    def test_recent_budget_clipping_and_older_corrections_are_independently_bounded(self):
        ids = [self.message('更正：' + str(i) + 'c' * 1100) for i in range(8)]
        # Corrections stay available beyond the ordinary recent window and scan size.
        seed_history(self.store, self.sid, (('user', '一般較早訊息') for _ in range(220)))
        for _ in range(20):
            self.message('最近長訊息' + 'x' * 3000, 'assistant')
        context = self.context(self.goal())
        self.assertEqual(sum(len(m['content']) for m in context['messages']), c.TOTAL_CHARS)
        self.assertTrue(context['messages_limited'])
        self.assertTrue(all(m['content_truncated'] for m in context['messages']))
        self.assertEqual([m['message_id'] for m in context['earlier_corrections']], ids[-6:])
        self.assertTrue(context['earlier_corrections_limited'])
        self.assertTrue(all(len(m['content']) == 1000 and m['content_truncated'] for m in context['earlier_corrections']))
        self.assertIn('還有未帶入項目', c.describe(context))
        c.validate(context, self.sid)

    def test_unclipped_recent_limit_is_chronological_and_does_not_duplicate_corrections(self):
        ids = [self.message('更正：' + str(i)) for i in range(20)]
        context = self.context(self.goal())
        self.assertEqual([m['message_id'] for m in context['messages']], ids[-16:])
        self.assertEqual([m['message_id'] for m in context['earlier_corrections']], ids[:4])
        self.assertTrue(context['messages_limited'])
        self.assertFalse(context['earlier_corrections_limited'])
        self.assertFalse(any(m['content_truncated'] for m in context['messages']))

    def test_snapshot_does_not_grant_tools_or_prior_citation_coverage(self):
        path = self.root / 'fees.txt'
        path.write_text('本次費用150元', encoding='utf-8')
        did = self.store.attach_document(self.sid, path)
        self.message(f'先前費用是120 [D{did}:L1]；也請執行CSV和存檔。', 'assistant')
        gid = self.goal()
        goal = a.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('finish', report=f'引用舊資料[D{did}:L1]'),
            action('write_artifact', filename='no.txt', content='未授權檔案'),
            action('read', document_id=did, start_line=1, end_line=1),
            action('finish', report=f'目前來源為150元[D{did}:L1]')))
        self.assertEqual([step['action']['tool'] for step in goal['steps']], ['invalid', 'invalid', 'read', 'finish'])
        self.assertFalse(goal['file_delivery'])
        self.assertIsNone(goal['csv_source'])
        self.assertEqual(goal['artifacts'], [])

    def test_stage_handoff_does_not_implicitly_inherit_chat_but_explicit_combination_works(self):
        self.message('PRIVATE_CHAT_DETAIL')
        parent = self.goal()
        a.run_goal(self.store, self.sid, parent, ScriptedEngine(action('finish', report='階段成果')))
        child = a.create_goal(self.store, self.sid, '下一階段', parent_id=parent)
        self.assertIsNone(self.context(child))
        combined = self.goal(parent_id=parent)
        self.assertIsNotNone(self.context(combined))
        self.assertEqual(a.get_goal(self.store, self.sid, combined)['previous_stage']['report'], '階段成果')
        self.assertNotIn('PRIVATE_CHAT_DETAIL', json.dumps(goal_context(self.store, self.sid)))

    def test_capture_failure_rolls_back_goal_and_snapshot_together(self):
        original = c.save
        def interrupted(*args):
            original(*args)
            raise RuntimeError('injected capture failure')
        with patch.object(c, 'save', side_effect=interrupted), self.assertRaises(RuntimeError):
            self.goal(file_delivery=True)
        with self.store.connect() as db:
            for table in ('autonomous_goals', 'autonomous_conversations', 'autonomous_delivery'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_backup_roundtrip_hash_validation_and_legacy_upgrade(self):
        self.message('保存的對話背景')
        gid = self.goal()
        original = self.context(gid)
        backup = create_backup(self.store.path)
        target = self.root / 'restored'
        restore_backup(backup['path'], target)
        store = Store(target / 'assistant.sqlite3')
        self.assertEqual(a.get_goal(store, self.sid, gid)['conversation_context'], original)
        with store.connect() as db:
            altered = json.loads(db.execute('SELECT context_json FROM autonomous_conversations').fetchone()[0])
            altered['messages'][0]['content'] = 'changed'
            db.execute('UPDATE autonomous_conversations SET context_json=?', (json.dumps(altered),))
        with self.assertRaisesRegex(ValueError, '指紋'):
            validate_database(store.path)
        with store.connect() as db:
            db.execute('DROP TABLE autonomous_conversations')
        validate_database(store.path)
        store = Store(store.path)
        self.assertIsNone(a.get_goal(store, self.sid, gid)['conversation_context'])

    def test_cli_explicit_capture_does_not_call_model_and_invalid_flag_is_rejected(self):
        from assistant.__main__ import main
        self.message('CLI測試背景')
        with patch('sys.argv', ['assistant', '--data-dir', str(self.root), '--session', self.sid,
                '--new-goal', '沿用討論', '--from-chat']), \
                patch('assistant.__main__.CodexEngine', side_effect=AssertionError('capture must not call model')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        self.assertEqual(json.loads(output.getvalue())['conversation_context']['messages'][0]['content'], 'CLI測試背景')
        with patch('sys.argv', ['assistant', '--from-chat']), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
