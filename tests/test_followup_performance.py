"""Reject bounded but adversarial chat text without runaway regex backtracking."""
import subprocess
import sys
import unittest
from pathlib import Path


class FollowupPerformanceTests(unittest.TestCase):
    def test_long_nonmatch_finishes_in_an_isolated_process(self):
        # A failed child is killed and waited by subprocess.run on timeout;
        # never run a potentially unbounded matcher in the test/UI thread.
        code = """
from assistant.context import is_followup
for question in [
    '我選第二個' + ' ' * 15000 + 'x',
    '我選第二個了' + ' ' * 15000 + 'x',
    'I choose option two' + ' ' * 15000 + 'x',
]:
    assert not is_followup(question)
for question in [
    '我' + ' ' * 15000 + '選第二個。',
    'I choose' + ' ' * 15000 + 'option two.',
]:
    assert is_followup(question)
print('completed')
"""
        result = subprocess.run(
            [sys.executable, '-B', '-c', code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'completed')


if __name__ == '__main__':
    unittest.main()
