import copy
import hashlib
import unittest

from assistant import document_lines, document_ranges
from scripts.evaluate_multiturn_delivery import score, validate_cases
from scripts.multiturn_read_coverage import measure


class MultiturnReadCoverageTests(unittest.TestCase):
    def setUp(self):
        self.case = {
            'id': 'verified_read', 'prompt': '核對本批規則。', 'file_delivery': False,
            'documents': [
                {'name': 'current.txt', 'content': '本批\n使用白桌。\n依公告核對。'},
                {'name': 'old.txt', 'content': '前批\n使用銀台。\n僅供比對。'},
            ],
            'must_match': ['白桌'],
        }
        self.ranges = [
            {'doc': 0, 'start_line': 1, 'end_line': 3},
            {'doc': 1, 'start_line': 2, 'end_line': 2},
        ]

    def state(self):
        docs = [{**row, 'id': did, 'sha256': hashlib.sha256(row['content'].encode('utf-8')).hexdigest()}
                for did, row in zip((7, 12), self.case['documents'], strict=True)]
        sources = [{'document_id': row['id'], 'name': row['name'], 'sha256': row['sha256']} for row in docs]
        return docs, dict(sources=sources, steps=[])

    @staticmethod
    def read_request(doc, low, high):
        return dict(document_id=doc, start_line=low, end_line=high)

    def step(self, tool, observation, **args):
        return dict(action=dict(tool=tool, args=args), observation=observation)

    def read(self, docs, did, low, high):
        excerpts, requests, _ = document_ranges.select_ranges(f'[D{did}:L{low}-L{high}]', docs, 1, 10000)
        return self.step('read', dict(excerpts=excerpts, requested_ranges=requests))

    def test_full_range_can_be_read_in_one_batch_or_several_steps(self):
        docs, state = self.state()
        ranges = [self.read_request(7, 1, 3), self.read_request(12, 2, 2)]
        batch = self.step('read_ranges', document_ranges.read_batch(docs, {'ranges': ranges}))
        state['steps'] = [batch]
        self.assertEqual(measure(self.case, state, self.ranges), [True, True])
        for reads, result in (
            ([(7, 1, 2), (7, 3, 3), (12, 2, 2)], [True, True]),
            ([(7, 1, 1), (7, 3, 3), (12, 2, 2)], [False, True]),
            ([(7, 1, 2), (7, 3, 4), (12, 2, 2)], [False, True]),
            ([(7, 1, 3), (7, 2, 2)], [True, False]),
        ):
            with self.subTest(reads=reads):
                state['steps'] = [self.read(docs, *read) for read in reads]
                self.assertEqual(measure(self.case, state, self.ranges), result)

    def test_long_line_requires_exact_contiguous_pages_of_the_current_version(self):
        self.case['documents'][0]['content'] = '本批\n' + '甲' * 4010 + '正式地方白桌'
        docs, state = self.state()
        requested = [{'doc': 0, 'start_line': 1, 'end_line': 2}]
        prefix = self.read(docs, 7, 1, 1)
        pages = [self.step('read_line', document_lines.read(docs, dict(document_id=7, line_number=2, start_char=start)))
                 for start in (1, 4001)]
        state['steps'] = [prefix, *pages]
        self.assertEqual(measure(self.case, state, requested), [True])
        for mutation in ({'sha256': '0' * 64}, {'text': 'L2: ' + '乙' * 16}, {'partial_line': False},
                         {'start_char': 4002}, {'line_chars': True}):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(pages)
                changed[-1]['observation']['excerpts'][0].update(mutation)
                state['steps'] = [prefix, *changed]
                self.assertEqual(measure(self.case, state, requested), [False])
        state['steps'] = [prefix, pages[-1]]
        self.assertEqual(measure(self.case, state, requested), [False])

    def test_score_uses_reads_before_the_answer_or_the_first_saved_output(self):
        self.case.update(file_delivery=True, artifact='report.md', required_read_ranges=self.ranges)
        self.case['replies'] = [{'answer': '甲版。', 'question_has': ['版本'],
                                  'required_read_ranges': self.ranges, 'restore_before': True}]
        validate_cases([self.case])
        docs, state = self.state()
        first, second = self.read(docs, 7, 1, 3), self.read(docs, 12, 2, 2)
        ask = self.step('ask', {'question': '請問採用哪個版本？'})
        write = self.step('write_artifact', {'artifact': {'name': 'report.md'}})
        finish = self.step('deliver', {})
        final = {**state, 'status': 'report_ready', 'result': '白桌',
                 'steps': [first, ask, second, write, finish]}
        before = {**state, 'status': 'needs_input', 'result': '', 'artifacts': [], 'steps': [first, ask]}
        turns = [{'kind': 'reply', 'before': before, 'saved': True, 'restored': True}]
        files = [{'name': 'report.md', 'content': '白桌'}]
        checks, metrics = score(self.case, final, files, turns, [])
        self.assertTrue(checks['read_ranges'])
        self.assertTrue(checks['restored_waiting'])
        self.assertFalse(checks['intermediate_read_ranges'])
        self.assertEqual(metrics['intermediate_read_ranges_covered'], 1)
        before['steps'] = [first, second, ask]
        self.assertTrue(score(self.case, final, files, turns, [])[0]['intermediate_read_ranges'])
        rejected = self.step('write_artifact', {'error': '尚未寫入'}, filename='report.md')
        unrelated = self.step('write_artifact', {'artifact': {'name': 'other.md'}})
        final['steps'] = [first, rejected, unrelated, second, write, finish]
        self.assertTrue(score(self.case, final, files, turns, [])[0]['read_ranges'])
        final['steps'] = [first, write, second, finish]
        self.assertFalse(score(self.case, final, files, turns, [])[0]['read_ranges'])

    def test_fixture_requires_exact_in_bounds_ranges(self):
        for ranges in ([], [self.ranges[0]] * 2, [{'doc': True, 'start_line': 1, 'end_line': 2}],
                       [{'doc': 0, 'start_line': 2, 'end_line': 5}],
                       [{'doc': 1, 'start_line': 1, 'end_line': 1, 'tool': 'read'}]):
            with self.subTest(ranges=ranges):
                self.case['required_read_ranges'] = ranges
                with self.assertRaises(ValueError):
                    validate_cases([self.case])


if __name__ == '__main__':
    unittest.main()
