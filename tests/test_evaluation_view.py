"""Read-only report browsing with explicit provenance and owned UI work."""
import copy
import gc
import hashlib
import json
import queue
import tempfile
import threading
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

from assistant.evaluation_view import load_snapshot, summary_text, case_detail, LoadingCancelled
from assistant.evaluation_ui import EvaluationWindow
from assistant.gui import App
from assistant.store import Store
from scripts.review_understanding import apply_reviews


def example(count=2, passed=False, reviewer='development_agent'):
    ids=['case-'+str(i) for i in range(count)]
    r={'format_version':1,'git_head':'synthetic','fingerprint':{'suite_sha256':'a'*64,'runner_sha256':'b'*64,'source_sha256':'c'*64},
       'case_ids':ids,'results':[{'id':id,'category':'reasoning','prompt':'合成問題 '+id,'criteria':['結果正確'],
          'answer':'合成答案 '+id,'human_grade':None,'source_sha256':'d'*64,'state_path':'Z:/must-not-open.sqlite3'} for id in ids]}
    cases=[{key:row[key] for key in ('id','category','prompt','criteria')} for row in r['results']]
    raw=json.dumps(cases,ensure_ascii=False).encode('utf-8')
    r['fingerprint']['suite_sha256']=hashlib.sha256(raw).hexdigest()
    if passed is not None:
        apply_reviews(r,[{'id':ids[0],'criteria_pass':[passed],'note':'合成評讀'}],reviewer)
    return r,raw


class EvaluationViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.a,self.suite_bytes=example()
        self.b,_=example(passed=True,reviewer='user')
        self.paths=[self.write('a',self.a),self.write('b',self.b),self.path/'suite.json']
        self.paths[2].write_bytes(self.suite_bytes)

    def write(self,name,value):
        p=self.path/(name+'.json');p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');return p

    def test_paired_view_reuses_validation_and_preserves_all_source_bytes(self):
        original=[p.read_bytes() for p in self.paths]
        with patch('assistant.engine.Assistant.ask') as ask,patch('subprocess.Popen') as process:
            view=load_snapshot(self.paths)
            ask.assert_not_called();process.assert_not_called()
        text=summary_text(view)
        self.assertIn('未通過→通過 1',text);self.assertIn('待補結果／評讀 1',text)
        self.assertIn('評讀者變動 1',text);self.assertIn('已核對選取檔案',text)
        self.assertIn('開發代理',text);self.assertIn('使用者',text)
        self.assertEqual(view['comparison']['transitions'][0]['before'],'fail')
        self.assertEqual(view['comparison']['transitions'][0]['after'],'pass')
        self.assertEqual(original,[p.read_bytes() for p in self.paths])
        self.assertEqual(view['files'][0]['sha256'],hashlib.sha256(original[0]).hexdigest())
        self.assertIn('原答案',case_detail(view,0,'case-0'))
        self.assertIn('合成答案 case-0',case_detail(view,0,'case-0'))
        self.assertIsNone(view['reports'][0]['results'][0]['human_grade'])

    def test_partial_errors_pending_and_legacy_binding_are_explicit(self):
        r=copy.deepcopy(self.a);r['results'][0]['reviews'][0].pop('evidence_sha256')
        r['results'][1].update(answer='',error='合成錯誤');r['case_ids'].append('uncollected')
        path=self.write('partial',r)
        view=load_snapshot((path,None,None));text=summary_text(view)
        self.assertIn('已收集 2／3',text);self.assertIn('空答案 1',text);self.assertIn('錯誤 1',text)
        self.assertIn('舊評讀未綁定 1',text);self.assertIn('尚未驗證',text)
        self.assertIn('尚未收集',case_detail(view,0,'uncollected'))
        self.assertIn('合成錯誤',case_detail(view,0,'case-1'))
        self.assertIn('單次查看',case_detail(view,1,'case-0'))
        self.assertNotIn('evidence_sha256',view['reports'][0]['results'][0]['reviews'][0])

    def test_bad_suite_changed_evidence_and_incompatible_pair_reject_whole_load(self):
        wrong_suite=self.path/'wrong.json';wrong_suite.write_bytes(self.suite_bytes+b' ')
        with self.assertRaises(ValueError):load_snapshot((self.paths[0],None,wrong_suite))
        changed=copy.deepcopy(self.a);changed['results'][0]['answer']='改過答案'
        with self.assertRaises(ValueError):load_snapshot((self.write('changed',changed),None,None))
        changed=copy.deepcopy(self.b);changed['results'][0].pop('reviews');changed['results'][0]['criteria']=['不同標準']
        with self.assertRaises(ValueError):load_snapshot((self.paths[0],self.write('other',changed),None))
        with self.assertRaises(ValueError):load_snapshot((None,None,None))
        with self.assertRaises(ValueError):load_snapshot((self.paths[0],))

    def test_snapshot_does_not_follow_later_file_changes_and_details_are_bounded(self):
        r,_=example(passed=None);r['results'][0]['answer']='長'*50000
        path=self.write('long',r);view=load_snapshot((path,None,None))
        detail=case_detail(view,0,'case-0');self.assertIn('顯示省略',detail);self.assertLess(len(detail),25000)
        before=copy.deepcopy(view['reports'][0]);path.write_text('{}',encoding='utf-8')
        self.assertEqual(view['reports'][0],before)
        with self.assertRaises(ValueError):load_snapshot((path,None,None))

    def test_cancellation_and_file_boundaries_do_not_return_partial_view(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(LoadingCancelled):load_snapshot(self.paths,stop)
        stop.clear()
        from assistant import evaluation_view
        original=evaluation_view.summarize
        def cancel_after_summary(*args,**kwargs):
            result=original(*args,**kwargs);stop.set();return result
        with patch.object(evaluation_view,'summarize',side_effect=cancel_after_summary),self.assertRaises(LoadingCancelled):
            load_snapshot(self.paths,stop)
        with patch.object(evaluation_view,'MAX_JSON_BYTES',10),self.assertRaises(ValueError):load_snapshot(self.paths)
        duplicate=self.path/'duplicate.json';duplicate.write_text('{"case_ids":["a"],"case_ids":["b"],"results":[]}',encoding='utf-8')
        with self.assertRaises(ValueError):load_snapshot((duplicate,None,None))


class EvaluationWindowTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.root=tk.Tk();self.root.withdraw()
        self.threads=[];self.releases=[];self.addCleanup(self.dispose)
        r,raw=example();self.file=self.path/'report.json';self.file.write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
        self.view=EvaluationWindow(self.root,self.path);self.view.window.withdraw()

    def dispose(self):
        for release in self.releases:release.set()
        for thread in self.threads:
            thread.join(5);self.assertFalse(thread.is_alive())
        try:
            for timer in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',timer)
            self.root.destroy()
        except tk.TclError:pass
        gc.collect()

    @staticmethod
    def immediate(target,on_failure,**kwargs):
        target();return True

    def load(self,path=None):
        self.view.paths[0]=str(path or self.file)
        with patch('assistant.evaluation_ui.start_worker',self.immediate):self.view.begin()
        self.assertTrue(self.view.busy);self.view.poll();self.assertFalse(self.view.busy)

    def test_select_load_case_detail_and_same_file_refresh(self):
        self.load();self.assertEqual(len(self.view.listing.get_children()),2)
        self.assertIn('合成答案',self.view.details[0].get('1.0','end'))
        self.assertIn(str(self.file.resolve()),self.view.loaded_label.get())
        self.assertEqual(str(self.view.summary['state']),'disabled')
        r,raw=example(passed=True);self.file.write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
        self.assertEqual(self.view.snapshot['reports'][0]['results'][0]['reviews'][-1]['result'],'fail')
        self.load();self.assertEqual(self.view.snapshot['reports'][0]['results'][0]['reviews'][-1]['result'],'pass')

    def test_selection_change_and_failed_load_preserve_clearly_labeled_previous_view(self):
        self.load();snapshot=self.view.snapshot;label=self.view.loaded_label.get()
        bad=self.path/'invalid.json';bad.write_text('{}',encoding='utf-8')
        with patch('assistant.evaluation_ui.filedialog.askopenfilename',return_value=str(bad)):self.view.choose(0)
        self.assertIn('畫面仍是上次',self.view.notice.get());self.assertEqual(self.view.loaded_label.get(),label)
        with patch('assistant.evaluation_ui.start_worker',self.immediate):self.view.begin()
        self.view.poll();self.assertIn('載入未成功',self.view.notice.get());self.assertIn('仍顯示上次',self.view.notice.get())
        self.assertIs(self.view.snapshot,snapshot);self.assertEqual(self.view.loaded_label.get(),label)
        self.view.paths[1]=str(self.file);self.view.clear(1)
        self.assertIsNone(self.view.paths[1]);self.assertIs(self.view.snapshot,snapshot)

    def test_pagination_covers_cases_without_silently_dropping_uncollected_items(self):
        r,raw=example(105,passed=None);r['results']=r['results'][:1]
        path=self.path/'many.json';path.write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
        self.load(path);self.assertEqual(len(self.view.listing.get_children()),100)
        self.assertIn('1–100／105',self.view.page_label.get())
        self.view.change_page(1);self.assertEqual(len(self.view.listing.get_children()),5)
        self.assertIn('101–105／105',self.view.page_label.get())
        self.assertIn('尚未收集',self.view.details[0].get('1.0','end'))
        self.assertEqual(str(self.view.next_btn['state']),'disabled')
        self.view.change_page(-1);self.assertEqual(len(self.view.listing.get_children()),100)

    def test_launch_failure_recovers_without_read_and_shutdown_rejects_new_work(self):
        self.view.paths[0]=str(self.file)
        with patch('threading.Thread.start',side_effect=RuntimeError('synthetic launch failure')),patch('assistant.evaluation_ui.load_snapshot') as load:
            self.view.begin();self.view.begin();load.assert_not_called()
        self.assertTrue(self.view.busy);self.view.poll()
        self.assertIn('未啟動',self.view.notice.get());self.assertFalse(self.view.busy)
        self.assertEqual(str(self.view.load_btn['state']),'normal')
        def closed_dialog(*args,**kwargs):
            self.root._assistant_closing=True;return str(self.file)
        self.view.paths[0]=None
        with patch('assistant.evaluation_ui.filedialog.askopenfilename',closed_dialog):self.view.choose(0)
        self.assertIsNone(self.view.paths[0])
        with patch('assistant.evaluation_ui.start_worker') as worker,patch('assistant.evaluation_ui.filedialog.askopenfilename') as choose:
            self.view.begin();self.view.choose(0);self.view.clear(1)
            worker.assert_not_called();choose.assert_not_called()
        timer=self.view.poll_timer;self.view.close();self.assertTrue(self.view.closed)
        self.assertNotIn(timer,self.root.tk.call('after','info'))

    def test_real_worker_is_owned_until_terminal_event_consumed(self):
        entered,release=threading.Event(),threading.Event();self.releases.append(release)
        original=load_snapshot
        def delayed(paths,cancel):
            entered.set()
            if not release.wait(5):raise RuntimeError('test timeout')
            return original(paths,cancel)
        def launch(target,on_failure,**kwargs):
            thread=threading.Thread(target=target);self.threads.append(thread);thread.start();return True
        self.view.paths[0]=str(self.file)
        app=App.__new__(App);app.root=self.root;app.busy=False;app.cancel=threading.Event()
        app.reminder_timer=None;app.autonomous_windows=[];app.memory_draft_windows=[];app.drafts=Mock();app.drafts.flush.return_value=True
        app.status=tk.StringVar(self.root)
        with patch('assistant.evaluation_ui.load_snapshot',delayed),patch('assistant.evaluation_ui.start_worker',launch),patch.object(self.root,'destroy') as destroy:
            self.view.begin();self.assertTrue(entered.wait(3))
            app.close();destroy.assert_not_called();self.assertTrue(self.view.cancel.is_set())
            self.assertTrue(self.view.window.winfo_exists())
            release.set();self.threads[-1].join(5);self.assertFalse(self.threads[-1].is_alive())
            self.assertTrue(self.view.busy);app.close();destroy.assert_not_called()
            self.view.poll();self.assertTrue(self.view.closed);self.assertIsNone(self.view.snapshot)
            app.close();destroy.assert_called_once()

    def test_sidebar_entry_preserves_chat_draft_and_does_not_auto_load(self):
        store=Store(self.path/'assistant.sqlite3');app=App(self.root,store,None)
        app.input.insert('1.0','合成未送出草稿')
        with patch('assistant.evaluation_ui.load_snapshot') as load:
            app.evaluation_btn.invoke();load.assert_not_called()
            self.assertEqual(app.input.get('1.0','end-1c'),'合成未送出草稿')
            self.assertIsNone(self.root._assistant_data_windows[-1].snapshot)
            count=len(self.root._assistant_data_windows);app.closing=True;app.evaluation_reports()
            self.assertEqual(len(self.root._assistant_data_windows),count)

    def test_finished_worker_does_not_retain_tk_controller_after_disposal(self):
        queued, release = threading.Event(), threading.Event()
        self.releases.append(release)

        class DelayedReturnQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                super().put(item, *args, **kwargs)
                queued.set()
                release.wait(5)

        def launch(target, on_failure, **kwargs):
            thread = threading.Thread(target=target)
            self.threads.append(thread)
            thread.start()
            return True

        self.view.events = DelayedReturnQueue()
        self.view.paths[0] = str(self.file)
        reference = weakref.ref(self.view)
        with patch('assistant.evaluation_ui.start_worker', launch):
            self.view.begin()
        self.assertTrue(queued.wait(3))
        self.view.close()
        self.view.poll()
        self.assertTrue(self.view.closed)
        self.view = None
        gc.collect()
        # Hold any unexpected survivor on the UI thread while the worker exits,
        # so the failing baseline cannot finalize Tcl variables off-thread.
        survivor = reference()
        release.set()
        self.threads[-1].join(5)
        self.assertFalse(self.threads[-1].is_alive())
        self.assertIsNone(survivor)


if __name__=='__main__':unittest.main()
