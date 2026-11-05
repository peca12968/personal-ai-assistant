from pathlib import Path
import tempfile
import unittest

from assistant.engine import Assistant, render_prompt
from assistant.response_delivery import read
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine, _scoped_missing_label, extract
from scripts.daily_expenses import DailyAssistant


class MissingDeliveryTests(unittest.TestCase):
    def test_explicit_label_and_same_subject_unknown(self):
        for literal in ('資料不足', '資訊不足', '無法確定'):
            q = f'外箱編號是多少？若無法確定，只輸出 {literal}。'
            for text in ('外箱編號未提供，無法得知', '外箱編號尚未提供，因此無法確定',
                         '外箱編號不詳'):
                raw = literal + '。' + text + '。'
                self.assertEqual(extract(q, raw), literal)

    def test_only_remove_punctuation_from_selected_missing_label(self):
        q = '樣品盒重量是多少？若無法得知，只輸出 資料不足。'
        for raw in ('資料不足', '資料不足。', '資料不足！'):
            self.assertEqual(extract(q, raw), '資料不足')
        for raw in ('資訊不足。', '也許資料不足。', '「資料不足」。'):
            self.assertEqual(extract(q, raw), raw)

    def test_unknown_must_match_complete_requested_subject(self):
        q = '大樣品盒重量是多少？若無法得知，只輸出 資料不足。'
        for raw in ('無法得知樣品盒重量。', '資料不足。樣品盒重量未提供，無法得知。',
                    '資料不足。大樣品盒編號未提供，無法得知。'):
            self.assertEqual(extract(q, raw), raw)
        self.assertEqual(extract(q, '無法得知大樣品盒重量。'), '資料不足')

    def test_do_not_hide_later_answer_or_extra_qualifications(self):
        q = '樣品盒重量是多少？若無法得知，只輸出 資料不足。'
        for raw in ('資料不足。樣品盒重量未提供，但其實已量到5公斤。',
                    '資料不足。樣品盒重量未提供。現在已有紀錄為5公斤。',
                    '資料不足。樣品盒重量可能未提供，無法得知。',
                    '資料不足。若樣品盒重量未提供，無法得知。',
                    '資料不足。引文說樣品盒重量未提供，無法得知。'):
            self.assertEqual(extract(q, raw), raw)

    def test_keep_positive_answers_and_additional_questions_before_missing_fallback(self):
        q = '樣品箱重量是多少？若無法得知，只輸出 資料不足。'
        absence = '無法得知樣品箱重量。'
        followup = '請補充秤重資料。'
        for raw in ('樣品箱重量為 6 公斤。' + absence,
                    '樣品箱重量為 6 公斤。' + absence + followup,
                    absence + '請補充秤重資料，但已量得6公斤，請確認。'):
            with self.subTest(raw=raw):
                self.assertEqual(extract(q, raw), raw)
        self.assertEqual(extract(q, absence + followup), '資料不足')
        multi = q.replace('若無法', '何時秤重？若無法')
        self.assertEqual(extract(multi, absence), absence)
        self.assertEqual(extract(multi, '資料不足。'), '資料不足。')
        quoted = '依這句回答「有重量嗎？」。' + q
        self.assertEqual(extract(quoted, absence), '資料不足')

    def test_multiple_questions_and_quoted_requests_remain_unshortened(self):
        raw = '無法得知大樣品盒重量。'
        for q in ('大樣品盒重量是多少？小樣品盒重量是多少？若無法得知，只輸出 資料不足。',
                  '翻譯「大樣品盒重量是多少？若無法得知，只輸出 資料不足。」'):
            self.assertEqual(extract(q, raw), raw)

    def test_legacy_complete_field_and_optional_document_category(self):
        q = '依唯一資料回答。問：Q28 的含稅金額是多少？若無法得知，只輸出 資料不足。'
        raw = '根據現有資料，無法得知報價單 Q28 的含稅金額。請提供相關文件以進行查詢。'
        self.assertEqual(extract(q, raw), '資料不足')
        q = '資料表 Q28 的含稅金額是多少？若無法得知，只輸出 資料不足。'
        self.assertEqual(extract(q, raw), raw)

    def test_unqualified_document_id_uses_the_category_in_current_source(self):
        question = '原文：「收據 C31 已留存。」問：C31 的核准日期是什麼？若無法得知，只輸出 資料不足。'
        raw = '依記錄，無法得知收據 C31 的核准日期。請提供相關資料。'
        self.assertEqual(extract(question, raw), '資料不足')
        self.assertEqual(extract(question.replace('收據', '發票'), raw.replace('收據', '發票')), '資料不足')
        for q, reply in (
                (question.replace('收據', '發票'), raw),
                (question.replace('收據 C31', '收據 C310'), raw),
                (question.replace('收據 C31', 'C31'), raw),
                (question.replace('已留存。', '已留存。發票 C31 已寄出。'), raw),
                (question.replace('問：C31', '問：資料表 C31'), raw),
                (question, raw.replace('C31', 'C310')),
                (question, raw.replace('核准', '申請')),
                (question, raw.replace('收據', '報價單'))):
            with self.subTest(question=q, raw=reply):
                self.assertEqual(extract(q, reply), reply)

    def test_final_fallback_can_follow_two_absence_statements_for_the_same_field(self):
        q = '原文：「發票 C32 已留存。」問：C32 的核准日期是什麼？若無法得知，只輸出 資料不足。'
        initial = '根據提供的記錄，資料中未提及發票 C32 的核准日期。'
        raw = initial + '因此，無法確定 C32 的核准日期。\n資料不足。'
        self.assertEqual(extract(q, raw), '資料不足')
        self.assertEqual(extract(q, initial + '資料不足。'), '資料不足')
        for other in (raw.replace('因此，無法確定', '因此，已確認'),
                      raw.replace('C32 的核准日期。\n', 'C32 的申請日期。\n'),
                      raw.replace('發票 C32', '收據 C32'),
                      raw.replace('提供的記錄', '已確定五月核准的記錄'),
                      raw.replace('未提及', '可能未提及'),
                      raw + '不過已查到是五月。'):
            with self.subTest(raw=other):
                self.assertEqual(extract(q, other), other)

    def test_new_delivery_saves_real_raw_without_extra_call(self):
        class Engine:
            display_name = 'fixture'
            calls = 0
            raw = '資料不足。外箱編號未提供，無法得知。'
            def describe(self): return {'kind':'fixture'}
            def answer(self, prompt, cancel=None):
                self.calls += 1
                return self.raw
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            raw = Engine()
            engine = DailyLocalEngine(raw)
            q = '外箱編號是多少？若無法得知，只輸出 資料不足。'
            answer = Assistant(store, engine).ask(store.new_session(), q)
            self.assertEqual(answer['content'], '資料不足')
            self.assertEqual(raw.calls, 1)
            self.assertEqual(read(store, answer['id'])['raw_answer'], raw.raw)
            result = Assistant(store, engine, allow_local_calculation=False).ask(store.new_session(), q)
            self.assertEqual(result['content'], raw.raw)
            self.assertIsNone(read(store, result['id']))

    def test_standalone_fallback_preserves_exact_literal_in_both_languages(self):
        examples = (
            ('只根據這則訊息回答。外箱編號是什麼？資料不足時只回「未提供」。', '未提供。', '未提供'),
            ('外箱編號是多少？若無法得知，只回「Ｎ／Ａ」。請只根據本則訊息回答。', 'Ｎ／Ａ。', 'Ｎ／Ａ'),
            ('Answer using only this message. Is the layout specified? Reply with exactly Not specified if the layout is missing.',
             'Not specified.', 'Not specified'),
            ('What is the room number? If the room number is missing, reply with exactly "Unavailable". '
             'Use only the current message to answer.', 'Unavailable.', 'Unavailable'),
            ('只根據這則訊息回答。外箱編號是什麼？資料不足時只回「未提供！」。', '未提供！。', '未提供！'),
        )
        for question, raw, literal in examples:
            with self.subTest(question=question):
                self.assertEqual(_scoped_missing_label(question, raw), literal)

    def test_standalone_fallback_preserves_other_text_and_ambiguous_contracts(self):
        question = '只根據這則訊息回答。外箱編號是什麼？資料不足時只回「未提供」。'
        for raw in ('未提供', '未提供！', '未提供。。', '「未提供」。', '也許未提供。',
                    '未提供。後來確認是 ZA-42。', '未提供。 因為查不到。', ' 未提供。'):
            with self.subTest(raw=raw):
                self.assertIsNone(_scoped_missing_label(question, raw))
        for other in (
                question.replace('只根據這則訊息回答。', ''),
                question.replace('外箱編號是什麼？', '外箱編號是什麼？日期呢？'),
                question.replace('外箱編號是什麼？', '「外箱編號是什麼？'),
                question.replace('資料不足時', '不要在資料不足時'),
                question.replace('資料不足時只回「未提供」。', '「資料不足時只回『未提供』。」'),
                question.replace('「未提供」', '「未提供』'),
                question.replace('「未提供」', '「未提供。」'),
                question + '並解釋原因。'):
            with self.subTest(question=other):
                self.assertIsNone(_scoped_missing_label(other, '未提供。'))

    def test_daily_standalone_delivery_records_raw_and_model_only_stays_raw(self):
        class Engine:
            display_name = 'fixture'

            def __init__(self):
                self.prompts = []

            def describe(self):
                return {'kind': 'fixture'}

            def answer(self, prompt, cancel=None):
                self.prompts.append(prompt)
                return 'Not specified.'

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            raw = Engine()
            engine = DailyLocalEngine(raw)
            question = ('Answer using only this message. Is the layout specified? '
                        'Reply with exactly Not specified if the layout is missing.')
            result = DailyAssistant(store, engine).ask(store.new_session(), question)
            self.assertEqual(result['content'], 'Not specified')
            self.assertEqual(read(store, result['id'])['raw_answer'], 'Not specified.')
            self.assertEqual(raw.prompts, [render_prompt(store.answer_context(result['id']))])
            model_only = DailyAssistant(store, engine, allow_local_calculation=False)
            untouched = model_only.ask(store.new_session(), question)
            self.assertEqual(untouched['content'], 'Not specified.')
            self.assertIsNone(read(store, untouched['id']))
