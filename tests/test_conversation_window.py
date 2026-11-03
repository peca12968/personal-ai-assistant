import json
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, conversation_history as h
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ConversationWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'state.sqlite3')
        self.sid = self.store.new_session()

    def add(self, content, role='user'):
        return self.store.add_message(self.sid, role, content)

    def goal(self):
        return a.create_goal(self.store, self.sid, '查先前方案', from_chat=True, history_search=True)

    def window(self, gid, mid, before=3, after=3, cancel=None):
        return h.window(self.store, self.sid, gid,
                        {'message_id': mid, 'before': before, 'after': after}, cancel)

    def test_neighbors_follow_saved_order_with_roles_and_gaps_not_id_arithmetic(self):
        first = self.add('採購主題')
        other = self.store.new_session()
        foreign = self.store.add_message(other, 'user', '另一對話秘密')
        Assistant(self.store, None).ask(self.sid, '記住：不應出現在工作歷史的本機命令')
        middle = self.add('第一個125元；第二個140元。', 'assistant')
        last = self.add('選第二個，改9份。')
        gid = self.goal()
        result = self.window(gid, middle)
        self.assertEqual([r['message_id'] for r in result['conversation_messages']], [first, middle, last])
        self.assertEqual([r['role'] for r in result['conversation_messages']], ['user', 'assistant', 'user'])
        self.assertEqual((result['provided_before'], result['provided_after']), (1, 1))
        self.assertTrue(result['window_limited'])
        self.assertEqual((result['omitted_before'], result['omitted_after']), (0, 0))
        self.assertNotIn('excerpts', result)
        self.assertNotIn('秘密', json.dumps(result, ensure_ascii=False))
        for mid in (foreign, last + 100):
            with self.assertRaises(ValueError): self.window(gid, mid)
        with self.assertRaises(ValueError):
            h.window(self.store, other, gid, {'message_id': middle, 'before': 1, 'after': 1})
        future = self.add('後來改成99份')
        with self.store.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('已改寫', middle))
        self.assertEqual(self.window(gid, middle), result)
        with self.assertRaises(ValueError): self.window(gid, future)

    def test_seven_message_bound_clipping_edges_and_center_only(self):
        raw = '甲\0🙂' * 800
        ids = [self.add(raw + str(i)) for i in range(10)]
        gid = self.goal(); result = self.window(gid, ids[4])
        self.assertEqual(result['returned_count'], 7)
        self.assertEqual(sum(len(r['content']) for r in result['conversation_messages']), 14000)
        self.assertTrue(all(r['content'] == raw[:2000] and r['content_truncated']
                            for r in result['conversation_messages']))
        self.assertEqual((result['omitted_before'], result['omitted_after']), (1, 2))
        self.assertFalse(result['window_limited'])
        for index, before, after in ((0, 0, 3), (9, 3, 0)):
            edge = self.window(gid, ids[index])
            self.assertEqual((edge['provided_before'], edge['provided_after']), (before, after))
            self.assertTrue(edge['window_limited'])
        one = self.window(gid, ids[4], 0, 0)
        self.assertEqual([r['message_id'] for r in one['conversation_messages']], [ids[4]])
        self.assertFalse(one['window_limited'])

    def test_exact_arguments_scope_integrity_and_cancellation(self):
        mid = self.add('選項'); gid = self.goal()
        valid = {'message_id': mid, 'before': 1, 'after': 1}
        bad = [None, {}, {**valid, 'goal_id': gid}]
        for key, values in {'message_id': [True, 0, -1, '1'],
                            'before': [True, -1, 4, 1.0], 'after': [False, -1, 4, None]}.items():
            bad.extend({**valid, key: v} for v in values)
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                a.validate_action({'tool': 'read_conversation', 'args': args, 'note': '測試'})
        stop = threading.Event(); stop.set()
        with self.assertRaises(EngineError): self.window(gid, mid, cancel=stop)
        plain = a.create_goal(self.store, self.sid, '無歷史副本', from_chat=True)
        with self.assertRaises(ValueError): self.window(plain, mid)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_conversation_history SET context_sha256=? WHERE goal_id=?', ('0'*64, gid))
        with self.assertRaises(ValueError): self.window(gid, mid)

    def test_omitted_old_messages_and_empty_archives_are_unavailable(self):
        empty = self.goal()
        with self.assertRaises(ValueError): self.window(empty, 1)
        first = self.add('原始開場')
        with self.store.connect() as db:
            db.executemany('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)',
                           [(self.sid, 'user', '一般條件')]*1000)
        gid = self.goal()
        with self.assertRaises(ValueError): self.window(gid, first)
        result = self.window(gid, first+1)
        self.assertTrue(result['history']['messages_limited'])
        self.assertEqual(result['provided_before'], 0)

    def test_tool_dispatch_and_stage_metadata_do_not_grant_citations_or_copy_text(self):
        self.add('開場')
        for i in range(20): self.add('背景'+str(i))
        mid = self.add('獨立主題')
        self.add('獨特方案單價140元', 'assistant'); self.add('選第二個，改9份')
        for i in range(20): self.add('背景尾端'+str(i))
        gid = a.create_goal(self.store, self.sid, '查選項', from_chat=True, history_search=True, file_delivery=True)
        engine = ScriptedEngine(action('read_conversation', message_id=mid, before=0, after=2),
            action('write_artifact', filename='test.txt', content='結論[D1:L1]'), action('ask', question='暫停'))
        result = a.run_goal(self.store, self.sid, gid, engine, max_steps=3)
        self.assertNotIn('獨特方案', engine.prompts[0]); self.assertIn('獨特方案', engine.prompts[1])
        self.assertEqual(result['steps'][0]['observation']['status'], 'history_read')
        self.assertEqual(result['steps'][1]['action']['tool'], 'invalid')
        child = a.create_goal(self.store, self.sid, '下一階段', parent_id=gid)
        goal = a.get_goal(self.store, self.sid, child)
        stage = goal['previous_stage']; serialized = json.dumps(stage, ensure_ascii=False)
        self.assertNotIn('獨特方案', serialized)
        self.assertIn('history_details_omitted', serialized)
        self.assertIn('provided_after', serialized)
        self.assertIsNone(goal['conversation_history'])
        with self.assertRaises(ValueError): self.window(child, mid)


if __name__ == '__main__': unittest.main()
