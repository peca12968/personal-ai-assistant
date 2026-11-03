import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import learning_queue as queue
from assistant.engine import Assistant
from assistant.store import Store


class Engine:
    def answer(self, prompt, cancel=None):
        return '原本的錯誤回答'


class LearningQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.mid = Assistant(self.store,Engine()).ask(self.sid,'合成站點青灣的代號是什麼？')['id']
        self.store.rate(self.mid,-1,'代號應為TEST-42，請修正。')
        self.source = queue.source_from_feedback(self.store,self.mid)

    def test_only_explicit_confirmation_creates_example_and_preserves_exact_text(self):
        self.assertEqual(queue.page(self.store)['total'],0)
        prompt,answer='  合成站點青灣\n代號？  ','TEST-42\n'
        row=queue.confirm(self.store,prompt,answer,source=self.source)
        self.assertEqual((row['prompt'],row['answer'],row['revision'],row['readiness']),(prompt,answer,1,'ready'))
        self.source['correction']='caller mutation'
        self.assertEqual(queue.get(self.store,row['id'])['source']['correction'],'代號應為TEST-42，請修正。')
        with patch.object(Engine,'answer',side_effect=AssertionError('Must not call model')):
            same=queue.confirm(self.store,prompt,answer,source=row['source'],item_id=row['id'],expected_revision=1)
        self.assertEqual(same['revision'],1)
        self.assertEqual(len(queue.history(self.store,row['id'])['items']),1)
        queue.confirm(self.store,'QUEUE_ONLY_QUESTION_7','QUEUE_ONLY_TARGET_9')
        context=Assistant(self.store,Engine()).prepare_context(self.sid,'新的問題')
        self.assertNotIn('learning_examples',context)
        self.assertNotIn('QUEUE_ONLY_QUESTION_7',json.dumps(context,ensure_ascii=False))
        self.assertNotIn('QUEUE_ONLY_TARGET_9',json.dumps(context,ensure_ascii=False))
        self.assertNotIn('learning_capabilities',context)
        asked=Assistant(self.store,Engine()).ask(self.sid,'這個助理的學習佇列會直接訓練嗎？')
        recorded=self.store.answer_context(asked['id'])
        self.assertFalse(recorded['learning_capabilities']['queue_contents_provided'])
        self.assertFalse(recorded['learning_capabilities']['queue_training_implemented'])
        self.assertNotIn('QUEUE_ONLY_TARGET_9',json.dumps(recorded,ensure_ascii=False))

    def test_stale_feedback_blocks_save_and_existing_readiness_without_rewriting_history(self):
        row=queue.confirm(self.store,'獨立問題','TEST-42',source=self.source)
        before=queue.history(self.store,row['id'])
        self.store.rate(self.mid,-1,'最新更正：代號TEST-43。')
        self.assertEqual(queue.get(self.store,row['id'])['readiness'],'needs_review')
        self.assertEqual(queue.history(self.store,row['id']),before)
        with self.assertRaisesRegex(ValueError,'已變更'):
            queue.confirm(self.store,'另一題','TEST-43',source=self.source)
        fresh=queue.source_from_feedback(self.store,self.mid)
        revised=queue.confirm(self.store,'獨立問題','TEST-43',source=fresh,item_id=row['id'],expected_revision=1)
        self.assertEqual((revised['revision'],revised['readiness']),(2,'ready'))
        self.store.rate(self.mid,1,'')
        self.assertEqual(queue.get(self.store,row['id'])['readiness'],'needs_review')
        withdrawn=queue.withdraw(self.store,row['id'],2)
        self.assertEqual(withdrawn['readiness'],'withdrawn')
        self.assertEqual([r['status'] for r in queue.history(self.store,row['id'])['items']],['withdrawn','confirmed','confirmed'])

    def test_stale_editor_withdrawal_and_lineage_changes_rejected(self):
        row=queue.confirm(self.store,'問題','答案',source=self.source)
        withdrawn=queue.withdraw(self.store,row['id'],1)
        for action in [lambda:queue.withdraw(self.store,row['id'],1),
                       lambda:queue.confirm(self.store,'改題','改答案',source=self.source,item_id=row['id'],expected_revision=1),
                       lambda:queue.confirm(self.store,'改題','改答案',item_id=row['id'],expected_revision=2)]:
            with self.assertRaises(ValueError): action()
        self.assertEqual(queue.withdraw(self.store,row['id'],2)['revision'],2)
        restored=queue.confirm(self.store,'問題','更正答案',source=self.source,item_id=row['id'],expected_revision=2)
        self.assertEqual(restored['revision'],3)
        self.assertEqual(queue.history(self.store,row['id'])['items'][-1]['answer'],'答案')

    def test_same_prompt_cannot_acquire_conflicting_active_answers(self):
        row=queue.confirm(self.store,'同一問題','A')
        for answer in ['A','B']:
            with self.assertRaisesRegex(ValueError,'相同問題'):
                queue.confirm(self.store,'同一問題',answer)
        queue.withdraw(self.store,row['id'],1)
        new=queue.confirm(self.store,'同一問題','B')
        with self.assertRaisesRegex(ValueError,'相同問題'):
            queue.confirm(self.store,'同一問題','A',item_id=row['id'],expected_revision=2)
        self.assertEqual(queue.get(self.store,new['id'])['answer'],'B')

    def test_two_concurrent_editors_commit_only_one_revision(self):
        row=queue.confirm(self.store,'問題','原答案'); barrier=threading.Barrier(2); results=[]
        def edit(answer):
            barrier.wait()
            try:
                value=queue.confirm(self.store,'問題',answer,item_id=row['id'],expected_revision=1)
                results.append(('saved',value['answer']))
            except ValueError as exc: results.append(('stale',str(exc)))
        workers=[threading.Thread(target=edit,args=(answer,)) for answer in ['A','B']]
        for worker in workers: worker.start()
        for worker in workers: worker.join(20); self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(r[0] for r in results),['saved','stale'])
        self.assertEqual(len(queue.history(self.store,row['id'])['items']),2)

    def test_input_bounds_and_source_eligibility(self):
        for value in ['', ' ', 'x'*2001, '\0', '\ud800', None, 42]:
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                queue.confirm(self.store,'問題',value)
        fake=dict(self.source,correction='forged')
        with self.assertRaises(ValueError): queue.confirm(self.store,'問題','答案',source=fake)
        for context in [{'response_mode':'local_memory','current_request':'記住：合成資料'},
                        {'current_request':'圖片問題','image_inputs':{'items':['test']}}, {}]:
            with self.store.connect() as db: db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',(json.dumps(context),self.mid))
            with self.assertRaises(ValueError): queue.source_from_feedback(self.store,self.mid)
        self.assertEqual(queue.page(self.store)['total'],0)

    def test_source_ids_are_bounded_before_database_access(self):
        for value in [True,0,-1,2**63,2**200]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                queue.source_from_feedback(self.store,value)
            for key in ['message_id','feedback_revision']:
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    queue.validate_source(dict(self.source,**{key:value}))

    def test_capability_hint_recognizes_separated_topic_words_without_reading_queue(self):
        for question in ['建立可供日後學習的範例','學習用的完整例子怎麼填？','回饋如何進入學習？','Learning Queue status']:
            hint=queue.help_context(question)
            self.assertIsNotNone(hint)
            self.assertFalse(hint['queue_contents_provided'])
            self.assertFalse(hint['queue_training_implemented'])
        for question in ['今天想吃什麼？','給我一個加總的例子','學習Python需要哪些基本知識？']:
            self.assertIsNone(queue.help_context(question))

    def test_capacity_reserves_withdrawal_and_pages_do_not_hide_omissions(self):
        with patch.object(queue,'MAX_ITEMS',2),patch.object(queue,'MAX_REVISIONS',4):
            a=queue.confirm(self.store,'問題A','答案A'); b=queue.confirm(self.store,'問題B','答案B')
            with self.assertRaises(ValueError): queue.confirm(self.store,'問題C','答案C')
            with self.assertRaises(ValueError): queue.confirm(self.store,'問題A','新答案',item_id=a['id'],expected_revision=1)
            queue.withdraw(self.store,a['id'],1); queue.withdraw(self.store,b['id'],1)
            self.assertTrue(all(r['readiness']=='withdrawn' for r in queue.page(self.store)['items']))
        for n in range(22): queue.confirm(self.store,f'分頁問題{n}','答案')
        one,two=queue.page(self.store),queue.page(self.store,2)
        self.assertEqual((one['total'],one['pages'],len(one['items']),len(two['items'])),(24,2,20,4))
        self.assertFalse({r['id'] for r in one['items']} & {r['id'] for r in two['items']})

    def test_backup_preserves_history_and_changed_source_and_accepts_legacy(self):
        from assistant.backup import create_backup,restore_backup,validate_database
        row=queue.confirm(self.store,'問題','答案',source=self.source)
        queue.withdraw(self.store,row['id'],1)
        self.store.rate(self.mid,-1,'更新的來源')
        expected=queue.history(self.store,row['id'])
        saved=create_backup(self.store.path,self.root/'saved.paibackup')
        restored=self.root/'restored'; restore_backup(saved['path'],restored)
        other=Store(restored/'assistant.sqlite3')
        self.assertEqual(queue.history(other,row['id']),expected)
        with other.connect() as db: db.execute('UPDATE learning_examples SET answer=? WHERE revision=1',('tampered',))
        with self.assertRaises(ValueError): validate_database(other.path)
        with self.store.connect() as db: db.execute('DROP TABLE learning_examples')
        validate_database(self.store.path)
        reopened=Store(self.store.path)
        self.assertEqual(queue.page(reopened)['total'],0)

    def test_backup_rejects_gap_and_invalid_transition_even_with_recomputed_hash(self):
        from assistant.backup import validate_database
        row=queue.confirm(self.store,'問題','答案'); queue.withdraw(self.store,row['id'],1)
        with self.store.connect() as db: db.execute('DELETE FROM learning_examples WHERE revision=1')
        with self.assertRaises(ValueError): validate_database(self.store.path)
        other=Store(self.root/'transition.sqlite3')
        initial=queue.confirm(other,'問題','原答案')
        changed=queue.confirm(other,'問題','不同答案',item_id=initial['id'],expected_revision=1)
        changed['status']='withdrawn'
        fingerprint=queue.digest({k:changed[k] for k in ('id','revision','status','prompt','answer','source')})
        with other.connect() as db:
            db.execute("UPDATE learning_examples SET status='withdrawn',fingerprint=? WHERE revision=2",(fingerprint,))
        with self.assertRaises(ValueError): validate_database(other.path)

    def test_actual_ui_requires_answer_preserves_stale_draft_and_rechecks_source(self):
        import tkinter as tk
        from assistant.learning_ui import LearningWindow
        from assistant.feedback_ui import FeedbackWindow
        root=tk.Tk(); root.withdraw()
        try:
            feedback=FeedbackWindow(root,self.store); feedback.window.withdraw()
            feedback.listing.selection_set(str(self.mid)); feedback.select()
            ui=feedback.prepare_learning(); ui.window.withdraw()
            self.assertEqual(ui.fields['answer'].get('1.0','end-1c'),'')
            ui.confirm()
            self.assertEqual(queue.page(self.store)['total'],0)
            ui.fields['answer'].insert('1.0','TEST-42'); ui.confirm()
            row=ui.current; self.assertEqual(row['revision'],1)
            queue.confirm(self.store,row['prompt'],'另一視窗答案',source=row['source'],item_id=row['id'],expected_revision=1)
            ui.fields['answer'].delete('1.0','end'); ui.fields['answer'].insert('1.0','未送出草稿')
            ui.confirm()
            self.assertIn('已變更',ui.notice.get())
            self.assertEqual(ui.fields['answer'].get('1.0','end-1c'),'未送出草稿')
            ui.recheck(); self.assertIn('另一視窗答案',ui.saved.get('1.0','end'))
            self.store.rate(self.mid,-1,'最新TEST-43')
            ui.confirm(); self.assertIn('已變更',ui.notice.get())
            ui.recheck(); self.assertIn('最新TEST-43',ui.source_view.get('1.0','end'))
            ui.fields['answer'].delete('1.0','end'); ui.fields['answer'].insert('1.0','TEST-43')
            ui.confirm(); self.assertEqual(ui.current['revision'],3)
            ui.withdraw(); self.assertEqual(ui.current['readiness'],'withdrawn')
            self.assertEqual(ui.current['revision'],4)
            # Opening or refreshing a standalone editor does not create rows.
            empty=LearningWindow(root,self.store); empty.window.withdraw(); empty.refresh()
            self.assertEqual(queue.page(self.store)['total'],1)
        finally:
            for timer in root.tk.call('after','info'): root.tk.call('after','cancel',timer)
            root.destroy()


if __name__=='__main__': unittest.main()
