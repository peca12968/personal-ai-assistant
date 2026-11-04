import gc
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant import document_imports as imports
from assistant.document_import_ui import DocumentImportWindow
from assistant.document_ui import DocumentWindow
from assistant.search_ui import SearchWindow
from assistant.folder_search import search_folder
from assistant.gui import App
from assistant.store import Store
from scripts.pdf_fixture import example


class DocumentImportTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.store=Store(self.path/'state.sqlite3');self.sid=self.store.new_session()
        self.root=tk.Tk();self.root.withdraw();self.threads=[];self.releases=[];self.addCleanup(self.dispose)

    def dispose(self):
        for release in self.releases:release.set()
        for thread in self.threads:thread.join(5);self.assertFalse(thread.is_alive())
        try:
            for timer in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',timer)
            self.root.destroy()
        except tk.TclError:pass
        gc.collect()

    def launch(self,work,failed,**kwargs):
        thread=threading.Thread(target=work,**kwargs);self.threads.append(thread);thread.start();return True

    def join(self,view):
        for thread in self.threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertTrue(view.active());view.poll();self.assertFalse(view.active())

    def file(self,name,content='synthetic text'):
        path=self.path/name
        path.write_bytes(content if isinstance(content,bytes) else content.encode('utf-8'));return path

    def gate(self,operation):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        def gated(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise RuntimeError('test gate timed out')
            return operation(*args,**kwargs)
        return gated,entered,release

    def test_background_pdf_import_keeps_tk_responsive_and_owns_terminal_result(self):
        source=self.file('source.pdf',example());view=DocumentWindow(self.root,self.store,self.sid);view.window.withdraw()
        operation,entered,release=self.gate(self.store.attach_document)
        with patch('assistant.document_import_ui.start_worker',self.launch),patch.object(self.store,'attach_document',operation):
            progress=view.import_paths([source]);progress.window.withdraw()
            self.assertTrue(entered.wait(3));self.assertEqual(self.store.documents(self.sid),[])
            heartbeat=[];self.root.after(0,lambda:heartbeat.append(True));self.root.update()
            self.assertEqual(heartbeat,[True]);self.assertTrue(progress.active())
            self.assertIsNone(view.import_paths([source]))
            self.assertEqual(str(view.remove_btn['state']),'disabled')
            with patch.object(self.store,'detach_document') as detach,patch('assistant.document_ui.filedialog.askopenfilenames') as dialog:
                view.remove();view.add();view.reload();detach.assert_not_called();dialog.assert_not_called()
            release.set();self.join(progress)
        self.assertEqual(progress.result['results'][0]['status'],'imported')
        self.assertEqual(len(view.listing.get_children()),1)
        self.assertEqual(str(view.remove_btn['state']),'normal')
        self.assertEqual(len(self.store.documents(self.sid)),1)
        progress.close();self.assertTrue(progress.closed)
        self.assertNotIn(progress,self.root._assistant_data_windows)

    def test_partial_stop_preserves_committed_file_and_skips_remaining(self):
        paths=[self.file(name) for name in ('first.txt','second.txt','third.txt')]
        original=self.store.attach_document
        gated,entered,release=self.gate(original)
        def attach(sid,path,**kwargs):return original(sid,path,**kwargs) if path==paths[0] else gated(sid,path,**kwargs)
        with patch('assistant.document_import_ui.start_worker',self.launch),patch.object(self.store,'attach_document',attach):
            progress=DocumentImportWindow(self.root,self.store,self.sid,[{'path':p} for p in paths]);progress.window.withdraw()
            self.assertTrue(entered.wait(3));progress.stop();release.set();self.join(progress)
        self.assertEqual([r['status'] for r in progress.result['results']],['imported','cancelled','unprocessed'])
        self.assertEqual([d['name'] for d in self.store.documents(self.sid)],['first.txt'])
        self.assertIn('已匯入1／3',progress.notice.get());self.assertIn('停止或尚未處理2',progress.notice.get())

    def test_cancel_before_commit_rolls_back_replacement(self):
        path=self.file('old.txt','old');self.store.attach_document(self.sid,path)
        old=self.store.documents(self.sid,include_content=True);path.write_text('new',encoding='utf-8')
        event=threading.Event();original=self.store.connect
        from contextlib import contextmanager
        @contextmanager
        def connection():
            with original() as db:
                db.set_trace_callback(lambda sql:event.set() if sql.lstrip().startswith('INSERT INTO documents') else None)
                yield db
        with patch.object(self.store,'connect',connection),self.assertRaisesRegex(ValueError,'未保存'):
            self.store.attach_document(self.sid,path,cancel=event)
        self.assertEqual(self.store.documents(self.sid,include_content=True),old)
        with patch('assistant.store.read_document') as read,self.assertRaises(ValueError):
            self.store.attach_document(self.sid,path,cancel=event)
        read.assert_not_called()

    def test_search_import_freezes_session_rows_and_revalidates_scope_hash(self):
        folder=self.path/'sources';folder.mkdir();source=folder/'source.txt';source.write_text('needle old',encoding='utf-8')
        report=search_folder(folder,'needle')
        view=SearchWindow(self.root,self.store,self.sid);view.window.withdraw()
        view.busy=True;view.events.put((True,report));view.poll();view.listing.selection_set('0')
        original=self.store.attach_document;gated,entered,release=self.gate(original)
        with patch('assistant.document_import_ui.start_worker',self.launch),patch.object(self.store,'attach_document',gated):
            progress=view.import_selected();progress.window.withdraw();self.assertTrue(entered.wait(3))
            other=self.store.new_session();view.sid=other
            source.write_text('needle changed',encoding='utf-8');view.report['results'][0]['sha256']='0'*64
            release.set();self.join(progress)
        self.assertEqual(progress.sid,self.sid);self.assertEqual(progress.result['results'][0]['status'],'failed')
        self.assertIn('變更',progress.result['results'][0]['error'])
        self.assertEqual(self.store.documents(self.sid),[]);self.assertEqual(self.store.documents(other),[])
        outside=self.file('outside.txt','needle')
        result=imports.execute(self.store,self.sid,imports.freeze([{'path':outside,'scope':folder}]),threading.Event(),lambda *args:None)
        self.assertEqual(result['results'][0]['status'],'failed');self.assertEqual(self.store.documents(self.sid),[])

    def test_root_shutdown_waits_until_worker_result_consumed(self):
        path=self.file('note.txt');app=App.__new__(App);app.root=self.root;app.busy=app.closing=False
        app.drafts=Mock();app.drafts.flush.return_value=True;app.reminder_timer=None;app.cancel=threading.Event()
        app.autonomous_windows=[];app.memory_draft_windows=[];app.status=tk.StringVar(self.root)
        gated,entered,release=self.gate(self.store.attach_document)
        with patch('assistant.document_import_ui.start_worker',self.launch),patch.object(self.store,'attach_document',gated),patch.object(self.root,'destroy') as destroy:
            progress=DocumentImportWindow(self.root,self.store,self.sid,[{'path':path}]);progress.window.withdraw()
            self.assertTrue(entered.wait(3));app.close();destroy.assert_not_called();self.assertTrue(progress.cancel.is_set())
            release.set()
            for thread in self.threads:thread.join(5);self.assertFalse(thread.is_alive())
            app.close();destroy.assert_not_called();self.assertTrue(progress.active())
            progress.poll();self.assertTrue(progress.closed);app.close();destroy.assert_called_once()
        self.assertEqual(self.store.documents(self.sid),[])

    def test_parent_close_cancels_live_import_without_destroying_worker_window(self):
        parent=tk.Toplevel(self.root);parent.withdraw();path=self.file('note.txt');callback=Mock()
        gated,entered,release=self.gate(self.store.attach_document)
        with patch('assistant.document_import_ui.start_worker',self.launch),patch.object(self.store,'attach_document',gated):
            progress=DocumentImportWindow(parent,self.store,self.sid,[{'path':path}],callback);progress.window.withdraw()
            self.assertTrue(entered.wait(3));parent.destroy()
            self.assertTrue(progress.cancel.is_set());self.assertTrue(progress.window.winfo_exists())
            release.set();self.join(progress)
        self.assertTrue(progress.closed);callback.assert_not_called();self.assertEqual(self.store.documents(self.sid),[])

    def test_startup_failure_bounds_and_shutdown_never_read_sources(self):
        path=self.file('note.txt');view=DocumentWindow(self.root,self.store,self.sid);view.window.withdraw()
        with patch('threading.Thread.start',side_effect=RuntimeError('start failed')),patch.object(self.store,'attach_document') as attach:
            progress=view.import_paths([path]);progress.window.withdraw();self.assertTrue(progress.active())
            self.assertIsNone(view.import_paths([path]));progress.poll();attach.assert_not_called()
        self.assertIn('未啟動',progress.notice.get());self.assertFalse(progress.active())
        with patch('assistant.document_import_ui.start_worker') as start:
            for items in ([],[{'path':path}]*9,[{'path':path,'extra':'bad'}]):
                with self.assertRaises(ValueError):DocumentImportWindow(self.root,self.store,self.sid,items)
            self.root._assistant_closing=True
            with self.assertRaises(ValueError):DocumentImportWindow(self.root,self.store,self.sid,[{'path':path}])
            start.assert_not_called()

    def test_stopping_active_pdf_worker_kills_child_without_storing_partial_file(self):
        import subprocess,sys
        path=self.file('slow.pdf',example());popen=subprocess.Popen;entered=threading.Event();children=[]
        def slow(args,**kwargs):
            if '--worker' not in args:
                return popen(args, **kwargs)
            child=popen([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
            children.append(child);entered.set();return child
        with patch('assistant.document_import_ui.start_worker',self.launch),patch('assistant.pdf_text.subprocess.Popen',slow):
            progress=DocumentImportWindow(self.root,self.store,self.sid,[{'path':path}]);progress.window.withdraw()
            self.assertTrue(entered.wait(3));progress.stop();self.join(progress)
        self.assertEqual(progress.result['results'][0]['status'],'cancelled')
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.store.documents(self.sid),[])

    def test_failed_file_does_not_hide_other_commits_and_selection_is_detached(self):
        paths=[self.file('first.txt'),self.file('bad.pdf',b'%PDF-bad'),self.file('last.txt')]
        items=[{'path':p} for p in paths];frozen=imports.freeze(items);items[0]['path']='different.txt';items.clear()
        events=[];result=imports.execute(self.store,self.sid,frozen,threading.Event(),lambda *event:events.append(event))
        self.assertEqual([row['status'] for row in result['results']],['imported','failed','imported'])
        self.assertEqual([row['name'] for row in self.store.documents(self.sid)],['first.txt','last.txt'])
        self.assertFalse(result['stop_requested']);self.assertEqual(len(events),6)


if __name__=='__main__':unittest.main()
