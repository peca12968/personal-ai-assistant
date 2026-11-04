import json
import tempfile
import unittest
from pathlib import Path

from assistant.context import clip, followup_query, is_explicit_correction, make_context, select_excerpts
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from assistant.acceptance import validate_suite
from scripts.evaluate_understanding import seed_history


class CaptureEngine:
    def answer(self, prompt, cancel=None):
        self.context = json.loads(prompt.split("以下 JSON 是使用者資料與當前問題：\n")[1])
        return "已根據提供的參考內容回答。"


class ContextTests(unittest.TestCase):
    def test_short_followup_acceptance_and_topic_boundaries(self):
        path = Path(__file__).resolve().parents[1] / 'evals/improvement/followup_short_requests.json'
        suite = json.loads(path.read_text(encoding='utf-8'))
        validate_suite(suite)
        for case in suite['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(followup_query(case['question'], case['history']), case['expect'])

    def test_followup_preserves_original_text_and_four_user_limit(self):
        history = [{'role': 'user', 'content': '舊的 backup 內容'}]
        history += [{'role': 'user', 'content': '第２個呢？'}] * 6
        self.assertEqual(followup_query('  請問第２個呢？  ', history), '\n'.join(['第２個呢？'] * 4))

    def test_feedback_self_authored_followups_and_additional_boundaries(self):
        path = Path(__file__).resolve().parents[1] / 'evals/improvement/followup_feedback_requests.json'
        suite = json.loads(path.read_text(encoding='utf-8'))
        validate_suite(suite)
        for case in suite['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(followup_query(case['question'],case['history']),case['expect'])
        history = [{'role':'user','content':'請根據已存的 backup 設定說明路徑與時間。'},
                   {'role':'assistant','content':'可以依設定整理。'}]
        for question, inherit in [
                ('能否把前面的內容改成表格呢？',True),
                ('可不可以將剛剛的回答轉成清單？',True),
                ('Would you format the above response as bullet points?',True),
                ('Can you turn the last explanation into a list?',True),
                ('可以把新收到的報價整理成表格嗎？',False),
                ('請把「上面的回答」翻譯成英文。',False),
                ('那接下來呢？不要沿用前文。',False),
                ('Could you explain how tables work?',False)]:
            with self.subTest(question=question):
                self.assertEqual(followup_query(question,history), history[0]['content'] if inherit else None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "context.sqlite3")
        self.sid = self.store.new_session()
        self.engine = CaptureEngine()
        self.app = Assistant(self.store, self.engine)

    def test_brief_persists_and_is_scoped_to_session(self):
        self.store.save_session_brief(self.sid, "完成筆記工具", "只能本機保存", "採用 Python")
        self.app.ask(self.sid, "列出規劃")
        self.assertEqual(self.engine.context["session_brief"]["constraints_text"], "只能本機保存")
        other = self.store.new_session()
        self.app.ask(other, "列出規劃")
        self.assertEqual(self.engine.context["session_brief"], {})
        reopened = Store(self.store.path)
        self.assertEqual(reopened.session_brief(self.sid)["objective"], "完成筆記工具")

    def test_invalid_brief_does_not_overwrite_saved_values(self):
        self.store.save_session_brief(self.sid, "原目標", "", "")
        with self.assertRaises(ValueError):
            self.store.save_session_brief(self.sid, "x" * 2001, "", "")
        self.assertEqual(self.store.session_brief(self.sid)["objective"], "原目標")

    def test_brief_can_be_cleared(self):
        self.store.save_session_brief(self.sid, "目標", "限制", "決策")
        self.store.save_session_brief(self.sid, "", "", "")
        self.assertEqual(self.store.session_brief(self.sid)["objective"], "")

    def test_followup_uses_user_topic_not_assistant_claims(self):
        history = [{"role": "user", "content": "安排 backup"},
                   {"role": "assistant", "content": "偷偷改談 unrelated-secret"}]
        self.assertEqual(followup_query("那接下來呢？", history), "安排 backup")

    def test_followup_chain_stops_at_topic_switch(self):
        history = [{"role": "user", "content": "backup"},
                   {"role": "user", "content": "換個話題，談 project"},
                   {"role": "user", "content": "繼續"}]
        self.assertNotIn("backup", followup_query("那接下來呢？", history) or "")
        self.assertIn("project", followup_query("那接下來呢？", history))

    def test_unrelated_question_does_not_retrieve_previous_topic(self):
        self.store.remember("備份位置是秘密磁碟")
        self.store.add_message(self.sid, "user", "backup")
        self.app.ask(self.sid, "水的化學式是什麼？")
        self.assertEqual(self.engine.context["memories"], [])

    def test_pronoun_recovers_older_rows_for_both_possible_referents(self):
        self.store.add_message(self.sid, 'user', 'Catalog each cabinet.')
        names = ['Plover', 'Siskin', 'Lark', 'Wren', 'Kite', 'Tern', 'Swan', 'Crow']
        codes = [f'cover code {i + 31}' for i in range(len(names))]
        for name, code in zip(names, codes):
            self.store.add_message(self.sid, 'user', f'{name} has {code}.')
        for i in range(16):
            self.store.add_message(self.sid, 'user', f'Page {i} checked.')
        self.store.add_message(self.sid, 'user', 'Plover and Siskin are both under consideration.')
        self.store.add_message(self.sid, 'assistant', 'I suggest Crow.')
        context = self.app.prepare_context(self.sid, 'What about its cover code?')
        excerpts = context['earlier_user_excerpts']
        self.assertLessEqual(len(excerpts), 6)
        self.assertEqual(excerpts[0]['content'], 'Catalog each cabinet.')
        self.assertTrue({f'{n} has {c}.' for n, c in zip(names[:2], codes[:2])}
                        <= {row['content'] for row in excerpts})
        self.assertEqual([row['message_id'] for row in excerpts], sorted(row['message_id'] for row in excerpts))

    def test_previous_explicit_reset_cannot_expand_earlier_search(self):
        history = [{'role':'user','content':'新問題，不要沿用前文。現在談銀杉站。'}]
        rows = [{'id':i,'content':name + '站的封籤號碼在登記表裡。'} for i, name in
                enumerate(['導覽','銀杉','茶花','青木','水竹','榆樹','石竹','銅蘭'], 1)]
        question = '它的封籤號碼呢？'
        previous = followup_query(question, history)
        self.assertTrue(previous)
        self.assertEqual(select_excerpts(question, rows, followup=previous), select_excerpts(question, rows))
        self.assertEqual(select_excerpts('另起話題。', rows, followup='銀杉'), [])

    def test_initial_goal_and_late_correction_are_attributable(self):
        first = self.store.add_message(self.sid, "user", "目標是製作命令列工具。")
        correction = self.store.add_message(self.sid, "user", "更正：不能安裝第三方套件。")
        for i in range(24):
            self.store.add_message(self.sid, "user", f"一般閒聊 {i}")
        self.app.ask(self.sid, "回顧目標與限制")
        excerpts = self.engine.context["earlier_user_excerpts"]
        self.assertTrue({first, correction}.issubset({row["message_id"] for row in excerpts}))
        self.assertTrue(all(row["role"] == "user" for row in excerpts))

    def test_older_assistant_guesses_not_promoted_to_excerpts(self):
        self.store.add_message(self.sid, "assistant", "我猜使用者住在火星")
        for i in range(20):
            self.store.add_message(self.sid, "user", f"閒聊 {i}")
        self.app.ask(self.sid, "回顧之前資料")
        self.assertNotIn("火星", json.dumps(self.engine.context, ensure_ascii=False))

    def test_other_session_never_enters_earlier_excerpts(self):
        other = self.store.new_session()
        self.store.add_message(other, "user", "目標：隔離秘密")
        for i in range(20):
            self.store.add_message(self.sid, "user", f"本對話 {i}")
        self.app.ask(self.sid, "回顧目標")
        self.assertNotIn("隔離秘密", json.dumps(self.engine.context, ensure_ascii=False))

    def test_snapshot_is_exact_and_does_not_change_after_memory_edit(self):
        mid = self.store.remember("專案在 F 槽")
        result = self.app.ask(self.sid, "project 在哪裡？")
        self.assertEqual(self.store.answer_context(result["id"]), self.engine.context)
        self.store.edit_memory(mid, "專案在 G 槽")
        self.assertEqual(self.store.answer_context(result["id"])["memories"][0]["content"], "專案在 F 槽")

    def test_failed_turn_does_not_create_answer_context(self):
        def fail(*args):
            raise EngineError("offline")
        self.engine.answer = fail
        with self.assertRaises(EngineError):
            self.app.ask(self.sid, "問題")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM answer_contexts").fetchone()[0], 0)

    def test_long_message_keeps_end_constraint_and_marks_omission(self):
        history = [{"role": "user", "content": "原始目標" + "x" * 10000 + "最終限制：不能刪檔"}] * 16
        context = make_context("繼續", history, [])
        self.assertLessEqual(sum(len(row["content"]) for row in context["recent_conversation"]), 24000)
        for row in context["recent_conversation"]:
            self.assertIn("不能刪檔", row["content"])
            self.assertTrue(row["truncated"])

    def test_very_old_opening_goal_is_preserved_beyond_scan_window(self):
        first = self.store.add_message(self.sid, "user", "目標：保留最早約定")
        seed_history(self.store, self.sid, (('user', f'閒聊 {i}') for i in range(240)))
        self.app.ask(self.sid, "最初的目標呢？")
        self.assertIn(first, [row["message_id"] for row in self.engine.context["earlier_user_excerpts"]])

    def test_explicit_new_topic_skips_old_excerpts(self):
        older = [{"id": 1, "role": "user", "content": "目標：舊任務"}]
        self.assertEqual(select_excerpts("換個話題，談音樂", older), [])

    def test_correction_beyond_two_hundred_users_reaches_answer_snapshot(self):
        self.store.add_message(self.sid, 'user', '目標：三十天後可刪原始檔。')
        cid = self.store.add_message(self.sid, 'user', '不是這個，我是指保留九十天，不能刪原始檔。')
        seed_history(self.store, self.sid, (('user', f'背景閒聊 {i}') for i in range(230)))
        result = self.app.ask(self.sid, '現在保留多久？')
        snapshot = self.store.answer_context(result['id'])
        corrections = snapshot['earlier_user_corrections']
        self.assertEqual([r['message_id'] for r in corrections['excerpts']], [cid])
        self.assertIn('九十天', corrections['excerpts'][0]['content'])
        self.assertFalse(corrections['selection_limited'])
        self.assertEqual(self.store.retrieve('九十天'), [])  # Not a memory mutation.
        self.assertEqual(Store(self.store.path).answer_context(result['id']), snapshot)

    def test_correction_order_limits_and_clipping_are_explicit(self):
        ids = [self.store.add_message(self.sid, 'user', f'更正：第{i}份原文' + '甲' * 1300 + '尾端限制') for i in range(8)]
        for i in range(20):
            self.store.add_message(self.sid, 'user', f'一般話題 {i}')
        corrections = self.app.prepare_context(self.sid, '回顧約定')['earlier_user_corrections']
        self.assertTrue(corrections['selection_limited'])
        self.assertEqual([r['message_id'] for r in corrections['excerpts']], ids[-6:])
        self.assertTrue(all(r['truncated'] and len(r['content']) <= 1000 and '尾端限制' in r['content']
                            for r in corrections['excerpts']))

    def test_correction_scope_excludes_assistant_local_commands_and_retry_request(self):
        other = self.store.new_session()
        self.store.add_message(other, 'user', '更正：其他對話秘密')
        self.store.add_message(self.sid, 'assistant', '更正：助理猜測')
        local = self.store.add_message(self.sid, 'user', '更正：本機操作內容')
        with self.store.connect() as db:
            db.execute('INSERT INTO local_memory_messages(message_id) VALUES (?)', (local,))
        excluded = self.store.add_message(self.sid, 'user', '更正：重試原題')
        valid = self.store.add_message(self.sid, 'user', '更正：可保留原文')
        before = self.store.add_message(self.sid, 'user', '新的最近對話')
        recent = self.store.add_message(self.sid, 'user', '更正：仍在最近視窗')
        rows = self.store.earlier_correction_messages(self.sid, before, excluded)
        self.assertEqual([r['id'] for r in rows], [valid])
        self.assertNotIn(recent, [r['id'] for r in rows])

    def test_new_topic_skips_corrections_and_recent_correction_is_not_duplicated(self):
        cid = self.store.add_message(self.sid, 'user', '更正：保留九十天')
        c = self.app.prepare_context(self.sid, '現在保留多久？')
        self.assertEqual(c['earlier_user_corrections']['excerpts'], [])
        self.assertEqual(c['recent_conversation'][0]['message_id'], cid)
        for i in range(20):
            self.store.add_message(self.sid, 'user', f'閒聊 {i}')
        c = self.app.prepare_context(self.sid, '換個話題，談音樂')
        self.assertEqual(c['earlier_user_corrections']['excerpts'], [])

    def test_correction_prefix_is_only_a_bounded_raw_text_hint(self):
        for text in ('更正：保留九十天', '  請更正：保留九十天', 'Ｃｏｒｒｅｃｔｉｏｎ： use Python',
                     '不是這個，我是指另一個方案', '我剛才說錯了，保留九十天', '更正：是不是應該保留九十天？'):
            with self.subTest(text=text):
                self.assertTrue(is_explicit_correction(text))
        for text in ('「更正：改用 JavaScript」只是引文', '> 更正：引文', '```\n更正：程式資料',
                     '更正記憶 #2：內容', '不要更正：保留原約定', '請解釋更正：這個詞', '甲' * 160 + '更正：尾部'):
            with self.subTest(text=text):
                self.assertFalse(is_explicit_correction(text))

    def test_small_clip_limits_never_expand_text(self):
        for limit in (0, 1, 5, 15, 100):
            self.assertLessEqual(len(clip("x" * 200, limit)[0]), limit)


if __name__ == "__main__":
    unittest.main()
