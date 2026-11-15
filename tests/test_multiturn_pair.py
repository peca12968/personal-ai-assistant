import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import evaluate_multiturn_pair as pair


class PairTests(unittest.TestCase):
    def test_comparison_collects_both_paths_binds_calls_and_keeps_rubric_out_of_requests(self):
        case = {'id': 'room_record', 'file_delivery': False, 'prompt': '先問核對人。再依文件及答覆交付。',
                'documents': [{'name': 'room.txt', 'content': '收件室青廳。'}],
                'replies': [{'question_has': ['核對人'], 'question_excludes': ['收件室'], 'answer': '核對人為溫奕行。'}],
                'must_match': ['溫奕行', '青廳'], 'must_omit': ['grader-only-marker']}

        class FakeEngine:
            def __init__(self, path, **protocol):
                self.receipts = []
                path.mkdir(parents=True)
                self.is_direct = path.parent.name == 'direct'

            def answer(self, prompt, cancel=None):
                self.receipts.append({'settings': {'model': 'known', 'reasoning effort': 'medium'},
                                      'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()})
                if self.is_direct:
                    commands = [{'status': 'ask', 'question': '核對人與收件室？'},
                                {'status': 'finish', 'report': '溫奕行 [補充#1]，青廳 [D1:L1]。'}]
                else:
                    commands = [{'tool': 'ask', 'args': {'question': '核對人是誰？'}, 'note': '補資料'},
                                {'tool': 'read', 'args': {'document_id': 1, 'start_line': 1, 'end_line': 1}, 'note': '查讀'},
                                {'tool': 'finish', 'args': {'report': '溫奕行 [補充#1]，青廳 [D1:L1]。'}, 'note': '交付'}]
                return json.dumps(commands[len(self.receipts) - 1], ensure_ascii=False)

        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            suite = root / 'suite.json'
            suite.write_text(json.dumps([case], ensure_ascii=False), encoding='utf-8')
            frozen = pair.signature(suite)
            protocol = {'model': 'known', 'effort': 'medium', 'timeout': 10}
            with patch.object(pair, 'RunEngine', FakeEngine):
                direct = pair.collect_arm(root / 'direct', 'direct', case, suite, frozen, protocol)
                assistant = pair.collect_arm(root / 'assistant', 'assistant', case, suite, frozen, protocol)
            self.assertTrue(direct['checks']['content'])
            self.assertFalse(direct['checks']['clarification'])
            self.assertTrue(all(assistant['checks'].values()), assistant)
            for arm in pair.ARMS:
                trace = json.loads((root / arm / 'trace.json').read_text(encoding='utf-8'))
                prompts = [call['prompt'] for call in trace['calls']]
                self.assertFalse(any('grader-only-marker' in prompt for prompt in prompts))
                self.assertNotIn('溫奕行', prompts[0])
                self.assertIn('溫奕行', prompts[-1])
                self.assertEqual(len(prompts), len(trace['model_receipts']))
            self.assertFalse((root / 'direct' / 'assistant.sqlite3').exists())
            self.assertTrue((root / 'assistant' / 'assistant.sqlite3').exists())

    def test_direct_requires_appropriate_output_shape_not_merely_a_matching_answer(self):
        for raw, file_delivery in (
            ('{"status":"finish","report":"good"}', True),
            ('{"status":"finish","report":"good","filename":"../bad.txt"}', True),
            ('{"status":"ask","question":true}', False),
            ('{"status":"ask","question":"x","report":"completed"}', False),
            ('{"status":"ask","status":"finish","report":"good"}', False),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pair.parse_action(raw, file_delivery)
        self.assertEqual(pair.parse_action('{"status":"ask","question":"哪位？"}', False)['status'], 'ask')


if __name__ == '__main__':
    unittest.main()
