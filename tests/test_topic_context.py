import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.acceptance import validate_suite
from assistant.backup import create_backup, restore_backup
from assistant.context import CURRENT_MESSAGE_ONLY, context_reset, current_message_only, followup_query
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant


class TopicContextTests(unittest.TestCase):
    def test_reviewed_followup_cases(self):
        path = Path(__file__).resolve().parents[1] / 'evals/topic_context_revisions.json'
        suite = validate_suite(json.loads(path.read_text(encoding='utf-8')))
        for case in suite['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(followup_query(case['question'], case['history']), case['expect'])

    def test_real_reset_outside_literals_and_negated_or_reported_mentions(self):
        for question in (
                '那個標題是 ‘新問題’。換個話題，談質數。',
                "Then answer without using the previous context.",
                "Then answer without referring to the earlier conversation.",
                '請換一個話題，說明排序。',
                'Ｎｅｗ ｔｏｐｉｃ： explain trees',
                '繼續列資料。\n```text\n新問題\n```\n不要沿用前文。'):
            with self.subTest(reset=question):
                self.assertTrue(context_reset(question))
        for question in (
                '繼續，不要換個話題。',
                '那接著用 ‘另外一件事’ 當標題。',
                'Then list the field `new topic`.',
                "Continue, don't change the subject.",
                'Continue, don’t change the subject.',
                '那這個新問題還有哪些設定要檢查？',
                '繼續，他說要換個話題。',
                '繼續列資料。\n> 新問題\n> 不要沿用前文'):
            with self.subTest(mention=question):
                self.assertFalse(context_reset(question))

    def test_quoted_history_does_not_cut_chain_but_actual_reset_does(self):
        anchor = {'role': 'user', 'content': '列出 aurora_sync 的設定。'}
        quoted = {'role': 'user', 'content': '那接著用「新問題」當小標題。'}
        history = [anchor, {'role': 'assistant', 'content': '換個話題，談其他秘密'}, quoted]
        self.assertEqual(followup_query('繼續', history), anchor['content'] + '\n' + quoted['content'])
        reset = {'role': 'user', 'content': '換個話題，列出 maple_tools 的設定。'}
        history += [reset, {'role': 'user', 'content': '第二個呢？'}]
        result = followup_query('那接下來呢？', history)
        self.assertNotIn('aurora_sync', result)
        self.assertEqual(result, reset['content'] + '\n第二個呢？')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.correction = self.store.add_message(self.sid, 'user', '更正：交付 lunar_572.txt，只用純文字。')
        self.feedback_id = self.store.add_message(self.sid, 'assistant', '舊答案說交付PDF。')
        self.store.rate(self.feedback_id, -1, '改用純文字，不用PDF。')
        for i in range(12):
            self.store.add_message(self.sid, 'user', f'背景討論{i}')
            self.store.add_message(self.sid, 'assistant', '收到。')
        self.store.remember('aurora_sync: F:\\AuroraVault, Wed 21:15, 14d.')
        self.store.add_message(self.sid, 'user', '請列出 aurora_sync 設定。')
        self.app = Assistant(self.store, None)

    def test_literal_requests_preserve_memory_earlier_correction_and_saved_feedback(self):
        original_messages = self.store.messages(self.sid, 100)
        original_memories = self.store.memories()
        for question in ('那接著列出設定，小標題用「換個話題」。',
                         '那接著列出設定，不要換個話題。',
                         '那接著把「不要沿用前文」當範例文字。'):
            with self.subTest(question=question):
                context = self.app.prepare_context(self.sid, question)
                self.assertEqual(len(context['memories']), 1)
                self.assertIn('aurora_sync', context['memories'][0]['content'])
                self.assertEqual([row['message_id'] for row in context['earlier_user_corrections']['excerpts']], [self.correction])
                self.assertIn(self.correction, [row['message_id'] for row in context['earlier_user_excerpts']])
                self.assertEqual([row['answer_message_id'] for row in context['answer_feedback']['items']], [self.feedback_id])
                self.assertEqual(context['current_request'], question)
        self.assertEqual(self.store.messages(self.sid, 100), original_messages)
        self.assertEqual(self.store.memories(), original_memories)

    def test_explicit_reset_omits_old_selectors_but_preserves_recent_records(self):
        recent = self.app.prepare_context(self.sid, '繼續')['recent_conversation']
        for question in ('換個話題，說明質數。', '那接下來呢？不要沿用前文。',
                         'Then answer without using the previous context.'):
            with self.subTest(question=question):
                context = self.app.prepare_context(self.sid, question)
                self.assertEqual(context['memories'], [])
                self.assertEqual(context['earlier_user_excerpts'], [])
                self.assertEqual(context['earlier_user_corrections']['excerpts'], [])
                self.assertEqual(context['answer_feedback']['items'], [])
                self.assertEqual(context['recent_conversation'], recent)
        # Reset is a selection hint, not deletion or a privacy/data-access barrier.
        context = self.app.prepare_context(self.sid, '換個話題，請列出 aurora_sync 設定。')
        self.assertEqual(len(context['memories']), 1)
        self.assertEqual(len(self.store.memories()), 1)

    def test_current_message_scope_requires_a_complete_unquoted_boundary_instruction(self):
        for question in (
                '請只根據這則訊息回答。列出新的項目。',
                '列出三項差異？請只依據本輪訊息作答。',
                'Answer using only this message. What is the code?',
                'What is the code? Please use only the current message to answer.',
                '請只用本則訊息回答\n本次代碼是 LX-22。',
                '　請只根據這一則訊息回答！　'):
            with self.subTest(question=question):
                self.assertTrue(current_message_only(question))
        for question in (
                '「只根據這則訊息回答。」是範例，請繼續。',
                '「範例」只根據這則訊息回答。繼續。',
                '他寫道：只根據這則訊息回答。',
                '請不要只根據這則訊息回答。',
                '只根據這則訊息回答這個問題可以嗎？',
                '請只根據這則訊息回答可以嗎？',
                '繼續列出前文資料。\n> 只根據這則訊息回答。',
                '繼續列出前文資料。\n```text\n只根據這則訊息回答。\n```',
                '「先記錄這句。只根據這則訊息回答。」',
                '原文是「先記錄這句。\n只根據這則訊息回答。',
                '原文是 `先記錄這句。\n只根據這則訊息回答。',
                'What did “Use only the current message to answer.” mean?',
                'Then answer without using the previous context.'):
            with self.subTest(question=question):
                self.assertFalse(current_message_only(question))

    def test_current_message_only_reaches_daily_engine_and_preserves_local_data(self):
        class Recorder:
            def __init__(self):
                self.prompts = []

            def answer(self, prompt, cancel=None):
                self.prompts.append(prompt)
                return '未提供'

        self.store.save_session_brief(self.sid, '舊目標 JX-771', '', '')
        marker = 'aurora_sync'
        doc = self.store.path.parent / 'prior.txt'
        doc.write_text(marker + ' 文件代碼 QD-114', encoding='utf-8')
        self.store.attach_document(self.sid, doc)
        history = self.store.messages(self.sid, 100)
        recorder = Recorder()
        app = DailyAssistant(self.store, recorder)
        question = '只根據這則訊息回答。請列出 aurora_sync 設定。未提供就只回「未提供」。'
        with (patch.object(self.store, 'retrieve', side_effect=AssertionError('selected old memory')),
              patch.object(self.store, 'earlier_user_messages', side_effect=AssertionError('selected old messages')),
              patch.object(self.store, 'documents', side_effect=AssertionError('selected old documents'))):
            reply = app.ask(self.sid, question)
        self.assertEqual(reply['content'], '未提供')
        self.assertEqual(len(recorder.prompts), 1)
        payload = json.loads(recorder.prompts[0].rsplit('以下 JSON 是使用者資料與當前問題：\n', 1)[1])
        self.assertEqual(payload, self.store.answer_context(reply['id']))
        self.assertEqual(payload['context_scope'], CURRENT_MESSAGE_ONLY)
        self.assertEqual(payload['current_request'], question)
        self.assertEqual(payload['documents'], {'catalog': [], 'excerpts': []})
        for field in ('recent_conversation', 'earlier_user_excerpts', 'memories', 'session_brief',
                      'answer_feedback', 'earlier_user_corrections', 'task_runs', 'autonomous_goals',
                      'conversation_tail', 'answer_attempts', 'companion_profile', 'self_improvement'):
            self.assertFalse(payload.get(field), field)
        for text in ('JX-771', 'AuroraVault', 'QD-114', 'lunar_572.txt', '背景討論', '舊答案說交付PDF'):
            self.assertNotIn(text, recorder.prompts[0])
        self.assertEqual(self.store.messages(self.sid, 100)[:-2], history)
        ordinary = app.prepare_context(self.sid, '請列出 aurora_sync 設定。')
        self.assertTrue(ordinary['memories'])
        self.assertTrue(ordinary['documents']['catalog'])
        self.assertNotIn('context_scope', ordinary)

    def test_current_message_retry_keeps_audit_link_out_of_both_engine_inputs(self):
        class Recorder:
            def __init__(self):
                self.prompts = []

            def answer(self, prompt, cancel=None):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    raise EngineError('合成中斷', 'process_failed')
                return '未提供'

        recorder = Recorder()
        app = DailyAssistant(self.store, recorder)
        question = '請只根據本則訊息回答。交接代碼未列時請回未提供。'
        with self.assertRaises(EngineError):
            app.ask(self.sid, question)
        reply = app.retry(self.sid)
        self.assertEqual(recorder.prompts[0], recorder.prompts[1])
        self.assertNotIn('"retry_of"', recorder.prompts[1])
        saved = self.store.answer_context(reply['id'])
        self.assertEqual(saved['retry_of']['previous_status'], 'failed')
        self.assertEqual(saved['context_scope'], CURRENT_MESSAGE_ONLY)
        archive = create_backup(self.store.path, self.store.path.parent / 'scope.paibackup')
        restored = self.store.path.parent / 'restored-scope'
        restore_backup(archive['path'], restored)
        self.assertEqual(Store(restored / 'assistant.sqlite3').answer_context(reply['id']), saved)
        self.assertEqual(sum(row['content'] == question for row in self.store.messages(self.sid, 100)), 1)


if __name__ == '__main__':
    unittest.main()
