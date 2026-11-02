"""Retain reviewed synthetic cases after the actual self-revision run."""
import json
import unittest
from pathlib import Path

from assistant.acceptance import grade, validate_suite
from assistant.communication import conversation_style


class CommunicationRevisionTests(unittest.TestCase):
    def test_latest_style_corrections_preserve_quotes_negation_and_topic_changes(self):
        rows=json.loads((Path(__file__).resolve().parents[1]/'evals/communication_revisions.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows),48)
        self.assertEqual(len({row['id'] for row in rows}),len(rows))
        for start in range(0,len(rows),40):
            suite=validate_suite({'version':1,'cases':rows[start:start+40]},['assistant/communication.py'])
            observations=[{'id':c['id'],'actual':conversation_style(c['question'],c['history'])} for c in suite['cases']]
            for result in grade(suite,observations):
                with self.subTest(case=result['id']):self.assertTrue(result['passed'],result)
