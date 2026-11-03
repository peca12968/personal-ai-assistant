import json
import tempfile
import unittest
from pathlib import Path

from assistant import autonomy, goal_conversation as gc, goal_drafts, memory_drafts
from assistant.backup import create_backup, restore_backup
from assistant.store import Store


class ConversationPriorityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root/'state.sqlite3'); self.sid = self.store.new_session()

    def add(self, role, content):
        return self.store.add_message(self.sid, role, content)

    def capture(self):
        with self.store.connect() as db:
            return gc.validate(gc.capture(db, self.sid), self.sid)

    def crowded(self):
        original = '採購8份，每份125元，整單運費40元。不要付款。'
        first = self.add('user', original)
        for i in range(8):
            self.add('assistant', '只是在說明，沒有新增採購條件。'*150)
            if i < 7:
                self.add('user', '條件沒有變，繼續說明。')
        return first, original

    def test_long_assistant_replies_do_not_displace_recent_user_requirements(self):
        first, original = self.crowded()
        value = self.capture()
        self.assertEqual(value['messages'][0]['message_id'], first)
        self.assertEqual(value['messages'][0]['content'], original)
        self.assertEqual(sum(m['role']=='user' for m in value['messages']), 8)
        self.assertEqual(sum(len(m['content']) for m in value['messages']), 12000)
        self.assertTrue(value['messages_limited'])
        self.assertTrue(any(m['role']=='assistant' for m in value['messages']))
        self.assertTrue(any(m['content_truncated'] for m in value['messages']))
        self.assertEqual([m['message_id'] for m in value['messages']], sorted(m['message_id'] for m in value['messages']))

    def test_drafts_can_quote_preserved_user_text_without_quoting_assistant(self):
        first, original = self.crowded()
        source = goal_drafts.capture(self.store, self.sid)
        key = 'M'+str(first)
        self.assertEqual(goal_drafts.catalog(source)[key], original)
        item = {'kind':'goal','text':'按已提供的條件計算總額，不付款。',
                'sources':[{'key':key,'quote':original}]}
        goal_drafts.validate({'items':[item], 'questions':[]}, source)
        memory_drafts.validate_items({'items':[{'kind':'fact','content':'這次採購8份，每份125元。',
            'sources':[{'key':key,'quote':'採購8份，每份125元'}]}]}, source['context'])
        self.assertEqual(len(goal_drafts.catalog(source)), 8)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0], 0)

    def test_user_overflow_still_prefers_newest_and_keeps_earlier_corrections(self):
        ids = [self.add('user', '更正：'+str(i)+'𠮷'*2200) for i in range(16)]
        value = self.capture()
        self.assertEqual([m['message_id'] for m in value['messages']], ids[-6:])
        self.assertEqual([m['message_id'] for m in value['earlier_corrections']], ids[4:10])
        self.assertTrue(value['earlier_corrections_limited'])
        self.assertTrue(all(m['content_truncated'] for m in value['messages']))

    def test_small_conversation_preserves_full_assistant_referent_and_roles(self):
        rows = [('user','列兩個方案'), ('assistant','甲：每月120。乙：每月150。'),
                ('user','第二個，算6個月。')]
        for role, text in rows: self.add(role,text)
        value = self.capture()
        self.assertEqual([(m['role'],m['content']) for m in value['messages']], rows)
        self.assertFalse(value['messages_limited'])
        self.assertFalse(any(m['content_truncated'] for m in value['messages']))

    def test_selection_stays_within_latest_sixteen_and_excludes_private_commands(self):
        old = self.add('user', 'OLD_NOT_SELECTED')
        for i in range(16): self.add('assistant', str(i))
        local = self.add('user','PRIVATE_LOCAL')
        with self.store.connect() as db:
            db.execute('INSERT INTO local_memory_messages VALUES (?)',(local,))
        other = self.store.new_session(); self.store.add_message(other,'user','PRIVATE_OTHER')
        value = self.capture()
        self.assertNotIn(old,[m['message_id'] for m in value['messages']])
        self.assertEqual(len(value['messages']),16)
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_snapshot_remains_immutable_and_survives_backup(self):
        first, original = self.crowded()
        source = goal_drafts.capture(self.store,self.sid)
        gid = autonomy.create_goal(self.store,self.sid,'計算採購總額',from_chat=True)
        saved = autonomy.get_goal(self.store,self.sid,gid)['conversation_context']
        self.assertEqual(saved['messages'][0]['content'],original)
        with self.store.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?',('改為9份',first))
        self.assertNotEqual(goal_drafts.fingerprint(source),goal_drafts.fingerprint(goal_drafts.capture(self.store,self.sid)))
        self.assertEqual(autonomy.get_goal(self.store,self.sid,gid)['conversation_context'],saved)
        backup = create_backup(self.store.path,self.root/'saved.paibackup')
        restore_backup(backup['path'],self.root/'restore')
        self.assertEqual(autonomy.get_goal(Store(self.root/'restore'/'assistant.sqlite3'),self.sid,gid)['conversation_context'],saved)


if __name__ == '__main__': unittest.main()
