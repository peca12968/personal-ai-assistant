import unittest

from scripts.evaluate_multiturn_delivery import claim_support, validate_cases


class MultiturnClaimSourcesTests(unittest.TestCase):
    def setUp(self):
        self.old_answer = '先確認 4 箱。'
        self.answer = '箱數更正為 8 箱。'
        self.guidance = '本批改採新版。'
        self.case = {
            'id': 'batch_count', 'prompt': '列出結果及其來源。',
            'file_delivery': False, 'first_steps': 1, 'guidance': self.guidance,
            'documents': [{'content': '新版\n每箱 9 件。'}],
            'replies': [{'answer': answer, 'question_has': ['箱數']}
                        for answer in (self.old_answer, self.answer)],
            'must_match': ['總件數'],
            'claim_sources': [{
                'claim': '總件數[^\n]*72\\s*件',
                'sources': [
                    {'kind': 'document', 'doc': 0, 'line': 2, 'text': '每箱 9 件'},
                    {'kind': 'reply', 'index': 1, 'text': '箱數更正為 8 箱'},
                    {'kind': 'guidance', 'text': '本批改採新版'},
                ],
            }],
        }
        self.final = {
            'sources': [{'document_id': 7}],
            'reply_sources': [{'ask_sequence': sequence, 'citation': f'[補充#{sequence}]', 'answer': answer}
                              for sequence, answer in ((2, self.old_answer), (5, self.answer))],
            'guidance_sources': [{'citation': '[更正#1]', 'content': self.guidance}],
        }
        self.turns = [{'kind': 'reply', 'before': {'steps': [{'sequence': sequence}]}, 'saved': True}
                      for sequence in (2, 5)]

    def support(self, report):
        return claim_support(self.case, self.final, report, self.turns)

    def test_support_requires_correct_document_range_and_actual_reply_in_the_claim_row(self):
        self.assertEqual(self.support('| 總件數 | 72 件 | [D7:L1-L2] [補充#5] [更正#1] |'), [True])
        for report in (
            '總件數 72 件 [D7:L1] [補充#5] [更正#1]',
            '總件數 72 件 [D1:L2] [補充#5] [更正#1]',
            '總件數 72 件 [D7:L2] [補充#2] [更正#1]',
            '總件數 72 件 [D7:L2] [補充#5] [更正#2]',
            '總件數 72 件\n來源 [D7:L2] [補充#5] [更正#1]',
            '總件數 72 件 [D7:L2] [補充#5] [更正#1]\n總件數 72 件',
        ):
            with self.subTest(report=report):
                self.assertEqual(self.support(report), [False])

    def test_rubric_rejects_source_excerpt_mismatch_and_negative_index(self):
        self.assertEqual(validate_cases([self.case]), [self.case])
        source = self.case['claim_sources'][0]['sources'][1]
        source['text'] = '先確認 4 箱'
        with self.assertRaises(ValueError):
            validate_cases([self.case])
        source.update(text='箱數更正為 8 箱', index=-1)
        with self.assertRaises(ValueError):
            validate_cases([self.case])

    def test_old_evidence_cannot_be_mixed_into_the_current_claim(self):
        claim = self.case['claim_sources'][0]
        claim['forbidden_sources'] = [
            {'kind': 'document', 'doc': 0, 'line': 1, 'text': '新版'},
            {'kind': 'reply', 'index': 0, 'text': '先確認 4 箱'},
        ]
        self.assertEqual(validate_cases([self.case]), [self.case])
        correct = '總件數 72 件 [D7:L2] [補充#5] [更正#1]'
        self.assertEqual(self.support(correct + '\n先前值僅供歷史核對 [補充#2]'), [True])
        self.assertEqual(self.support(correct + ' [補充#2]'), [False])
        self.assertEqual(self.support(correct.replace('[D7:L2]', '[D7:L1-L2]')), [False])
        claim['forbidden_sources'][0]['text'] = '不存在的摘錄'
        with self.assertRaises(ValueError):
            validate_cases([self.case])
