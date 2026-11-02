import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import memory_drafts as d
from assistant.answer_runs import lease, LeaseBusy
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine


class MemoryDraftTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'assistant.sqlite3')
        self.sid=self.store.new_session()
        self.mid=self.store.add_message(self.sid,'user','我做星橋專案，程式放在F槽。')
        self.did=d.create(self.store,self.sid)

    def proposal(self, **changes):
        return {'kind':'fact','content':'星橋專案程式放在F槽。',
                'sources':[{'key':'M'+str(self.mid),'quote':'星橋專案，程式放在F槽'}],**changes}

    def generate(self, items=None, **kwargs):
        return d.generate(self.store,self.sid,self.did,ScriptedEngine(json.dumps({'items':items if items is not None else [self.proposal()]},ensure_ascii=False)),**kwargs)

    def accept(self, **kwargs):
        draft=d.get(self.store,self.sid,self.did)
        return d.accept(self.store,self.sid,self.did,0,draft['proposals_sha256'],
            **({'content':'星橋專案程式放在F槽。','kind':'fact'}|kwargs))

    def test_draft_generation_never_writes_memory_and_acceptance_has_provenance(self):
        result=self.generate()
        self.assertEqual(result['status'],'ready');self.assertEqual(self.store.memories(),[])
        receipt=self.accept(content='星橋專案放在F槽，這是我核對後的說法。',expires_on='2099-12-31')
        self.assertEqual(self.store.memories()[0]['id'],receipt['memory_id'])
        proof=d.provenance(self.store,receipt['memory_id'])[0]
        self.assertEqual(proof['source_texts']['M'+str(self.mid)],'我做星橋專案，程式放在F槽。')
        self.assertIn('我核對後',proof['acceptance']['content'])
        self.assertNotIn('我核對後',proof['proposal']['content'])
        self.store.edit_memory(receipt['memory_id'],'後來改用其他路徑')
        self.assertEqual(d.provenance(self.store,receipt['memory_id'])[0],proof)

    def test_same_session_snapshot_is_fixed_and_excludes_local_operations(self):
        Assistant(self.store,None).ask(self.sid,'記住：PRIVATE_LOCAL')
        other=self.store.new_session();self.store.add_message(other,'user','PRIVATE_OTHER')
        did=d.create(self.store,self.sid);before=d.get(self.store,self.sid,did)
        self.store.add_message(self.sid,'user','後來的資料')
        self.assertEqual(d.get(self.store,self.sid,did),before)
        self.assertNotIn('PRIVATE_',json.dumps(before))
        with self.assertRaises(ValueError):d.get(self.store,other,did)
        with self.assertRaises(ValueError):d.generate(self.store,other,did,None)

    def test_only_user_or_feedback_quotes_can_support_a_proposal(self):
        aid=self.store.add_message(self.sid,'assistant','我猜你喜歡簡短回答。')
        self.store.rate(aid,-1,'更正：我偏好先給結論。')
        self.did=d.create(self.store,self.sid)
        context=d.get(self.store,self.sid,self.did)['context']
        for refs in ([{'key':'M'+str(aid),'quote':'喜歡簡短回答'}],
                     [{'key':'M999','quote':'不在來源'}],
                     [{'key':'M'+str(self.mid),'quote':'編造引文'}]):
            with self.subTest(refs=refs),self.assertRaises(ValueError):
                d.validate_items({'items':[self.proposal(sources=refs)]},context)
        result=self.generate([self.proposal(kind='preference',content='偏好先給結論。',sources=[{'key':'F'+str(aid),'quote':'我偏好先給結論'}])])
        self.assertEqual(result['status'],'ready')

    def test_empty_draft_is_valid_and_cannot_be_accepted(self):
        self.generate([])
        with self.assertRaises(ValueError):self.accept()
        self.assertEqual(self.store.memories(),[])

    def test_limits_duplicates_bad_json_and_untrusted_action_fields_are_rejected(self):
        context=d.get(self.store,self.sid,self.did)['context']
        for value in ({'items':[self.proposal()]*9},{'items':[self.proposal()]*2},
                      {'items':[self.proposal(content='x'*801)]},{'items':[self.proposal(kind='execute')]},
                      {'items':[self.proposal(command='delete')]},{'items':'wrong'},
                      {'items':[self.proposal(sources=[])]}):
            with self.subTest(value=value),self.assertRaises(ValueError):d.validate_items(value,context)
        with self.assertRaises(ValueError):d.generate(self.store,self.sid,self.did,ScriptedEngine('{"items":[],"items":[]}'))
        self.assertEqual(d.get(self.store,self.sid,self.did)['status'],'failed')
        self.assertEqual(self.store.memories(),[])

    def test_cancelled_generation_and_failed_retry_preserve_snapshot_without_memory(self):
        cancel=threading.Event()
        def stop():cancel.set();return json.dumps({'items':[self.proposal()]})
        with self.assertRaises(InterruptedError):
            d.generate(self.store,self.sid,self.did,ScriptedEngine(stop),cancel=cancel)
        before=d.get(self.store,self.sid,self.did)
        self.assertEqual(before['status'],'cancelled');self.assertEqual(before['items'],[])
        def fail():raise EngineError('PRIVATE_ERROR','process_failed')
        with self.assertRaises(EngineError):d.generate(self.store,self.sid,self.did,ScriptedEngine(fail))
        saved=d.get(self.store,self.sid,self.did)
        self.assertEqual(saved['status'],'failed');self.assertNotIn('PRIVATE_ERROR',str(saved))
        result=self.generate();self.assertEqual(result['attempts'],3)
        self.assertEqual(result['context'],before['context']);self.assertEqual(self.store.memories(),[])

    def test_cancel_during_validation_cannot_commit_late_proposals(self):
        cancel=threading.Event();original=d.validate_items
        def validate(value,context):
            result=original(value,context)
            if result['items']:cancel.set()
            return result
        with patch.object(d,'validate_items',side_effect=validate),self.assertRaises(InterruptedError):self.generate(cancel=cancel)
        self.assertEqual(d.get(self.store,self.sid,self.did)['items'],[])

    def test_lease_blocks_parallel_generation_and_save_without_mutation(self):
        with lease(self.store,self.did),self.assertRaises(LeaseBusy):self.generate()
        self.assertEqual(d.get(self.store,self.sid,self.did)['attempts'],0)
        self.generate()
        with lease(self.store,self.did),self.assertRaises(LeaseBusy):self.accept()
        self.assertEqual(self.store.memories(),[])
        self.assertEqual(d.generate(self.store,self.sid,self.did,None)['attempts'],1)

    def test_lost_running_owner_can_retry_only_after_lease_acquired(self):
        with self.store.connect() as db:db.execute("UPDATE memory_drafts SET status='running',attempts=1 WHERE id=?",(self.did,))
        with lease(self.store,self.did),self.assertRaises(LeaseBusy):self.generate()
        self.assertEqual(self.generate()['attempts'],2)

    def test_replay_reuses_receipt_and_never_resurrects_deleted_memory(self):
        self.generate();receipt=self.accept()
        self.assertEqual(self.accept(),receipt);self.assertEqual(len(self.store.memories()),1)
        self.store.forget(receipt['memory_id'])
        self.assertEqual(self.accept(),receipt);self.assertEqual(self.store.memories(),[])
        with self.assertRaisesRegex(ValueError,'已保存'):self.accept(content='不同內容')
        self.assertGreater(self.store.remember('新資訊'),receipt['memory_id'])
        validate_database(self.store.path)

    def test_exact_existing_memory_reused_and_other_memories_never_overwritten(self):
        old=self.store.remember('星橋專案在C槽。')
        identical=self.store.remember('星橋專案程式放在F槽。')
        self.generate();receipt=self.accept()
        self.assertEqual(receipt['memory_id'],identical);self.assertEqual(receipt['reused'],1)
        self.assertEqual(next(r for r in self.store.memories() if r['id']==old)['content'],'星橋專案在C槽。')
        self.assertEqual(len(self.store.memories()),2)

    def test_acceptance_and_id_allocation_roll_back_together(self):
        self.generate()
        with self.store.connect() as db:
            previous=db.execute('SELECT last_id FROM memory_sequence').fetchone()[0]
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON memory_acceptances BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.accept()
        self.assertEqual(self.store.memories(),[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT last_id FROM memory_sequence').fetchone()[0],previous)

    def test_stale_hash_bad_index_and_invalid_edits_cannot_save(self):
        self.generate()
        with self.assertRaises(ValueError):
            d.accept(self.store,self.sid,self.did,0,'wrong',content='內容',kind='fact')
        for kwargs in ({'content':''},{'content':'x'*4001},{'kind':'execute'},{'expires_on':'tomorrow'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.accept(**kwargs)
        self.assertEqual(self.store.memories(),[])

    def test_backup_restore_tamper_and_legacy_missing_tables(self):
        self.generate();receipt=self.accept();before=d.get(self.store,self.sid,self.did)
        backup=create_backup(self.store.path);restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(d.get(restored,self.sid,self.did),before)
        self.assertEqual(d.provenance(restored,receipt['memory_id']),d.provenance(self.store,receipt['memory_id']))
        with restored.connect() as db:db.execute("UPDATE memory_drafts SET proposals_json='{}'")
        with self.assertRaises(ValueError):validate_database(restored.path)
        with restored.connect() as db:
            db.execute('DROP TABLE memory_acceptances');db.execute('DROP TABLE memory_drafts')
        validate_database(restored.path);restored=Store(restored.path)
        self.assertEqual(d.drafts(restored,self.sid),[])

    def test_accepted_memory_retrieved_in_new_conversation_but_drafts_are_not(self):
        self.generate();sid=self.store.new_session()
        before=Assistant(self.store,None).prepare_context(sid,'星橋專案程式放哪裡？')
        self.assertEqual(before['memories'],[])
        receipt=self.accept()
        after=Assistant(self.store,None).prepare_context(sid,'星橋專案程式放哪裡？')
        self.assertIn(receipt['memory_id'],[m['id'] for m in after['memories']])
        self.assertNotIn('context_sha256',json.dumps(after))
