import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import memory_drafts as d
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine


class MemoryReplacementTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'assistant.sqlite3')
        self.sid=self.store.new_session()
        self.old=self.store.remember('星橋專案目前位於D槽。')
        self.other=self.store.remember('另一個專案仍在C槽。')
        mid=self.store.add_message(self.sid,'user','更正：星橋專案目前位於F槽。')
        self.proposal={'items':[{'kind':'fact','content':'星橋專案目前位於F槽。',
            'sources':[{'key':'M'+str(mid),'quote':'星橋專案目前位於F槽。'}]}]}
        self.did=self.make_draft()

    def make_draft(self):
        did=d.create(self.store,self.sid)
        d.generate(self.store,self.sid,did,ScriptedEngine(json.dumps(self.proposal,ensure_ascii=False)))
        return did

    def target(self, mid=None):
        snapshot=d.memory_target(self.store,mid or self.old)
        return {'memory_id':snapshot['memory']['id'],'fingerprint':snapshot['fingerprint']}

    def accept(self, target=None, did=None, **kwargs):
        did=did or self.did;draft=d.get(self.store,self.sid,did)
        return d.accept(self.store,self.sid,did,0,draft['proposals_sha256'],replace=target,
            **({'content':'星橋專案目前位於F槽。','kind':'fact'}|kwargs))

    def test_explicit_target_replaces_only_selected_memory_and_preserves_identity_and_before(self):
        before=d.memory_target(self.store,self.old)['memory']
        with self.store.connect() as db:sequence=db.execute('SELECT last_id FROM memory_sequence').fetchone()[0]
        receipt=self.accept(self.target())
        after=d.memory_target(self.store,self.old)['memory']
        self.assertEqual((after['id'],after['created_at'],after['active']),(before['id'],before['created_at'],before['active']))
        self.assertEqual(after['content'],'星橋專案目前位於F槽。')
        self.assertEqual(receipt['replacement']['before'],before)
        self.assertEqual(len(self.store.memories()),2)
        self.assertEqual(d.memory_target(self.store,self.other)['memory']['content'],'另一個專案仍在C槽。')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT last_id FROM memory_sequence').fetchone()[0],sequence)
        proof=d.provenance(self.store,self.old)[0]
        self.assertEqual(proof['acceptance'],receipt)

    def test_default_remains_new_memory_and_never_guesses_which_fact_to_replace(self):
        receipt=self.accept()
        self.assertIsNone(receipt['replacement'])
        self.assertNotEqual(receipt['memory_id'],self.old)
        self.assertIn('D槽',d.memory_target(self.store,self.old)['memory']['content'])

    def test_changes_to_content_kind_expiry_active_or_deletion_reject_stale_selection(self):
        original=d.memory_target(self.store,self.old)['memory']
        for field,value in [('content','另一視窗的新資訊'),('kind','lesson'),('expires_on','2099-12-31'),('active',0)]:
            with self.subTest(field=field):
                target=self.target()
                with self.store.connect() as db:
                    db.execute('UPDATE memories SET '+field+'=? WHERE id=?',(value,self.old))
                with self.assertRaisesRegex(ValueError,'已變更'):self.accept(target)
                self.assertEqual(d.get(self.store,self.sid,self.did)['acceptances'],[])
                with self.store.connect() as db:
                    db.execute('UPDATE memories SET '+field+'=? WHERE id=?',(original[field],self.old))
        target=self.target();self.store.forget(self.old)
        with self.assertRaisesRegex(ValueError,'刪除'):self.accept(target)
        self.assertEqual(len(self.store.memories()),1)

    def test_two_drafts_cannot_both_apply_against_the_same_old_snapshot(self):
        second=self.make_draft();target=self.target();barrier=threading.Barrier(2);outcomes=[]
        def work(did):
            barrier.wait()
            try:outcomes.append(self.accept(target,did))
            except Exception as exc:outcomes.append(exc)
        threads=[threading.Thread(target=work,args=(did,)) for did in (self.did,second)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(item,dict) for item in outcomes),1)
        self.assertEqual(sum(isinstance(item,ValueError) for item in outcomes),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_replacements').fetchone()[0],1)

    def test_replay_returns_historical_receipt_without_reapplying_or_resurrecting(self):
        target=self.target();receipt=self.accept(target)
        self.store.edit_memory(self.old,'後來修改的內容')
        self.assertEqual(self.accept(target),receipt)
        self.assertEqual(d.memory_target(self.store,self.old)['memory']['content'],'後來修改的內容')
        self.store.forget(self.old)
        self.assertEqual(self.accept(target),receipt)
        with self.assertRaises(ValueError):d.memory_target(self.store,self.old)
        with self.assertRaisesRegex(ValueError,'已保存'):self.accept()
        with self.assertRaisesRegex(ValueError,'已保存'):self.accept(self.target(self.other))
        validate_database(self.store.path)

    def test_disabled_state_and_expiry_are_not_implicitly_reenabled(self):
        self.store.edit_memory(self.old,'星橋專案舊資訊','fact','2000-01-01',False)
        receipt=self.accept(self.target(),expires_on='2000-01-01')
        after=d.memory_target(self.store,self.old)['memory']
        self.assertEqual((after['active'],after['expires_on']),(0,'2000-01-01'))
        self.assertEqual(receipt['replacement']['before']['active'],0)
        self.assertNotIn(self.old,[m['id'] for m in self.store.retrieve('星橋專案')])

    def test_update_receipt_failure_rolls_back_memory_change_and_all_audit_rows(self):
        before=d.memory_target(self.store,self.old)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_replace BEFORE INSERT ON memory_replacements BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.accept(self.target())
        self.assertEqual(d.memory_target(self.store,self.old),before)
        self.assertEqual(d.get(self.store,self.sid,self.did)['acceptances'],[])

    def test_backup_roundtrip_and_historical_fingerprint_tamper_rejected(self):
        receipt=self.accept(self.target());backup=create_backup(self.store.path)
        restore_backup(backup['path'],self.root/'restored');restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(d.get(restored,self.sid,self.did)['acceptances'][0],receipt)
        with restored.connect() as db:
            before=receipt['replacement']['before']|{'content':'tampered'}
            db.execute('UPDATE memory_replacements SET before_json=?',(json.dumps(before),))
        with self.assertRaisesRegex(ValueError,'指紋'):validate_database(restored.path)

    def test_v028_backup_without_replacement_table_keeps_original_receipts(self):
        receipt=self.accept()
        with self.store.connect() as db:db.execute('DROP TABLE memory_replacements')
        validate_database(self.store.path)
        self.assertEqual(d.get(self.store,self.sid,self.did)['acceptances'][0],receipt)
        upgraded=Store(self.store.path)
        self.assertEqual(d.get(upgraded,self.sid,self.did)['acceptances'][0],receipt)

    def test_new_conversation_receives_current_value_without_historical_before_text(self):
        self.accept(self.target())
        context=Assistant(self.store,None).prepare_context(self.store.new_session(),'星橋專案在哪裡？')
        raw=json.dumps(context,ensure_ascii=False)
        self.assertIn('星橋專案目前位於F槽',raw)
        self.assertNotIn('星橋專案目前位於D槽',raw)
        self.assertNotIn('before_sha256',raw)

    def test_invalid_target_spec_is_rejected_before_changes(self):
        for target in ({},{'memory_id':True,'fingerprint':'x'},{'memory_id':-1,'fingerprint':'x'},
                       {'memory_id':self.old,'fingerprint':None},{'memory_id':self.old,'fingerprint':'x','command':'run'}):
            with self.subTest(target=target),self.assertRaises(ValueError):self.accept(target)
        self.assertEqual(d.get(self.store,self.sid,self.did)['acceptances'],[])
