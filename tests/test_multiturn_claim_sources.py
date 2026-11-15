import unittest
from pathlib import Path

from scripts.evaluate_multiturn_delivery import claim_support, question_matches, score, validate_cases
from scripts.evaluation_reports import strict_json


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

    def test_selected_claim_row_allows_context_elsewhere_and_rejects_a_conflicting_value(self):
        proof = '72 件 [D7:L2] [補充#5] [更正#1]'
        claim = self.case['claim_sources'][0]
        claim['row_selector'] = r'^\s*(?:[-*]\s+|\|\s*)?總件數'
        self.assertEqual(validate_cases([self.case]), [self.case])
        correct = f'總件數：{proof}'
        self.assertEqual(self.support(correct + '\n規則適用於總件數 72 件。'), [True])
        self.assertEqual(self.support(correct + '\n| 總件數 | 36 件 | [補充#2] |'), [False])
        self.assertEqual(self.support(correct + '\n- 總件數：72 件 [D7:L2]'), [False])
        self.assertEqual(self.support('規則適用於總件數 ' + proof), [False])
        claim['row_selector'] = '.*'
        with self.assertRaises(ValueError):
            validate_cases([self.case])

    def test_complete_question_variants_accept_intent_and_reject_a_different_field(self):
        first = self.case['replies'][0]
        first['question_forms'] = [r'請確認本批要裝機幾盤[？?]', r'本批裝機盤數是多少[？?]']
        with self.assertRaises(ValueError):
            validate_cases([self.case])
        del first['question_has']
        validate_cases([self.case])
        for question in ('請確認本批要裝機幾盤？', ' 本批裝機盤數是多少? '):
            self.assertTrue(question_matches(question, first))
        for question in ('請確認本批要留存幾盤？', '「請確認本批要裝機幾盤？」是原話，請告知回收盤數。',
                         '本批裝機盤數是多少？另外請問回收盤。', '本批裝機盤數已確認。'):
            self.assertFalse(question_matches(question, first))
        first['question_forms'] = ['.*']
        with self.assertRaises(ValueError):
            validate_cases([self.case])

    def test_print_folder_question_forms_keep_the_field_boundary(self):
        suite = Path(__file__).resolve().parents[1] / 'evals/goal_multiturn_rubric_calibration.json'
        cases = validate_cases(strict_json(suite.read_text(encoding='utf-8')))
        rule = next(case['replies'][0] for case in cases if case['id'] == 'print_folders_distributed_count')
        for question in ('這批講座講義袋實際派發了幾袋？', '本批實際派發了幾袋講義？', '本批派發袋數是多少？'):
            self.assertTrue(question_matches(question, rule))
        for question in ('本批實際回收了幾袋講義？', '本批每袋講義有幾份？',
                         '本批實際派發了幾袋講義？也請告訴我回收幾袋。'):
            self.assertFalse(question_matches(question, rule))

    def test_calibration_pending_rows_reject_wrong_state_even_with_matching_labels_and_citations(self):
        suite = Path(__file__).resolve().parents[1] / 'evals/goal_multiturn_rubric_calibration.json'
        cases = validate_cases(strict_json(suite.read_text(encoding='utf-8')))
        case = next(case for case in cases if case['id'] == 'audio_capsule_pending_check')
        final = {'sources': [{'document_id': 1}, {'document_id': 2}]}
        partial = '封袋：B-8 已完成 [D2:L1]\n盤點碼：B-8 尚未登記 [D2:L2]'
        self.assertEqual(claim_support(case, final, partial, []), [True, True])
        self.assertEqual(claim_support(case, final, partial.replace('已完成', '未完成'), []), [False, True])
        self.assertEqual(claim_support(case, final, partial.replace('尚未登記', 'Y-117，已確認'), []), [True, False])

    def test_final_pending_partial_is_checked_independently_from_the_question(self):
        self.case.pop('guidance')
        self.case['replies'] = []
        self.case.pop('claim_sources')
        self.case['must_match'] = ['盤點碼']
        self.case['expected_question'] = {'question_forms': [r'請問本批盤點碼為何[？?]'],
                                          'partial_has': ['封袋完成', '盤點碼仍未登記'], 'partial_omits': ['E-540']}
        validate_cases([self.case])
        self.final.update(status='needs_input', question='請問本批盤點碼為何？', result='', artifacts=[],
                          steps=[{'action': {'tool': 'ask'}, 'observation': {'question': '請問本批盤點碼為何？'}}])
        checks, _ = score(self.case, self.final, [], [], [])
        self.assertTrue(checks['waiting_for_required_data'])
        self.assertFalse(checks['partial_content'])
        self.final['partial_result'] = '封袋完成。盤點碼仍未登記。'
        self.assertTrue(score(self.case, self.final, [], [], [])[0]['partial_content'])
        self.final['partial_result'] += '舊值 E-540。'
        self.assertFalse(score(self.case, self.final, [], [], [])[0]['partial_content'])

    def test_recovery_requires_the_saved_waiting_checkpoint_before_the_reply(self):
        self.case['replies'][1].update(partial_has=['箱數待確認'], partial_omits=['32 件'], restore_before=True)
        self.final.update(status='report_ready', result='總件數 72 件 [D7:L2] [補充#5] [更正#1]',
                          steps=[{'action': {'tool': 'finish'}}])
        before = self.turns[1]['before']
        before.update(status='needs_input', artifacts=[], result='', partial_result='箱數待確認。')
        self.assertEqual(validate_cases([self.case]), [self.case])
        checks, _ = score(self.case, self.final, [], self.turns, [])
        self.assertTrue(checks['intermediate_waiting'])
        self.assertFalse(checks['restored_waiting'])
        self.turns[1]['restored'] = True
        self.assertTrue(score(self.case, self.final, [], self.turns, [])[0]['restored_waiting'])
        for change in ({'artifacts': ['draft.md']}, {'result': '已交付'}, {'status': 'paused'},
                       {'partial_result': '箱數待確認。現行總件數 32 件。'}, {'partial_result': ''}):
            with self.subTest(change=change):
                candidate = [self.turns[0], {**self.turns[1], 'before': {**before, **change}}]
                self.assertFalse(score(self.case, self.final, [], candidate, [])[0]['intermediate_waiting'])
        self.assertFalse(score(self.case, self.final, [], self.turns[:1], [])[0]['intermediate_waiting'])
        del self.case['replies'][1]['partial_has']
        with self.assertRaises(ValueError):
            validate_cases([self.case])
