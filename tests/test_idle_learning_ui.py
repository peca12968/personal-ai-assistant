import gc,json,queue,tempfile,threading,time,tkinter as tk,unittest
from pathlib import Path
from unittest.mock import patch
from assistant import learning_jobs as jobs,learning_batches as batches,learning_queue as examples,idle_learning as idle
from assistant.idle_learning_ui import IdleLearningWindow
from assistant.learning_ui import LearningWindow
from assistant.store import Store
from assistant.gui import App

class IdleLearningUiTests(unittest.TestCase):
    def setUp(self):
        gc.collect();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.store=Store(self.path/'assistant.sqlite3')
        self.item=examples.confirm(self.store,'合成測試：代號？','TEST-42')
        self.root=tk.Tk();self.root.withdraw();self.threads=[];self.releases=[];self.addCleanup(self.dispose)

    def dispose(self):
        for release in self.releases:release.set()
        for view in list(getattr(self.root,'_assistant_data_windows',[])):view.cancel.set()
        for thread in self.threads:thread.join(10)
        try:
            for view in list(getattr(self.root,'_assistant_data_windows',[])):view.poll();view.close()
            for timer in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',timer)
            self.root.destroy()
        except tk.TclError:pass
        gc.collect()

    def launch(self,operation,failed,**kwargs):
        thread=threading.Thread(target=operation,**kwargs);self.threads.append(thread);thread.start();return True

    def view(self):
        view=IdleLearningWindow(self.root,self.store);view.window.withdraw()
        view.listing.selection_set(self.item['id']);view.add_items()
        view.question.insert('1.0','合成測試的代號，只答代號。');view.answer.insert('1.0','TEST-42');view.add_case()
        return view

    def join(self,view):
        for thread in self.threads:thread.join(10);self.assertFalse(thread.is_alive())
        self.assertTrue(view.busy);view.poll();self.assertFalse(view.busy)

    def prepared(self,view):
        with patch('assistant.idle_learning_ui.start_worker',self.launch):view.prepare();self.join(view)
        self.assertIsNotNone(view.job,view.notice.get());self.assertTrue(view.job['startable'])
        return view.job['id']

    def test_editor_prepares_actual_frozen_batch_and_job_without_training(self):
        view=self.view()
        with patch.object(idle,'run') as run:
            uid=self.prepared(view);run.assert_not_called()
        manifest=idle.read_manifest(idle.folder(self.path,uid),live=True)
        self.assertEqual(len(manifest['suite']['cases']),3)
        batch=batches.read_manifest(batches.folder(self.path,manifest['batch_id']))
        self.assertEqual(batch['items'][0]['answer'],'TEST-42')
        self.assertIn('TEST-42',view.preview.get('1.0','end'));self.assertEqual(str(view.question['state']),'disabled')
        view.new();self.assertIsNone(view.job);self.assertEqual(str(view.question['state']),'normal')
        self.assertEqual(len(view.cases),3)

    def test_invalid_suite_and_unadded_draft_create_no_batch(self):
        view=self.view();view.cases=view.cases[:2]
        with patch.object(batches,'prepare') as prepare:view.prepare();prepare.assert_not_called()
        self.assertFalse(view.busy);self.assertIn('至少',view.notice.get())
        view.question.insert('1.0','尚未加入的問題');view.answer.insert('1.0','答案')
        with patch.object(batches,'prepare') as prepare:view.prepare();prepare.assert_not_called()
        self.assertIn('尚未加入',view.notice.get())

    def test_stale_selection_keeps_draft_and_does_not_train(self):
        view=self.view();examples.confirm(self.store,self.item['prompt'],'TEST-43',item_id=self.item['id'],expected_revision=1)
        with patch('assistant.idle_learning_ui.start_worker',self.launch):view.prepare();self.join(view)
        self.assertIsNone(view.job);self.assertIn('變更',view.notice.get());self.assertEqual(view.basket[self.item['id']]['revision'],1)
        self.assertEqual(view.cases[-1]['answer'],'TEST-42')

    def test_partial_preparation_preserves_batch_for_explicit_reuse(self):
        view=self.view();suite={'schema':'full_assistant_cases_v1','cases':view.cases};settings={'idle_seconds':30,'wait_seconds':1,'work_seconds':10}
        with patch.object(idle,'prepare',side_effect=OSError('synthetic save failure')):
            result=jobs.prepare(self.store,[{'id':self.item['id'],'revision':1}],suite,settings)
        self.assertIsNone(result['job_id']);self.assertIsNotNone(result['batch_id']);self.assertIn('save failure',result['error'])
        again=jobs.prepare(self.store,[],suite,settings,existing=result['batch_id'])
        self.assertIsNone(again['error']);self.assertTrue(again['preview']['startable'])

    def test_background_preparation_responsive_and_terminal_event_remains_owned(self):
        view=self.view();entered=threading.Event();release=threading.Event();self.releases.append(release);real=jobs.prepare
        def blocked(*args,**kwargs):entered.set();release.wait(8);return real(*args,**kwargs)
        with patch('assistant.idle_learning_ui.start_worker',self.launch),patch.object(jobs,'prepare',side_effect=blocked):
            view.prepare();self.assertTrue(entered.wait(3));view.prepare();self.assertEqual(len(self.threads),1)
            beat=[];self.root.after(0,lambda:beat.append(True));self.root.update();self.assertEqual(beat,[True])
            self.assertIn(view,self.root._assistant_data_windows);release.set();self.threads[-1].join(10)
            self.assertTrue(view.busy);view.poll();self.assertFalse(view.busy)

    def test_thread_start_failure_recovers_editor_without_implicit_retry(self):
        view=self.view()
        with patch('threading.Thread.start',side_effect=RuntimeError('cannot start')),patch.object(jobs,'prepare') as prepare:
            view.prepare();view.prepare();self.assertTrue(view.busy);prepare.assert_not_called();view.poll()
        self.assertFalse(view.busy);self.assertEqual(str(view.prepare_btn['state']),'normal');self.assertIn('未啟動',view.notice.get());self.assertEqual(len(view.cases),3)

    def test_actual_idle_wait_cancel_and_close_wait_for_worker_result(self):
        view=self.view();uid=self.prepared(view);real=idle.run;entered=threading.Event()
        def wait(*args,**kwargs):
            callback=kwargs['on_progress']
            def progress(value):callback(value);entered.set()
            return real(*args,**{**kwargs,'on_progress':progress,'sensor':lambda:{'ticks_ms':1,'last_input_tick':1,'idle_ms':0}})
        with patch('assistant.idle_learning_ui.start_worker',self.launch),patch.object(idle,'run',side_effect=wait):
            view.run();self.assertTrue(entered.wait(3));view.close();self.assertFalse(view.closed);self.assertTrue(view.active())
            self.join(view)
        self.assertTrue(view.closed);self.assertNotIn(view,self.root._assistant_data_windows);self.assertIsNone(view.poll_timer)
        result=idle.report(self.path,uid);self.assertEqual(result['receipt']['status'],'cancelled')
        self.assertEqual(batches.local_models.read_json(batches.folder(self.path,result['receipt'].get('batch_id',idle.read_manifest(idle.folder(self.path,uid))['batch_id']))/'receipt.json')['status'],'prepared')

    def test_root_shutdown_waits_for_preparation_and_blocks_new_work(self):
        app=App(self.root,self.store,None);view=self.view();entered=threading.Event();release=threading.Event();self.releases.append(release)
        def blocked(*args,**kwargs):entered.set();release.wait(8);return {'error':'cancelled','job_id':None,'batch_id':None}
        with patch('assistant.idle_learning_ui.start_worker',self.launch),patch.object(jobs,'prepare',side_effect=blocked):
            view.prepare();self.assertTrue(entered.wait(3))
            with patch.object(self.root,'destroy') as destroy:
                app.close();destroy.assert_not_called();self.assertTrue(view.cancel.is_set())
                with self.assertRaises(ValueError):IdleLearningWindow(self.root,self.store)
                release.set();self.join(view);app.close();destroy.assert_called_once()

    def test_learning_parent_can_close_without_destroying_owned_work_window(self):
        parent=LearningWindow(self.root,self.store);parent.window.withdraw();view=parent.idle_work();view.window.withdraw()
        self.assertEqual(view.window.master,self.root);parent.window.destroy();self.assertTrue(view.window.winfo_exists())
        view.close();self.assertTrue(view.closed)

    def test_historical_running_is_not_resumed_and_external_file_rejected(self):
        view=self.view();uid=self.prepared(view);path=idle.folder(self.path,uid)
        receipt=idle.models.local_models.read_json(path/'receipt.json');receipt['status']='training';batches.save(path/'receipt.json',receipt)
        inspected=jobs.inspect(self.path,uid);self.assertFalse(inspected['startable']);self.assertIn('不是程序',inspected['text'])
        with self.assertRaises(ValueError):jobs.picked_id(self.path,self.path/'elsewhere'/('0'*32)/'manifest.json',job=True)
        self.assertEqual(jobs.picked_id(self.path,path/'manifest.json',job=True),uid)

    def test_small_window_keeps_actions_and_scrolled_controls_reachable(self):
        view=self.view();view.window.geometry('640x420');view.window.deiconify();self.root.update()
        for button in (view.prepare_btn,view.run_btn,view.stop_btn,view.new_btn):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),view.window.winfo_rootx()+view.window.winfo_width())
        self.assertGreater(view.train_form.body.winfo_height(),view.train_form.canvas.winfo_height())
        view.train_form.canvas.yview_moveto(1);self.root.update()
        self.assertAlmostEqual(view.train_form.canvas.yview()[1],1,places=2)
        view.tabs.select(1);self.root.update();view.form.canvas.yview_moveto(1);self.root.update()
        last=view.settings['work_seconds']
        self.assertGreaterEqual(last.winfo_rooty(),view.form.canvas.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty()+last.winfo_height(),view.form.canvas.winfo_rooty()+view.form.canvas.winfo_height())

    def test_progress_observer_failure_cannot_leave_started_worker(self):
        view=self.view();uid=self.prepared(view)
        with patch.object(batches,'run') as train:
            result=idle.run(self.path,uid,on_progress=lambda value:(_ for _ in ()).throw(RuntimeError('observer failed')))
            train.assert_not_called()
        self.assertEqual(result['status'],'failed');self.assertIn('observer failed',result['error'])

if __name__=='__main__':unittest.main()
