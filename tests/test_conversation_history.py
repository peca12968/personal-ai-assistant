import copy
from contextlib import closing
import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, conversation_history as h, work_plan, goal_review
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.store=Store(self.root/'state.sqlite3');self.sid=self.store.new_session()

    def add(self,text,role='user'):
        return self.store.add_message(self.sid,role,text)

    def fill(self,count,text='一般討論'):
        with self.store.connect() as db:
            db.executemany('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                           [(self.sid,'user',text+str(i)) for i in range(count)])

    def goal(self,**extra):
        return a.create_goal(self.store,self.sid,'查找舊條件',from_chat=True,history_search=True,**extra)

    def search(self,gid,query='橡樹',offset=0,cancel=None):
        return h.search(self.store,self.sid,gid,{'query':query,'offset':offset},cancel)

    def test_scope_is_explicit_strict_and_not_inherited_by_a_new_stage(self):
        self.add('橡樹條件')
        plain=a.create_goal(self.store,self.sid,'普通目標',from_chat=True)
        self.assertIsNone(a.get_goal(self.store,self.sid,plain)['conversation_history'])
        with self.assertRaises(ValueError):self.search(plain)
        for flag in (1,'true',None):
            with self.subTest(flag=flag),self.assertRaises(ValueError):
                a.create_goal(self.store,self.sid,'無效',from_chat=True,history_search=flag)
        with self.assertRaises(ValueError):a.create_goal(self.store,self.sid,'無帶入',history_search=True)
        gid=self.goal()
        a.run_goal(self.store,self.sid,gid,ScriptedEngine(action('ask',question='測試暫停')),max_steps=1)
        child=a.create_goal(self.store,self.sid,'下一階段',parent_id=gid,from_chat=True)
        self.assertIsNone(a.get_goal(self.store,self.sid,child)['conversation_history'])
        with self.assertRaises(ValueError):self.search(child)

    def test_frozen_middle_text_excludes_local_other_sessions_and_future_changes(self):
        self.add('開場問候');self.fill(30)
        mid=self.add('橡樹試算7份，每份145元，運費35元。')
        self.add('橡樹建議20份，但使用者尚未選定。','assistant')
        Assistant(self.store,None).ask(self.sid,'記住：橡樹本機命令秘密')
        other=self.store.new_session();self.store.add_message(other,'user','橡樹另一對話秘密')
        self.fill(30);gid=self.goal()
        initial=a.get_goal(self.store,self.sid,gid)
        self.assertNotIn('7份',json.dumps(initial,ensure_ascii=False))
        original=self.search(gid)
        self.assertEqual(original['total_matches'],2)
        self.assertEqual([row['role'] for row in original['conversation_matches']],['assistant','user'])
        self.assertEqual(original['conversation_matches'][1]['message_id'],mid)
        self.assertNotIn('秘密',json.dumps(original,ensure_ascii=False))
        self.assertNotIn('excerpts',original)
        self.add('橡樹後來改成99份。')
        with self.store.connect() as db:db.execute('UPDATE messages SET content=? WHERE id=?',('原訊息已改寫',mid))
        self.assertEqual(self.search(gid),original)
        with self.assertRaises(ValueError):h.search(self.store,other,gid,{'query':'橡樹','offset':0})

    def test_literal_normalized_search_and_stable_pages(self):
        ids=[self.add(f'橡樹 {i}') for i in range(14)]
        self.add('ＡＢＣ Straße 100%_literal\x00尾端')
        gid=self.goal();pages=[self.search(gid,offset=n) for n in (0,6,12)]
        found=[row['message_id'] for page in pages for row in page['conversation_matches']]
        self.assertEqual(found,list(reversed(ids)))
        self.assertEqual([p['next_offset'] for p in pages],[6,12,None])
        self.assertTrue(all(p['total_matches']==14 for p in pages))
        self.assertEqual(self.search(gid,'abc strasse')['total_matches'],1)
        self.assertEqual(self.search(gid,'%_')['total_matches'],1)
        self.assertEqual(self.search(gid,'不存在%_')['total_matches'],0)
        self.assertIn('\x00尾端',self.search(gid,'尾端')['conversation_matches'][0]['content'])
        tail=self.search(gid,offset=99)
        self.assertEqual(tail['conversation_matches'],[]);self.assertIsNone(tail['next_offset'])

    def test_capture_message_character_and_unicode_limits_are_explicit(self):
        self.add('橡樹超出保存範圍');self.fill(1001)
        gid=self.goal();result=self.search(gid)
        self.assertEqual(result['total_matches'],0)
        self.assertEqual(result['history']['captured_message_count'],1000)
        self.assertTrue(result['history']['messages_limited'])
        self.assertEqual(result['history']['source_message_count'],1002)
        sid=self.store.new_session();self.sid=sid
        raw='橡樹\x00'+'🙂'*2100
        self.fill(510,raw);gid=self.goal();result=self.search(gid)
        self.assertEqual(result['history']['captured_characters'],1000000)
        self.assertEqual(result['history']['captured_message_count'],500)
        self.assertTrue(result['history']['content_truncated'])
        self.assertTrue(result['history']['messages_limited'])
        self.assertEqual(result['conversation_matches'][0]['content'],raw[:2000])
        self.assertTrue(result['conversation_matches'][0]['content_truncated'])

    def test_parameters_cancellation_and_corrupted_snapshots_are_rejected(self):
        self.add('橡樹條件');gid=self.goal()
        for args in ({'query':'','offset':0},{'query':'x','offset':True},{'query':'x','offset':-1},
                     {'query':'x','offset':1001},{'query':'x'*201,'offset':0},
                     {'query':'橡樹','offset':0,'goal_id':gid}):
            with self.subTest(args=args),self.assertRaises(ValueError):h.validate_args(args)
        stop=threading.Event();stop.set()
        with self.assertRaises(EngineError):self.search(gid,cancel=stop)
        with self.store.connect() as db:value=h.read(db,self.sid,gid)
        for change in ({'session_id':self.sid+'other'},{'messages_limited':True},{'source_message_count':True}):
            candidate=copy.deepcopy(value);candidate.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):h.validate(candidate,self.sid)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_conversation_history SET context_json=? WHERE goal_id=?',('{}',gid))
        with self.assertRaises(ValueError):self.search(gid)
        with self.assertRaises(ValueError):validate_database(self.store.path)

    def test_goal_tool_uses_archive_without_document_citation_or_raw_stage_copy(self):
        self.add('開場');self.fill(20);self.add('橡樹7份，每份145元，運費35元。');self.fill(20)
        gid=self.goal(file_delivery=True)
        engine=ScriptedEngine(action('search_conversation',query='橡樹',offset=0),
            action('write_artifact',filename='test.txt',content='數據[D1:L1]'),action('ask',question='測試暫停'))
        result=a.run_goal(self.store,self.sid,gid,engine,max_steps=3)
        self.assertNotIn('7份',engine.prompts[0]);self.assertIn('7份',engine.prompts[1])
        self.assertEqual(result['steps'][0]['observation']['total_matches'],1)
        self.assertEqual(result['steps'][1]['action']['tool'],'invalid')
        child=a.create_goal(self.store,self.sid,'下一階段',parent_id=gid)
        stage=a.get_goal(self.store,self.sid,child)['previous_stage']
        self.assertNotIn('7份',json.dumps(stage,ensure_ascii=False))

    def test_backup_roundtrip_and_legacy_upgrade_do_not_grant_search(self):
        self.add('橡樹45天');gid=self.goal();original=self.search(gid)
        archive=create_backup(self.store.path,self.root/'new.paibackup')
        restore_backup(archive['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(h.search(restored,self.sid,gid,{'query':'橡樹','offset':0}),original)
        with self.store.connect() as db:db.execute('DROP TABLE autonomous_conversation_history')
        legacy=create_backup(self.store.path,self.root/'old.paibackup')
        restore_backup(legacy['path'],self.root/'legacy')
        upgraded=Store(self.root/'legacy'/'assistant.sqlite3')
        self.assertIsNone(a.get_goal(upgraded,self.sid,gid)['conversation_history'])

    def test_empty_archive_and_utf16_prefixes(self):
        gid=self.goal();self.assertEqual(self.search(gid)['history']['source_message_count'],0)
        path=self.root/'utf16.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA encoding='UTF-16le'")
            db.execute('CREATE TABLE encoding_seed(x)')
            db.execute('DROP TABLE encoding_seed');db.commit()
        store=Store(path);sid=store.new_session()
        with store.connect() as db:self.assertEqual(db.execute('PRAGMA encoding').fetchone()[0],'UTF-16le')
        raw='橡樹\x00🙂'+'甲'*2000
        store.add_message(sid,'user',raw)
        gid=a.create_goal(store,sid,'查找',from_chat=True,history_search=True)
        result=h.search(store,sid,gid,{'query':'橡樹','offset':0})
        self.assertEqual(result['conversation_matches'][0]['content'],raw[:2000])
        self.assertTrue(result['conversation_matches'][0]['content_truncated'])

    def test_archive_hash_is_part_of_plan_and_review_without_changing_legacy_hashes(self):
        self.add('橡樹條件');gid=self.goal()
        goal=a.get_goal(self.store,self.sid,gid)
        without=copy.deepcopy(goal);without['conversation_history']=None
        self.assertNotEqual(work_plan.conditions(goal),work_plan.conditions(without))
        legacy=copy.deepcopy(without);legacy.pop('conversation_history')
        self.assertEqual(work_plan.conditions(without),work_plan.conditions(legacy))
        before=next(r for r in goal_review.snapshot(self.store)['items'] if r['goal_id']==gid)['fingerprint']
        with self.store.connect() as db:
            value=h.read(db,self.sid,gid);value['messages'][0]['content']='橡樹另一條件'
            db.execute('UPDATE autonomous_conversation_history SET context_json=?,context_sha256=? WHERE goal_id=?',
                       (h.encoded(value).decode(),h.digest(value),gid))
        after=next(r for r in goal_review.snapshot(self.store)['items'] if r['goal_id']==gid)['fingerprint']
        self.assertNotEqual(before,after)

    def test_capture_failure_rolls_back_the_new_goal_and_duplicate_ids_are_invalid(self):
        self.add('橡樹條件')
        with patch.object(h,'capture',side_effect=ValueError('capture failure')),self.assertRaises(ValueError):self.goal()
        self.assertEqual(a.goals(self.store,self.sid),[])
        gid=self.goal()
        with self.store.connect() as db:value=h.read(db,self.sid,gid)
        value['messages']*=2;value['source_message_count']=2
        with self.assertRaises(ValueError):h.validate(value,self.sid)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_conversation_history SET context_json=? WHERE goal_id=?',('x'*(h.MAX_BYTES+1),gid))
        with self.assertRaises(ValueError):self.search(gid)

    def test_cli_requires_both_new_goal_and_from_chat(self):
        from assistant.__main__ import main
        for options in (['--history-search'],['--history-search','--new-goal','test']):
            with self.subTest(options=options),patch('sys.argv',['assistant',*options]),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code,2)

    def test_cli_creates_an_opted_in_pending_goal_without_model_calls(self):
        folder=self.root/'cli';store=Store(folder/'assistant.sqlite3');sid=store.new_session()
        store.add_message(sid,'user','橡樹條件')
        command=[sys.executable,'-m','assistant','--data-dir',str(folder),'--session',sid,
                 '--new-goal','查找舊條件','--from-chat','--history-search']
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        goal=json.loads(result.stdout)
        self.assertEqual(goal['status'],'pending');self.assertEqual(goal['steps'],[])
        self.assertEqual(goal['conversation_history']['captured_message_count'],1)

    def test_large_utf16_archive_checks_utf8_serialized_limit_without_false_rejection(self):
        path=self.root/'large-utf16.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA encoding='UTF-16le'")
            db.execute('CREATE TABLE encoding_seed(x)');db.execute('DROP TABLE encoding_seed');db.commit()
        store=Store(path);sid=store.new_session()
        with store.connect() as db:
            db.executemany('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                           [(sid,'user','x'+'\0'*1999)]*500)
        gid=a.create_goal(store,sid,'查找',from_chat=True,history_search=True)
        result=h.search(store,sid,gid,{'query':'x','offset':0})
        self.assertEqual(result['history']['captured_characters'],1000000)
        self.assertEqual(result['total_matches'],500)
        self.assertFalse(result['history']['messages_limited'])


if __name__=='__main__':unittest.main()
