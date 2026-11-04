import tempfile
import unittest
from pathlib import Path

from scripts import evaluate_daily_conversations as gate


class DailyGateTests(unittest.TestCase):
    def test_reject_unscorable_or_cross_session_source(self):
        first = dict(id='a', session='one', question='問題', route='model', answer='正解')
        for cases in ([], [first, first], [{**first, 'answer':''}],
                      [first, {**first, 'id':'b', 'session':'two', 'source':'a'}]):
            with self.assertRaises(ValueError):
                gate.check_cases(cases)

    def test_block_real_inference_when_a_local_case_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = dict(id='fallback', question='請用一句話說明這項任務。', route='daily_meeting_calculation_v1')
            with self.assertRaisesRegex(ValueError, 'offline path attempted model inference'):
                gate.turn(Path(tmp),case,None,{},False)

    def test_correct_display_cannot_hide_wrong_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = dict(id='receipt', question='包材 15 元已核准。核准金額共多少？只輸出數字。',
                        answer='15', route='approved_expense_calculation_v1',
                        evidence={'expense_ledger.rows.0.included':False})
            with self.assertRaisesRegex(ValueError,'wrong evidence'):
                gate.turn(Path(tmp),case,None,{},False)

