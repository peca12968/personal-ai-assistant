import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from assistant import autonomy, goal_conversation as gc, goal_drafts, answer_feedback
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store


class ConversationTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state.sqlite3'); self.sid = self.store.new_session()

    def capture(self):
        with self.store.connect() as db: return gc.capture(db, self.sid)

    def test_recent_nul_keeps_complete_original_and_exact_limit_flags(self):
        for text in ['條件\0人數8人，每人125元。', '\0', '𠮷'*1999+'\0', 'x'*1999+'\0後文']:
            with self.subTest(length=len(text)):
                mid = self.store.add_message(self.sid, 'user', text)
                row = self.capture()['messages'][-1]
                self.assertEqual(row['message_id'], mid)
                self.assertEqual(row['content'], text[:2000])
                self.assertEqual(row['content_truncated'], len(text)>2000)

    def test_earlier_correction_preserves_tail_and_excludes_other_scopes(self):
        mid = self.store.add_message(self.sid, 'user', '更正：\0改為6人，每人180元。')
        other = self.store.new_session()
        self.store.add_message(other, 'user', '更正：PRIVATE_OTHER')
        local = self.store.add_message(self.sid, 'user', '更正：PRIVATE_LOCAL')
        with self.store.connect() as db: db.execute('INSERT INTO local_memory_messages(message_id) VALUES(?)',(local,))
        for _ in range(20): self.store.add_message(self.sid, 'user', '一般背景')
        value = self.capture(); gc.validate(value,self.sid)
        self.assertEqual(value['earlier_corrections'][0]['message_id'], mid)
        self.assertEqual(value['earlier_corrections'][0]['content'], '更正：\0改為6人，每人180元。')
        self.assertFalse(value['earlier_corrections'][0]['content_truncated'])
        self.assertNotIn('PRIVATE_',json.dumps(value))
        self.assertIn('改為6人',self.store.earlier_correction_messages(self.sid,10**9)[0]['content'])

    def test_shared_budget_counts_nul_and_unicode_as_original_codepoints(self):
        text = '開頭\0' + '𠮷🦉漢e\u0301'*600
        for _ in range(17): self.store.add_message(self.sid,'user',text)
        value=self.capture()
        self.assertEqual(len(value['messages']),6)
        self.assertEqual(sum(len(r['content']) for r in value['messages']),12000)
        self.assertTrue(value['messages_limited'])
        self.assertTrue(all(r['content']==text[:2000] and r['content_truncated'] for r in value['messages']))

    def test_source_change_after_nul_invalidates_draft_without_rewriting_goal(self):
        mid=self.store.add_message(self.sid,'user','條件\0總額為1040元。')
        source=goal_drafts.capture(self.store,self.sid,'整理總額')
        gid=autonomy.create_goal(self.store,self.sid,'保留歷史',from_chat=True)
        historical=autonomy.get_goal(self.store,self.sid,gid)['conversation_context']
        with self.store.connect() as db: db.execute('UPDATE messages SET content=? WHERE id=?',('條件\0總額為1080元。',mid))
        newer=goal_drafts.capture(self.store,self.sid,'整理總額')
        self.assertNotEqual(goal_drafts.fingerprint(source),goal_drafts.fingerprint(newer))
        self.assertEqual(autonomy.get_goal(self.store,self.sid,gid)['conversation_context'],historical)

    def test_goal_draft_quote_after_separator_is_available_without_new_authority(self):
        mid=self.store.add_message(self.sid,'user','以下為條件\0請核對人數8人，不要付款。')
        source=goal_drafts.capture(self.store,self.sid,'整理工作')
        value={'items':[{'kind':'goal','text':'核對人數8人，不付款',
               'sources':[{'key':'M'+str(mid),'quote':'請核對人數8人，不要付款。'}]}],'questions':[]}
        goal_drafts.validate(value,source)
        self.assertEqual(autonomy.goals(self.store,self.sid),[])
        self.assertEqual(self.store.memories(),[])

    def test_utf16_database_prefixes_and_feedback_remain_exact(self):
        for encoding in ('UTF-16le','UTF-16be'):
            with self.subTest(encoding=encoding):
                path=self.root/(encoding+'.sqlite3')
                with closing(sqlite3.connect(path)) as db:
                    db.execute("PRAGMA encoding='"+encoding+"'")
                    db.execute('CREATE TABLE seed(x)')
                    db.commit()
                store=Store(path);sid=store.new_session()
                text='更正：\0'+'𠮷漢🦉'*900
                mid=store.add_message(sid,'user',text)
                answer=store.add_message(sid,'assistant','回答\0'+'🦉'*2000)
                store.rate(answer,-1,text)
                with store.connect() as db:
                    db.execute('DROP TABLE seed')
                    self.assertEqual(db.execute('PRAGMA encoding').fetchone()[0],encoding)
                    captured=gc.capture(db,sid)
                    feedback=answer_feedback.capture(db,sid)['items'][0]
                self.assertEqual(captured['messages'][0]['message_id'],mid)
                self.assertEqual(captured['messages'][0]['content'],text[:2000])
                self.assertEqual(feedback['correction'],text[:1000])
                self.assertEqual(feedback['answer_excerpt'],('回答\0'+'🦉'*2000)[:1200])
                self.assertTrue(feedback['correction_truncated'])
                self.assertTrue(feedback['answer_truncated'])
                validate_database(path)

    def test_malformed_text_fails_instead_of_silently_dropping_bytes(self):
        mid=self.store.add_message(self.sid,'user','placeholder')
        with self.store.connect() as db:
            db.execute("UPDATE messages SET content=CAST(X'61FF62' AS TEXT) WHERE id=?",(mid,))
        with self.assertRaises(ValueError):self.capture()
        with self.assertRaises(ValueError):autonomy.create_goal(self.store,self.sid,'不可存半份來源',from_chat=True)
        self.assertEqual(autonomy.goals(self.store,self.sid),[])

    def test_snapshot_backup_and_plain_text_compatibility(self):
        original='原文\0後面的明確條件。'
        self.store.add_message(self.sid,'user',original)
        gid=autonomy.create_goal(self.store,self.sid,'核對',from_chat=True)
        value=autonomy.get_goal(self.store,self.sid,gid)['conversation_context']
        self.assertEqual(value['messages'][0]['content'],original)
        backup=create_backup(self.store.path,self.root/'saved.paibackup')
        restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored,self.sid,gid)['conversation_context'],value)
        self.store.add_message(self.sid,'user','一般內容')
        row=self.capture()['messages'][-1]
        self.assertEqual(row['content'],'一般內容');self.assertFalse(row['content_truncated'])


if __name__=='__main__':unittest.main()
