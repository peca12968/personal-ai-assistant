import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.engine import Assistant, render_prompt
from assistant.store import Store


class DeliveryCapabilitiesTests(unittest.TestCase):
    def help(self, question):
        from assistant.response_delivery import help_context
        return help_context(question)

    def test_scoped_help_including_quoted_button_name(self):
        for q in ('這個個人AI助理的「答案整理」在哪裡？',
                  '本助理的舊回答都有模型原答嗎？',
                  '這套助理的整理前原答怎麼查看？',
                  '這個助理的答案整理是不是會更新權重？'):
            with self.subTest(q=q):
                facts = self.help(q)
                self.assertIsNotNone(facts)
                self.assertIn('政策', facts['scope'])
                self.assertFalse(facts['per_answer_state_observed'])

    def test_other_apps_data_and_unrelated_requests_do_not_add_help(self):
        for q in ('別家的軟體也有答案整理嗎？不要套用這個個人AI助理。',
                  '不是問本助理，是另一個程式的模型原答怎麼查。',
                  '請翻譯「這個個人AI助理的答案整理在哪裡？」。',
                  '> 這個個人AI助理的答案整理在哪裡？\n請翻譯上面引文。',
                  '```\n本助理的舊回答都有模型原答嗎？\n```\n只做排版。',
                  '這個助理，請解釋為什麼4減1等於3。',
                  '這個助理，請翻譯「答案整理」這個詞。',
                  '本助理的舊回答都有模型原答嗎？' * 2000):
            with self.subTest(q=q[:60]):
                self.assertIsNone(self.help(q))

    def test_only_static_policy_without_reading_private_records(self):
        from assistant import response_delivery
        q = '這個個人AI助理的答案整理有原答嗎？程式自我改進會更新權重嗎？'
        with patch.object(response_delivery, 'read', side_effect=AssertionError('private read')):
            facts = self.help(q)
        self.assertEqual(facts['view']['entry'], ['查看本次參考', response_delivery.VIEW_TAB])
        self.assertEqual(facts['view']['fields'], list(response_delivery.VIEW_FIELDS))
        for label in response_delivery.VIEW_FIELDS:
            self.assertIn(label, facts['view']['meaning'])
        self.assertFalse(facts['view']['semantic_consistency_flags'])
        self.assertFalse(facts['history']['reconstruct_unsaved_raw'])
        self.assertIn('未保存', facts['history']['raw_model_answer'])
        self.assertIn('交付', facts['history']['visible_chat'])
        for name in ('答案整理', '程式自我改進', '確認佇列', '不更新權重'):
            self.assertIn(name, facts['training']['distinctions'])
        for step in ('準備', '明確啟動', '有限本機訓練', '驗收', '明確選用', '不自動切換'):
            self.assertIn(step, facts['training']['parameter_update'])
        self.assertLess(len(json.dumps(facts, ensure_ascii=False)), 2200)

    def test_training_focus_omits_unasked_record_details(self):
        facts = self.help('這個助理的答案整理、程式自我改進、學習佇列都會更新權重嗎？要改動模型參數還缺哪些步驟？')
        self.assertIn('training', facts)
        self.assertNotIn('history', facts)
        self.assertNotIn('view', facts)
        facts = self.help('這個助理的答案整理頁如何查看？')
        self.assertIn('history', facts)
        self.assertNotIn('training', facts)

    def test_evidence_question_is_not_a_request_for_training_procedure(self):
        facts = self.help('這個助理整理前的完整回答在哪看？所有舊回答都有嗎？這代表權重已訓練變強嗎？')
        self.assertIn('training_evidence', facts)
        self.assertNotIn('training', facts)
        self.assertIn('可能', facts['question_focus'])
        self.assertFalse(facts['per_answer_state_observed'])

    def test_prompt_expands_default_guidance_once_without_duplication(self):
        from assistant.response_delivery import GUIDANCE, PROMPT_MARKER
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = Assistant(store, None)
            ordinary = render_prompt(app.prepare_context(store.new_session(), '解釋4-1。'))
            help_prompt = render_prompt(app.prepare_context(store.new_session(), '本助理的模型原答怎麼查看？'))
            self.assertEqual(ordinary.count(GUIDANCE), 1)
            self.assertNotIn(PROMPT_MARKER, ordinary)
            self.assertNotIn(GUIDANCE, help_prompt)
            self.assertNotIn(PROMPT_MARKER, help_prompt)

    def test_mixed_features_keep_full_manual_and_current_request(self):
        from assistant.response_delivery import GUIDANCE, FOCUSED_GUIDANCE
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            for question in ('本助理的答案整理和PDF文件匯入怎麼一起用？',
                             '本助理的答案整理與備份還原有何不同？',
                             '本助理的答案整理能否拿來計算CSV總額？'):
                context = Assistant(store, None).prepare_context(store.new_session(), question)
                self.assertEqual(context['current_request'], question)
                self.assertNotIn('delivery_capabilities', context)
                prompt = render_prompt(context)
                self.assertIn(GUIDANCE, prompt)
                self.assertNotIn(FOCUSED_GUIDANCE, prompt)

    def test_attached_sources_keep_full_manual_without_losing_policy_or_data(self):
        from assistant.response_delivery import GUIDANCE, FOCUSED_GUIDANCE
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            context = Assistant(store, None).prepare_context(store.new_session(), '本助理的答案整理頁在哪裡？')
            for key, value in (('documents', {'catalog':[{'id':1}], 'excerpts':[]}),
                               ('documents', {'catalog':[], 'excerpts':[{'text':'synthetic excerpt'}]}),
                               ('image_inputs', [{'id':'synthetic'}])):
                copy = json.loads(json.dumps(context))
                copy[key] = value
                before = json.dumps(copy, ensure_ascii=False)
                prompt = render_prompt(copy)
                self.assertIn(GUIDANCE, prompt)
                self.assertNotIn(FOCUSED_GUIDANCE, prompt)
                self.assertEqual(json.dumps(copy, ensure_ascii=False), before)

    def test_focused_prompt_retains_complete_context_and_source_boundary(self):
        from assistant.response_delivery import FOCUSED_GUIDANCE
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = store.new_session()
            store.add_message(sid, 'user', '前提：這次只問功能，不要執行。')
            store.add_message(sid, 'assistant', '歷史回覆不代表實際操作。')
            question = '本助理的答案整理頁在哪裡？'
            context = Assistant(store, None).prepare_context(sid, question)
            prompt = render_prompt(context)
            self.assertIn(FOCUSED_GUIDANCE, prompt)
            self.assertTrue(prompt.endswith('\n\n以下 JSON 是使用者資料與當前問題：\n' + json.dumps(context, ensure_ascii=False)))
            self.assertEqual(len(context['recent_conversation']), 2)

    def test_formatting_does_not_create_training_examples(self):
        from scripts.daily_delivery import DailyLocalEngine
        class Engine:
            display_name = 'fixture'
            def describe(self): return {'kind':'fixture'}
            def answer(self, prompt, cancel=None): return '答案是 42。'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            answer = Assistant(store, DailyLocalEngine(Engine())).ask(store.new_session(), '結果？只輸出數字。')
            self.assertEqual(answer['content'], '42')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_examples').fetchone()[0], 0)

    def test_fresh_detached_policy_objects(self):
        first = self.help('這個助理的答案整理如何查看？')
        first['view']['fields'].append('虛構欄位')
        first['history']['raw_model_answer'] = '不實政策'
        second = self.help('這個助理的答案整理如何查看？')
        self.assertNotIn('虛構欄位', second['view']['fields'])
        self.assertNotEqual(second['history']['raw_model_answer'], '不實政策')

    def test_context_preserves_request_and_does_not_read_or_rewrite_old_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = store.new_session()
            question = '本助理的舊回答都有模型原答嗎？'
            old = store.add_message(sid, 'assistant', '歷史交付文字')
            before = store.messages(sid)
            context = Assistant(store, None).prepare_context(sid, question)
            self.assertEqual(context['current_request'], question)
            self.assertEqual(context['delivery_capabilities'], self.help(question))
            self.assertEqual(store.messages(sid), before)
            from assistant.response_delivery import read
            self.assertIsNone(read(store, old))
            self.assertNotIn('delivery_capabilities', Assistant(store, None).prepare_context(sid, '請解釋4-1。'))

    def test_saved_prompt_keeps_historical_policy_snapshot(self):
        class Engine:
            calls = []
            def answer(self, prompt, cancel=None):
                self.calls.append(prompt)
                return '只有新版經改寫的回答才可能有原答紀錄。'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            engine = Engine()
            answer = Assistant(store, engine).ask(store.new_session(), '這個助理的答案整理在哪裡？')
            context = store.answer_context(answer['id'])
            self.assertEqual(render_prompt(context), engine.calls[0])
            original = json.dumps(context, ensure_ascii=False)
            with patch('assistant.response_delivery.help_context', return_value={'changed':True}):
                self.assertEqual(json.dumps(store.answer_context(answer['id']), ensure_ascii=False), original)

