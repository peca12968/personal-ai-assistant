"""Known cases retained after actual self-edits and separate candidate audits."""
import copy
import json
import unittest
from pathlib import Path

from assistant.acceptance import grade, validate_suite
from assistant.communication import conversation_style


class CommunicationScopeTests(unittest.TestCase):
    def test_negated_style_frames_nested_quotes_and_apostrophes(self):
        rows = json.loads((Path(__file__).resolve().parents[1] /
                           'evals/communication_scope_revisions.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 59)
        self.assertEqual(len({row['id'] for row in rows}), len(rows))
        original = copy.deepcopy(rows)
        for start in range(0, len(rows), 40):
            suite = validate_suite({'version':1, 'cases':rows[start:start+40]}, ['assistant/communication.py'])
            observations = [{'id':c['id'], 'actual':conversation_style(c['question'], c['history'])}
                            for c in suite['cases']]
            for result in grade(suite, observations):
                with self.subTest(case=result['id']):
                    self.assertTrue(result['passed'], result)
            for result in observations:
                self.assertTrue(result['actual']['hints_only'])
        self.assertEqual(rows, original)


if __name__ == '__main__': unittest.main()
