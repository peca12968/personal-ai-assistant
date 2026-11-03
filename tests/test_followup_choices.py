import json
import tempfile
import unittest
from pathlib import Path

from assistant.acceptance import validate_suite
from assistant.context import followup_query, is_followup
from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_understanding import prepare_case, validate_cases

ROOT = Path(__file__).resolve().parents[1]

class FollowupChoiceTests(unittest.TestCase):
    def test_retained_proposal_and_withheld_cases(self):
        for name in ('followup_choices', 'followup_choices_holdout'):
            suite = json.loads((ROOT / 'evals/improvement' / (name + '.json')).read_text(encoding='utf-8'))
            validate_suite(suite)
            for case in suite['cases']:
                with self.subTest(suite=name, case=case['id']):
                    self.assertEqual(followup_query(case['question'], case['history']), case['expect'])

    def test_real_context_retrieval_without_memory_mutation(self):
        cases = validate_cases(json.loads((ROOT / 'evals/followup_choices.json').read_text(encoding='utf-8')))
        for case in cases:
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'state.db')
                sid = prepare_case(store, case)
                before = store.memories()
                self.assertEqual(store.retrieve(case['prompt']), [])
                context = Assistant(store, None).prepare_context(sid, case['prompt'])
                expected = [case['memories'][0]['content']] if case['category'] == 'followup' else []
                self.assertEqual([m['content'] for m in context['memories']], expected)
                self.assertEqual(context['current_request'], case['prompt'])
                self.assertEqual(store.memories(), before)
                other = store.new_session()
                self.assertEqual(Assistant(store, None).prepare_context(other, case['prompt'])['memories'], [])

    def test_raw_user_chain_limit_and_reset_are_preserved(self):
        raw = '  我選第２個。  '
        history = [{'role': 'user', 'content': 'private_anchor'}]
        for _ in range(6):
            history += [{'role': 'user', 'content': raw}, {'role': 'assistant', 'content': 'assistant_claim'}]
        self.assertEqual(followup_query('I choose option two.', history), '\n'.join([raw] * 4))
        self.assertIsNone(followup_query('我選第二個。不要沿用前文。', history))
        self.assertIsNone(followup_query('I choose option two. New topic.', history))
        self.assertIsNone(followup_query('我選第二個。', []))

    def test_new_grammar_does_not_discard_qualifications_or_expand_prefixes(self):
        negatives = [
            '我選第二個，「但還沒決定」。',
            'I choose option two, "if it is safe".',
            '請幫我繼續分析另一個問題。',
            'Could you please explain the previous answer?',
            '我選第二個，請繼續。這是引用例子。',
            'I choose option two. Do not use the previous context.',
            '```text\nI choose option two.\n```',
        ]
        for question in negatives:
            with self.subTest(question=question):
                self.assertFalse(is_followup(question))

if __name__ == '__main__':
    unittest.main()
