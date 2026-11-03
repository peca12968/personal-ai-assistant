"""Fixture batching must preserve the state seen by real context consumers."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_understanding import prepare_case, seed_history
from scripts import evaluate_understanding as fixtures, evaluate_goal_drafts, evaluate_conversation_priority


class FixtureHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_same_rows_ids_titles_and_context_as_public_message_writes(self):
        stores = [Store(self.root / f'{mode}.sqlite3') for mode in ('single', 'batch')]
        rows = [('assistant', '尚未決定的建議'), ('user', '新對話'),
                ('user', '更正：保留原始檔。\x00尾端🙂' + '甲' * 65)]
        rows += [('user', f'背景討論 {index}') for index in range(220)]
        rows += [('assistant', '方案只是建議。'), ('user', '現在只試算，不執行。')]
        actual_ids = []
        for index, store in enumerate(stores):
            with patch('assistant.store.uuid.uuid4', return_value=SimpleNamespace(hex='fixture')):
                sid = store.new_session()
            other = store.new_session('其他對話')
            store.add_message(other, 'user', '隔離內容，令本對話編號從2開始。')
            actual_ids.append(seed_history(store, sid, iter(rows)) if index else
                              [store.add_message(sid, role, content) for role, content in rows])
            with store.connect() as db:
                saved = db.execute('SELECT id,role,content,created_at FROM messages WHERE session_id=? ORDER BY id', (sid,)).fetchall()
                self.assertEqual([(row['role'], row['content']) for row in saved], rows)
                for row in saved:
                    datetime.fromisoformat(row['created_at'])
                self.assertEqual(db.execute('SELECT title FROM sessions WHERE id=?', (sid,)).fetchone()[0], rows[2][1][:50])
                # The real clock differs between runs. Freeze only these fixture
                # timestamps before comparing the entire downstream context.
                db.execute("UPDATE messages SET created_at='2026-01-01 00:00:00'")
        self.assertEqual(actual_ids[0], actual_ids[1])
        self.assertEqual(Assistant(stores[0], None).prepare_context('fixture', '最初的限制呢？'),
                         Assistant(stores[1], None).prepare_context('fixture', '最初的限制呢？'))
        for store in stores:
            custom = store.new_session('保留手動標題')
            seed_history(store, custom, [('user', '不要以此取代標題')])
            self.assertEqual(next(row for row in store.sessions() if row['id'] == custom)['title'], '保留手動標題')

    def test_single_commit_and_failed_batch_leaves_no_partial_history_or_title(self):
        store = Store(self.root / 'atomic.sqlite3')
        sid = store.new_session()
        traces = []
        connect = store.connect

        @contextmanager
        def observed():
            with connect() as db:
                db.set_trace_callback(traces.append)
                yield db

        with patch.object(store, 'connect', observed):
            ids = seed_history(store, sid, [('assistant', '建議')] * 250)
        self.assertEqual(len(ids), 250)
        self.assertEqual(sum(statement == 'COMMIT' for statement in traces), 1)
        before = store.messages(sid, limit=1000)
        for rows in ([('user', '未提交標題'), ('invalid', '無效角色')],
                     [('user', '未提交標題'), ('assistant', None)]):
            with self.assertRaises(sqlite3.IntegrityError):
                seed_history(store, sid, rows)
            self.assertEqual(store.messages(sid, limit=1000), before)
            self.assertEqual(store.sessions()[0]['title'], '新對話')
        with self.assertRaises(sqlite3.IntegrityError):
            seed_history(store, 'missing-session', [('user', '不能建立孤兒訊息')])

        def interrupted():
            yield 'user', '迭代中斷也不能部分提交'
            raise RuntimeError('fixture interrupted')

        with self.assertRaisesRegex(RuntimeError, 'fixture interrupted'):
            seed_history(store, sid, interrupted())
        self.assertEqual(store.messages(sid, limit=1000), before)
        self.assertEqual(store.sessions()[0]['title'], '新對話')
        self.assertEqual(seed_history(store, sid, []), [])

    def test_prepare_preserves_full_history_filler_order_and_long_literal(self):
        store = Store(self.root / 'prepared.sqlite3')
        history = [{'role': 'assistant', 'content': '引文\x00仍在'},
                   {'role': 'user', 'content': '明確原文🙂'}]
        sid = prepare_case(store, {'history': history, 'filler_pairs': 3, 'long_message': True})
        rows = store.messages(sid, limit=100)
        expected = [(row['role'], row['content']) for row in history]
        for number in range(1, 4):
            expected.extend([('user', f'一般背景討論第 {number} 回。'), ('assistant', '收到這項背景。')])
        expected.append(('user', '長文開始。\n' + '一般背景。' * 1000 + '\n復原代號：不可見-947。\n' + '其他背景。' * 1000 + '\n長文結束。'))
        self.assertEqual([(row['role'], row['content']) for row in rows], expected)
        self.assertEqual([row['id'] for row in rows], list(range(1, 10)))

    def test_shared_builder_changes_are_bound_in_older_collectors_too(self):
        builder = self.root / 'builder.py'
        builder.write_text('before', encoding='utf-8')
        with patch.object(fixtures, 'RUNNER_PATH', builder):
            for runner in (evaluate_goal_drafts, evaluate_conversation_priority):
                builder.write_text('before', encoding='utf-8')
                before = runner.signature(fixtures.SUITE)
                builder.write_text('after', encoding='utf-8')
                after = runner.signature(fixtures.SUITE)
                self.assertNotEqual(before['fixture_builder_sha256'], after['fixture_builder_sha256'])
                self.assertEqual({k: v for k, v in before.items() if k != 'fixture_builder_sha256'},
                                 {k: v for k, v in after.items() if k != 'fixture_builder_sha256'})
                self.assertEqual(before['runner_sha256'], fixtures.sha(Path(runner.__file__).read_bytes()))
