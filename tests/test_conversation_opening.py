import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy as a, goal_conversation as gc, goal_drafts, memory_drafts
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ConversationOpeningTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'state.sqlite3')
        self.sid=self.store.new_session()

    def capture(self):
        with self.store.connect() as db:
            return gc.validate(gc.capture(db,self.sid),self.sid)

    def fill(self,count=230):
        for i in range(count):self.store.add_message(self.sid,'user',f'背景討論{i}')

    def test_opening_and_later_correction_reach_real_goal_and_draft_sources(self):
        text='採購8份，每份125元，整單運費40元；只試算、不付款。'
        first=self.store.add_message(self.sid,'user',text)
        cid=self.store.add_message(self.sid,'user','更正一下，份數改9份，其他條件不變。')
        self.fill()
        value=self.capture();opening=value['opening_user_message']
        self.assertEqual((opening['message_id'],opening['content']),(first,text))
        self.assertEqual([r['message_id'] for r in value['earlier_corrections']],[cid])
        source=goal_drafts.capture(self.store,self.sid,'本次只規劃，不執行。')
        self.assertEqual(goal_drafts.catalog(source)[f'M{first}'],text)
        items={'items':[{'kind':'lesson','content':'採購試算保留整單運費。',
                       'sources':[{'key':f'M{first}','quote':'整單運費40元'}]}]}
        self.assertTrue(memory_drafts.validate_items(items,value))
        gid=a.create_goal(self.store,self.sid,'這次改10份，其他條件沿用。',from_chat=True)
        engine=ScriptedEngine(action('ask',question='測試暫停'))
        goal=a.run_goal(self.store,self.sid,gid,engine,max_steps=1)
        self.assertEqual(goal['conversation_context'],value | {'captured_at':goal['conversation_context']['captured_at']})
        self.assertIn(text,engine.prompts[0]);self.assertIn('這次改10份',engine.prompts[0])
        plain=a.create_goal(self.store,self.sid,'普通工作')
        self.assertIsNone(a.get_goal(self.store,self.sid,plain)['conversation_context'])
        self.assertEqual(self.store.retrieve('採購'),[])

    def test_local_operations_other_sessions_and_duplicate_excerpts_are_excluded(self):
        Assistant(self.store,None).ask(self.sid,'記住：本機操作不可帶入')
        self.store.add_message(self.sid,'assistant','助理不是開場使用者條件')
        other=self.store.new_session();self.store.add_message(other,'user','另一對話秘密')
        first=self.store.add_message(self.sid,'user','有效開場')
        self.assertIsNone(self.capture()['opening_user_message'])
        self.fill(20)
        opening=self.capture()['opening_user_message']
        self.assertEqual(opening['message_id'],first)
        raw=json.dumps(self.capture(),ensure_ascii=False)
        self.assertNotIn('本機操作不可帶入',raw);self.assertNotIn('另一對話秘密',raw)
        with self.store.connect() as db:db.execute('UPDATE messages SET content=? WHERE id=?',('更正一下，有效開場',first))
        value=self.capture()
        self.assertIsNone(value['opening_user_message'])
        self.assertEqual(value['earlier_corrections'][0]['message_id'],first)

    def test_opening_is_bounded_unicode_text_with_nul_and_explicit_truncation(self):
        raw='原始\x00條件'+ '🙂'*2100+'不在片段的尾端'
        first=self.store.add_message(self.sid,'user',raw)
        self.fill(20)
        opening=self.capture()['opening_user_message']
        self.assertEqual(opening['message_id'],first)
        self.assertEqual(opening['content'],raw[:2000])
        self.assertTrue(opening['content_truncated'])
        self.assertIn('開場',gc.describe(self.capture()))
        self.assertNotIn('不在片段的尾端',memory_drafts.catalog(self.capture())[f'M{first}'])

    def test_validation_rejects_invalid_anchor_and_preserves_legacy_bytes(self):
        self.store.add_message(self.sid,'user','開場原文');self.fill(20)
        current=self.capture()
        for change in ({'role':'assistant'},{'message_id':True},
                       {'message_id':current['messages'][0]['message_id']},
                       {'content':'x'*2001},{'content_truncated':1},{'extra':'field'}):
            value=copy.deepcopy(current);value['opening_user_message'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):gc.validate(value,self.sid)
        for bad in ([],True,'text',{}):
            value=copy.deepcopy(current);value['opening_user_message']=bad
            with self.subTest(bad=bad),self.assertRaises(ValueError):gc.validate(value,self.sid)
        legacy=copy.deepcopy(current);legacy.pop('opening_user_message')
        digest=gc.digest(legacy)
        self.assertEqual(gc.validate(legacy,self.sid),legacy)
        self.assertEqual(gc.digest(legacy),digest)
        older=copy.deepcopy(legacy);older.pop('answer_feedback')
        self.assertEqual(gc.validate(older,self.sid),older)
        gid=a.create_goal(self.store,self.sid,'帶入舊版',from_chat=True)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_conversations SET context_json=?,context_sha256=? WHERE goal_id=?',
                       (json.dumps(legacy,ensure_ascii=False),digest,gid))
        self.assertEqual(a.get_goal(self.store,self.sid,gid)['conversation_context'],legacy)

    def test_frozen_goal_memory_draft_and_backup_survive_later_history_edits(self):
        first=self.store.add_message(self.sid,'user','開場條件保留45天。');self.fill(20)
        gid=a.create_goal(self.store,self.sid,'沿用條件',from_chat=True)
        did=memory_drafts.create(self.store,self.sid)
        original=a.get_goal(self.store,self.sid,gid)['conversation_context']
        draft=memory_drafts.get(self.store,self.sid,did)
        source=goal_drafts.capture(self.store,self.sid)
        with self.store.connect() as db:db.execute('UPDATE messages SET content=? WHERE id=?',('開場改成90天。',first))
        self.assertNotEqual(goal_drafts.fingerprint(source),goal_drafts.fingerprint(goal_drafts.capture(self.store,self.sid)))
        self.assertEqual(a.get_goal(Store(self.store.path),self.sid,gid)['conversation_context'],original)
        self.assertEqual(memory_drafts.get(self.store,self.sid,did)['context'],draft['context'])
        archive=create_backup(self.store.path,self.root/'backup.paibackup')
        restore_backup(archive['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,gid)['conversation_context'],original)
        self.assertEqual(memory_drafts.get(restored,self.sid,did)['context'],draft['context'])


if __name__=='__main__':unittest.main()
