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

    def test_colloquial_chat_returns_to_the_older_named_goal(self):
        title = '晴溪 R7 的週三交接'
        target = a.create_goal(self.store,self.sid,title)
        needed = '請提供週三晚班使用的吊牌字樣。'
        a.run_goal(self.store,self.sid,target,ScriptedEngine(action('ask',question=needed)),max_steps=1)
        for other in ('晴溪 R7 的週四交接','備品櫃調查','晴溪 R70 的週三交接'):
            a.create_goal(self.store,self.sid,other)
        self.store.add_message(self.sid,'user','換個話題，談晴天和跑步。')
        self.store.add_message(self.sid,'assistant','可以聊跑步。')
        self.assertNotIn(target,[row['id'] for row in goal_context(self.store,self.sid)['goals']])
        query = f'對了，{title}那件，現在還需要我回覆哪個欄位？請照原待答問題回答。'
        recorded = a.get_goal(self.store,self.sid,target)
        answer = Assistant(self.store,ScriptedEngine(needed)).ask(self.sid,query)
        saved = self.store.answer_context(answer['id'])
        snapshot = saved['autonomous_goals']
        self.assertEqual(snapshot['reference'],{'name':title,'status':'matched'})
        self.assertEqual([row['recorded_question'] for row in snapshot['goals']],[needed])
        self.assertEqual(chat_goal_target(self.store,self.sid,answer['id']),target)
        self.assertEqual(a.get_goal(self.store,self.sid,target),recorded)

        copy = self.root/'natural-status-copy'
        archive = create_backup(self.store.path,self.root/'natural-status.paibackup')
        restore_backup(archive['path'],copy)
        restored = Store(copy/'assistant.sqlite3')
        view = goal_replies.snapshot(restored,self.sid,target)
        goal_replies.save(restored,self.sid,target,'週三晚班吊牌寫紫苑。',
                          expected_fingerprint=view['fingerprint'])
        live = goal_context(restored,self.sid,query)
        self.assertEqual(live['goals'][0]['next_action'],'resume')
        self.assertEqual(live['goals'][0]['recorded_question'],'')
        self.assertEqual(restored.answer_context(answer['id']),saved)

    def test_natural_ambiguity_and_visible_choice_precede_other_title_matches(self):
        prefix = '晴溪 R7'
        titles = (prefix+' 的週三交接',prefix+' 的週四交接')
        jobs = [a.create_goal(self.store,self.sid,title) for title in titles]
        for gid in jobs:
            a.run_goal(self.store,self.sid,gid,ScriptedEngine(action('ask',question='本件的專屬未答欄位？')),max_steps=1)
        decoy = a.create_goal(self.store,self.sid,'週四交接')
        query = f'{prefix}那件工作，現在還需要我回覆哪個欄位？'
        listed = f'你指哪一件？\n1. 「{titles[1]}」\n2. 「{titles[0]}」'
        anchor = Assistant(self.store,ScriptedEngine(listed)).ask(self.sid,query)
        context = self.store.answer_context(anchor['id'])['autonomous_goals']
        self.assertEqual(context['reference'],{'name':prefix,'status':'ambiguous'})
        self.assertEqual({row['id'] for row in context['goals']},set(jobs))
        self.assertTrue(all(not row['recorded_question'] for row in context['goals']))
        choice = goal_context(self.store,self.sid,'對了，週四交接那件，現在還缺什麼？')
        self.assertEqual(choice['reference']['selection'],
                         {'answer_id':anchor['id'],'label':'週四交接','goal_id':jobs[1]})
        self.assertNotEqual(choice['goals'][0]['id'],decoy)
        for reply in ('1','１'):
            numbered = goal_context(self.store,self.sid,reply)
            self.assertEqual(numbered['reference']['selection']['answer_id'],anchor['id'])
            self.assertEqual(numbered['reference']['selection']['goal_id'],jobs[1])
        number = goal_context(self.store,self.sid,'3')
        self.assertEqual(number['reference']['status'],'ambiguous')
        self.assertNotIn('goal_id',number['reference']['selection'])
        # A new independent, fully named request still resolves its own title.
        second = a.create_goal(self.store,self.sid,'碧澤 M3 交接')
        explicit = goal_context(self.store,self.sid,'碧澤 M3 交接這件，目前進度如何？')
        self.assertEqual([row['id'] for row in explicit['goals']],[second])
        self.store.add_message(self.sid,'user','換個話題。')
        self.assertNotIn('reference',goal_context(self.store,self.sid,'1'))

    def test_natural_named_status_respects_exact_codes_and_leaves_plain_topics_alone(self):
        local = a.create_goal(self.store,self.sid,'晴溪 R70 的週三交接')
        other = self.store.new_session()
        a.create_goal(self.store,other,'晴溪 R71 的週三交接')
        no_match = goal_context(self.store,self.sid,'對了，晴溪 Ｒ７１ 的週三交接那件，現在還在等我回覆哪個欄位？')
        self.assertEqual(no_match,{'goals':[],'limited':False,
                                   'reference':{'name':'晴溪 Ｒ７１ 的週三交接','status':'not_found'}})
        for ordinary in (
            '回到剛才跑步那個話題，用一句話說明怎麼熱身。',
            '如果是晴溪 R70 的週三交接那件，現在還缺哪項資料？',
            '不要查晴溪 R70 的週三交接那件，現在還缺哪項資料？',
            '例如「晴溪 R70 的週三交接那件，現在還缺哪項資料？」',
            '晴溪 R70 的週三交接那件，現在請幫我完成。',
            '晴溪 R70 或 晴溪 R71 那件，現在還缺哪項資料？',
        ):
            with self.subTest(ordinary=ordinary):
                default = goal_context(self.store,self.sid,ordinary)
                self.assertNotIn('reference',default)
                self.assertEqual(default['goals'][0]['id'],local)
        mechanical = '清點這個工具流程'
        local_other = a.create_goal(self.store,self.sid,mechanical)
        exact = goal_context(self.store,self.sid,mechanical+'那件，現在卡在哪裡？')
        self.assertEqual(exact['goals'][0]['id'],local_other)

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


    def test_saved_field_reads_latest_scoped_source_without_replaying_notes(self):
        title = '榛庭 W3 的投影點交'
        gid = a.create_goal(self.store, self.sid, title)
        decoy = a.create_goal(self.store, self.sid, title.replace('W3', 'W30'))
        ask = '請提供東廳使用的校驗碼。'
        for target, value in ((gid, 'SUN-14'), (decoy, 'MOON-89')):
            a.run_goal(self.store, self.sid, target, ScriptedEngine(action('ask', question=ask)))
            reply = goal_replies.snapshot(self.store, self.sid, target)
            goal_replies.save(self.store, self.sid, target, value, expected_fingerprint=reply['fingerprint'])
        query = f'查詢「{title}」最後已確認的校驗碼是什麼？'
        app = Assistant(self.store, ScriptedEngine('SUN-14'))
        answer = app.ask(self.sid, query)
        saved = self.store.answer_context(answer['id'])
        original = saved['autonomous_goals']['saved_field']
        self.assertEqual((original['goal_id'], original['value'], original['citation']),
                         (gid, 'SUN-14', '[補充#1]'))
        self.assertEqual(chat_goal_target(self.store, self.sid, answer['id']), gid)

        for text in ('正式更正：榛庭 W3 的東廳校驗碼改為 SKY-21。',
                     '如果東廳校驗碼改為 RAIN-77，這只是提議。',
                     '正式更正：榛庭 W30 的東廳校驗碼改為 CLOUD-23。',
                     '西廳的校驗碼正式改為 WIND-13。',
                     '原文：「東廳的校驗碼正式改為 SNOW-88。」'):
            view = goal_guidance.snapshot(self.store, self.sid, gid)
            goal_guidance.save(self.store, self.sid, gid, text, expected_fingerprint=view['fingerprint'])

        self.store.add_message(self.sid, 'user', '先記一份草稿：校驗碼 STAR-10。')
        before = a.get_goal(self.store, self.sid, gid)
        query = f'{title}那件，進度如何？這一則先談草稿 STAR-20，請只回報最後正式保存的「校驗碼」。'
        context = goal_context(self.store, self.sid, query)
        field = context['saved_field']
        self.assertEqual((field['status'], field['value'], field['citation']), ('confirmed', 'SKY-21', '[更正#1]'))
        self.assertNotEqual(field['sha256'], original['sha256'])
        projection = json.dumps(context, ensure_ascii=False)
        for private in ('MOON-89', 'SUN-14', 'RAIN-77', 'CLOUD-23', 'WIND-13', 'SNOW-88', 'STAR-20'):
            self.assertNotIn(private, projection)
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
        self.assertNotIn('saved_field', goal_context(self.store, self.sid, f'{title}那件，進度如何？'))
        self.assertEqual(self.store.answer_context(answer['id']), saved)

        backup = create_backup(self.store.path)
        restored = self.root/'verified'
        restore_backup(backup['path'], restored)
        restored_store = Store(restored/'assistant.sqlite3')
        self.assertEqual(goal_context(restored_store, self.sid, query), context)
        self.assertEqual(restored_store.answer_context(answer['id']), saved)

        import tkinter as tk
        from tkinter import ttk
        from assistant.context_ui import show_context
        root = tk.Tk()
        root.withdraw()
        try:
            show_context(root, restored_store, answer['id'])
            window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            window.withdraw()
            notebook = next(child for child in window.winfo_children() if isinstance(child, ttk.Notebook))
            tab = next(tab for tab in notebook.tabs() if notebook.tab(tab, 'text') == '工作欄位')
            text = next(child for child in notebook.nametowidget(tab).winfo_children() if isinstance(child, tk.Text))
            shown = text.get('1.0', 'end-1c')
            self.assertIn('SUN-14', shown)
            self.assertIn('[補充#1]', shown)
            self.assertIn(original['sha256'], shown)
            self.assertNotIn('SKY-21', shown)
        finally:
            root.destroy()

    def test_saved_field_distinguishes_missing_value_from_ambiguous_scope(self):
        title = '砂梨器材點交'
        gid = a.create_goal(self.store, self.sid, title)
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='請提供晨班的交件櫃。')))
        query = f'{title}那件，請只回報上次已確認的交件櫃。'
        view = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '如果晨班的交件櫃改用松色櫃，這只是提議。',
                           expected_fingerprint=view['fingerprint'])
        result = goal_context(self.store, self.sid, query)
        self.assertEqual(result['reference']['status'], 'matched')
        self.assertEqual(result['saved_field'], {'goal_id': gid, 'field': '交件櫃', 'status': 'unverified'})
        self.assertNotIn('松色櫃', json.dumps(result, ensure_ascii=False))
        view = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, '確定是「霜竹櫃」。', expected_fingerprint=view['fingerprint'])
        self.assertEqual(goal_context(self.store, self.sid, query)['saved_field']['value'], '霜竹櫃')

        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='請提供晚班的交件櫃。')))
        field = goal_context(self.store, self.sid, query)['saved_field']
        self.assertEqual(field['status'], 'needs_scope')
        self.assertEqual(field['scope_choices'], ['晨班', '晚班'])
        self.assertNotIn('value', field)
        a.create_goal(self.store, self.sid, title)
        self.assertEqual(goal_context(self.store, self.sid, query)['reference']['status'], 'ambiguous')
        self.assertNotIn('saved_field', goal_context(self.store, self.sid, query))

    def test_saved_field_scope_choice_uses_visible_prior_options_and_survives_restore(self):
        title = '雲苔 C4 的場地移交'
        gid = a.create_goal(self.store, self.sid, title)
        for scope, value in (('東區', 'E-14'), ('西區', 'W-18')):
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=f'請提供{scope}使用的接收代號。')))
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, f'{scope}的接收代號確定寫{value}。',
                              expected_fingerprint=view['fingerprint'])
        query = f'查詢「{title}」最後已確認的「接收代號」是什麼？'
        qualified = query.replace('「接收代號」', '「西區的接收代號」')
        field = goal_context(self.store, self.sid, qualified)['saved_field']
        self.assertEqual((field['value'], field['scope'], field['citation']), ('W-18', '西區', '[補充#2]'))

        choice = '西區那一個，請只回報代號。'
        Assistant(self.store, ScriptedEngine('1. 「西區」\n請問要查哪個區域？')).ask(self.sid, query)
        self.assertNotIn('saved_field', goal_context(self.store, self.sid, choice))
        offered = Assistant(self.store, ScriptedEngine('1. 「西區」\n2. 「東區」\n請問要查哪個範圍？')).ask(self.sid, query)
        snapshot = self.store.answer_context(offered['id'])
        shown_field = snapshot['autonomous_goals']['saved_field']
        self.assertEqual(shown_field['scope_choices'], ['東區', '西區'])
        self.assertNotIn('value', shown_field)
        for question in ('不要選西區。', '西區或東區。', '西區那個，只回報簽收人。', '3', '先只看這一則訊息。西區'):
            self.assertNotIn('saved_field', goal_context(self.store, self.sid, question))
        other = self.store.new_session()
        self.assertNotIn('saved_field', goal_context(self.store, other, choice))
        for pick in (choice, '１'):
            field = goal_context(self.store, self.sid, pick)['saved_field']
            self.assertEqual((field['value'], field['scope_selection']['answer_id']), ('W-18', offered['id']))

        archive = create_backup(self.store.path)
        target = self.root/'scope'
        restore_backup(archive['path'], target)
        restored = Store(target/'assistant.sqlite3')
        app = Assistant(restored, ScriptedEngine('W-20'))
        prepared = app.prepare_context(self.sid, choice)
        for text in ('東區的接收代號正式改為 E-16。', '西區的接收代號正式改為 W-20。'):
            current = goal_guidance.snapshot(restored, self.sid, gid)
            goal_guidance.save(restored, self.sid, gid, text, expected_fingerprint=current['fingerprint'])
        with self.assertRaisesRegex(ValueError, '保存來源已變更'):
            answer_runs.begin(restored, self.sid, 'b' * 32, choice, prepared)
        state = a.get_goal(restored, self.sid, gid)
        answer = app.ask(self.sid, choice)
        field = restored.answer_context(answer['id'])['autonomous_goals']['saved_field']
        self.assertEqual((field['value'], field['scope'], field['citation']), ('W-20', '西區', '[更正#2]'))
        self.assertEqual(a.get_goal(restored, self.sid, gid), state)
        self.assertEqual(chat_goal_target(restored, self.sid, answer['id']), gid)
        self.assertEqual(restored.answer_context(offered['id']), snapshot)
        self.assertNotIn('saved_field', goal_context(restored, self.sid, choice))

        current = goal_guidance.snapshot(restored, self.sid, gid)
        goal_guidance.save(restored, self.sid, gid, '接收代號正式改為 ANY-1。', expected_fingerprint=current['fingerprint'])
        field = goal_context(restored, self.sid, qualified)['saved_field']
        self.assertEqual(field['status'], 'unverified')
        self.assertNotIn('value', field)
        self.assertEqual(restored.answer_context(answer['id'])['autonomous_goals']['saved_field']['value'], 'W-20')

    def test_compare_saved_work_fields_preserves_independent_sources_and_unknown_rows(self):
        first_title = '栗原 N4 的轉場驗收'
        second_title = '白棠 A8 的配送退件'
        first = a.create_goal(self.store, self.sid, first_title)
        second = a.create_goal(self.store, self.sid, second_title)
        decoy = a.create_goal(self.store, self.sid, second_title.replace('A8', 'A80'))
        for goal, ask, reply in ((first, '請提供第一站所用的覆核人。', '第一站的覆核人確定為許楠。'),
                                 (second, '請提供轉運箱代號。', None),
                                 (decoy, '請提供轉運箱代號。', 'DECOY-27')):
            a.run_goal(self.store, self.sid, goal, ScriptedEngine(action('ask', question=ask)))
            if reply:
                view = goal_replies.snapshot(self.store, self.sid, goal)
                goal_replies.save(self.store, self.sid, goal, reply, expected_fingerprint=view['fingerprint'])
        query = (f'請比較「{first_title}」的「第一站的覆核人」與「{second_title}」的「轉運箱代號」'
                 '最後已確認的欄值。每件一行，找不到就填待確認。')
        view = goal_guidance.snapshot(self.store, self.sid, second)
        goal_guidance.save(self.store, self.sid, second, '提議：轉運箱代號改為 DRAFT-18。',
                           expected_fingerprint=view['fingerprint'])

        app = Assistant(self.store, ScriptedEngine('許楠\n待確認'))
        prepared = app.prepare_context(self.sid, query)
        partial = prepared['autonomous_goals']
        self.assertEqual(partial['goals'], [])
        self.assertEqual([(row['goal_id'], row['status']) for row in partial['saved_work_fields']],
                         [(first, 'confirmed'), (second, 'unverified')])
        self.assertEqual(partial['saved_work_fields'][0]['citation'], '[補充#1]')
        self.assertNotIn('value', partial['saved_work_fields'][1])
        self.assertNotIn('DRAFT-18', json.dumps(partial, ensure_ascii=False))
        self.assertNotIn('DECOY-27', json.dumps(partial, ensure_ascii=False))
        before = [a.get_goal(self.store, self.sid, goal) for goal in (first, second, decoy)]
        answer = app.ask(self.sid, query)
        self.assertIsNone(chat_goal_target(self.store, self.sid, answer['id']))
        historic = self.store.answer_context(answer['id'])
        self.assertEqual(historic['autonomous_goals'], partial)
        self.assertEqual([a.get_goal(self.store, self.sid, goal) for goal in (first, second, decoy)], before)

        view = goal_guidance.snapshot(self.store, self.sid, second)
        goal_guidance.save(self.store, self.sid, second, '轉運箱代號正式改為 BOX-44。',
                           expected_fingerprint=view['fingerprint'])
        with self.assertRaisesRegex(ValueError, '保存來源已變更'):
            answer_runs.begin(self.store, self.sid, 'b' * 32, query, prepared)
        current = goal_context(self.store, self.sid, query)['saved_work_fields']
        self.assertEqual([(row['value'], row['citation']) for row in current], [('許楠', '[補充#1]'), ('BOX-44', '[更正#2]')])
        archive = create_backup(self.store.path)
        restored_dir = self.root/'compare'
        restore_backup(archive['path'], restored_dir)
        restored = Store(restored_dir/'assistant.sqlite3')
        self.assertEqual(goal_context(restored, self.sid, query)['saved_work_fields'], current)
        self.assertEqual(restored.answer_context(answer['id']), historic)

    def test_compare_saved_work_fields_keeps_requested_order_and_isolates_each_match(self):
        known = a.create_goal(self.store, self.sid, '青嶼 M8 場地領件')
        a.run_goal(self.store, self.sid, known, ScriptedEngine(action('ask', question='請提供確認代號。')))
        reply = goal_replies.snapshot(self.store, self.sid, known)
        goal_replies.save(self.store, self.sid, known, 'K-19', expected_fingerprint=reply['fingerprint'])
        for _ in range(2):
            a.create_goal(self.store, self.sid, '河紋晚場退件')
        other_session = self.store.new_session()
        a.create_goal(self.store, other_session, '雨泉外借交接')

        request = ('對了，請並排核對以下三件工作的最後正式確認值。\n'
                   '『雨泉外借交接』的『領用人』。\n'
                   '「青嶼 M8 場地領件」的「確認代號」。\n'
                   '「河紋晚場退件」的「退件處」。\n每件一行。')
        comparison = goal_context(self.store, self.sid, request)['saved_work_fields']
        self.assertEqual([row['status'] for row in comparison], ['not_found', 'confirmed', 'ambiguous_work'])
        self.assertEqual(comparison[1]['value'], 'K-19')
        self.assertNotIn('goal_id', comparison[0])
        self.assertNotIn('goal_id', comparison[2])
        for prompt in ('如果要' + request, '範例：' + request, request.replace('三件', '兩件'),
                       request.replace('每件一行。', '每件一行。取消查詢。'),
                       request.replace('「河紋晚場退件」的「退件處」。', '「河紋晚場退件」。')):
            self.assertNotIn('saved_work_fields', goal_context(self.store, self.sid, prompt))

    def test_saved_field_rechecks_its_source_before_recording_chat(self):
        title = '月台送件'
        gid = a.create_goal(self.store, self.sid, title)
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question='封存人是誰？')))
        query = f'{title}那件，進度如何？上次已確認的封存人是誰？'
        guide = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '封存人正式確認為周寧。', expected_fingerprint=guide['fingerprint'])
        app = Assistant(self.store, ScriptedEngine('周寧'))
        prepared = app.prepare_context(self.sid, query)
        self.assertEqual(prepared['autonomous_goals']['saved_field']['value'], '周寧')
        guide = goal_guidance.snapshot(self.store, self.sid, gid)
        goal_guidance.save(self.store, self.sid, gid, '撤回「封存人」的正式確認。',
                           expected_fingerprint=guide['fingerprint'])
        self.assertEqual(goal_context(self.store, self.sid, query)['saved_field']['status'], 'unverified')
        count = len(self.store.messages(self.sid))
        with self.assertRaisesRegex(ValueError, '保存來源已變更'):
            answer_runs.begin(self.store, self.sid, 'a' * 32, query, prepared)
        self.assertEqual(len(self.store.messages(self.sid)), count)
        for text, expected in (('封存人正式確認為林真。', '林真'), ('封存人尚未確認。', None)):
            guide = goal_guidance.snapshot(self.store, self.sid, gid)
            goal_guidance.save(self.store, self.sid, gid, text, expected_fingerprint=guide['fingerprint'])
            self.assertEqual(goal_context(self.store, self.sid, query)['saved_field'].get('value'), expected)


if __name__ == '__main__':
    unittest.main()
