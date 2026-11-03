import gc
import json
import tempfile
import threading
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from assistant import chat_drafts as drafts
from assistant.engine import Assistant, EngineError
from assistant.gui import App
from assistant.store import Store
from assistant.backup import create_backup, restore_backup, validate_database


class Engine:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def answer(self,prompt,cancel=None):
        self.calls.append(prompt)
        if self.fail: raise EngineError('合成失敗','process_failed')
        return '合成回答'


class ChatDraftTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root_path=Path(tmp.name);self.store=Store(self.root_path/'assistant.sqlite3')
        self.sid=self.store.new_session()

    def draft(self,text='  請回答測試\n原始空白  '):
        return drafts.submission(drafts.save(self.store,self.sid,text,drafts.get(self.store,self.sid)['revision']))

    def app(self,engine=None):
        root=tk.Tk();root.withdraw()
        def dispose():
            try:
                for timer in root.tk.call('after','info'):root.tk.call('after','cancel',timer)
                root.destroy()
            except tk.TclError:pass
            # Dispose Tk cycles on their owning thread, before subsequent tests
            # create background workers that can trigger Python's collector.
            gc.collect()
        self.addCleanup(dispose)
        return App(root,self.store,engine or Engine())

    def terminal(self,app):
        value=app.events.get(timeout=5);app.events.put(value);app.poll()

    def test_per_session_switch_restart_raw_unicode_and_no_model_context(self):
        app=self.app();sid=app.sid
        raw='  草稿秘密🦉\n第二行\0尾端  '
        app.input.insert('1.0',raw);app.new_session();other=app.sid
        self.assertEqual(app.input.get('1.0','end-1c'),'')
        app.input.insert('1.0','另一段');self.assertTrue(app.drafts.switch(sid))
        self.assertEqual(app.drafts.raw(),raw)
        self.assertEqual(drafts.get(self.store,other)['content'],'另一段')
        with patch.object(app.root,'destroy'):app.close()
        reopened=self.app();reopened.drafts.switch(sid)
        self.assertEqual(reopened.drafts.raw(),raw)
        self.assertEqual(self.store.messages(sid),[])
        context=Assistant(self.store,None).prepare_context(sid,'請回答普通問題')
        self.assertNotIn('草稿秘密',json.dumps(context,ensure_ascii=False))
        path=self.root_path/'chat.json';self.store.export_session(sid,path)
        self.assertNotIn('草稿秘密',path.read_text(encoding='utf-8'))

    def test_autosave_tick_and_normal_close_flush_without_model_call(self):
        app=self.app();app.input.insert('1.0','定期保存內容')
        app.drafts.stop_timer();app.drafts.tick()
        self.assertEqual(drafts.get(self.store,app.sid)['content'],'定期保存內容')
        app.input.insert('end','及最後一字')
        with patch.object(app.root,'destroy') as destroy:
            app.close();destroy.assert_called_once()
        self.assertEqual(drafts.get(self.store,app.sid)['content'],'定期保存內容及最後一字')
        self.assertEqual(app.assistant.engine.calls,[])

    def test_destroyed_app_does_not_wait_for_worker_cyclic_collection(self):
        app=self.app();ref=weakref.ref(app)
        for timer in app.root.tk.call('after','info'):app.root.tk.call('after','cancel',timer)
        app.root.destroy();del app
        self.assertIsNone(ref())

    def test_concurrent_edit_stale_review_and_clear_aba_do_not_overwrite(self):
        first=self.app();second=self.app()
        first.input.insert('1.0','甲');self.assertTrue(first.drafts.flush())
        second.input.insert('1.0','乙');self.assertFalse(second.drafts.flush())
        self.assertEqual(second.drafts.raw(),'乙')
        observed=drafts.get(self.store,self.sid)
        first.input.insert('end','新');self.assertTrue(first.drafts.flush())
        with self.assertRaises(ValueError):second.drafts.resolve(observed,'乙','local')
        self.assertEqual(drafts.get(self.store,self.sid)['content'],'甲新')
        observed=drafts.get(self.store,self.sid)
        second.drafts.resolve(observed,'乙','local')
        self.assertEqual(drafts.get(self.store,self.sid)['content'],'乙')
        old=drafts.get(self.store,self.sid)
        cleared=drafts.save(self.store,self.sid,'',old['revision'])
        latest=drafts.save(self.store,self.sid,'乙',cleared['revision'])
        with self.assertRaises(ValueError):drafts.save(self.store,self.sid,'舊覆寫',old['revision'])
        self.assertGreater(latest['revision'],old['revision'])

    def test_send_consumes_atomically_and_failure_uses_retry_not_draft(self):
        token=self.draft();engine=Engine(fail=True);assistant=Assistant(self.store,engine)
        with self.assertRaises(EngineError):assistant.ask(self.sid,token['content'],draft=token)
        self.assertTrue(drafts.submitted(self.store,self.sid,token))
        self.assertEqual(drafts.get(self.store,self.sid)['content'],'')
        self.assertEqual(len(self.store.messages(self.sid)),1)
        with self.assertRaises(ValueError):assistant.ask(self.sid,token['content'],draft=token)
        self.assertEqual(len(engine.calls),1)
        assistant.engine=Engine();assistant.retry(self.sid)
        self.assertEqual(len(self.store.messages(self.sid)),2)

    def test_insertion_failure_rolls_back_clear_receipt_and_question(self):
        token=self.draft();engine=Engine()
        consume=drafts.consume
        def fail_after_consume(db,*args):
            consume(db,*args)
            self.assertEqual(db.execute('SELECT content FROM chat_drafts').fetchone()[0],'')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chat_draft_submissions').fetchone()[0],1)
            raise RuntimeError('inject after clear and receipt, before commit')
        with patch('assistant.chat_drafts.consume',side_effect=fail_after_consume):
            with self.assertRaises(RuntimeError):Assistant(self.store,engine).ask(self.sid,token['content'],draft=token)
        self.assertEqual(drafts.get(self.store,self.sid)['content'],token['content'])
        self.assertFalse(drafts.submitted(self.store,self.sid,token))
        self.assertEqual(self.store.messages(self.sid),[])
        self.assertEqual(engine.calls,[])

    def test_save_error_is_recoverable_and_newer_unchanged_editor_is_detected(self):
        app=self.app();app.input.insert('1.0','x'*64001)
        self.assertFalse(app.drafts.flush())
        self.assertEqual(drafts.get(self.store,app.sid)['content'],'')
        app.input.delete('1.0','end');app.input.insert('1.0','修正後')
        self.assertTrue(app.drafts.flush())
        observed=drafts.get(self.store,app.sid)
        drafts.save(self.store,app.sid,'另一視窗',observed['revision'])
        self.assertFalse(app.drafts.flush())
        self.assertEqual(app.drafts.raw(),'修正後')
        with patch.object(app.root,'destroy') as destroy:app.close();destroy.assert_not_called()
        app.drafts.resolve(drafts.get(self.store,app.sid),'修正後','saved')
        self.assertEqual(app.drafts.raw(),'另一視窗')

    def test_local_command_consumption_and_cancellation_are_atomic(self):
        token=self.draft('記住：本機測試偏好')
        result=Assistant(self.store,None).ask(self.sid,token['content'],draft=token)
        self.assertEqual(result['response_mode'],'local_memory')
        self.assertTrue(drafts.submitted(self.store,self.sid,token))
        other=self.draft('記住：不應保存')
        cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError):Assistant(self.store,None).ask(self.sid,other['content'],cancel,draft=other)
        self.assertFalse(drafts.submitted(self.store,self.sid,other))
        self.assertEqual(drafts.get(self.store,self.sid)['content'],other['content'])
        self.assertEqual(len(self.store.memories()),1)
        cancel.clear()
        from assistant import memory_commands
        operate=memory_commands.operate
        def cancel_after_operation(db,command):
            value=operate(db,command);cancel.set();return value
        with patch('assistant.memory_commands.operate',side_effect=cancel_after_operation):
            with self.assertRaises(RuntimeError):Assistant(self.store,None).ask(self.sid,other['content'],cancel,draft=other)
        self.assertFalse(drafts.submitted(self.store,self.sid,other))
        self.assertEqual(drafts.get(self.store,self.sid)['content'],other['content'])
        self.assertEqual(len(self.store.memories()),1)

    def test_gui_start_failure_keeps_raw_and_before_commit_failure_keeps_two_inputs(self):
        app=self.app();raw='  原問題\n保留空白  ';app.input.insert('1.0',raw)
        with patch('threading.Thread.start',side_effect=RuntimeError('start failed')):app.send()
        app.poll();self.assertEqual(app.drafts.raw(),raw)
        self.assertEqual(drafts.get(self.store,app.sid)['content'],raw)
        entered=threading.Event();release=threading.Event()
        def fail(*args,**kwargs):
            entered.set();release.wait(5);raise ValueError('pre-commit failure')
        with patch.object(app.assistant,'ask',side_effect=fail):
            app.send();self.assertTrue(entered.wait(5));app.input.insert('1.0','下一段草稿')
            release.set();self.terminal(app)
        self.assertTrue(app.drafts.conflict)
        self.assertEqual(app.drafts.raw(),'下一段草稿')
        self.assertEqual(app.drafts.failed_text,raw)
        self.assertEqual(drafts.get(self.store,app.sid)['content'],raw)
        with patch.object(app.root,'destroy') as destroy:app.close();destroy.assert_not_called()
        app.drafts.resolve(drafts.get(self.store,app.sid),'下一段草稿','failed')
        self.assertEqual(app.drafts.raw(),raw)

    def test_gui_next_input_can_save_while_answer_is_running(self):
        entered=threading.Event();release=threading.Event()
        class WaitEngine:
            def answer(self,prompt,cancel=None):
                entered.set();release.wait(5);return '已回答'
        app=self.app(WaitEngine());app.input.insert('1.0','送出這題');app.send()
        self.assertTrue(entered.wait(5));app.input.insert('1.0','尚未送出的下一題')
        self.assertTrue(app.drafts.flush())
        self.assertEqual(drafts.get(self.store,app.sid)['content'],'尚未送出的下一題')
        release.set();self.terminal(app)
        self.assertEqual(app.drafts.raw(),'尚未送出的下一題')
        self.assertEqual(len(self.store.messages(app.sid)),2)

    def test_backup_legacy_bounds_and_invalid_receipts(self):
        token=self.draft();Assistant(self.store,Engine()).ask(self.sid,token['content'],draft=token)
        drafts.save(self.store,self.sid,'🦉'*64000,drafts.get(self.store,self.sid)['revision'])
        with self.assertRaises(ValueError):drafts.save(self.store,self.sid,'x'*64001,drafts.get(self.store,self.sid)['revision'])
        backup=create_backup(self.store.path,self.root_path/'draft.paibackup')
        restore_backup(backup['path'],self.root_path/'restore')
        restored=Store(self.root_path/'restore'/'assistant.sqlite3')
        self.assertEqual(drafts.get(restored,self.sid)['content'],'🦉'*64000)
        self.assertTrue(drafts.submitted(restored,self.sid,token))
        with restored.connect() as db:db.execute('UPDATE chat_draft_submissions SET revision=999')
        with self.assertRaises(ValueError):validate_database(restored.path)
        with restored.connect() as db:
            db.execute('DROP TABLE chat_draft_submissions');db.execute('DROP TABLE chat_drafts')
        validate_database(restored.path)
        restored=Store(restored.path);self.assertEqual(drafts.get(restored,self.sid),{'revision':0,'content':''})


if __name__=='__main__':unittest.main()
