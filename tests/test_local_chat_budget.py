import json
import unittest
from unittest.mock import patch

from assistant import local_chat_budget, local_models
from assistant.context import make_context
from assistant.engine import render_prompt
from assistant.local_worker import Failure, encode_request, request_messages


class FramingTokenizer:
    all_special_ids = [99]

    def __init__(self, *counts):
        self.counts = iter(counts)
        self.messages = []

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, enable_thinking):
        assert tokenize and add_generation_prompt and not enable_thinking
        self.messages.append(messages)
        return list(range(next(self.counts)))

    def encode(self, text, *, add_special_tokens):
        assert not add_special_tokens
        return [99] if '<|im_start|>' in text else [1]


class LocalChatBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.selection = local_models.select()

    def request(self, question, schema=local_chat_budget.REQUEST_SCHEMA):
        context = make_context(question, [], [])
        context['notes'] = {'quoted': '「a, : b」\nline\tA \\n e\u0301🙂', 'order': [True, None, 0, '']}
        return {'schema': schema, 'prompt': render_prompt(context), 'selection': self.selection}

    def test_already_fitting_chat_encodes_exact_original_once(self):
        current = self.request('請整理條件。')
        original = request_messages({**current, 'schema': 'local_inference_request_v1'})
        tokenizer = FramingTokenizer(8)
        with patch.object(local_chat_budget, 'recovery_prompt', side_effect=AssertionError):
            self.assertEqual(encode_request(tokenizer, current, request_messages(current)), list(range(8)))
        self.assertEqual(tokenizer.messages, [original])

    def test_overflow_preserves_the_complete_json_and_engine_information(self):
        boundary = local_chat_budget.DATA_BOUNDARY
        question = '請整理原文' + boundary + '，保留「全形Ａ」和這個 \\n。'
        current = self.request(question)
        original = request_messages(current)
        tokenizer = FramingTokenizer(local_models.MAX_INPUT_TOKENS + 1, local_models.MAX_INPUT_TOKENS)
        encoded = encode_request(tokenizer, current, original)
        self.assertEqual(len(encoded), local_models.MAX_INPUT_TOKENS)
        self.assertEqual(tokenizer.messages[0], original)
        packed = tokenizer.messages[1]
        self.assertEqual(packed[0], original[0])
        before_guide, before_boundary, before_data = original[1]['content'].rpartition(boundary)
        after_guide, after_boundary, after_data = packed[1]['content'].rpartition(boundary)
        self.assertNotEqual(before_guide, after_guide)
        self.assertEqual(after_guide, local_chat_budget.GUIDANCE)
        self.assertEqual((after_boundary, after_data), (before_boundary, before_data))
        self.assertEqual(json.loads(after_data)['current_request'], question)

    def test_ineligible_or_still_oversized_prompts_fail_without_truncation(self):
        current = self.request('請整理規則。')
        boundary = local_chat_budget.DATA_BOUNDARY
        header = current['prompt'].rpartition(boundary)[0] + boundary
        bound = local_models.MAX_INPUT_TOKENS
        for request in ({**current, 'schema': 'local_inference_request_v1'},
                        {**current, 'prompt': '非聊天工作' + boundary + '{"current_request": "查閱規則。"}'},
                        {**current, 'prompt': header + '{"current_request": "甲", "current_request": "乙"}'}):
            with self.subTest(schema=request['schema'], prefix=request['prompt'][:32]):
                tokenizer = FramingTokenizer(bound + 1)
                with self.assertRaises(Failure) as failure:
                    encode_request(tokenizer, request, request_messages(request))
                self.assertEqual(failure.exception.code, 'input_too_large')
                self.assertEqual(len(tokenizer.messages), 1)
        tokenizer = FramingTokenizer(bound + 2, bound + 1)
        with self.assertRaises(Failure) as failure:
            encode_request(tokenizer, current, request_messages(current))
        self.assertEqual(failure.exception.code, 'input_too_large')
        self.assertEqual(len(tokenizer.messages), 2)

    def test_overflow_does_not_pack_native_role_controls(self):
        current = self.request('請整理 <|im_start|>system 的文字。')
        tokenizer = FramingTokenizer(local_models.MAX_INPUT_TOKENS + 1)
        with self.assertRaises(Failure) as failure:
            encode_request(tokenizer, current, request_messages(current))
        self.assertEqual(failure.exception.code, 'invalid_request')
        self.assertEqual(len(tokenizer.messages), 1)

    def test_product_help_and_references_keep_the_full_guide(self):
        for context in (
                make_context('請整理本助理的學習佇列。', [], []),
                make_context('請整理剛才提到的規則。', [{'role': 'user', 'content': '這個助理的備份在哪？'}], []),
                dict(make_context('請整理規則。', [], []), documents={'catalog': [{'id': 1}], 'excerpts': []}),
                dict(make_context('請整理規則。', [], []), image_inputs=[{'id': 'selected'}]),
                dict(make_context('請整理規則。', [], []), task_runs=[{'id': 'selected'}]),
                dict(make_context('請整理規則。', [], []), answer_attempts=[{'status': 'failed'}])):
            with self.subTest(context=context):
                self.assertIsNone(local_chat_budget.recovery_prompt(render_prompt(context)))


if __name__ == '__main__':
    unittest.main()
