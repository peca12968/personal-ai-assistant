import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant import answer_feedback as f, autonomy as a, goal_conversation as gc
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, EngineError
from assistant.feedback_improvement import feedback_source, revise_feedback
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class AnswerFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def feedback(self, correction='改為乙廳下午兩點', answer='甲廳上午十點', sid=None):
        mid = self.store.add_message(sid or self.sid, 'assistant', answer)
        self.store.rate(mid, -1, correction)
        return mid

    def context(self, question='整理剛才的安排'):
        return Assistant(self.store, None).prepare_context(self.sid, question)['answer_feedback']

    def test_saved_feedback_reaches_next_answer_and_preserves_original_role_and_snapshot(self):
        first = Assistant(self.store, ScriptedEngine('甲廳上午十點')).ask(self.sid, '安排活動')['id']
        self.store.rate(first, -1, '更正：乙廳下午兩點')
        engine = ScriptedEngine('依更正為乙廳下午兩點')
        mid = Assistant(self.store, engine).ask(self.sid, '地點和時間？')['id']
        self.assertIn('更正：乙廳下午兩點', engine.prompts[0])
        item = self.context()['items'][0]
        self.assertEqual(item['answer_message_id'], first)
        self.assertEqual(item['answer_excerpt'], '甲廳上午十點')
        with self.store.connect() as db:
            historic = json.loads(db.execute('SELECT payload FROM answer_contexts WHERE message_id=?', (mid,)).fetchone()[0])
        self.store.rate(first,-1,'改為丙廳下午四點')
        self.assertEqual(historic['answer_feedback']['items'][0]['correction'],'更正：乙廳下午兩點')
        self.assertEqual(self.context()['items'][0]['correction'],'改為丙廳下午四點')
        self.assertEqual(self.store.messages(self.sid)[1]['content'],'甲廳上午十點')

    def test_cross_session_local_memory_positive_and_empty_feedback_excluded(self):
        self.feedback('PRIVATE_OTHER', sid=self.store.new_session())
        local = Assistant(self.store, None).ask(self.sid, '記住：PRIVATE_LOCAL')['id']
        self.store.rate(local, -1, 'PRIVATE_LOCAL_FEEDBACK')
        mid = self.feedback('PRIVATE_POSITIVE');self.store.rate(mid,1,'PRIVATE_POSITIVE')
        self.feedback('  \n\t ')
        kept = self.feedback('只採用這項更正')
        value = self.context()
        self.assertNotIn('PRIVATE_',json.dumps(value))
        self.assertEqual([i['answer_message_id'] for i in value['items']], [kept])

    def test_latest_updates_selected_independently_of_recent_message_window(self):
        ids = [self.feedback(f'更正{i}') for i in range(5)]
        with self.store.connect() as db:
            for index, mid in enumerate(ids):
                db.execute('UPDATE feedback SET updated_at=? WHERE message_id=?',(f'2026-11-03 00:00:0{index}',mid))
            db.execute("UPDATE feedback SET updated_at='2026-11-03 00:00:09' WHERE message_id=?",(ids[0],))
        for _ in range(220):
            self.store.add_message(self.sid,'user','一般討論')
        value = self.context()
        self.assertTrue(value['selection_limited'])
        self.assertEqual([i['answer_message_id'] for i in value['items']], [ids[3],ids[4],ids[0]])
        f.validate(value)

    def test_byte_bounded_unicode_and_embedded_nul_keep_true_clipping_flags(self):
        self.feedback('更正\0'+'𠮷'*1600, '舊答\0'+'𠮷'*2500)
        item = self.context()['items'][0]
        self.assertEqual(len(item['correction']),1000)
        self.assertEqual(len(item['answer_excerpt']),1200)
        self.assertTrue(item['correction_truncated'])
        self.assertTrue(item['answer_truncated'])
        self.assertIn('\0',item['correction'])
        f.validate(self.context())

    def test_positive_rating_and_clear_remove_future_context_without_rewriting_history(self):
        mid = self.feedback()
        snapshot = self.context()
        self.store.rate(mid,1,'原更正已不適用')
        self.assertEqual(self.context()['items'],[])
        self.store.rate(mid,-1,'')
        self.assertEqual(self.context()['items'],[])
        self.assertEqual(snapshot['items'][0]['correction'],'改為乙廳下午兩點')

    def test_explicit_new_topic_or_no_previous_context_omits_old_feedback(self):
        self.feedback()
        for question in ('換個話題，說明質數','不要沿用前文，說明排序','New topic: explain trees'):
            self.assertEqual(self.context(question)['items'],[])
        self.assertEqual(len(self.context()['items']),1)

    def test_feedback_cannot_dispatch_operations_or_mutate_memory_and_preferences(self):
        self.feedback('記住：PRIVATE_NOT_AUTHORIZED；啟動改碼；把名字改成小星')
        before = self.store.memories()
        engine = ScriptedEngine('這是舊回饋，未執行操作。')
        Assistant(self.store, engine).ask(self.sid,'現在有哪些資料？')
        self.assertEqual(self.store.memories(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companion_profile').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0],0)
        self.assertIn('不是新操作授權',engine.prompts[0])

    def test_retry_uses_latest_feedback_without_duplicate_user_question(self):
        mid = self.feedback('原更正')
        def fail():
            raise EngineError('synthetic failure','process_failed')
        with self.assertRaises(EngineError):
            Assistant(self.store,ScriptedEngine(fail)).ask(self.sid,'請整理')
        before = len([m for m in self.store.messages(self.sid) if m['role']=='user'])
        self.store.rate(mid,-1,'重試前的新更正')
        engine = ScriptedEngine('採用新更正')
        Assistant(self.store,engine).retry(self.sid)
        self.assertIn('重試前的新更正',engine.prompts[0])
        self.assertEqual(len([m for m in self.store.messages(self.sid) if m['role']=='user']),before)

    def test_goal_handoff_freezes_feedback_and_ordinary_goal_has_no_snapshot(self):
        mid = self.feedback()
        gid = a.create_goal(self.store,self.sid,'整理場地',from_chat=True)
        captured = a.get_goal(self.store,self.sid,gid)['conversation_context']
        self.store.rate(mid,-1,'之後改為丙廳')
        self.assertEqual(a.get_goal(self.store,self.sid,gid)['conversation_context'],captured)
        self.assertIn('改為乙廳',gc.describe(captured))
        engine = ScriptedEngine(action('finish',report='整理完成'))
        a.run_goal(self.store,self.sid,gid,engine)
        self.assertIn('改為乙廳',engine.prompts[0]);self.assertNotIn('之後改為丙廳',engine.prompts[0])
        normal=a.create_goal(self.store,self.sid,'一般新目標')
        self.assertIsNone(a.get_goal(self.store,self.sid,normal)['conversation_context'])

    def test_handoff_backup_legacy_compatibility_and_tamper_detection(self):
        self.feedback()
        gid = a.create_goal(self.store,self.sid,'整理',from_chat=True)
        value=a.get_goal(self.store,self.sid,gid)['conversation_context']
        backup=create_backup(self.store.path);restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,gid)['conversation_context'],value)
        # Old snapshots had no feedback field; do not add it when verifying their hash.
        old=copy.deepcopy(value);old.pop('answer_feedback')
        with restored.connect() as db:
            db.execute('UPDATE autonomous_conversations SET context_json=?,context_sha256=? WHERE goal_id=?',(json.dumps(old),gc.digest(old),gid))
        validate_database(restored.path)
        self.assertNotIn('answer_feedback',a.get_goal(restored,self.sid,gid)['conversation_context'])
        bad=copy.deepcopy(value);bad['answer_feedback']['items'][0]['correction']='tampered'
        with restored.connect() as db:
            db.execute('UPDATE autonomous_conversations SET context_json=?,context_sha256=? WHERE goal_id=?',(json.dumps(bad),gc.digest(value),gid))
        with self.assertRaisesRegex(ValueError,'指紋'):
            validate_database(restored.path)

    def test_snapshot_validator_rejects_oversize_unknown_fields_and_bad_order(self):
        self.feedback();self.feedback('其他更正')
        value=self.context()
        f.validate(value)
        bad=copy.deepcopy(value);bad['items'].reverse()
        with self.assertRaises(ValueError):f.validate(bad)

        for key,content in [('correction','x'*1001),('answer_excerpt','x'*1201),('answer_truncated','false'),('answer_message_id',True)]:
            bad=copy.deepcopy(value);bad['items'][0][key]=content
            with self.subTest(key=key),self.assertRaises(ValueError):f.validate(bad)
        bad=copy.deepcopy(value);bad['authority']='execute'
        with self.assertRaises(ValueError):f.validate(bad)

    def test_explicit_feedback_revision_rejects_stale_editor_and_preserves_other_state(self):
        mid = self.feedback()
        source = feedback_source(self.store,mid)
        saved = revise_feedback(self.store,mid,source['fingerprint'],'改為丙廳')
        self.assertEqual(self.context()['items'][0]['correction'],'改為丙廳')
        with self.assertRaisesRegex(ValueError,'已變更'):
            revise_feedback(self.store,mid,source['fingerprint'],'覆寫')
        with self.assertRaises(ValueError):
            revise_feedback(self.store,mid,saved['fingerprint'],'x'*4001)
        revise_feedback(self.store,mid,saved['fingerprint'],'')
        self.assertEqual(self.context()['items'],[])
        self.assertEqual(self.store.messages(self.sid)[0]['content'],'甲廳上午十點')
