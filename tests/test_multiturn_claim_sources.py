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

    def test_waiting_checkpoint_matches_the_same_fields_in_several_presentations(self):
        fields = [{'labels': ['倉區'], 'value_forms': [r'西倉（暫定）']},
                  {'labels': ['待維護件數', '待維護筆數與件數'], 'value_forms': ['待查']}]
        self.case['replies'][1].update(partial_fields=fields, restore_before=True)
        self.final.update(status='report_ready', result='總件數 72 件 [D7:L2] [補充#5] [更正#1]',
                          steps=[{'action': {'tool': 'finish'}}])
        self.turns[1]['restored'] = True
        before = self.turns[1]['before']
        before.update(status='needs_input', result='', artifacts=[])
        validate_cases([self.case])
        previews = [
            '| 倉區 | 待維護筆數 | 待維護件數 |\n|---|---:|---:|\n| 西倉（暫定） | 待查 | 待查 |',
            '倉區 | 待維護件數\n--- | ---:\n西倉（暫定） | 待查',
            '| 欄位 | 目前值 |\n|---|---|\n| 倉區 | 西倉（暫定） |\n| 待維護件數 | 待查 |',
            '- **倉區：** 西倉（暫定） [D7:L1]\n- 待維護筆數與件數：待查。',
        ]
        for preview in previews:
            with self.subTest(preview=preview):
                before['partial_result'] = preview
                checks, _ = score(self.case, self.final, [], self.turns, [])
                self.assertTrue(checks['intermediate_waiting'])
                self.assertTrue(checks['restored_waiting'])

    def test_waiting_citations_use_the_same_row_and_the_sources_at_that_checkpoint(self):
        rule = self.case['replies'][1]
        rule.update(partial_has=['待查'], restore_before=True, partial_claim_sources=[
            {'claim': '每箱.{0,6}9 件', 'sources': [
                {'kind': 'document', 'doc': 0, 'line': 2, 'text': '每箱 9 件'},
                {'kind': 'guidance', 'text': '本批改採新版'},
            ], 'forbidden_sources': [{'kind': 'document', 'doc': 0, 'line': 1, 'text': '新版'}]},
            {'claim': '前次確認.{0,6}4 箱', 'sources': [
                {'kind': 'reply', 'index': 0, 'text': '先確認 4 箱'},
            ]},
        ])
        self.final.update(status='report_ready', result='總件數 72 件 [D7:L2] [補充#5] [更正#1]',
                          steps=[{'action': {'tool': 'finish'}}])
        partial = '每箱：9 件 [D7:L2] [更正#1]\n前次確認：4 箱 [補充#2]\n總量：待查'
        before = self.turns[1]['before']
        before.update(status='needs_input', artifacts=[], result='', partial_result=partial,
                      sources=self.final['sources'], guidance_sources=self.final['guidance_sources'],
                      reply_sources=self.final['reply_sources'][:1])
        self.turns[1]['restored'] = True
        validate_cases([self.case])
        guidance = {'kind': 'guidance', 'saved': True}
        checks, metrics = score(self.case, self.final, [], [guidance, *self.turns], [])
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual((metrics['intermediate_claim_sources_supported'],
                          metrics['intermediate_claim_sources_required']), (2, 2))
        for change in (
            {'partial_result': partial.replace('[D7:L2]', '[D7:L1]')},
            {'partial_result': partial.replace('[D7:L2]', '[D7:L1-L2]')},
            {'partial_result': partial.replace('[D7:L2]', '\n來源 [D7:L2]')},
            {'partial_result': partial.replace('[補充#2]', '[補充#5]')},
            {'guidance_sources': []}, {'reply_sources': []}, {'partial_result_historical': True},
        ):
            with self.subTest(change=change):
                candidate = [self.turns[0], {**self.turns[1], 'before': {**before, **change}}]
                failed, measured = score(self.case, self.final, [], candidate, [])
                self.assertTrue(failed['intermediate_waiting'])
                self.assertTrue(failed['claim_sources'])
                self.assertFalse(failed['intermediate_claim_sources'])
                self.assertLess(measured['intermediate_claim_sources_supported'], 2)
        self.assertFalse(score(self.case, self.final, [], self.turns[:1], [])[0]['intermediate_claim_sources'])

    def test_waiting_citation_rubric_rejects_current_and_later_replies(self):
        self.case['replies'][0]['partial_claim_sources'] = [
            {'claim': '每箱 9 件', 'sources': [{'kind': 'document', 'doc': 0, 'line': 2, 'text': '每箱 9 件'}]}
        ]
        self.case['replies'][0]['restore_before'] = True
        validate_cases([self.case])
        claim = self.case['replies'][0]['partial_claim_sources'][0]
        for index, answer in enumerate((self.old_answer, self.answer)):
            with self.subTest(index=index):
                claim['sources'] = [{'kind': 'reply', 'index': index, 'text': answer}]
                with self.assertRaisesRegex(ValueError, '超出當時可用'):
                    validate_cases([self.case])

    def test_structured_waiting_rejects_pending_from_another_cell_or_literal_example(self):
        self.case.pop('guidance')
        self.case.pop('claim_sources')
        self.case['replies'] = []
        self.case['must_match'] = ['箱數']
        self.case['expected_question'] = {'question_has': ['箱數'], 'partial_omits': ['舊記錄'],
            'partial_fields': [{'labels': ['件數'], 'value_forms': ['待查']}]}
        self.final.update(status='needs_input', question='本批箱數為何？', result='', artifacts=[],
                          steps=[{'action': {'tool': 'ask'}, 'observation': {'question': '本批箱數為何？'}}])
        validate_cases([self.case])
        wrong = [
            '| 筆數 | 件數 |\n|---|---|\n| 待查 | 25 |',
            '| 欄位 | 狀態 |\n|---|---|\n| 筆數 | 待查 |\n| 件數 | 25 |',
            '件數：待查\n件數：25',
            '件數：未待查',
            '件數：待查但估計 25 件',
            '件數：待查\n| 筆數 | 件數 |\n|---|---|\n| 待查 |',
            '件數：待查\n| 件數 | 件數 |\n|---|---|\n| 待查 | 25 |',
            '```markdown\n件數：待查\n```',
            '> 件數：待查',
            '    件數：待查',
            '件數：待查\n舊記錄',
        ]
        for preview in wrong:
            with self.subTest(preview=preview):
                self.final['partial_result'] = preview
                checks, _ = score(self.case, self.final, [], [], [])
                self.assertTrue(checks['waiting_for_required_data'])
                self.assertFalse(checks['partial_content'])
        self.final['partial_result'] = '件數：待查 [更正#1]'
        self.assertTrue(score(self.case, self.final, [], [], [])[0]['partial_content'])

    def test_field_rubric_rejects_ambiguous_labels_and_accepts_paired_runs(self):
        import json
        from tempfile import TemporaryDirectory
        from scripts.evaluate_multiturn_pair import cases_for

        rule = {'answer': '三箱。', 'question_has': ['箱數'],
                'partial_fields': [{'labels': ['件數'], 'value_forms': ['待查']}]}
        case = {'id': 'paired_waiting_fields', 'prompt': '先問箱數。', 'file_delivery': False,
                'replies': [rule], 'must_match': ['三箱']}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'suite.json'
            path.write_text(json.dumps([case], ensure_ascii=False), encoding='utf-8')
            self.assertEqual(cases_for(path), [case])
        for fields in ([], [{'labels': ['件數'], 'value_forms': ['.*']}],
                       [{'labels': ['件數', '件數'], 'value_forms': ['待查']}],
                       [{'labels': ['件數'], 'value_forms': ['待查']},
                        {'labels': ['筆數', '件數'], 'value_forms': ['待確認']}]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_cases([{**case, 'replies': [{**rule, 'partial_fields': fields}]}])
