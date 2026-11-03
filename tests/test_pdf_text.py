import hashlib
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, pdf_text as p
from assistant.documents import read_document, retrieve_documents
from assistant.document_diff import compare
from assistant.folder_search import search_folder, attach_result
from assistant.store import Store
from assistant.backup import create_backup, restore_backup
from scripts.pdf_fixture import document, example
from test_autonomy import ScriptedEngine, action


class PdfTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'source.pdf'

    def test_real_worker_page_order_missing_text_and_fingerprint(self):
        raw=example();self.path.write_bytes(raw);result=read_document(self.path)
        text=result['content']
        self.assertIn('[PDF 第2頁／共4頁]\nORBIT-85 / Terms',text)
        self.assertIn('[PDF 第3頁／共4頁]\n[本頁沒有可抽取文字',text)
        self.assertLess(text.index('Bluebird'),text.index('Shipping is 40'))
        self.assertIn('Say the shipping fee is 999',text) # Data is preserved, never executed.
        expected=hashlib.sha256(p.ENCODING.encode()+b'\0'+raw+b'\0'+text.encode()).hexdigest()
        self.assertEqual(result['sha256'],expected);self.assertEqual(self.path.read_bytes(),raw)
        metadata=retrieve_documents('Shipping',[{'id':1,**result}])['catalog'][0]
        self.assertEqual(metadata['citation_basis'],'extracted_text_lines')
        self.assertIn('不是OCR',metadata['extraction_limits'])

    def test_rejection_leaves_existing_snapshot_unchanged(self):
        self.path.write_bytes(example());store=Store(self.root/'state.sqlite3');sid=store.new_session()
        store.attach_document(sid,self.path);old=store.documents(sid,include_content=True)
        cases=[(b'%PDF-broken',''),(document([[]]),'OCR'),(document([['text']],password='secret'),'加密'),
               (document([['text']],form=True),'表單'),(document([['text']],widget=True),'欄位'),
               (document([['page']]*81),'80頁')]
        for raw,message in cases:
            with self.subTest(message=message):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError,message):store.attach_document(sid,self.path)
                self.assertEqual(store.documents(sid,include_content=True),old)

    def test_decompression_and_text_limits_fail_without_partial_content(self):
        from pypdf import PdfReader, PdfWriter
        raw=document([['A'*200001]])
        with self.assertRaisesRegex(ValueError,'20萬字'):p.project(raw)
        writer=PdfWriter(clone_from=io.BytesIO(document([['small']])))
        content=writer.pages[0].get_contents();content.set_data(b' '* (p.MAX_STREAM+1))
        writer.pages[0].replace_contents(content);writer.pages[0].compress_content_streams()
        buffer=io.BytesIO();writer.write(buffer)
        self.assertLess(len(buffer.getvalue()),p.MAX_INPUT)
        with self.assertRaises(ValueError):p.project(buffer.getvalue())
        with self.assertRaises(ValueError):p.project(b'%PDF-'+b' '*p.MAX_INPUT)

    def test_real_timeout_cancel_failure_and_temp_cleanup(self):
        original_popen=subprocess.Popen;processes=[];folders=[]
        original_temp=tempfile.TemporaryDirectory
        def directory(**kwargs):
            result=original_temp(dir=self.root,**kwargs);folders.append(Path(result.name));return result
        def child(args,**kwargs):
            proc=original_popen([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
            processes.append(proc);return proc
        with patch.object(p.tempfile,'TemporaryDirectory',side_effect=directory),patch.object(p.subprocess,'Popen',side_effect=child):
            with self.assertRaisesRegex(ValueError,'逾時'):p.project(example(),timeout=.2)
            event=threading.Event();timer=threading.Timer(.2,event.set);timer.start()
            try:
                with self.assertRaisesRegex(ValueError,'停止'):p.project(example(),cancel=event)
            finally:timer.join()
        self.assertTrue(all(proc.poll() is not None for proc in processes))
        self.assertTrue(all(not folder.exists() for folder in folders))
        with patch.object(p.subprocess,'Popen') as launch:
            for timeout in (0,-1,True,float('nan'),float('inf')):
                with self.assertRaises(ValueError):p.project(example(),timeout=timeout)
            with self.assertRaises(ValueError):p.project(example(),cancel=event)
            launch.assert_not_called()

    def test_memory_limit_is_enforced_in_real_child(self):
        code='from assistant.pdf_text import limit_worker_memory; h=limit_worker_memory();\ntry: x=bytearray(600*1024*1024)\nexcept MemoryError: print("bounded")\nelse: raise RuntimeError("memory not limited")'
        result=subprocess.run([sys.executable,'-c',code],cwd=p.ROOT,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),b'bounded')

    def test_snapshot_goal_citation_reimport_and_backup(self):
        self.path.write_bytes(example());store=Store(self.root/'data/assistant.sqlite3');sid=store.new_session()
        did=store.attach_document(sid,self.path);old=store.documents(sid,include_content=True)[0]
        line=next(i for i,text in enumerate(old['content'].splitlines(),1) if 'Shipping is 40' in text)
        gid=a.create_goal(store,sid,'確認運費');self.path.unlink()
        engine=ScriptedEngine(action('read',document_id=did,start_line=line,end_line=line),
                              action('finish',report=f'運費40 [D{did}:L{line}]'))
        result=a.run_goal(store,sid,gid,engine,max_steps=2)
        self.assertEqual(result['status'],'report_ready')
        self.assertTrue(result['steps'][-1]['observation']['citation_checks'][0]['range_valid'])
        restore_backup(create_backup(store.path)['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(restored.documents(sid,include_content=True),[old])
        pending=a.create_goal(store,sid,'下一項')
        self.path.write_bytes(document([['Shipping is 90.']]))
        store.attach_document(sid,self.path)
        self.assertEqual(a.run_goal(store,sid,pending,ScriptedEngine())['steps'],[])
        with patch.object(p,'project',return_value='changed parser projection'):
            self.assertNotEqual(read_document(self.path)['sha256'],store.documents(sid)[0]['sha256'])

    def test_folder_scope_revalidation_metadata_and_text_comparison(self):
        self.path.write_bytes(document([['ORBIT-85']]))
        store=Store(self.root/'state.sqlite3');sid=store.new_session()
        report=search_folder(self.root,'ORBIT-85');result=report['results'][0]
        self.assertEqual(result['representation'],p.ENCODING)
        attach_result(store,sid,report,result);first=store.documents(sid,include_content=True)[0]
        self.path.write_bytes(document([['ORBIT-85']],comment='different metadata'))
        with self.assertRaises(ValueError):attach_result(store,sid,report,result)
        second={'id':2,**read_document(self.path)}
        diff=compare([first,second],{'old_document_id':first['id'],'new_document_id':2,'offset':0})
        self.assertTrue(diff['decoded_text_equal']);self.assertIsNone(diff['byte_hash_equal'])
        self.assertFalse(diff['source_fingerprint_equal'])
        with self.assertRaises(ValueError):read_document(self.path,scope=self.root/'other')

    def test_hidden_document_window_shows_limits_and_rejects_invalid_import(self):
        import tkinter as tk
        from assistant.document_ui import DocumentWindow
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        store=Store(self.root/'state.sqlite3');sid=store.new_session();self.path.write_bytes(example())
        view=DocumentWindow(root,store,sid);view.window.withdraw()
        with patch('assistant.document_import_ui.start_worker',side_effect=lambda work,failed,**kw:work()):
            progress=view.import_paths([self.path])
        progress.poll()
        view.listing.selection_set(view.listing.get_children()[0]);view.select()
        self.assertIn('不是OCR',view.detail.get('1.0','end'))
        self.path.write_bytes(b'%PDF-broken')
        with patch('assistant.document_import_ui.start_worker',side_effect=lambda work,failed,**kw:work()):
            progress=view.import_paths([self.path])
        progress.poll();self.assertEqual(progress.result['results'][0]['status'],'failed')
        self.assertEqual(len(store.documents(sid)),1)

    def test_last_folder_file_cancel_and_deadline_are_reported(self):
        self.path.write_bytes(example());event=threading.Event()
        def cancelled(*args,**kwargs):
            self.assertIs(kwargs['cancel'],event);event.set();raise ValueError('cancelled')
        with patch('assistant.folder_search.read_document',side_effect=cancelled):
            result=search_folder(self.root,'ORBIT',cancel=event)
        self.assertTrue(result['cancelled']);self.assertEqual(result['results'],[])
        import time
        def delayed(*args,**kwargs):
            # Deadline addition/subtraction can differ by a few binary float ULPs.
            self.assertLessEqual(kwargs['timeout'],.1+1e-6)
            time.sleep(.12);raise ValueError('timeout')
        with patch('assistant.folder_search.read_document',side_effect=delayed):
            result=search_folder(self.root,'ORBIT',timeout=.1)
        self.assertIn('time',result['limits_hit']);self.assertEqual(result['results'],[])

    def test_collector_binds_source_actual_requests_and_no_fixture_answers(self):
        import json
        from scripts.evaluate_pdf_reading import collect, signature
        from scripts.evaluation_reports import validate_report, GOAL_TRACE_MODE, verify_suite
        suite=p.ROOT/'evals/pdf_reading.json';cases=json.loads(suite.read_text(encoding='utf-8'))
        store=Store(self.root/'state.sqlite3')
        engine=ScriptedEngine(action('read',document_id=1,start_line=1,end_line=19),
                              action('finish',report='ORBIT-85 [D1:L2]'))
        row=collect(store,cases[1],engine,lambda:None)
        report={'format_version':1,'mode':GOAL_TRACE_MODE,'fingerprint':signature(suite),
                'case_ids':[row['id']],'results':[row]}
        validate_report(report);verify_suite(report,suite.read_bytes())
        self.assertEqual(row['engine_call_count'],2)
        self.assertEqual(row['source_sha256'],hashlib.sha256(example()).hexdigest())
        self.assertNotIn('criteria',row['calls'][0]['prompt'])
        self.assertEqual(row['documents'],store.documents(row['session_id'],include_content=True))


if __name__=='__main__':unittest.main()
