import copy
import json
import tempfile
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from assistant import answer_feedback as feedback, autonomy, goal_conversation
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.feedback_improvement import feedback_source, revise_feedback
from assistant.store import Store


class FeedbackOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state.sqlite3')
        self.sid = self.store.new_session()

    def add(self, text):
        mid = self.store.add_message(self.sid, 'assistant', '舊的助理建議')
        self.store.rate(mid, -1, text)
        return mid

    def capture(self):
        with self.store.connect() as db:
            return feedback.capture(db, self.sid)

    def test_same_second_latest_edit_is_included_and_last(self):
        ids = [self.add(f'舊更正{i}') for i in range(4)]
        self.store.rate(ids[0], -1, '最後確認為5人')
        with self.store.connect() as db:
            db.execute("UPDATE feedback SET updated_at='2026-11-03 00:00:00'")
        value = self.capture()
        self.assertEqual([r['answer_message_id'] for r in value['items']], [ids[2],ids[3],ids[0]])
        self.assertEqual(value['items'][-1]['correction'], '最後確認為5人')
        self.assertTrue(value['selection_limited'])
        self.assertEqual(self.store.feedback_summary()[0]['message_id'], ids[0])
        feedback.validate(value)

    def test_clock_rollback_and_same_content_resave_have_new_revision(self):
        first = self.add('甲廳'); second = self.add('乙廳')
        source = feedback_source(self.store, first)
        revise_feedback(self.store, first, source['fingerprint'], '甲廳')
        with self.store.connect() as db:
            db.execute('UPDATE feedback SET updated_at=? WHERE message_id=?', (source['updated_at'], first))
            db.execute("UPDATE feedback SET updated_at='9999-12-31 23:59:59' WHERE message_id=?", (second,))
        with self.assertRaises(ValueError):
            revise_feedback(self.store, first, source['fingerprint'], '過期覆寫')
        value = self.capture()
        self.assertEqual(value['items'][-1]['answer_message_id'], first)
        self.assertGreater(value['items'][-1]['update_revision'], value['items'][0]['update_revision'])
        feedback.validate(value)

    def test_historical_goal_snapshot_and_legacy_validation_remain_immutable(self):
        first = self.add('最初甲廳')
        gid = autonomy.create_goal(self.store, self.sid, '核對', from_chat=True)
        before = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        self.store.rate(first, -1, '最新乙廳')
        self.assertEqual(autonomy.get_goal(self.store, self.sid, gid)['conversation_context'], before)
        legacy = copy.deepcopy(before['answer_feedback'])
        for row in legacy['items']:
            row.pop('update_revision', None); row.pop('order_basis', None)
        feedback.validate(legacy)
        bad = self.capture(); bad['items'][0]['update_revision'] = True
        with self.assertRaises(ValueError): feedback.validate(bad)
        self.add('別項')
        mixed = self.capture(); mixed['items'][0].pop('update_revision'); mixed['items'][0].pop('order_basis')
        with self.assertRaises(ValueError): feedback.validate(mixed)

    def test_legacy_backup_migrates_once_and_preserves_original_snapshot(self):
        ids = [self.add(str(i)) for i in range(3)]
        gid = autonomy.create_goal(self.store, self.sid, '舊目標', from_chat=True)
        original = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        for item in original['answer_feedback']['items']:
            item.pop('update_revision', None); item.pop('order_basis', None)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_conversations SET context_json=?,context_sha256=? WHERE goal_id=?',
                       (json.dumps(original), goal_conversation.digest(original), gid))
            db.execute('DROP TABLE IF EXISTS feedback_versions')
            for index, mid in enumerate(reversed(ids)):
                db.execute('UPDATE feedback SET updated_at=? WHERE message_id=?', (f'2026-11-03 00:00:0{index}',mid))
        old = create_backup(self.store.path, self.root/'legacy.paibackup')
        restore_backup(old['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        with restored.connect() as db:
            migrated = feedback.capture(db, self.sid)
        self.assertEqual([r['answer_message_id'] for r in migrated['items']], list(reversed(ids)))
        self.assertTrue(all(r['order_basis']=='legacy_timestamp' for r in migrated['items']))
        self.assertEqual(autonomy.get_goal(restored,self.sid,gid)['conversation_context'], original)
        Store(restored.path)
        with restored.connect() as db:
            self.assertEqual(feedback.capture(db,self.sid), migrated)
        restored.rate(ids[2], -1, '升級後的新更正')
        with restored.connect() as db:
            self.assertEqual(feedback.capture(db,self.sid)['items'][-1]['order_basis'], 'recorded_update')
        validate_database(restored.path)

    def test_concurrent_writes_and_failed_edit_keep_atomic_order(self):
        ids = [self.add(str(i)) for i in range(8)]
        def update(mid): self.store.rate(mid, -1, '更新'+str(mid))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, ids))
        with self.store.connect() as db:
            rows = db.execute('SELECT revision,message_id FROM feedback_versions ORDER BY revision').fetchall()
        self.assertEqual(len({r['revision'] for r in rows}), 8)
        self.assertGreater(rows[0]['revision'], 8)
        stale = feedback_source(self.store, ids[0])
        self.store.rate(ids[0], -1, '新的已保存內容')
        before = self.capture()
        with self.assertRaises(ValueError): revise_feedback(self.store, ids[0], stale['fingerprint'], '舊編輯器')
        self.assertEqual(self.capture(), before)
        self.store.rate(ids[0], 1, '')
        self.assertNotIn(ids[0], [r['answer_message_id'] for r in self.capture()['items']])

    def test_backup_rejects_incomplete_order_and_counter_rewind(self):
        mid = self.add('有效更正')
        validate_database(self.store.path)
        with self.store.connect() as db:
            revision = db.execute('SELECT revision FROM feedback_versions WHERE message_id=?',(mid,)).fetchone()[0]
            db.execute("UPDATE sqlite_sequence SET seq=0 WHERE name='feedback_versions'")
        with self.assertRaises(ValueError): validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='feedback_versions'",(revision,))
            db.execute('DELETE FROM feedback_versions WHERE message_id=?',(mid,))
        with self.assertRaises(ValueError): validate_database(self.store.path)

    def test_failure_between_content_and_order_write_rolls_back_both(self):
        mid = self.add('原內容')
        source = feedback_source(self.store, mid)
        snapshot = self.capture()
        with patch('assistant.feedback_order.record', side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError): self.store.rate(mid, -1, '不能留下的內容')
            with self.assertRaises(RuntimeError):
                revise_feedback(self.store, mid, source['fingerprint'], '另一項不能留下的內容')
        self.assertEqual(feedback_source(self.store, mid), source)
        self.assertEqual(self.capture(), snapshot)
        validate_database(self.store.path)

    def test_revision_not_reused_after_delete_and_backup_restore(self):
        mid = self.add('第一筆')
        revision = self.capture()['items'][0]['update_revision']
        with self.store.connect() as db: db.execute('DELETE FROM feedback WHERE message_id=?',(mid,))
        archive = create_backup(self.store.path, self.root/'empty.paibackup')
        restore_backup(archive['path'],self.root/'copy')
        restored = Store(self.root/'copy'/'assistant.sqlite3')
        restored.rate(mid,-1,'再保存')
        with restored.connect() as db:
            self.assertGreater(feedback.capture(db,self.sid)['items'][0]['update_revision'], revision)

    def test_feedback_window_displays_actual_order_and_keeps_selected_editor(self):
        from assistant.feedback_ui import FeedbackWindow
        root = tk.Tk(); root.withdraw()
        try:
            first = self.add('甲'); self.add('乙')
            self.store.rate(first, -1, '最後更正')
            view = FeedbackWindow(root, self.store); view.window.withdraw()
            self.assertEqual(view.listing.get_children()[0], str(first))
            view.listing.selection_set(str(first)); view.select()
            self.assertIn('實際保存順序', view.preview.get('1.0','end'))
            self.assertEqual(view.saved_correction.get('1.0','end').strip(), '最後更正')
            self.assertGreater(view.selected_source['update_revision'], 2)
        finally:
            for timer in root.tk.call('after','info'): root.tk.call('after','cancel',timer)
            root.destroy()


if __name__ == '__main__': unittest.main()
