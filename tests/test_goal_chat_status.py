import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import answer_runs, autonomy as a, goal_guidance, goal_replies
from assistant.autonomy_status import chat_goal_target, chat_reply_draft, goal_context
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError
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

    def _candidate_answer(self, *, numbered=True, omit_last=False):
        group = '水門 B6'
        titles = [group + ' 的交接報告', group + ' 的夜間盤點']
        questions = ['封條顏色是什麼？', '第二組盤點架號是多少？']
        gids = []
        for title, question in zip(titles, questions, strict=True):
            gid = a.create_goal(self.store, self.sid, title)
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
            gids.append(gid)
        for title in ('清掃紀錄', '借用紀錄', '水門 B60 的夜間盤點'):
            a.create_goal(self.store, self.sid, title)
        offered = ['「' + title + '」' for title in (titles[:1] if omit_last else titles)]
        text = ('\n'.join(f'{i}. {title}' for i, title in enumerate(offered, 1)) if numbered
                else '還是'.join(offered))
        app = Assistant(self.store, ScriptedEngine('你指哪一件？\n' + text))
        answer = app.ask(self.sid, f'查看「{group}」目前等我回答哪個問題？')
        return gids, questions, answer

    def test_short_choice_keeps_original_id_and_displays_latest_state_after_restore(self):
        gids, questions, prior = self._candidate_answer()
        self.assertIsNone(chat_goal_target(self.store, self.sid, prior['id']))
        question = '夜間盤點那件，現在要補哪項資料？'
        snapshot = goal_context(self.store, self.sid, question)
        self.assertEqual(snapshot['reference']['selection'], {'answer_id': prior['id'], 'label': '夜間盤點', 'goal_id': gids[1]})
        self.assertEqual(snapshot['goals'][0]['recorded_question'], questions[1])
        numbered = goal_context(self.store, self.sid, '選第一件，目前等我回答哪一個問題？')
        self.assertEqual(numbered['goals'][0]['id'], gids[0])
        self.assertEqual(numbered['goals'][0]['recorded_question'], questions[0])
        # A later goal with the same exact title was not among the offered choices.
        duplicate = a.create_goal(self.store, self.sid, '水門 B6 的夜間盤點')
        self.assertEqual(goal_context(self.store, self.sid, question), snapshot)

        copy = self.root / 'goal-choice-copy'
        archive = create_backup(self.store.path, self.root / 'goal-choice.paibackup')
        restore_backup(archive['path'], copy)
        restored = Store(copy / 'assistant.sqlite3')
        self.assertEqual(goal_context(restored, self.sid, question), snapshot)
        view = goal_replies.snapshot(restored, self.sid, gids[1])
        goal_replies.save(restored, self.sid, gids[1], '架號為 27 號。', expected_fingerprint=view['fingerprint'])
        answer = Assistant(restored, ScriptedEngine('已收到補充，可接續。')).ask(self.sid, question)
        current = restored.answer_context(answer['id'])['autonomous_goals']
        self.assertEqual(current['reference']['selection'], snapshot['reference']['selection'])
        self.assertEqual((current['goals'][0]['next_action'], current['goals'][0]['recorded_question']), ('resume', ''))
        self.assertEqual(chat_goal_target(restored, self.sid, answer['id']), gids[1])
        self.assertIsNone(chat_goal_target(restored, self.sid, prior['id']))
        self.assertIsNone(chat_goal_target(restored, restored.new_session(), answer['id']))
        self.assertEqual(a.get_goal(restored, self.sid, duplicate)['status'], 'pending')
        self.assertEqual(a.get_goal(restored, self.sid, gids[0])['question'], questions[0])
        # Even matching mirrored answers cannot substitute a different goal for
        # the one selected from the previous visible list.
        substituted = restored.answer_context(answer['id'])
        selected = substituted['autonomous_goals']
        selected['reference']['selection']['goal_id'] = duplicate
        selected['goals'][0]['id'] = duplicate
        text = json.dumps(substituted, ensure_ascii=False)
        with restored.connect() as db:
            db.execute('UPDATE answer_runs SET context_json=? WHERE answer_message_id=?', (text, answer['id']))
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (text, answer['id']))
        self.assertIsNone(chat_goal_target(restored, self.sid, answer['id']))

    def test_nonnumbered_or_unshown_options_cannot_select_and_intervening_turn_ends_scope(self):
        gids, _, prior = self._candidate_answer(numbered=False)
        ordinal = goal_context(self.store, self.sid, '選第二件。')
        self.assertEqual(ordinal['reference']['status'], 'ambiguous')
        self.assertNotIn('goal_id', ordinal['reference']['selection'])
        self.assertEqual({row['id'] for row in ordinal['goals']}, set(gids))
        self.assertTrue(all(not row['recorded_question'] for row in ordinal['goals']))
        short = '夜間盤點那件，現在還缺什麼？'
        self.assertEqual(goal_context(self.store, self.sid, short)['reference']['selection']['goal_id'], gids[1])
        self.assertNotIn('reference', goal_context(self.store, self.sid, '不要選夜間盤點那件。'))
        # Changing only the final saved context cannot introduce or change a choice.
        with self.store.connect() as db:
            db.execute("UPDATE answer_contexts SET payload='{}' WHERE message_id=?", (prior['id'],))
        self.assertNotIn('reference', goal_context(self.store, self.sid, short))
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=(SELECT context_json FROM answer_runs WHERE answer_message_id=?) '
                       'WHERE message_id=?', (prior['id'], prior['id']))
        self.store.add_message(self.sid, 'user', '換個話題，談日落。')
        self.assertNotIn('reference', goal_context(self.store, self.sid, short))

        self._candidate_answer(omit_last=True)
        missing = goal_context(self.store, self.sid, short)
        self.assertNotIn('reference', missing)
        other = self.store.new_session()
        self.assertNotIn('reference', goal_context(self.store, other, short))

    def test_chat_draft_requires_exact_user_span_and_original_question_generation(self):
        gid = a.create_goal(self.store, self.sid, '北橋 K8 的交接')
        question = '封條顏色是什麼？'
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
        quote = '封條顏色為銀灰。'
        request = '查看「北橋 K8」現在等我回答哪個問題？🙂 ' + quote
        answer = Assistant(self.store, ScriptedEngine('請核對原文。')).ask(self.sid, request)
        mid = answer['user_message_id']
        offset = request.index(quote)
        before = a.get_goal(self.store, self.sid, gid)
        draft = chat_reply_draft(self.store, self.sid, answer['id'], mid, offset, quote)
        self.assertEqual((draft['goal_id'], draft['text'], draft['user_message_id']), (gid, quote, mid))
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
        for wrong_mid, wrong_offset, wrong_text in (
                (answer['id'], offset, quote), (mid, offset + 1, quote), (mid, offset, '封條顏色為紅色。')):
            with self.assertRaisesRegex(ValueError, '使用者訊息'):
                chat_reply_draft(self.store, self.sid, answer['id'], wrong_mid, wrong_offset, wrong_text)
        with self.assertRaisesRegex(ValueError, '使用者訊息'):
            chat_reply_draft(self.store, self.store.new_session(), answer['id'], mid, offset, quote)

        goal_replies.save(self.store, self.sid, gid, '暫用紅色。', expected_fingerprint=draft['fingerprint'])
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
        with self.assertRaisesRegex(ValueError, '已變更'):
            chat_reply_draft(self.store, self.sid, answer['id'], mid, offset, quote)
        self.assertEqual(chat_goal_target(self.store, self.sid, answer['id']), gid)

    def test_chat_draft_cannot_use_an_unchanged_ask_after_sources_change(self):
        path = self.root / 'stock.txt'
        path.write_text('封條按本批清單核對。', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        gid = a.create_goal(self.store, self.sid, '松臺 C2 的點收')
        question = '封條顏色是什麼？'
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
        quote = '封條顏色為銀灰。'
        request = '查看「松臺 C2」目前等我回答哪個問題？' + quote
        answer = Assistant(self.store, ScriptedEngine(question)).ask(self.sid, request)
        path.write_text('封條改用另一批。', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        before = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(before['question'], question)
        with self.assertRaisesRegex(ValueError, '已變更'):
            chat_reply_draft(self.store, self.sid, answer['id'], answer['user_message_id'], request.index(quote), quote)
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)

    def test_choice_rechecked_before_recording_and_keeps_same_source_on_retry(self):
        gids, _, prior = self._candidate_answer()
        prompt = '夜間盤點那件。'
        app = Assistant(self.store, ScriptedEngine('可接續。'))
        prepared = app.prepare_context(self.sid, prompt)
        count = len(self.store.messages(self.sid))
        view = goal_replies.snapshot(self.store, self.sid, gids[1])
        goal_replies.save(self.store, self.sid, gids[1], '架號為 27 號。', expected_fingerprint=view['fingerprint'])
        with self.assertRaisesRegex(ValueError, '狀態已變更'):
            answer_runs.begin(self.store, self.sid, 'a' * 32, prompt, prepared)
        self.assertEqual(len(self.store.messages(self.sid)), count)

        class Fail:
            def answer(self, prompt, cancel=None):
                raise EngineError('合成失敗', 'process_failed')

        with self.assertRaises(EngineError):
            Assistant(self.store, Fail()).ask(self.sid, prompt)
        retried = app.retry(self.sid)
        reference = self.store.answer_context(retried['id'])['autonomous_goals']['reference']
        self.assertEqual(reference['selection']['answer_id'], prior['id'])
        self.assertEqual(reference['selection']['goal_id'], gids[1])


if __name__ == '__main__':
    unittest.main()
