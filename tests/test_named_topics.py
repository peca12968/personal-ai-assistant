import json
from pathlib import Path
import tempfile
import unittest

from assistant import local_chat_budget, named_topics
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError, render_prompt
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant
from scripts.evaluate_understanding import seed_history


class Recorder:
    def __init__(self):
        self.prompts = []
        self.interrupt = False

    def answer(self, prompt, cancel=None):
        self.prompts.append(prompt)
        if self.interrupt:
            raise EngineError('合成中斷', 'cancelled')
        return '已整理目前條件。'


class NamedTopicTests(unittest.TestCase):
    question = '回到「青禾補貨」的事。依最後確認，倉別和箱數是什麼？'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Recorder()
        self.app = DailyAssistant(self.store, self.engine)

    def add(self, *rows):
        return seed_history(self.store, self.sid, [('user', row) for row in rows])

    def context(self, question=None):
        return self.app.prepare_context(self.sid, question or self.question)

    def test_older_named_sources_reach_daily_prompt_with_order_and_exact_original_text(self):
        opening = '換個話題，談「青禾補貨」。先送北倉 11 箱。'
        update = '更正：改送東倉 7 箱。'
        self.add('先記錄通用流程。')
        source_ids = self.add(opening, update)
        self.add('話題：「青禾補貨備案」。南倉 19 箱。')
        other = self.store.new_session()
        self.store.add_message(other, 'user', '話題：「青禾補貨」。不要混入其他對話。')
        self.store.add_message(self.sid, 'assistant', '舊助理推測為西倉。')
        self.add(*(f'其他主題第 {n} 項。' for n in range(230)))
        result = self.app.ask(self.sid, self.question)
        context = self.store.answer_context(result['id'])
        topic = context['named_topic']
        self.assertEqual(topic['status'], 'found')
        self.assertEqual(topic['sources'], [{'message_id': mid, 'content': raw}
                         for mid, raw in zip(source_ids, (opening, update), strict=True)])
        self.assertNotIn(update, json.dumps(context['earlier_user_excerpts'], ensure_ascii=False))
        self.assertEqual(self.engine.prompts, [render_prompt(context)])
        self.assertIn('東倉 7 箱', self.engine.prompts[0])
        backup = create_backup(self.store.path, self.root / 'named.paibackup')
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(restored.answer_context(result['id'])['named_topic'], topic)

    def test_returns_join_same_title_but_duplicate_opening_is_ambiguous(self):
        opening = '話題：「青禾補貨」。第一版用北倉。'
        resumed = '回到「青禾補貨」。換成東倉。'
        followup = '再更正為 9 箱。'
        self.add(opening, '談「青禾補貨備案」。備案仍用北倉。', resumed, followup,
                 '換個題目。這是另外的工作。')
        topic = self.context()['named_topic']
        self.assertEqual([row['content'] for row in topic['sources']], [opening, resumed, followup])
        self.add('先談「青禾補貨」。這是一個另外的安排。')
        topic = self.context()['named_topic']
        self.assertEqual((topic['status'], topic['sources']), ('ambiguous', []))

    def test_excessive_rows_or_truncated_sources_are_reported_without_partial_topic(self):
        opening = '話題：「青禾補貨」。安排尚待確認。'
        self.add(opening, '更正：' + '重' * named_topics.MAX_SOURCE_CHARS)
        topic = self.context()['named_topic']
        self.assertEqual((topic['status'], topic['sources']), ('source_limit', []))
        question = '回到「新倉」。請列出最後計畫。'
        self.add('話題：「新倉」。安排尚待確認。', *('仍在討論。' for _ in range(8)))
        topic = self.context(question)['named_topic']
        self.assertEqual((topic['status'], topic['sources']), ('source_limit', []))

    def test_outside_search_window_and_similar_title_are_distinct_from_a_match(self):
        self.add('話題：「青禾補貨」。早期原話。')
        self.add(*(f'其他第 {n} 項。' for n in range(named_topics.MAX_USER_MESSAGES)))
        self.add('回到「青禾補貨」。留下無法與開頭核對的原話。',
                 '談「青禾補貨備案」。只有備案。')
        topic = self.context()['named_topic']
        self.assertEqual(topic['status'], 'not_found')
        self.assertTrue(topic['history_limited'])
        self.assertEqual(topic['scanned_user_messages'], named_topics.MAX_USER_MESSAGES)
        self.assertEqual(topic['sources'], [])

    def test_only_direct_named_returns_read_and_isolated_turns_end_named_scope(self):
        opening = '話題：「青禾補貨」。倉別先用北倉。'
        self.add(opening)
        Assistant(self.store, self.engine).ask(self.sid, '記住事實：私人記錄 AC-347')
        self.add('只根據這則訊息回答。這是獨立資料。', '這一段仍不屬於舊話題。')
        topic = self.context()['named_topic']
        self.assertEqual([row['content'] for row in topic['sources']], [opening])
        for question in ('翻譯「回到『青禾補貨』。請列出進度。」',
                         '回到「青禾補貨」。不要沿用前文，請寫新計畫。',
                         '回到「青禾補貨」。請只根據本則訊息回答。'):
            with self.subTest(question=question):
                self.assertNotIn('named_topic', self.context(question))

    def test_retry_does_not_reuse_the_question_as_a_historical_source(self):
        opening = '話題：「青禾補貨」。以東倉為準。'
        self.add(opening, '話題：「山城物流」。請先檢查地址。')
        self.engine.interrupt = True
        with self.assertRaises(EngineError):
            self.app.ask(self.sid, self.question)
        self.engine.interrupt = False
        result = self.app.retry(self.sid)
        context = self.store.answer_context(result['id'])
        self.assertEqual([row['content'] for row in context['named_topic']['sources']], [opening])
        self.assertEqual(context['retry_of']['previous_status'], 'cancelled')

    def test_named_overflow_preserves_full_context_and_declines_specialized_or_forged_inputs(self):
        self.add('話題：「青禾補貨」。以東倉為準。', '換個話題。現在談其他事項。')
        context = self.context()
        original = render_prompt(context)
        recovered = local_chat_budget.recovery_prompt(original)
        boundary = local_chat_budget.DATA_BOUNDARY
        guide, _, payload = recovered.rpartition(boundary)
        self.assertEqual(guide, local_chat_budget.GUIDANCE + named_topics.GUIDANCE)
        self.assertEqual(payload, original.rpartition(boundary)[2])
        for extra in ({'image_inputs': [{'id': 1}]},
                      {'documents': {'catalog': [{'id': 1}], 'excerpts': []}},
                      {'named_topic': {**context['named_topic'], 'title': '偽造題名'}},
                      {'current_request': '回到「青禾補貨」。請上傳原始資料。'}):
            with self.subTest(extra=extra):
                self.assertIsNone(local_chat_budget.recovery_prompt(render_prompt({**context, **extra})))
