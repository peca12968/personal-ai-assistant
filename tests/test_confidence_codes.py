import unittest

from scripts.learning.confidence_codes import prompt, scores


class ConfidenceCodesTests(unittest.TestCase):
    def test_counterbalance_keeps_original_question_and_answer(self):
        a = prompt('樣本問題', '原始回答', '甲')
        b = prompt('樣本問題', '原始回答', '乙')
        self.assertIn('甲＝至少八成，乙＝低於八成', a)
        self.assertIn('甲＝低於八成，乙＝至少八成', b)
        self.assertEqual(a.split('\n')[1:], b.split('\n')[1:])

    def test_decode_before_comparing_and_preserve_missing_values(self):
        trials = [dict(case=i, high_code=code, numeric_high=(None if i == 2 else True), exact=i == 0)
                  for i in range(3) for code in ('甲乙' if i % 2 == 0 else '乙甲')]
        result = scores(trials, ['甲','乙','甲','甲',None,'低'])
        self.assertEqual((result['paired'], result['consistent']), (2, 1))
        self.assertEqual((result['numeric_matches'], result['numeric_compared']), (3, 4))
        self.assertEqual(result['high_and_valid_counts'], {'correct': [2, 2], 'incorrect': [1, 2]})
        self.assertEqual((result['engine_errors'], result['format_failures']), (1, 1))
        with self.assertRaises(ValueError):
            scores(trials, ['甲'])

