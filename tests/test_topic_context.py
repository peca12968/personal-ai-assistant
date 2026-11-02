import json
import tempfile
import unittest
from pathlib import Path

from assistant.acceptance import validate_suite
from assistant.context import context_reset, followup_query
from assistant.engine import Assistant
from assistant.store import Store


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


if __name__ == '__main__':
    unittest.main()
