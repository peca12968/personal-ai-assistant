import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from assistant import answer_runs, chat_drafts, image_inputs
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, CodexEngine, EngineError
from assistant.store import Store


def pixels(color='red', fmt='PNG', size=(80,60)):
    out=io.BytesIO()
    Image.new('RGB',size,color).save(out,format=fmt)
    return out.getvalue()


class Recorder:
    def __init__(self):self.calls=[];self.fail=False;self.metadata={'model':'synthetic-test'}
    def answer(self,prompt,cancel=None):
        self.calls.append((prompt,()));return '純文字回答。'
    def answer_images(self,prompt,images,cancel=None):
        self.calls.append((prompt,images))
        if self.fail:raise EngineError('合成失敗','process_failed')
        return '已根據這次附圖回答。'


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'資料,含逗號';self.root.mkdir()
        self.store=Store(self.root/'assistant.sqlite3');self.sid=self.store.new_session()
        self.engine=Recorder();self.app=Assistant(self.store,self.engine)

    def source(self,name='圖片,一.png',data=None):
        path=self.root/name;path.write_bytes(pixels() if data is None else data);return path

    def attach(self,name='圖片,一.png',data=None):
        return image_inputs.attach_paths(self.store,self.sid,[self.source(name,data)])[0]

    def test_format_size_animation_corruption_and_atomic_import(self):
        self.assertEqual(image_inputs.inspect_bytes(pixels(fmt='JPEG'))['format'],'JPEG')
        animated=io.BytesIO()
        Image.new('RGB',(10,10),'red').save(animated,format='PNG',save_all=True,append_images=[Image.new('RGB',(10,10),'blue')])
        for data in (b'',b'not image',pixels()[:50],pixels(fmt='JPEG')[:200],animated.getvalue(),b'x'*(image_inputs.MAX_BYTES+1)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):image_inputs.inspect_bytes(data)
        with patch.object(image_inputs,'MAX_PIXELS',100), self.assertRaises(ValueError):image_inputs.inspect_bytes(pixels())
        good=self.source();bad=self.source('bad.png',b'bad')
        with self.assertRaises(ValueError):image_inputs.attach_paths(self.store,self.sid,[good,bad])
        self.assertEqual(image_inputs.catalog(self.store,self.sid),[])

    def test_ordered_snapshots_survive_original_change_no_implicit_images(self):
        a=self.attach();b=self.attach('二.jpg',pixels('blue','JPEG'))
        self.source().unlink()
        text=self.app.ask(self.sid,'先回答純文字。')
        self.assertEqual(self.engine.calls[-1][1],())
        self.assertNotIn('image_inputs',self.store.answer_context(text['id']))
        result=self.app.ask(self.sid,'比較這兩張圖。',image_ids=[b,a])
        ctx=self.store.answer_context(result['id'])
        self.assertEqual([r['id'] for r in ctx['image_inputs']],[b,a])
        self.assertEqual([r['position'] for r in ctx['image_inputs']],[1,2])
        self.assertEqual([hashlib.sha256(d).hexdigest() for d in self.engine.calls[-1][1]], [r['sha256'] for r in ctx['image_inputs']])
        self.assertNotIn(str(self.root),self.engine.calls[-1][0])
        self.assertEqual(result['image_count'],2)
        image_inputs.deactivate(self.store,self.sid,a,ctx['image_inputs'][1]['sha256'])
        self.assertEqual(self.store.answer_context(result['id']),ctx)
        self.app.ask(self.sid,'接著只用文字。')
        self.assertEqual(self.engine.calls[-1][1],())

    def test_wrong_session_duplicates_unsupported_engine_and_local_command_are_not_sent(self):
        image=self.attach();other=self.store.new_session()
        for ids in ([image,image],[image]*5,[1],image):
            with self.assertRaises(ValueError):self.app.ask(self.sid,'看圖',image_ids=ids)
        with self.assertRaises(ValueError):self.app.ask(other,'看圖',image_ids=[image])
        with self.assertRaises(ValueError):Assistant(self.store,object()).ask(self.sid,'看圖',image_ids=[image])
        with self.assertRaises(ValueError):self.app.ask(self.sid,'記住：測試資訊',image_ids=[image])
        self.assertEqual(self.store.messages(self.sid),[]);self.assertEqual(self.store.memories(),[])
        self.assertEqual(self.engine.calls,[])

    def test_retry_uses_original_image_even_after_deactivation_and_failure_keeps_draft_receipt(self):
        image=self.attach();self.engine.fail=True
        draft=chat_drafts.save(self.store,self.sid,'看原圖',0)
        # The public draft submission protocol supplies its own token.
        from assistant.chat_drafts import submission
        sending=submission(draft)
        with self.assertRaises(EngineError):self.app.ask(self.sid,'看原圖',image_ids=[image],draft=sending)
        self.assertEqual(answer_runs.runs(self.store,self.sid)[0]['status'],'failed')
        self.assertEqual(chat_drafts.get(self.store,self.sid)['content'],'')
        original=self.engine.calls[0][1]
        metadata=image_inputs.catalog(self.store,self.sid)[0]
        image_inputs.deactivate(self.store,self.sid,image,metadata['sha256'])
        self.source().write_bytes(pixels('green'))
        self.engine.fail=False;done=self.app.retry(self.sid)
        self.assertEqual(self.engine.calls[-1][1],original)
        self.assertEqual(len([m for m in self.store.messages(self.sid) if m['role']=='user']),1)
        self.assertEqual(self.store.answer_context(done['id'])['image_inputs'][0]['id'],image)

    def test_deactivation_before_begin_rolls_back_message_and_preserves_draft(self):
        image=self.attach();metadata=image_inputs.catalog(self.store,self.sid)[0]
        draft=chat_drafts.save(self.store,self.sid,'不得漏掉附圖',0)
        original=self.app.prepare_context
        def prepare(*args,**kwargs):
            result=original(*args,**kwargs)
            image_inputs.deactivate(self.store,self.sid,image,metadata['sha256'])
            return result
        with patch.object(self.app,'prepare_context',side_effect=prepare), self.assertRaises(ValueError):
            self.app.ask(self.sid,'不得漏掉附圖',image_ids=[image],draft=chat_drafts.submission(draft))
        self.assertEqual(self.store.messages(self.sid),[]);self.assertEqual(self.engine.calls,[])
        self.assertEqual(chat_drafts.get(self.store,self.sid)['content'],'不得漏掉附圖')

    def test_backup_restore_and_tampered_pixels_or_missing_table_are_rejected(self):
        image=self.attach();self.app.ask(self.sid,'看圖',image_ids=[image])
        backup=create_backup(self.store.path);restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(image_inputs.prepare(restored,self.sid,[image]),image_inputs.prepare(self.store,self.sid,[image]))
        with self.store.connect() as db:db.execute('UPDATE chat_images SET data=? WHERE id=?',(pixels('blue'),image))
        with self.assertRaises(ValueError):image_inputs.prepare(self.store,self.sid,[image])
        with self.assertRaises(ValueError):validate_database(self.store.path)
        with self.store.connect() as db:db.execute('DROP TABLE chat_images')
        with self.assertRaises(ValueError):validate_database(self.store.path)
        legacy=Store(self.root/'legacy.sqlite3')
        with legacy.connect() as db:db.execute('DROP TABLE chat_images')
        validate_database(legacy.path)

    def test_real_subprocess_receives_only_generated_image_paths_and_cleans_up(self):
        engine=CodexEngine(self.root/'engine',timeout=5)
        data=(pixels(),pixels('blue','JPEG'))
        code=('import sys,json,hashlib;from pathlib import Path;'
              'args=sys.argv[2:];paths=[args[i+1] for i,a in enumerate(args) if a=="--image"];'
              'out={"hashes":[hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths],'
              '"names":paths,"prompt":sys.stdin.read()};'
              'Path(sys.argv[1]).write_text(json.dumps(out),encoding="utf-8")')
        with patch.object(engine,'command',side_effect=lambda out:[sys.executable,'-c',code,str(out),'-']):
            answer=json.loads(engine.answer_images('圖形問題',data))
        self.assertEqual(answer['hashes'],[hashlib.sha256(d).hexdigest() for d in data])
        self.assertTrue(all(not Path(p).is_absolute() and ',' not in p for p in answer['names']))
        self.assertEqual(answer['prompt'],'圖形問題');self.assertEqual(list(engine.workdir.iterdir()),[])
        with patch.object(engine,'command',side_effect=lambda out:[sys.executable,'-c','import sys;sys.exit(3)',str(out),'-']), self.assertRaises(EngineError):
            engine.answer_images('失敗',data)
        self.assertEqual(list(engine.workdir.iterdir()),[])
        cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineError):engine.answer_images('取消',data,cancel)
        self.assertEqual(list(engine.workdir.iterdir()),[])

    def test_catalog_limit_and_deactivation_batch_are_atomic(self):
        a=self.attach();b=self.attach('b.jpg',pixels('blue','JPEG'))
        with patch.object(image_inputs,'MAX_ACTIVE',2), self.assertRaises(ValueError):
            self.attach('third.png')
        rows=image_inputs.catalog(self.store,self.sid)
        self.assertEqual(len(rows),2)
        with self.assertRaises(ValueError):
            image_inputs.deactivate_many(self.store,self.sid,[(a,rows[0]['sha256']),(b,'bad')])
        self.assertEqual(image_inputs.catalog(self.store,self.sid),rows)

    def test_fixed_image_collector_binds_actual_pixels_without_sending_fixture_answers(self):
        from scripts.evaluate_images import SUITE, collect, image_bytes, signature
        from scripts.evaluation_reports import validate_trace, validate_report, TRACE_MODE
        case=json.loads(SUITE.read_text(encoding='utf-8'))[1]
        row=collect(self.store,case,self.engine,lambda:None)
        validate_trace(row)
        validate_report({'format_version':1,'mode':TRACE_MODE,'fingerprint':signature(SUITE),'case_ids':[case['id']],'results':[row]})
        self.assertEqual(len(self.engine.calls),1)
        self.assertEqual(row['image_payload_sha256'],[hashlib.sha256(image_bytes(s)).hexdigest() for s in case['images']])
        self.assertNotIn('"circles"',row['request'])
        self.assertNotIn('criteria',row['request'])
        self.assertEqual(row['answer'],row['engine_answer'])
        trace=json.loads(self.store.path.with_suffix('.request.json').read_text(encoding='utf-8'))
        self.assertEqual(trace['status'],'returned')
        self.assertEqual(trace['sha256'],row['image_payload_sha256'])

    def test_real_tk_selection_sends_frozen_ids_and_refuses_changed_session(self):
        import gc
        import tkinter as tk
        from assistant.gui import App
        root=tk.Tk();root.withdraw()
        app=App(root,self.store,self.engine)
        try:
            app.sid=self.sid
            view=app.choose_images();view.window.withdraw()
            with patch('assistant.image_ui.filedialog.askopenfilenames',return_value=(str(self.source()),)):
                view.import_files()
            self.assertEqual(self.engine.calls,[])
            selected=view.selected();self.assertEqual(len(selected),1)
            self.assertIsNotNone(view.photo)
            view.commit(selected)
            self.assertEqual(app.image_selections[self.sid],selected)
            app.input.insert('1.0','看這張圖')
            def start(work,on_failure,**kwargs):work();return True
            with patch('assistant.gui.start_worker',side_effect=start):app.send()
            app.poll()
            self.assertEqual(len(self.engine.calls),1)
            self.assertEqual(len(self.engine.calls[0][1]),1)
            self.assertEqual(app.image_selections[self.sid],selected)
            self.assertIn('1 張',app.image_notice.get())
            view=app.choose_images();view.window.withdraw()
            app.sid=self.store.new_session()
            view.commit(selected)
            self.assertFalse(view.closed)
            self.assertIn('對話已改變',view.notice.get())
            self.assertNotIn(app.sid,app.image_selections)
            view.close()
        finally:
            for timer in root.tk.call('after','info'):root.tk.call('after','cancel',timer)
            root.destroy()
            del app
            gc.collect()
