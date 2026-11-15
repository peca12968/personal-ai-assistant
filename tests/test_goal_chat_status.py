import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, goal_guidance, goal_replies
from assistant.autonomy_status import goal_context
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_goal_status import prepare, SUITE
from test_autonomy import ScriptedEngine, action


class GoalChatStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root/'state.sqlite3')
        self.sid = self.store.new_session()

    def question(self):
        gid = a.create_goal(self.store, self.sid, '巡檢')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='青禾站或白石站？')))
        return gid

    def row(self):
        return goal_context(self.store, self.sid)['goals'][0]

    def test_actual_question_guidance_reply_and_immutable_answer_context(self):
        gid = self.question()
        row = self.row()
        self.assertEqual(row['recorded_question'], '青禾站或白石站？')
        self.assertEqual(row['next_action'], 'reply')
        self.assertTrue(row['sources_match'])
        self.assertFalse(row['question_truncated'])
        self.assertFalse(row['user_notes_present'])
        before = a.get_goal(self.store, self.sid, gid)
        app = Assistant(self.store, ScriptedEngine('尚待核對站點。'))
        answer = app.ask(self.sid, '目前待補什麼？')
        saved = self.store.answer_context(answer['id'])
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
        goal_guidance.save(self.store, self.sid, gid, '選白石站。PRIVATE_NOTES',
            expected_fingerprint=goal_guidance.snapshot(self.store, self.sid, gid)['fingerprint'])
        self.assertTrue(self.row()['user_notes_present'])
        self.assertEqual(self.row()['recorded_question'], row['recorded_question'])
        self.assertNotIn('PRIVATE_NOTES', json.dumps(self.row()))
        goal_replies.save(self.store, self.sid, gid, '白石站',
            expected_fingerprint=goal_replies.snapshot(self.store, self.sid, gid)['fingerprint'])
        self.assertEqual(self.row()['recorded_question'], '')
        self.assertEqual(self.row()['next_action'], 'resume')
        self.assertEqual(self.store.answer_context(answer['id']), saved)
        backup = create_backup(self.store.path)
        restored = self.root/'restored'
        restore_backup(backup['path'], restored)
        self.assertEqual(goal_context(Store(restored/'assistant.sqlite3'), self.sid), goal_context(self.store, self.sid))

    def test_source_observation_uses_imported_versions_not_external_files(self):
        path = self.root/'source.txt'
        path.write_text('PRIVATE_SOURCE_12', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = self.question()
        path.write_text('PRIVATE_SOURCE_18', encoding='utf-8')
        self.assertTrue(self.row()['sources_match'])
        self.store.attach_document(self.sid, path)
        path.unlink()
        row = self.row()
        self.assertFalse(row['sources_match'])
        self.assertEqual(row['next_action'], 'new_goal')
        self.assertEqual(row['recorded_question'], '青禾站或白石站？')
        self.assertNotIn('PRIVATE_SOURCE', json.dumps(row))
        self.assertEqual(a.get_goal(self.store, self.sid, gid)['status'], 'needs_input')

    def test_budget_report_and_running_states(self):
        gid = a.create_goal(self.store, self.sid, '長工作')
        for _ in range(3):
            a.run_goal(self.store, self.sid, gid,
                ScriptedEngine(*[action('calculate', operation='sum', values=['1'])]*12), max_steps=12)
        self.assertEqual(self.row()['step_count'], 32)
        self.assertEqual(self.row()['next_action'], 'new_goal')
        gid2 = a.create_goal(self.store, self.sid, '已存報告')
        a.run_goal(self.store, self.sid, gid2, ScriptedEngine(action('finish', report='PRIVATE_REPORT')))
        self.assertEqual(self.row()['next_action'], 'review')
        self.assertNotIn('PRIVATE_REPORT', json.dumps(self.row()))
        gid3 = a.create_goal(self.store, self.sid, '執行紀錄')
        a.update(self.store, gid3, 'running')
        self.assertEqual(self.row()['next_action'], 'inspect')
        self.assertEqual(self.row()['recorded_status'], 'running')
        self.assertEqual(self.row()['recorded_question'], '')

    def test_latest_three_scope_bounds_and_unicode_no_tool_replay(self):
        for i in range(4):
            gid = a.create_goal(self.store, self.sid, str(i)+'目'*450)
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='站😀'*399)))
        # Defensive clipping also handles historical/imported oversized questions.
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_goals SET question=? WHERE id=?', ('問\x00😀'*400, gid))
        result = goal_context(self.store, self.sid)
        self.assertTrue(result['limited'])
        self.assertEqual([r['objective'][0] for r in result['goals']], ['3','2','1'])
        row = result['goals'][0]
        self.assertEqual(len(row['objective']), 400)
        self.assertTrue(row['objective_truncated'])
        self.assertEqual(row['recorded_question'], ('問\x00😀'*400)[:800])
        self.assertTrue(row['question_truncated'])
        other = self.store.new_session()
        self.assertEqual(goal_context(self.store, other), {'goals':[], 'limited':False})
        self.assertNotIn('steps', row)
        self.assertNotIn('sources_json', row)
        self.assertNotIn('result', row)

    def test_goal_and_sources_share_one_sqlite_snapshot(self):
        path = self.root/'source.txt'
        path.write_text('12', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = self.question()
        original = self.store.connect
        with original() as db:
            db.execute('PRAGMA journal_mode=WAL')
        writes = []
        @contextlib.contextmanager
        def connect():
            with original() as db:
                def trace(sql):
                    if sql.startswith('SELECT id,name,sha256 FROM documents') and not writes:
                        with original() as writer:
                            writer.execute('UPDATE documents SET sha256=? WHERE session_id=?', ('a'*64,self.sid))
                            writer.execute('UPDATE autonomous_goals SET question=? WHERE id=?', ('新問題',gid))
                        writes.append(True)
                db.set_trace_callback(trace)
                yield db
        with patch.object(self.store, 'connect', connect):
            observed = self.row()
        self.assertEqual(writes, [True])
        self.assertEqual(observed['recorded_question'], '青禾站或白石站？')
        self.assertTrue(observed['sources_match'])
        self.assertEqual(self.row()['recorded_question'], '新問題')
        self.assertFalse(self.row()['sources_match'])

    def test_named_chat_resolves_older_exact_job_and_preserves_origin_after_backup(self):
        old = a.create_goal(self.store, self.sid, '整理澄木 A7 的交接報告\n保存核對結果。')
        a.run_goal(self.store, self.sid, old, ScriptedEngine(action('ask', question='本批封條顏色是什麼？')))
        progress = a.get_goal(self.store, self.sid, old)
        for title in ('青鳥 J5 的架號', '港口領取清單', '澄木 A70 的交接報告'):
            extra = a.create_goal(self.store, self.sid, title)
            a.run_goal(self.store, self.sid, extra, ScriptedEngine(action('ask', question='另一件的接收人是誰？')))
        self.assertNotIn(old, [row['id'] for row in goal_context(self.store, self.sid)['goals']])
        question = '回到澄木Ａ７。請問目前必須回答哪個問題？'
        app = Assistant(self.store, ScriptedEngine('本批封條顏色是什麼？'))
        answer = app.ask(self.sid, question)
        saved = self.store.answer_context(answer['id'])
        status = saved['autonomous_goals']
        self.assertEqual(status['reference'], {'name': '澄木Ａ７', 'status': 'matched'})
        job, = status['goals']
        self.assertEqual((job['id'], job['next_action'], job['recorded_question']),
                         (old, 'reply', '本批封條顏色是什麼？'))
        self.assertEqual(a.get_goal(self.store, self.sid, old), progress)
        copy = self.root / 'named-task-copy'
        archive = create_backup(self.store.path, self.root / 'named-task.paibackup')
        restore_backup(archive['path'], copy)
        restored = Store(copy / 'assistant.sqlite3')
        self.assertEqual(goal_context(restored, self.sid, question), status)
        view = goal_replies.snapshot(restored, self.sid, old)
        goal_replies.save(restored, self.sid, old, '銀灰。', expected_fingerprint=view['fingerprint'])
        self.assertEqual(goal_context(restored, self.sid, question)['goals'][0]['next_action'], 'resume')
        self.assertEqual(restored.answer_context(answer['id']), saved)
        self.assertEqual(a.get_goal(restored, self.sid, extra)['question'], '另一件的接收人是誰？')

    def test_named_candidates_require_disambiguation_and_never_import_cross_session(self):
        # The cap cannot turn many matches into an automatic selection.
        jobs = [a.create_goal(self.store, self.sid, title) for title in
                ['水門 B6 的交接', '水門 B6 的盤點', '水門 B6 的領取', '水門 B6 的清理']]
        for gid in jobs:
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='本件的專屬資料？')))
        different = a.create_goal(self.store, self.sid, '水門 B60 的交接')
        other_sid = self.store.new_session()
        foreign = a.create_goal(self.store, other_sid, '外橋 K2 的交接')
        a.run_goal(self.store, other_sid, foreign, ScriptedEngine(action('ask', question='另一個對話的待答資料？')))
        query = '查看「水門 B6」的工作目前在等哪一條回覆？'
        match = goal_context(self.store, self.sid, query)
        self.assertEqual(match['reference']['status'], 'ambiguous')
        self.assertTrue(match['limited'])
        self.assertEqual([row['id'] for row in match['goals']], list(reversed(jobs[1:])))
        self.assertTrue(all(not row['recorded_question'] and row['created_at'] for row in match['goals']))
        for name in ('外橋 K2', foreign):
            missing = goal_context(self.store, self.sid, f'查看目標「{name}」目前狀態？')
            self.assertEqual(missing, {'goals': [], 'limited': False,
                                       'reference': {'name': name, 'status': 'not_found'}})
        for name in ('水門 B6 的交接', jobs[0]):
            found = goal_context(self.store, self.sid, f'查看目標「{name}」目前狀態？')
            self.assertEqual(found['reference']['status'], 'matched')
            self.assertEqual([row['id'] for row in found['goals']], jobs[:1])
        for ordinary in ('不要回到「水門 B6」，現在缺什麼？',
                         '回到「水門 B6」這個話題，現在還缺什麼證據？',
                         '「水門 B6」與「水門 B60」目前進度如何？'):
            default = goal_context(self.store, self.sid, ordinary)
            self.assertNotIn('reference', default)
            self.assertEqual(default['goals'][0]['id'], different)
        isolated = Assistant(self.store, None).prepare_context(self.sid, '只根據這則訊息作答。' + query)
        self.assertNotIn('autonomous_goals', isolated)

    def test_fixed_controlled_fixtures_and_chat_projection(self):
        cases = json.loads(SUITE.read_text(encoding='utf-8'))
        self.assertEqual(len(cases), 5)
        for i, case in enumerate(cases):
            root = self.root/str(i)
            root.mkdir()
            store = Store(root/'fixture.sqlite3')
            sid, gid = prepare(store, case)
            before = a.get_goal(store, sid, gid)
            ctx = Assistant(store, None).prepare_context(sid, case['prompt'])
            self.assertEqual(ctx['current_request'], case['prompt'])
            self.assertEqual(a.get_goal(store, sid, gid), before)
            self.assertNotIn('PRIVATE_REPORT_DO_NOT_REPLAY', json.dumps(ctx))
            self.assertEqual(ctx['autonomous_goals']['goals'][0]['id'], gid)


if __name__ == '__main__':
    unittest.main()
