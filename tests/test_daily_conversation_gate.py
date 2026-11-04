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

    def test_general_dialogue_suite_uses_separate_model_sessions_and_exact_answers(self):
        import json
        cases = json.loads(gate.GENERAL_SUITE.read_text(encoding='utf8'))
        gate.check_cases(cases)
        self.assertGreaterEqual(len({c['session'] for c in cases}), 2)
        for c in cases:
            self.assertEqual(c['route'], 'general_model')
            self.assertIsInstance(c['answer'], str)
            self.assertTrue(c['answer'].strip())
        self.assertNotEqual(gate.fingerprint(gate.GENERAL_SUITE), gate.fingerprint(gate.SUITE))

    def test_feasibility_controls_cover_each_outcome_in_independent_chats(self):
        import json
        from collections import Counter
        cases = json.loads(gate.FEASIBILITY_SUITE.read_text(encoding='utf8'))
        gate.check_cases(cases)
        self.assertEqual(len({c['session'] for c in cases}), len(cases))
        self.assertTrue(all(c['route'] == 'general_model' for c in cases))
        self.assertEqual(Counter(c['answer'] for c in cases), {'可行': 2, '不可行': 2, '資料不足': 2})

    def test_paired_cases_start_from_identical_history_in_separate_sessions(self):
        from unittest.mock import patch
        from assistant.store import Store
        seen = {}
        def observe(folder, case, sid, refs, live):
            self.assertTrue(live)
            seen[sid] = [(r['role'], r['content']) for r in Store(folder/'assistant.sqlite3').messages(sid)]
            return sid, dict(status='pass', raw_exact=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, 'turn', side_effect=observe):
            self.assertTrue(gate.evaluate(Path(tmp), True, gate.PAIR_SUITE))
        self.assertEqual(len(seen), 2)
        first, second = seen.values()
        self.assertEqual(first, second)
        self.assertTrue(first)

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

