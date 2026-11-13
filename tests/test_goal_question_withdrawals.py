import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, goal_guidance, goal_replies
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalQuestionWithdrawalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.gid = autonomy.create_goal(self.store, self.sid, '入場須有通行碼及同行人')

    def review(self):
        return goal_replies.snapshot(self.store, self.sid, self.gid)

    def amend(self, text):
        return goal_guidance.save(self.store, self.sid, self.gid, text,
                                  expected_fingerprint=self.review()['fingerprint'])

    def ask(self, question='通行碼及同行人分別是什麼？'):
        return autonomy.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('ask', question=question, partial_result='已知事項，暫待查。')))

    def withdraw(self, number, fingerprint=None):
        return goal_replies.withdraw(self.store, self.sid, self.gid, number,
                                     expected_fingerprint=fingerprint or self.review()['fingerprint'])

    def test_only_reviewed_current_ask_is_withdrawn_and_resume_can_ask_remaining_field(self):
        self.amend('最初兩欄都要填。')
        waiting = self.ask()
        before = self.review()
        self.amend('同行人欄改為不用填，通行碼仍需確認。')
        with self.assertRaisesRegex(ValueError, '已變更'):
            self.withdraw(2, before['fingerprint'])
        self.assertEqual(self.review()['question_withdrawal']['correction_numbers'], [2])
        with self.assertRaisesRegex(ValueError, '目前提問後'):
            self.withdraw(1)
        self.assertEqual(self.review()['goal']['status'], 'needs_input')

        saved = self.withdraw(2)['goal']
        self.assertEqual((saved['status'], saved['question']), ('paused', ''))
        self.assertNotIn('reply_sources', saved)
        self.assertNotIn('partial_result', saved)
        record = saved['withdrawn_questions'][0]
        self.assertEqual((record['ask_sequence'], record['question'], record['guidance_citation']),
                         (1, waiting['question'], '[更正#2]'))
        self.assertEqual(autonomy.get_goal(self.store, self.sid, self.gid)['steps'], waiting['steps'])
        with self.assertRaisesRegex(ValueError, '沒有等待'):
            goal_replies.save(self.store, self.sid, self.gid, '舊問題的答案',
                              expected_fingerprint=self.review()['fingerprint'])

        engine = ScriptedEngine(action('ask', question='目前通行碼是什麼？'))
        next_goal = autonomy.run_goal(self.store, self.sid, self.gid, engine)
        payload = json.loads(engine.prompts[0].split('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(payload['withdrawn_questions'], [{'ask_sequence': 1, 'guidance_citation': '[更正#2]'}])
        self.assertNotIn('question_withdrawal', self.review())
        goal_replies.save(self.store, self.sid, self.gid, 'J-42',
                          expected_fingerprint=self.review()['fingerprint'])
        self.assertEqual(next_goal['question'], '目前通行碼是什麼？')
        final = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual(final['reply_sources'][0]['citation'], '[補充#2]')
        self.assertEqual(final['withdrawn_questions'], [record])

    def test_lease_transaction_and_changed_document_guard_preserve_waiting_state(self):
        waiting = self.ask()
        self.amend('範圍更正，原問題不適用。')
        fingerprint = self.review()['fingerprint']
        with lease(self.store, self.gid), self.assertRaises(LeaseBusy):
            self.withdraw(1, fingerprint)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_pause BEFORE UPDATE OF status ON autonomous_goals "
                       "BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.withdraw(1)
        self.assertNotIn('withdrawn_questions', self.review()['goal'])
        with self.store.connect() as db:
            db.execute('DROP TRIGGER reject_pause')
        document = self.root / 'current.txt'
        document.write_text('本次使用新版來源', encoding='utf-8')
        self.store.attach_document(self.sid, document)
        with self.assertRaisesRegex(ValueError, '已變更'):
            self.withdraw(1, fingerprint)
        with self.assertRaisesRegex(ValueError, '目標仍可接續'):
            self.withdraw(1)
        goal = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertEqual((goal['question'], goal['steps']), (waiting['question'], waiting['steps']))

    def test_cli_saves_without_engine_and_backup_checks_exact_old_ask(self):
        from assistant.__main__ import main
        self.ask()
        self.amend('取消入場工作，改整理交接資料。')
        output = io.StringIO()
        command = ['assistant', '--data-dir', str(self.root), '--session', self.sid,
                   '--withdraw-goal-question', self.gid, '--goal-correction', '1',
                   '--goal-fingerprint', self.review()['fingerprint']]
        with (patch('sys.argv', command), contextlib.redirect_stdout(output),
              patch('assistant.__main__.CodexEngine', side_effect=AssertionError('must not start'))):
            main()
        receipt = json.loads(output.getvalue())['goal']['withdrawn_questions']
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        path = self.root / 'restored' / 'assistant.sqlite3'
        restored = Store(path)
        self.assertEqual(autonomy.get_goal(restored, self.sid, self.gid)['withdrawn_questions'], receipt)
        with restored.connect() as db:
            (raw,) = db.execute('SELECT action_json FROM autonomous_steps WHERE goal_id=?', (self.gid,)).fetchone()
            ask = json.loads(raw)
            ask['note'] = '手動改過的行動說明'
            db.execute('UPDATE autonomous_steps SET action_json=? WHERE goal_id=?',
                       (json.dumps(ask, ensure_ascii=False), self.gid))
        with self.assertRaisesRegex(ValueError, '原提問步驟指紋'):
            validate_database(path)

    def test_restored_correction_can_dismiss_current_ask_without_saving_an_answer(self):
        waiting = self.ask()
        fingerprint = self.review()['fingerprint']
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        original = self.store
        self.store = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(self.review()['fingerprint'], fingerprint)
        saved = self.amend('本批改為新版，同行人不再需要。請撤下目前問題。')['goal']
        self.assertEqual((saved['status'], saved['question']), ('paused', ''))
        self.assertNotIn('reply_sources', saved)
        self.assertNotIn('partial_result', saved)
        self.assertEqual(saved['withdrawn_questions'][0]['question'], waiting['question'])
        self.assertEqual(saved['withdrawn_questions'][0]['guidance_citation'], '[更正#1]')
        self.assertEqual(autonomy.get_goal(original, self.sid, self.gid), waiting)
        with self.assertRaisesRegex(ValueError, '已變更'):
            goal_replies.save(self.store, self.sid, self.gid, '過期回答', expected_fingerprint=fingerprint)
        validate_database(self.store.path)

    def test_only_explicit_standalone_dismissal_changes_waiting_state(self):
        waiting = self.ask()
        for content in ('同事提議「請撤下目前問題」。', '請撤下目前問題？', '若欄位不適用，請撤下目前問題。'):
            with self.subTest(content=content):
                saved = self.amend(content)['goal']
                self.assertEqual((saved['status'], saved['question']), ('needs_input', waiting['question']))
                self.assertNotIn('withdrawn_questions', saved)
        self.assertEqual(self.amend('目前欄位不再需要。撤下這則問題。')['goal']['status'], 'paused')
