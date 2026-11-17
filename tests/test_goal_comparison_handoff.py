import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import artifacts, autonomy, goal_comparison_handoff, goal_conversation, goal_guidance, goal_replies
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ComparisonHandoffTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.names = ['春桃 A2 的置物點交', '春桃 A20 的樣品核收']
        self.fields = ['箱號', '驗收台']
        self.parents = []
        for work, field, value in zip(self.names, self.fields, ['B-22', '榆光台']):
            gid = autonomy.create_goal(self.store, self.sid, work)
            self.parents.append(gid)
            autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=f'請提供{field}。')))
            view = goal_replies.snapshot(self.store, self.sid, gid)
            goal_replies.save(self.store, self.sid, gid, value, expected_fingerprint=view['fingerprint'])
        pairs = '和'.join(f'「{work}」的「{field}」' for work, field in zip(self.names, self.fields))
        question = f'比較{pairs}最後已確認的欄值。每件一行。'
        self.answer = Assistant(self.store, ScriptedEngine('歷史比較內容不可代替來源。')).ask(self.sid, question)
        self.objective = ('沿用剛才的比較表。交付 JSON 陣列，每列只包含'
                          '「工作」、「欄位」、「範圍」、「確認值」、「來源種類」、「來源編號」。')

    def rows(self, value='榆光台', kind='補充'):
        return json.dumps([
            {'工作': self.names[0], '欄位': self.fields[0], '範圍': '', '確認值': 'B-22', '來源種類': '補充', '來源編號': 1},
            {'工作': self.names[1], '欄位': self.fields[1], '範圍': '', '確認值': value, '來源種類': kind, '來源編號': 1},
        ], ensure_ascii=False)

    def test_comparison_requires_explicit_bring_in_and_can_limit_rows(self):
        for options, title in (({}, self.objective),
                               ({'from_chat': True}, '整理一份待辦清單。'),
                               ({'from_chat': True}, self.objective + '不要沿用剛才的比較表。')):
            gid = autonomy.create_goal(self.store, self.sid, title, **options)
            snapshot = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
            self.assertNotIn(goal_comparison_handoff.BASIS, snapshot or {})
        name, field = self.names[1], self.fields[1]
        gid = autonomy.create_goal(self.store, self.sid, f'沿用剛才的比較表。只帶入「{name}」的「{field}」。',
                                   from_chat=True)
        snapshot = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        basis = snapshot[goal_comparison_handoff.BASIS]
        self.assertEqual((basis['answer_id'], basis['indices']), (self.answer['id'], [1]))
        prompt = ScriptedEngine(action('finish', report='榆光台'))
        autonomy.run_goal(self.store, self.sid, gid, prompt, max_steps=1)
        payload = json.loads(prompt.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertEqual([row['goal_id'] for row in payload['verified_work_fields']], [self.parents[1]])
        self.assertNotIn('B-22', json.dumps(payload['verified_work_fields'], ensure_ascii=False))

    def test_comparison_with_uncounted_list_binds_latest_answer_on_restore(self):
        pairs = '\n'.join(f'「{work}」的「{field}」' for work, field in zip(self.names, self.fields))
        question = f'請對照下列任務各自最後一次已正式保存的欄值：\n{pairs}\n每件一行。'
        answer = Assistant(self.store, ScriptedEngine('B-22\n榆光台')).ask(self.sid, question)
        context = self.store.answer_context(answer['id'])
        self.assertEqual(context['current_request'], question)
        rows = context['autonomous_goals']['saved_work_fields']
        self.assertEqual([row['goal_id'] for row in rows], self.parents)
        self.assertEqual([row['value'] for row in rows], ['B-22', '榆光台'])

        gid = autonomy.create_goal(self.store, self.sid, self.objective, from_chat=True, file_delivery=True)
        snapshot = autonomy.get_goal(self.store, self.sid, gid)
        basis = snapshot['conversation_context'][goal_comparison_handoff.BASIS]
        self.assertEqual(basis['answer_id'], answer['id'])
        self.assertEqual(basis['indices'], [0, 1])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'natural')
        restored = Store(self.root / 'natural' / 'assistant.sqlite3')
        self.assertEqual(restored.answer_context(answer['id']), context)
        self.assertEqual(autonomy.get_goal(restored, self.sid, gid), snapshot)

    def test_latest_visible_table_supersedes_earlier_work_evidence(self):
        table = '| 方式 | 特點 |\n| --- | --- |\n| 紙本 | 免網路 |\n| 電子 | 可搜尋 |'
        objective = '沿用本對話最後一張比較表，做一頁摘要。'
        older = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        older_snapshot = autonomy.get_goal(self.store, self.sid, older)
        for request in ('把比較問題改成更口語的示例。', '請比較兩種入場方式，但只示範原始格式。'):
            answer = f'```markdown\n{table}\n```'
            Assistant(self.store, ScriptedEngine(answer)).ask(self.sid, request)
            held = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
            snapshot = autonomy.get_goal(self.store, self.sid, held)['conversation_context']
            self.assertEqual(snapshot[goal_comparison_handoff.BASIS]['answer_id'], self.answer['id'])

        request = '另一個主題，請比較紙本和電子入場表。'
        newest = Assistant(self.store, ScriptedEngine(table)).ask(self.sid, request)
        Assistant(self.store, ScriptedEngine('請帶筆記本。')).ask(self.sid, '請提醒我攜帶筆記本。')
        gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        held = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        self.assertNotIn(goal_comparison_handoff.BASIS, held)
        reference = held[goal_comparison_handoff.REFERENCE]
        self.assertEqual((reference['answer_id'], reference['request'], reference['answer']),
                         (newest['id'], request, table))

        work_goal = autonomy.create_goal(self.store, self.sid, '沿用最近兩件工作的比較表，整理來源。', from_chat=True)
        self.assertEqual(autonomy.get_goal(self.store, self.sid, work_goal)['conversation_context']
                         [goal_comparison_handoff.BASIS]['answer_id'], self.answer['id'])
        run = ScriptedEngine(action('finish', report='紙本免網路，電子可搜尋。'))
        final = autonomy.run_goal(self.store, self.sid, gid, run)
        payload = json.loads(run.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertNotIn('verified_work_fields', payload)
        self.assertEqual(payload['conversation_context'][goal_comparison_handoff.REFERENCE], reference)

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'latest-table')
        restored = Store(self.root / 'latest-table' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, gid), final)
        self.assertEqual(autonomy.get_goal(restored, self.sid, older), older_snapshot)
        with restored.connect() as db:
            held[goal_comparison_handoff.REFERENCE]['answer'] = table.replace('可搜尋', '可收費')
            db.execute('UPDATE autonomous_conversations SET context_json=?,context_sha256=? WHERE goal_id=?',
                       (json.dumps(held, ensure_ascii=False), goal_conversation.digest(held), gid))
        with self.assertRaisesRegex(ValueError, '比較回答快照'):
            validate_database(restored.path)

    def test_explicit_answer_number_binds_an_older_completed_comparison(self):
        table = '| 工序 | 適用時間 |\n| --- | --- |\n| 預約分流 | 開館前 |\n| 現場領號 | 開館後 |'
        question = '請比較預約分流和現場領號。'
        ordinary = Assistant(self.store, ScriptedEngine(table)).ask(self.sid, question)
        for _ in range(goal_conversation.MAX_MESSAGES):
            self.store.add_message(self.sid, 'user', '補充的是一般行程。')

        first = f'沿用回答 #{self.answer["id"]} 的比較表，整理保存欄位。'
        old = autonomy.create_goal(self.store, self.sid, first, from_chat=True)
        loaded = autonomy.get_goal(self.store, self.sid, old)['conversation_context']
        self.assertNotIn(self.answer['id'], [row['message_id'] for row in loaded['messages']])
        basis = loaded[goal_comparison_handoff.BASIS]
        self.assertEqual((basis['answer_id'], basis['version']), (self.answer['id'], 3))
        self.assertEqual(loaded[goal_comparison_handoff.REFERENCE]['answer_id'], self.answer['id'])

        second = f'沿用回答 #{ordinary["id"]} 的比較表，整理排隊說明。'
        new = autonomy.create_goal(self.store, self.sid, second, from_chat=True)
        snapshot = autonomy.get_goal(self.store, self.sid, new)
        context = snapshot['conversation_context']
        self.assertNotIn(goal_comparison_handoff.BASIS, context)
        self.assertEqual(context[goal_comparison_handoff.REFERENCE]['answer'], table)
        before = autonomy.goals(self.store, self.sid)
        foreign = self.store.new_session()
        elsewhere = Assistant(self.store, ScriptedEngine(table)).ask(foreign, question)
        for request in (first + second, second.replace(str(ordinary['id']), '999999'),
                        second.replace(f'#{ordinary["id"]}', f'#{elsewhere["id"]}')):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, '比較回答|指定的已完成回答'):
                autonomy.create_goal(self.store, self.sid, request, from_chat=True)
        self.assertEqual(autonomy.goals(self.store, self.sid), before)

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'pinned')
        restored = Store(self.root / 'pinned' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, new), snapshot)
        self.assertEqual(autonomy.get_goal(restored, self.sid, old)['conversation_context'], loaded)

    def test_latest_result_can_use_prose_and_table_request_keeps_its_form(self):
        table = '| 方式 | 特點 |\n| --- | --- |\n| 紙本 | 免網路 |\n| 電子 | 可搜尋 |'
        earlier = Assistant(self.store, ScriptedEngine(table)).ask(self.sid, '比較紙本和電子的適用情況。')
        request = '換個主題，請比較「伸縮旗」與「牆面箭頭」的適用情況。用兩句話。'
        prose = '伸縮旗較適合入口廣場。牆面箭頭適用於狹長走道。'
        later = Assistant(self.store, ScriptedEngine(prose)).ask(self.sid, request)
        for user, reply in (
            ('請抄一份示例答案。', prose),
            (request, '```markdown\n' + prose + '\n```'),
            (request, '資料不足，無法比較伸縮旗和牆面箭頭。伸縮旗適合哪裡？牆面箭頭適用於哪裡？'),
            (request, '伸縮旗適合入口廣場。牆面箭頭的資料還需要你提供。請提供牆面箭頭資料。'),
        ):
            Assistant(self.store, ScriptedEngine(reply)).ask(self.sid, user)

        result_request = '沿用本對話最後一次比較結果，整理交接摘要。'
        capture = goal_comparison_handoff.capture
        with patch('assistant.goal_comparison_handoff.capture',
                   side_effect=lambda *args: capture(*args, reference_version=1)):
            old = autonomy.create_goal(self.store, self.sid, result_request, from_chat=True)
        old_state = autonomy.get_goal(self.store, self.sid, old)
        old_reference = old_state['conversation_context'][goal_comparison_handoff.REFERENCE]
        self.assertEqual((old_reference['answer_id'], old_reference['version']), (earlier['id'], 1))

        options = ((result_request, later, request),
                   ('沿用本對話最後一張比較表，整理結果。', earlier, '比較紙本和電子的適用情況。'),
                   (f'沿用回答 #{later["id"]} 的比較結果，整理摘要。', later, request))
        captured = []
        for objective, chosen, original in options:
            with self.subTest(objective=objective):
                gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
                state = autonomy.get_goal(self.store, self.sid, gid)
                context = state['conversation_context']
                self.assertNotIn(goal_comparison_handoff.BASIS, context)
                ref = context[goal_comparison_handoff.REFERENCE]
                self.assertEqual(ref, {'answer_id': chosen['id'], 'user_id': chosen['user_message_id'],
                                       'request': original, 'answer': chosen['content'], 'version': 5})
                captured.append((gid, state))
        with self.assertRaisesRegex(ValueError, '比較形式'):
            autonomy.create_goal(self.store, self.sid, f'沿用回答 #{later["id"]} 的比較表。', from_chat=True)

        relation_request = '請比較「伸縮旗」和「牆面箭頭」的安裝時間。'
        relation = '牆面箭頭的安裝時間比伸縮旗少 7 分鐘。'
        relative = Assistant(self.store, ScriptedEngine(relation)).ask(self.sid, relation_request)
        Assistant(self.store, ScriptedEngine('牆面箭頭比伸縮旗快嗎？')).ask(self.sid, relation_request)
        gid = autonomy.create_goal(self.store, self.sid, result_request, from_chat=True)
        state = autonomy.get_goal(self.store, self.sid, gid)
        self.assertEqual(state['conversation_context'][goal_comparison_handoff.REFERENCE]['answer_id'], relative['id'])
        captured.append((gid, state))

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'prose')
        restored = Store(self.root / 'prose' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, old), old_state)
        for gid, state in captured:
            self.assertEqual(autonomy.get_goal(restored, self.sid, gid), state)

    def test_named_options_scope_keeps_only_matching_comparisons(self):
        older_table = '| 特點 | 導視牌 | 地板貼 |\n| --- | --- | --- |\n| 適用位置 | 樓梯口 | 走道 |\n| 來源 | 會議 | 實測 |'
        old = Assistant(self.store, ScriptedEngine(older_table)).ask(self.sid, '比較導視牌和地板貼。')
        newer_table = '| 方式 | 適用 |\n| --- | --- |\n| 導視牌 | 門口 |\n| 桌面卡 | 櫃台 |'
        Assistant(self.store, ScriptedEngine(newer_table)).ask(self.sid, '比較導視牌和桌面卡。')
        prose = '掛繩標籤適用於入口。腕帶標記適用於報到桌。'
        recent = Assistant(self.store, ScriptedEngine(prose)).ask(self.sid, '請比較掛繩標籤與腕帶標記的適用情況。')
        question = '請比較「臨時紙條」和「櫃台標記」。'
        Assistant(self.store, ScriptedEngine('這兩項資料還缺。可提供兩種方式的適用情況嗎？')).ask(self.sid, question)
        prefix = '沿用本對話最後一次比較結果'
        old_options = '，只整理「導視牌」與「地板貼」的適用位置。'
        recent_options = '。整理「腕帶標記」及「掛繩標籤」的適用情況。'

        capture = goal_comparison_handoff.capture
        with patch('assistant.goal_comparison_handoff.capture',
                   side_effect=lambda *args: capture(*args, reference_version=2)):
            old_goal = autonomy.create_goal(self.store, self.sid, prefix + old_options, from_chat=True)
        old_snapshot = autonomy.get_goal(self.store, self.sid, old_goal)
        previous = old_snapshot['conversation_context'][goal_comparison_handoff.REFERENCE]
        self.assertEqual((previous['answer_id'], previous['version']), (recent['id'], 2))

        expected = (
            (prefix + old_options, old),
            (prefix + recent_options, recent),
            ('沿用最後一張比較表：只整理「導視牌」與「地板貼」。', old),
            (prefix + '。範例「只整理『臨時紙條』和『櫃台標記』」。', recent),
            (f'沿用回答 #{old["id"]} 的比較結果' + old_options, old),
            (prefix + '，只整理「臨時紙條」與「櫃台標記」的適用情況。', None),
            (prefix + '，只整理「導視牌」和「腕帶標記」的適用位置。', None),
            (prefix + '，只整理「導視」和「地板貼」的適用位置。', None),
        )
        captured = []
        for objective, chosen in expected:
            with self.subTest(objective=objective):
                gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
                snapshot = autonomy.get_goal(self.store, self.sid, gid)
                context = snapshot['conversation_context']
                self.assertNotIn(goal_comparison_handoff.BASIS, context)
                if chosen is None:
                    self.assertNotIn(goal_comparison_handoff.REFERENCE, context)
                    self.assertIn(question, [item['content'] for item in context['messages']])
                else:
                    ref = context[goal_comparison_handoff.REFERENCE]
                    self.assertEqual((ref['answer_id'], ref['version'], ref['answer']),
                                     (chosen['id'], 5, chosen['content']))
                captured.append((gid, snapshot))
        with self.assertRaisesRegex(ValueError, '選項名稱'):
            autonomy.create_goal(self.store, self.sid, f'沿用回答 #{recent["id"]} 的比較結果' + old_options,
                                 from_chat=True)

        first_work, second_work = self.names
        work_goal = autonomy.create_goal(
            self.store, self.sid, prefix + f'，只整理「{first_work}」和「{second_work}」。', from_chat=True)
        work_state = autonomy.get_goal(self.store, self.sid, work_goal)
        info = work_state['conversation_context']
        self.assertEqual(info[goal_comparison_handoff.BASIS]['indices'], [0, 1])
        self.assertEqual(info[goal_comparison_handoff.REFERENCE]['answer_id'], self.answer['id'])
        captured.append((work_goal, work_state))

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'named-scope')
        copy = Store(self.root / 'named-scope' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(copy, self.sid, old_goal), old_snapshot)
        for gid, snapshot in captured:
            self.assertEqual(autonomy.get_goal(copy, self.sid, gid), snapshot)

    def test_named_dimension_keeps_the_matching_cells_and_legacy_recency(self):
        request = '請比較「導視牌」和「地板貼」的放置位置。'
        headers = '| 欄目 | 導視牌 | 地板貼 |\n| --- | --- | --- |'
        earlier = Assistant(self.store, ScriptedEngine(headers + '\n| 放置位置 | 樓梯口 | 走道 |\n| 適用 | 晚班 | 全日 |')).ask(self.sid, request)
        incomplete = headers + '\n| 放置位置 | 櫃台 | **待確認** [補充#1] |\n| 適用 | 全日 | 全日 |'
        Assistant(self.store, ScriptedEngine(incomplete)).ask(self.sid, request)
        recent = Assistant(self.store, ScriptedEngine('導視牌比地板貼省 7 分鐘。')).ask(
            self.sid, '比較「導視牌」與「地板貼」的布置時間。')
        lead = '沿用最後一次比較結果，只整理「導視牌」與「地板貼」'
        position = lead + '在放置位置上的差異。'
        capture = goal_comparison_handoff.capture
        with patch('assistant.goal_comparison_handoff.capture',
                   side_effect=lambda *args: capture(*args, reference_version=3)):
            legacy = autonomy.create_goal(self.store, self.sid, position, from_chat=True)
        legacy_state = autonomy.get_goal(self.store, self.sid, legacy)
        ref = legacy_state['conversation_context'][goal_comparison_handoff.REFERENCE]
        self.assertEqual((ref['answer_id'], ref['version']), (recent['id'], 3))

        tracked = [(legacy, legacy_state)]
        for objective, expected in ((position, earlier), (lead + '的布置時間。', recent),
                                    (lead + '。', recent), (lead + '的維護成本。', None)):
            with self.subTest(objective=objective):
                gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
                state = autonomy.get_goal(self.store, self.sid, gid)
                ref = state['conversation_context'].get(goal_comparison_handoff.REFERENCE)
                if expected:
                    self.assertEqual((ref['answer_id'], ref['version']), (expected['id'], 5))
                else:
                    self.assertIsNone(ref)
                tracked.append((gid, state))
        with self.assertRaisesRegex(ValueError, '比較形式'):
            autonomy.create_goal(self.store, self.sid, f'沿用回答 #{recent["id"]} 的比較結果，'
                                 '只整理「導視牌」與「地板貼」的放置位置。', from_chat=True)

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'dimensions')
        copy = Store(self.root / 'dimensions' / 'assistant.sqlite3')
        for gid, state in tracked:
            self.assertEqual(autonomy.get_goal(copy, self.sid, gid), state)

    def test_formal_withdrawal_blocks_old_and_unconfirmed_repeated_values(self):
        compare = '請比較「隔音板」和「固定簾」的存放位置。'
        objective = '沿用本對話最後一次比較結果，只整理「隔音板」和「固定簾」的存放位置。'
        title = '| 方案 | 存放位置 |\n| --- | --- |\n'
        complete = title + '| 隔音板 | 星枇格 |\n| 固定簾 | 天晴架 |'
        old = Assistant(self.store, ScriptedEngine(complete)).ask(
            self.sid, compare + '隔音板放星枇格，固定簾放天晴架。')
        partial = complete.replace('星枇格', '待確認')
        latest = Assistant(self.store, ScriptedEngine(partial)).ask(
            self.sid, compare + '正式撤回隔音板先前保存的存放位置，過去的位置不可再用。固定簾仍放天晴架。')
        capture = goal_comparison_handoff.capture
        with patch('assistant.goal_comparison_handoff.capture',
                   side_effect=lambda *args: capture(*args, reference_version=4)):
            legacy = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        before = autonomy.get_goal(self.store, self.sid, legacy)
        ref = before['conversation_context'][goal_comparison_handoff.REFERENCE]
        self.assertEqual((ref['answer_id'], ref['version']), (old['id'], 4))

        gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        engine = ScriptedEngine(action('ask', question='隔音板的新存放位置是哪裡？', partial_result=partial))
        waiting = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=1)
        payload = json.loads(engine.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        context = payload['conversation_context']
        self.assertNotIn(goal_comparison_handoff.REFERENCE, context)
        self.assertNotIn('verified_work_fields', payload)
        self.assertIn((latest['id'], partial), [(item['message_id'], item['content']) for item in context['messages']])

        historical = f'沿用回答 #{old["id"]} 的比較表，只整理「隔音板」和「固定簾」的存放位置。'
        history = autonomy.create_goal(self.store, self.sid, historical, from_chat=True)
        saved_history = autonomy.get_goal(self.store, self.sid, history)
        self.assertEqual(saved_history['conversation_context'][goal_comparison_handoff.REFERENCE]['answer'], complete)
        with self.assertRaisesRegex(ValueError, '已撤回'):
            autonomy.create_goal(self.store, self.sid, historical + '交付現行的位置。', from_chat=True)

        wrong = complete.replace('星枇格', '雲棗架')
        reconfirm = '重新正式確認「隔音板」的「存放位置」為楊木櫃。'
        for answer, request in ((complete, compare), (wrong, compare + reconfirm)):
            Assistant(self.store, ScriptedEngine(answer)).ask(self.sid, request)
            picked = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
            self.assertNotIn(goal_comparison_handoff.REFERENCE,
                             autonomy.get_goal(self.store, self.sid, picked)['conversation_context'])
        current = complete.replace('星枇格', '楊木櫃')
        correct = Assistant(self.store, ScriptedEngine(current)).ask(self.sid, compare + reconfirm)
        chosen = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        chosen_state = autonomy.get_goal(self.store, self.sid, chosen)
        self.assertEqual(chosen_state['conversation_context'][goal_comparison_handoff.REFERENCE]['answer_id'],
                         correct['id'])
        self.store.add_message(self.sid, 'user', '隔音板的存放位置已正式撤回。')
        later = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        self.assertNotIn(goal_comparison_handoff.REFERENCE,
                         autonomy.get_goal(self.store, self.sid, later)['conversation_context'])

        saved = create_backup(self.store.path)
        restore_backup(saved['path'], self.root / 'authority')
        copy = Store(self.root / 'authority' / 'assistant.sqlite3')
        for goal, state in ((legacy, before), (gid, waiting), (chosen, chosen_state), (history, saved_history)):
            self.assertEqual(autonomy.get_goal(copy, self.sid, goal), state)

    def test_current_comparison_skips_proposal_without_blocking_other_scopes(self):
        compare = '請比較「隔音板」和「固定簾」的存放位置。'
        head = '| 方案 | 存放位置 |\n| --- | --- |\n'
        base = head + '| 隔音板 | 星枇格 |\n| 固定簾 | 天晴架 |'
        approved = Assistant(self.store, ScriptedEngine(base)).ask(self.sid, compare)
        proposed = head + '| 隔音板 | 提議：秋堇袋 |\n| 固定簾 | 提議：琉槐台 |'
        proposal = Assistant(self.store, ScriptedEngine(proposed)).ask(
            self.sid, compare + '這只是提案，尚未正式採用。')
        for control in ('正式撤回隔音板的布置時間。', '正式撤回隔音板甲的存放位置。',
                        '請讀例句「正式撤回隔音板的存放位置」。',
                        '如果改成夏季，正式撤回隔音板的存放位置。'):
            self.store.add_message(self.sid, 'user', control)
        objective = '沿用本對話最後一次比較結果，只整理「隔音板」和「固定簾」的存放位置。'
        for extra, expected in (('交付現行已正式確認的結果。', approved), ('交付原比較的提案。', proposal)):
            gid = autonomy.create_goal(self.store, self.sid, objective + extra, from_chat=True)
            self.assertEqual(autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
                             [goal_comparison_handoff.REFERENCE]['answer_id'], expected['id'])
        with self.assertRaisesRegex(ValueError, '尚未採用'):
            autonomy.create_goal(self.store, self.sid, f'沿用回答 #{proposal["id"]} 的比較表。'
                                 '只整理「隔音板」和「固定簾」的存放位置。交付現行的位置。', from_chat=True)

    def test_named_work_dimension_filters_live_fields_before_delivery(self):
        works = ('西側櫃台清點', '東側櫃台清點')
        parents = [(work, autonomy.create_goal(self.store, self.sid, work)) for work in works]
        for _, gid in parents:
            for label, value in (('班別', '午後班'), ('備品櫃', '桃磚櫃')):
                autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=f'請提供{label}。')))
                view = goal_replies.snapshot(self.store, self.sid, gid)
                goal_replies.save(self.store, self.sid, gid, value, expected_fingerprint=view['fingerprint'])
        parts = '、'.join(f'「{work}」的「{field}」' for work in works for field in ('班別', '備品櫃'))
        reply = Assistant(self.store, ScriptedEngine('四項工作欄位已查到。')).ask(
            self.sid, f'比較{parts}最後已確認的欄值。每件一行。')
        objective = f'沿用最後一次比較結果，只整理「{works[0]}」與「{works[1]}」的備品櫃。'
        gid = autonomy.create_goal(self.store, self.sid, objective, from_chat=True)
        source = autonomy.get_goal(self.store, self.sid, gid)['conversation_context']
        self.assertEqual(source[goal_comparison_handoff.BASIS]['version'], 4)
        self.assertEqual(source[goal_comparison_handoff.BASIS]['indices'], [1, 3])
        self.assertEqual(source[goal_comparison_handoff.REFERENCE]['answer_id'], reply['id'])
        engine = ScriptedEngine(action('finish', report='兩件的備品櫃都是桃磚櫃。'))
        final = autonomy.run_goal(self.store, self.sid, gid, engine, max_steps=1)
        model_data = json.loads(engine.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertEqual([(row['work'], row['field'], row['value']) for row in model_data['verified_work_fields']],
                         [(work, '備品櫃', '桃磚櫃') for work in works])
        saved = create_backup(self.store.path)
        restore_backup(saved['path'], self.root / 'dimensions-work')
        copy = Store(self.root / 'dimensions-work' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(copy, self.sid, gid), final)

    def test_comparison_checks_current_sources_and_historical_file_bytes(self):
        gid = autonomy.create_goal(self.store, self.sid, self.objective, from_chat=True, file_delivery=True)
        original_answer = self.store.answer_context(self.answer['id'])
        other_before = autonomy.get_goal(self.store, self.sid, self.parents[0])
        wrong = ScriptedEngine(action('write_artifact', filename='wrong.json', content=self.rows('別架')))
        state = autonomy.run_goal(self.store, self.sid, gid, wrong, max_steps=1)
        self.assertFalse(state['artifacts'])
        self.assertIn('現行欄值不一致', state['steps'][-1]['observation']['error'])

        def change_during_delivery():
            parent = self.parents[1]
            view = goal_guidance.snapshot(self.store, self.sid, parent)
            goal_guidance.save(self.store, self.sid, parent, '驗收台正式改為銀枝桌。',
                               expected_fingerprint=view['fingerprint'])
            return action('deliver', filenames=['handoff.json'], report='已交付。')

        initial = ScriptedEngine(action('write_artifact', filename='handoff.json', content=self.rows()),
                                 change_during_delivery)
        state = autonomy.run_goal(self.store, self.sid, gid, initial, max_steps=2)
        self.assertEqual(state['status'], 'paused')
        self.assertIn('剛變更', state['steps'][-1]['observation']['error'])
        old_bytes = (artifacts.directory(self.store, gid) / 'handoff.json').read_bytes()
        self.assertEqual(old_bytes, self.rows().encode('utf-8'))

        retry = ScriptedEngine(action('deliver', filenames=['handoff.json'], report='已交付。'),
                               action('write_artifact', filename='handoff-v2.json', content=self.rows('銀枝桌', '更正')),
                               action('deliver', filenames=['handoff-v2.json'], report='已交付新版。'))
        final = autonomy.run_goal(self.store, self.sid, gid, retry, max_steps=3)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('重新建立', final['steps'][-3]['observation']['error'])
        receipts = [item['observation'][goal_comparison_handoff.RECEIPT] for item in final['steps']
                    if goal_comparison_handoff.RECEIPT in item['observation']]
        self.assertEqual([item['rows'][1]['value'] for item in receipts], ['榆光台', '銀枝桌', '銀枝桌'])
        self.assertEqual(receipts[0]['rows'][0], receipts[-1]['rows'][0])
        checkpoint = create_backup(self.store.path)
        restore_backup(checkpoint['path'], self.root / 'restored')
        received = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(received.answer_context(self.answer['id']), original_answer)
        self.assertEqual(autonomy.get_goal(received, self.sid, self.parents[0]), other_before)
        self.assertEqual(autonomy.get_goal(received, self.sid, gid)['steps'], final['steps'])
        artifacts.rebuild(received, self.sid, gid)
        folder = artifacts.directory(received, gid)
        self.assertEqual((folder / 'handoff.json').read_bytes(), old_bytes)
        self.assertEqual((folder / 'handoff-v2.json').read_bytes(), self.rows('銀枝桌', '更正').encode('utf-8'))

        with received.connect() as db:
            row, = db.execute('SELECT observation_json FROM autonomous_steps WHERE goal_id=? AND sequence=2',
                              (gid,)).fetchone()
            modified = json.loads(row)
            modified[goal_comparison_handoff.RECEIPT]['rows'][1]['value'] = '混用'
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=2',
                       (json.dumps(modified, ensure_ascii=False), gid))
        with self.assertRaisesRegex(ValueError, '歷史欄列'):
            validate_database(received.path)


if __name__ == '__main__':
    unittest.main()
