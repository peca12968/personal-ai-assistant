import json
import unittest

from scripts.evaluate_understanding import ROOT, mechanical_check, validate_cases


class ClaimJsonEvaluationTests(unittest.TestCase):
    def check(self, answer):
        return mechanical_check({'check':'claim_json'}, answer)

    def test_complete_unicode_proposal_has_only_syntax_and_label_checks(self):
        answer=json.dumps({'claim':'喇叭現在可以正常播放聲音。'},ensure_ascii=False)
        self.assertEqual(self.check(answer), {'claim_json_shape':True, 'claim_not_label':True})
        self.assertEqual(mechanical_check({}, answer), {})

    def test_observed_answer_labels_cannot_masquerade_as_function_proposals(self):
        for label in ('已確認成立', '已確認未知', '未知', '不成立', ' 成立。 ', 'TRUE', 'Unknown'):
            self.assertEqual(self.check(json.dumps({'claim':label})),
                             {'claim_json_shape':True, 'claim_not_label':False})

    def test_ambiguous_extra_invalid_and_oversize_json_is_not_accepted(self):
        examples = ['{"claim":"能播放","claim":"不能播放"}',
                    '{"claim":"能播放","answer":"成立"}',
                    '{"claim":null}', '{"claim":{"text":"能播放"}}',
                    '{"claim":""}', '{"claim":"   "}', '{"claim":NaN}',
                    '[{"claim":"能播放"}]', '```json\n{"claim":"能播放"}\n```',
                    json.dumps({'claim':'聲'*201}), json.dumps({'claim':' '*201+'能播放'}),
                    json.dumps({'claim':'有\x00聲'}),
                    json.dumps({'claim':'有\ud800聲'}), ' '*8193, '\ud800', None]
        for answer in examples:
            with self.subTest(answer=repr(answer)):
                self.assertEqual(self.check(answer), {'claim_json_shape':False, 'claim_not_label':False})

    def test_valid_shape_never_certifies_polarity_or_question_understanding(self):
        for claim in ('喇叭已故障而無法發出聲音。', '喇叭是否能出聲尚未測試。', '這是無關的房間。'):
            self.assertEqual(self.check(json.dumps({'claim':claim})),
                             {'claim_json_shape':True, 'claim_not_label':True})

    def test_extraction_suite_keeps_the_original_questions_and_new_rubric_ids(self):
        rows=json.loads((ROOT/'evals/function_claim_extraction.json').read_text(encoding='utf8'))
        original=json.loads((ROOT/'evals/epistemic_learning.json').read_text(encoding='utf8'))
        validate_cases(rows)
        self.assertEqual(len(rows),10)
        self.assertEqual({r['source_question'] for r in rows if r['study_role']=='development'},
                         {r['prompt'] for r in original['heldout']})
        self.assertEqual(sum(r['study_role']=='transfer' for r in rows),4)
        for row in rows:
            self.assertTrue(row['id'].startswith('extract_function_'))
            self.assertEqual(row['check'],'claim_json')
            self.assertIn(json.dumps(row['source_question'],ensure_ascii=False),row['prompt'])
            self.assertIn('肯定功能',row['criteria'][1])
