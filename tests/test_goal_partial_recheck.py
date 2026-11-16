import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, goal_guidance, goal_partial_recheck, goal_replies
from assistant.__main__ import main
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.goal_stages import digest
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalPartialRecheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        path = self.root / 'current.txt'
        path.write_text('現場用白松台。\n核對數量 8 匣。\n見證人再確認。', encoding='utf-8')
        self.doc = self.store.attach_document(self.sid, path)
        self.gid = autonomy.create_goal(self.store, self.sid, '編本批暫表，附來源並問見證人。')
        self.question = '本批見證人是誰？'
        self.original = autonomy.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('read', document_id=self.doc, start_line=1, end_line=3),
            action('ask', question=self.question,
                   partial_result=f'台面：白松台 [D{self.doc}:L1]\n數量：8 匣 [D{self.doc}:L2]\n見證人待補。')))

    def save(self, text, store=None):
        store = store or self.store
        snapshot = goal_guidance.snapshot(store, self.sid, self.gid)
        return goal_guidance.save(store, self.sid, self.gid, text, expected_fingerprint=snapshot['fingerprint'])

    def result(self, *, text=None, withdrawal=None):
        if text is None:
            text = f'台面：青石台 [更正#1]\n數量：8 匣 [D{self.doc}:L2]\n見證人待補。'
        return json.dumps({'partial_result': text, 'withdrawal_source': withdrawal}, ensure_ascii=False)

    def test_refresh_is_bound_to_the_original_question_through_two_corrections_and_restore(self):
        previous = self.save('本批台面正式改為青石台，見證人沿用原問題。')
        engine = ScriptedEngine(self.result())
        fresh = goal_partial_recheck.run(self.store, self.sid, self.gid, engine, expected_fingerprint=previous['fingerprint'])
        self.assertEqual(len(engine.prompts), 1)
        self.assertIn('白松台', engine.prompts[0])
        self.assertIn('青石台', engine.prompts[0])
        self.assertEqual(fresh['step_count'], previous['step_count'])
        self.assertNotIn('partial_result_historical', fresh['goal'])
        self.assertEqual((fresh['goal']['status'], fresh['goal']['question']), ('needs_input', self.question))
        self.assertEqual(autonomy.get_goal(self.store, self.sid, self.gid)['steps'], self.original['steps'])
        with self.assertRaisesRegex(ValueError, '重新載入'):
            goal_replies.save(self.store, self.sid, self.gid, '羅衡文', expected_fingerprint=previous['fingerprint'])

        historical = self.save('另有青地台提案，尚未採用，維持青石台。')
        self.assertIn('青石台', historical['goal']['partial_result'])
        self.assertTrue(historical['goal']['partial_result_historical'])
        second = goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(self.result()),
                                          expected_fingerprint=historical['fingerprint'])
        self.assertNotIn('partial_result_historical', second['goal'])
        self.assertNotEqual(fresh['goal']['partial_recheck_sha256'], second['goal']['partial_recheck_sha256'])
        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'copy')
        restored = Store(self.root / 'copy' / 'assistant.sqlite3')
        restored_view = goal_replies.snapshot(restored, self.sid, self.gid)
        self.assertEqual(second, restored_view)
        paused = goal_replies.save(restored, self.sid, self.gid, '羅衡文', expected_fingerprint=restored_view['fingerprint'])
        self.assertEqual(paused['goal']['reply_sources'][0]['ask_sequence'], previous['step_count'])
        self.assertEqual(paused['goal']['reply_sources'][0]['question'], self.question)
        validate_database(restored.path)
        new_document = self.root / 'later.txt'
        new_document.write_text('後來新增的來源', encoding='utf-8')
        self.store.attach_document(self.sid, new_document)
        changed = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertTrue(changed['partial_source_changed'])
        self.assertTrue(changed['partial_result_historical'])
        self.assertNotIn('can_recheck_partial', changed)

        with restored.connect() as db:
            raw, = db.execute('SELECT receipt_json FROM autonomous_partial_rechecks WHERE goal_id=? AND guidance_number=2',
                              (self.gid,)).fetchone()
            receipt = json.loads(raw)
            receipt['input_sha256'] = '0' * 64
            db.execute('UPDATE autonomous_partial_rechecks SET receipt_json=?,receipt_sha256=? WHERE goal_id=? AND guidance_number=2',
                       (json.dumps(receipt, ensure_ascii=False), digest([self.gid, previous['step_count'], 2, receipt]), self.gid))
        with self.assertRaisesRegex(ValueError, '依據已改變'):
            validate_database(restored.path)

    def test_suggested_withdrawal_stays_pending_until_the_existing_withdrawal(self):
        previous = self.save('本批免填見證人，可沿用已核對台面與數量。')
        refreshed = goal_partial_recheck.run(
            self.store, self.sid, self.gid,
            ScriptedEngine(self.result(text=f'台面：白松台 [D{self.doc}:L1]\n數量：8 匣 [D{self.doc}:L2]\n見證人免填 [更正#1]',
                                       withdrawal='[更正#1]')),
            expected_fingerprint=previous['fingerprint'])
        self.assertEqual(refreshed['goal']['partial_withdrawal_source'], '[更正#1]')
        self.assertEqual(refreshed['goal']['question'], self.question)
        self.assertFalse(refreshed['goal'].get('reply_sources'))
        paused = goal_replies.withdraw(self.store, self.sid, self.gid, 1, expected_fingerprint=refreshed['fingerprint'])
        self.assertEqual(paused['goal']['status'], 'paused')
        self.assertEqual(paused['goal']['withdrawn_questions'][0]['ask_sequence'], previous['step_count'])
        validate_database(self.store.path)

    def test_failed_cancelled_or_stale_refresh_does_not_change_the_goal(self):
        previous = self.save('本批台面正式改為青石台。')
        never = ScriptedEngine()
        with self.assertRaises(LeaseBusy), lease(self.store, self.gid):
            goal_partial_recheck.run(self.store, self.sid, self.gid, never, expected_fingerprint=previous['fingerprint'])
        with self.assertRaisesRegex(ValueError, '重新載入'):
            goal_partial_recheck.run(self.store, self.sid, self.gid, never, expected_fingerprint='0' * 64)
        self.assertEqual(never.prompts, [])
        rejected = [self.result(text=f'未讀位置 [D{self.doc}:L4]'),
                    self.result(withdrawal='[更正#2]'), self.result(withdrawal=['[更正#1]']),
                    '{"tool":"finish","args":{"report":"已完成"}}']
        for raw in rejected:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(raw),
                                         expected_fingerprint=previous['fingerprint'])
        cancel = threading.Event()

        def canceled_answer():
            cancel.set()
            return self.result()

        with self.assertRaisesRegex(ValueError, '已停止'):
            goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(canceled_answer),
                                     expected_fingerprint=previous['fingerprint'], cancel=cancel)
        self.assertEqual(goal_replies.snapshot(self.store, self.sid, self.gid), previous)

        def source_changed():
            other = self.root / 'other.txt'
            other.write_text('新版內容', encoding='utf-8')
            self.store.attach_document(self.sid, other)
            return self.result()

        with self.assertRaisesRegex(ValueError, '重核期間'):
            goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(source_changed),
                                     expected_fingerprint=previous['fingerprint'])
        latest = autonomy.get_goal(self.store, self.sid, self.gid)
        self.assertTrue(latest['partial_result_historical'])
        self.assertNotIn('can_recheck_partial', latest)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_partial_rechecks').fetchone()[0], 0)

    def prepare_recount(self):
        recount = self.root / 'recount.txt'
        recount.write_text('本批補點資料。\n新計數是 17 匣。', encoding='utf-8')
        changed = self.store.attach_document(self.sid, recount)
        self.gid = autonomy.create_goal(self.store, self.sid, '更新本批數量及乘以每匣2件的合計，仍須確認見證人。')
        initial = autonomy.run_goal(self.store, self.sid, self.gid, ScriptedEngine(
            action('read', document_id=self.doc, start_line=1, end_line=2),
            action('ask', question=self.question, partial_result=f'台面：白松台 [D{self.doc}:L1]，數量 8 匣 [D{self.doc}:L2]')))
        current = self.save(f'新版數量以 [D{changed}:L2] 為準。見證人先照原問題。')
        read = json.dumps({'tool': 'read_ranges', 'args': {
            'ranges': [{'document_id': changed, 'start_line': 2, 'end_line': 2}]}}, ensure_ascii=False)
        operands = [{'path': '/values/0', 'citation': f'[D{changed}:L2]', 'quote': '17 匣'},
                    {'path': '/values/1', 'citation': '[目標]', 'quote': '每匣2件'}]
        calc = {'tool': 'calculate_expression', 'args': {
            'expression': {'operation': 'product', 'values': ['17', '2']}, 'operands': operands}}
        report = f'台面：白松台 [D{self.doc}:L1]。新計數 17 匣 [D{changed}:L2]；共 34 件。見證人待補。'
        return changed, initial, current, read, calc, report

    def test_recheck_restores_targeted_source_and_exact_calculation_and_continues(self):
        changed, initial, current, read, calc, report = self.prepare_recount()
        engine = ScriptedEngine(read, json.dumps(calc, ensure_ascii=False), self.result(text=report))
        refreshed = goal_partial_recheck.run(self.store, self.sid, self.gid, engine,
                                              expected_fingerprint=current['fingerprint'])
        self.assertIn('新計數是 17 匣', engine.prompts[1])
        self.assertIn('"exact": "34"', engine.prompts[2])
        self.assertEqual(refreshed['goal']['question'], self.question)
        self.assertEqual(refreshed['goal']['partial_result'], report)
        self.assertEqual(initial['steps'], autonomy.get_goal(self.store, self.sid, self.gid)['steps'])

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'recount-restored')
        restored = Store(self.root / 'recount-restored' / 'assistant.sqlite3')
        after = goal_replies.snapshot(restored, self.sid, self.gid)
        self.assertEqual(after, refreshed)
        paused = goal_replies.save(restored, self.sid, self.gid, '林代庭', expected_fingerprint=after['fingerprint'])
        self.assertEqual(paused['goal']['reply_sources'][0]['ask_sequence'], 2)
        final_engine = ScriptedEngine(action('finish', report=f'17 匣 [D{changed}:L2]，共 34 件，見證人林代庭 [補充#2]。'))
        final = autonomy.run_goal(restored, self.sid, self.gid, final_engine, max_steps=1)
        self.assertEqual(final['status'], 'report_ready')
        self.assertIn('pending_recheck_evidence', final_engine.prompts[0])
        proof = final['steps'][-1]['observation']['recheck_evidence_sha256']
        self.assertEqual(proof, refreshed['goal']['partial_recheck_sha256'])
        validate_database(restored.path)
        self.store.detach_document(self.sid, changed)
        validate_database(self.store.path)
        older = autonomy.get_goal(self.store, self.sid, self.gid, include_recheck_evidence=True)
        self.assertTrue(older['partial_result_historical'])
        self.assertNotIn('recheck_evidence', older)

        with restored.connect() as db:
            sequence = final['steps'][-1]['sequence']
            observation = final['steps'][-1]['observation'].copy()
            del observation['recheck_evidence_sha256']
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(observation, ensure_ascii=False), self.gid, sequence))
        with self.assertRaisesRegex(ValueError, '成果引用與保存來源不一致'):
            validate_database(restored.path)
        with restored.connect() as db:
            db.execute('UPDATE autonomous_steps SET observation_json=? WHERE goal_id=? AND sequence=?',
                       (json.dumps(final['steps'][-1]['observation'], ensure_ascii=False), self.gid, sequence))
            raw, = db.execute('SELECT receipt_json FROM autonomous_partial_rechecks WHERE goal_id=?',
                              (self.gid,)).fetchone()
            receipt = json.loads(raw)
            receipt['tools'][1]['result']['exact'] = '35'
            db.execute('UPDATE autonomous_partial_rechecks SET receipt_json=?,receipt_sha256=? WHERE goal_id=?',
                       (json.dumps(receipt, ensure_ascii=False), digest([self.gid, 2, 1, receipt]), self.gid))
        with self.assertRaisesRegex(ValueError, '精確運算'):
            validate_database(restored.path)

    def test_recheck_rejects_unread_operands_and_out_of_scope_documents(self):
        changed, _, current, read, calc, report = self.prepare_recount()
        outside = json.loads(read)
        outside['args']['ranges'][0]['document_id'] = changed + 100
        for raw, error in ((json.dumps(outside), '已選取'), (json.dumps(calc), '缺少唯一')):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(raw),
                                          expected_fingerprint=current['fingerprint'])
        valid = json.dumps(calc, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, '工具已用完'):
            goal_partial_recheck.run(self.store, self.sid, self.gid, ScriptedEngine(read, valid, valid, valid),
                                      expected_fingerprint=current['fingerprint'])
        calc['args']['operands'][0]['quote'] = '7 匣'
        calc['args']['expression']['values'][0] = '7'
        engine = ScriptedEngine(read, json.dumps(calc, ensure_ascii=False), self.result(text=report))
        with self.assertRaisesRegex(ValueError, '截斷'):
            goal_partial_recheck.run(self.store, self.sid, self.gid, engine, expected_fingerprint=current['fingerprint'])
        self.assertEqual(goal_replies.snapshot(self.store, self.sid, self.gid), current)

    def test_recheck_reuses_same_ask_evidence_and_recalculates_a_changed_operand(self):
        changed, _, first_waiting, read, calc, report = self.prepare_recount()
        first = goal_partial_recheck.run(
            self.store, self.sid, self.gid, ScriptedEngine(read, json.dumps(calc), self.result(text=report)),
            expected_fingerprint=first_waiting['fingerprint'])
        prior_sha = first['goal']['partial_recheck_sha256']
        second_waiting = self.save('台面正式改為赤榆台。新版匣數、每匣件數及總件數沿用，見證人照原問題。')
        second_text = f'台面：赤榆台 [更正#2]。新計數 17 匣 [D{changed}:L2]，共 34 件。見證人待補。'
        output = json.loads(self.result(text=second_text))
        for invalid in ([True], [2], [1, 1]):
            output['reuse_calculations'] = invalid
            with self.subTest(selection=invalid), self.assertRaisesRegex(ValueError, '不重複編號'):
                goal_partial_recheck.run(
                    self.store, self.sid, self.gid, ScriptedEngine(json.dumps(output)),
                    expected_fingerprint=second_waiting['fingerprint'])
        output['reuse_calculations'] = [1]
        engine = ScriptedEngine(json.dumps(output))
        second = goal_partial_recheck.run(self.store, self.sid, self.gid, engine,
                                             expected_fingerprint=second_waiting['fingerprint'])
        self.assertEqual(len(engine.prompts), 1)
        self.assertIn('新計數是 17 匣', engine.prompts[0])
        self.assertIn('"exact": "34"', engine.prompts[0])
        self.assertEqual(second['goal']['question'], self.question)
        with self.store.connect() as db:
            raw, = db.execute('SELECT receipt_json FROM autonomous_partial_rechecks WHERE goal_id=? AND guidance_number=2',
                              (self.gid,)).fetchone()
            saved = json.loads(raw)
        self.assertEqual(saved['reuse'], {'sha256': prior_sha, 'calculations': [1]})
        self.assertNotIn('tools', saved)

        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'carry-restored')
        restored = Store(self.root / 'carry-restored' / 'assistant.sqlite3')
        self.assertEqual(second, goal_replies.snapshot(restored, self.sid, self.gid))
        goal_replies.save(restored, self.sid, self.gid, '周霽羽', expected_fingerprint=second['fingerprint'])
        finished = autonomy.run_goal(restored, self.sid, self.gid, ScriptedEngine(
            action('finish', report=second_text.replace('見證人待補', '見證人周霽羽 [補充#2]'))), max_steps=1)
        self.assertEqual(finished['status'], 'report_ready')
        self.assertEqual(finished['steps'][-1]['observation']['recheck_evidence_sha256'],
                         second['goal']['partial_recheck_sha256'])
        validate_database(restored.path)

        third_waiting = self.save('本批匣數正式改為 19 匣，台面和每匣件數保持。請重算。')
        calc['args']['expression']['values'][0] = '19'
        calc['args']['operands'][0].update(citation='[更正#3]', quote='19 匣')
        output = json.loads(self.result(text='台面：赤榆台 [更正#2]。本批 19 匣 [更正#3]，共 38 件。見證人待補。'))
        output['reuse_calculations'] = []
        engine = ScriptedEngine(json.dumps(calc), json.dumps(output))
        third = goal_partial_recheck.run(self.store, self.sid, self.gid, engine,
                                            expected_fingerprint=third_waiting['fingerprint'])
        self.assertIn('"exact": "38"', engine.prompts[1])
        self.assertEqual(third['goal']['question'], self.question)
        goal_replies.save(self.store, self.sid, self.gid, '周霽羽', expected_fingerprint=third['fingerprint'])
        carried = autonomy.get_goal(self.store, self.sid, self.gid, include_recheck_evidence=True)['recheck_evidence']
        self.assertEqual([row['result']['exact'] for row in carried['calculations']], ['38'])
        validate_database(self.store.path)

        with restored.connect() as db:
            saved['reuse']['sha256'] = '0' * 64
            db.execute('UPDATE autonomous_partial_rechecks SET receipt_json=?,receipt_sha256=? WHERE goal_id=? AND guidance_number=2',
                       (json.dumps(saved), digest([self.gid, 2, 2, saved]), self.gid))
        with self.assertRaisesRegex(ValueError, '同一原問的前次重核'):
            validate_database(restored.path)

    def test_cli_is_an_explicit_fingerprinted_entry_point(self):
        previous = self.save('本批台面正式改為青石台。')
        args = ['assistant', '--data-dir', str(self.root), '--session', self.sid, '--recheck-goal-partial', self.gid]
        engine = ScriptedEngine(self.result())
        with patch('assistant.__main__.CodexEngine', return_value=engine) as factory:
            with patch('sys.argv', args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()
            factory.assert_not_called()
            out = io.StringIO()
            with patch('sys.argv', args + ['--goal-fingerprint', previous['fingerprint']]), contextlib.redirect_stdout(out):
                main()
        self.assertEqual(json.loads(out.getvalue())['goal']['question'], self.question)
        self.assertEqual(len(engine.prompts), 1)
