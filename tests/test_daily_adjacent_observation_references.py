import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary as daily
from scripts.daily_expenses import DailyAssistant


CODES = '只輸出指定代碼。確定可行回 R，確定不可行回 B，尚無法判定回 X。'
HEAD = ('判斷「交回墨盒」須同時符合「北側檢測盒」的「封口完整」及「北側檢測盒」的「識別卡已蓋章」，'
        '這些就是全部要求。')
ANCHOR = '本輪確認北側檢測盒的封口完整'
REFERENCE = '它的識別卡已有章'
ASK = HEAD + ANCHOR + '。' + REFERENCE + '。' + CODES
OTHER = '本輪確認南側檢測盒的封口完整'
AMBIGUOUS = HEAD + ANCHOR + '。' + OTHER + '。' + REFERENCE + '。' + CODES
VETO = AMBIGUOUS.replace(ANCHOR, ANCHOR.replace('完整', '不完整'))
SEPARATOR = '當次確認南側檢測盒的警示燈已熄滅'
MULTI_SELECTIVE = HEAD + ANCHOR + '。' + OTHER + '。它的編號已入冊。' + SEPARATOR + '。' + REFERENCE + '。' + CODES
MULTI_VETO = MULTI_SELECTIVE.replace(ANCHOR, ANCHOR.replace('完整', '不完整'))
MULTI_ADAPTIVE = (HEAD + '本輪確認北側檢測盒的通氣閥已關閉。' + OTHER.replace('封口完整', '通氣閥已關閉')
                  + '。它的識別卡沒有章。' + SEPARATOR + '。它的封口不完整。' + CODES)


class AdjacentObservationReferenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型替身'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_bare_and_scoped_references_bind_the_same_object_and_keep_both_sources(self):
        for reference in (REFERENCE, '本輪確認' + REFERENCE, '當次確認該物件的識別卡已有章'):
            with self.subTest(reference=reference):
                question = HEAD + ANCHOR + '。' + reference + '。' + CODES
                reply = self.app.ask(self.store.new_session(), question)
                proof = daily.read(self.store, reply['id'])
                self.assertEqual((reply['content'], proof['plan']['schema']), ('R', daily.V11))
                card = proof['plan']['conditions'][1]
                self.assertEqual((card['subject'], card['observation'], card['observation_anchor']),
                                 ('北側檢測盒', reference, ANCHOR))
                self.assertIn('指代原句：' + ANCHOR, daily.describe(proof))
        prior = daily.parse_version(HEAD + ANCHOR + '。本輪確認' + REFERENCE + '。' + CODES, daily.V10)
        self.assertEqual(daily.evaluate(prior)[0], 'X')
        self.engine.answer.assert_not_called()

    def test_one_anchor_supports_consecutive_references_with_separate_polarity(self):
        rule = ('判斷「使用北盒」須同時符合「北側檢測盒」的「封口完整」、「北側檢測盒」的「警示燈已熄滅」'
                '及「北側檢測盒」的「識別卡已蓋章」，這些就是全部要求。')
        question = rule + ANCHOR + '。該物件的警示燈還亮著。同一物件的識別卡已有章。' + CODES
        answer = self.app.ask(self.store.new_session(), question)
        proof = daily.read(self.store, answer['id'])
        conditions = proof['plan']['conditions']
        self.assertEqual(answer['content'], 'B')
        self.assertEqual([row['state'] for row in conditions], ['true', 'false', 'true'])
        self.assertEqual([row['observation_anchor'] for row in conditions[1:]], [ANCHOR, ANCHOR])
        self.engine.answer.assert_not_called()

    def test_earlier_other_object_and_unrelated_referential_field_keep_their_own_scope(self):
        question = (HEAD + '上次確認南側檢測盒的識別卡已有章。' + ANCHOR
                    + '。它的編號已入冊。' + CODES)
        answer = self.app.ask(self.store.new_session(), question)
        proof = daily.read(self.store, answer['id'])
        self.assertEqual(answer['content'], 'X')
        self.assertIsNone(proof['plan']['conditions'][1]['observation'])
        old, different_field = proof['plan']['other_observations']
        self.assertTrue(old['earlier'])
        self.assertEqual(old['subject'], '南側檢測盒')
        self.assertEqual(different_field['subject'], '北側檢測盒')
        self.assertEqual(different_field['observation_anchor'], ANCHOR)
        self.assertNotIn('編號已入冊', proof['canonical'])
        self.engine.answer.assert_not_called()

    def test_ambiguous_or_noncurrent_reference_does_not_fall_through_to_the_legacy_parser(self):
        for reports in (
            '本輪確認它的識別卡已有章。',
            ANCHOR + '。' + OTHER + '。上次確認甲箱的封口完整。它的識別卡已有章。',
            ANCHOR + '。' + OTHER + '。它的識別卡已有章。該物件的警示燈還亮著。',
            ANCHOR + '。' + OTHER + '。它的封口不完整。',
            ANCHOR + '。' + OTHER + '。它的識別卡已有章。再交回墨盒。',
            ANCHOR.replace('本輪', '上次') + '。它的識別卡已有章。',
            ANCHOR + '。上次確認南側檢測盒的封口完整。它的識別卡已有章。',
            ANCHOR + '。上次確認它的識別卡已有章。',
            ANCHOR + '。它的識別卡可能已有章。',
            ANCHOR + '。它的封口不完整。',
            ANCHOR + '。它的識別卡已有章。並交回墨盒。',
        ):
            with self.subTest(reports=reports):
                question = HEAD + reports + CODES
                self.assertIsNone(daily.parse(question))
                self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], '模型替身')

    def test_one_question_lists_both_objects_and_each_choice_recomputes_the_original(self):
        self.assertIsNone(daily.parse(AMBIGUOUS))
        for name, choice, code in (
            ('北側檢測盒', '代稱指的是「北側檢測盒」。', 'R'),
            ('南側檢測盒', '南側檢測盒', 'X'),
        ):
            with self.subTest(name=name):
                sid = self.store.new_session()
                asked = self.app.ask(sid, AMBIGUOUS)
                receipt = daily.read(self.store, asked['id'])
                self.assertEqual(receipt['status'], 'clarification_required')
                self.assertNotIn('analysis', receipt)
                self.assertIn('「北側檢測盒」、「南側檢測盒」', asked['content'])
                with self.assertRaisesRegex(EngineError, '上一則列出'):
                    self.app.ask(sid, '代稱指的是「西側檢測盒」')
                answer = self.app.ask(sid, choice)
                proof = daily.read(self.store, answer['id'])
                self.assertEqual(answer['content'], code)
                self.assertEqual(proof['plan']['source']['answer_id'], asked['id'])
                state = daily.effective(proof['plan'])
                observation = state['conditions'][1] if code == 'R' else state['other_observations'][-1]
                self.assertEqual(observation['subject'], name)
                self.assertEqual(observation['observation_anchor'], ANCHOR if code == 'R' else OTHER)
                for source in (choice, AMBIGUOUS, REFERENCE, ANCHOR if code == 'R' else OTHER):
                    self.assertIn(source, daily.describe(proof))
        self.engine.answer.assert_not_called()

    def test_choice_only_uses_immediately_preceding_clarification_in_same_session(self):
        sid = self.store.new_session()
        self.app.ask(sid, AMBIGUOUS)
        self.assertEqual(self.app.ask(self.store.new_session(), '北側檢測盒')['content'], '模型替身')
        self.assertEqual(self.app.ask(sid, '明天的工作另開討論。')['content'], '模型替身')
        self.assertEqual(self.app.ask(sid, '北側檢測盒')['content'], '模型替身')
        self.app.ask(sid, AMBIGUOUS)
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(sid, '北側檢測盒')['content'], '模型替身')
        self.app.ask(sid, AMBIGUOUS)
        path = self.path / 'notes.txt'
        path.write_text('測試附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, '北側檢測盒')['content'], '模型替身')
        self.assertEqual(self.app.ask(sid, AMBIGUOUS)['content'], '模型替身')

    def test_unanimous_candidates_return_the_exact_code_with_separate_hypothetical_sources(self):
        alternative = AMBIGUOUS.replace('「北側檢測盒」的「識別卡已蓋章」',
                                       '（「北側檢測盒」的「識別卡已蓋章」或「南側檢測盒」的「識別卡已蓋章」）')
        necessary_only = AMBIGUOUS.replace('就是全部要求', '只是必要條件')
        for question, code, states in (
            (VETO, 'B', [['false', 'true'], ['false', 'unknown']]),
            (alternative, 'R', [['true', 'true', 'unknown'], ['true', 'unknown', 'true']]),
            (necessary_only, 'X', [['true', 'true'], ['true', 'unknown']]),
        ):
            with self.subTest(code=code):
                answer = self.app.ask(self.store.new_session(), question)
                receipt = daily.read(self.store, answer['id'])
                self.assertEqual((answer['content'], receipt['plan']['schema']), (code, daily.AGREEMENT_PLAN))
                north_report = ANCHOR.replace('完整', '不完整') if question == VETO else ANCHOR
                self.assertEqual([row['observation_anchor'] for row in receipt['branches']], [north_report, OTHER])
                self.assertEqual([[condition['state'] for condition in row['conditions']]
                                  for row in receipt['branches']], states)
                rendered = daily.describe(receipt)
                self.assertIn('若指「北側檢測盒」', rendered)
                self.assertIn('若指「南側檢測盒」', rendered)
        forged = dict(daily.clarification(AMBIGUOUS), schema=daily.AGREEMENT_PLAN)
        with self.assertRaisesRegex(ValueError, '必須先確認'):
            daily.evaluate(forged)
        self.engine.answer.assert_not_called()

    def test_agreement_keeps_the_identity_open_until_an_explicit_choice(self):
        sid = self.store.new_session()
        self.app.ask(sid, VETO)
        self.assertEqual(self.app.ask(sid, '北側檢測盒')['content'], '模型替身')
        agreed = self.app.ask(sid, VETO)
        with self.assertRaisesRegex(EngineError, '完整名稱'):
            self.app.ask(sid, '還要核對什麼？')
        resolved = self.app.ask(sid, '代稱指的是「北側檢測盒」。')
        self.assertEqual(resolved['content'], 'B')
        proof = daily.read(self.store, resolved['id'])
        self.assertEqual(proof['plan']['source']['answer_id'], agreed['id'])
        self.assertEqual(daily.effective(proof['plan'])['conditions'][1]['observation'], REFERENCE)
        fixed = self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的封口完整。重算。')
        self.assertEqual(fixed['content'], 'R')
        self.engine.answer.assert_called_once()

    def test_cancelled_legacy_clarification_and_agreement_retry_keep_their_saved_decision(self):
        old = self.store.new_session()
        with patch.object(daily, 'plan_reference', side_effect=lambda question, _: daily.clarification(question)), \
             patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(old, VETO)
        legacy = self.app.retry(old)
        self.assertEqual(legacy[daily.FIELD]['status'], 'clarification_required')
        self.assertEqual(self.app.ask(old, '南側檢測盒')['content'], 'B')

        sid = self.store.new_session()
        with patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, VETO)
        agreed = self.app.retry(sid)
        self.assertEqual(agreed['content'], 'B')
        bundle = create_backup(self.store.path, self.path / 'agreement.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily.read(restored, agreed['id']), agreed[daily.FIELD])
        context = restored.answer_context(agreed['id'])
        context[daily.FIELD]['branches'][0]['observation_anchor'] = OTHER
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), agreed['id']))
        with self.assertRaisesRegex(ValueError, '原問或推論不符'):
            daily.read(restored, agreed['id'])
        self.engine.answer.assert_not_called()

    def test_multiple_references_ask_only_for_the_effective_choice_or_answer_directly(self):
        sid = self.store.new_session()
        first = self.app.ask(sid, MULTI_SELECTIVE)
        proof = daily.read(self.store, first['id'])
        self.assertEqual(proof['questions_left'], 1)
        self.assertEqual(proof['plan']['target']['number'], 2)
        self.assertIn(f'第2處「{REFERENCE}」', first['content'])
        with self.assertRaisesRegex(EngineError, '上一則列出'):
            self.app.ask(sid, '代稱指的是「西側檢測盒」')
        final = self.app.ask(sid, '北側檢測盒')
        receipt = daily.read(self.store, final['id'])
        self.assertEqual(final['content'], 'R')
        self.assertEqual(receipt['plan']['source']['answer_id'], first['id'])
        self.assertEqual(receipt['selections'], [dict(number=2, subject='北側檢測盒', reply='北側檢測盒')])
        self.assertEqual(len(receipt['branches']), 2)
        self.assertIn('第2處的原始回覆：北側檢測盒', daily.describe(receipt))
        with self.assertRaisesRegex(EngineError, '身份未確認'):
            self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的識別卡沒有章。重算。')
        direct = self.app.ask(self.store.new_session(), MULTI_VETO)
        self.assertEqual(direct['content'], 'B')
        self.assertEqual(len(daily.read(self.store, direct['id'])['branches']), 4)
        self.engine.answer.assert_not_called()

    def test_reference_order_minimizes_worst_case_and_can_stop_after_the_first_answer(self):
        rows = [(choices, choices[0] if choices[1] == 'A' else choices[2])
                for choices in product(('A', 'B'), repeat=3)]
        self.assertEqual(daily.reference_question(rows), (2, 1))
        for first_name, expected_remaining, expected in (('北側檢測盒', 0, 'B'), ('南側檢測盒', 1, 'X')):
            with self.subTest(name=first_name):
                sid = self.store.new_session()
                first = self.app.ask(sid, MULTI_ADAPTIVE)
                self.assertEqual(daily.read(self.store, first['id'])['questions_left'], 2)
                second = self.app.ask(sid, first_name)
                self.assertEqual(daily.read(self.store, second['id'])['questions_left'], expected_remaining)
                if expected_remaining:
                    self.assertIn('第2處', second['content'])
                    second = self.app.ask(sid, '南側檢測盒')
                self.assertEqual(second['content'], expected)
        self.engine.answer.assert_not_called()

    def test_multiple_reference_reply_retry_restore_and_correction_rebuild_the_chain(self):
        sid = self.store.new_session()
        initial = self.app.ask(sid, MULTI_ADAPTIVE)
        second = self.app.ask(sid, '南側檢測盒')
        evaluate = daily.evaluate

        def stop_final(plan, cancel=None):
            if plan['schema'] == daily.MULTI_REFERENCE_STEP and plan['target'] is None:
                raise EngineError('停止', 'cancelled')
            return evaluate(plan, cancel)

        with patch.object(daily, 'evaluate', side_effect=stop_final):
            with self.assertRaises(EngineError):
                self.app.ask(sid, '北側檢測盒')
        final = self.app.retry(sid)
        self.assertEqual(final['content'], 'B')
        self.assertEqual(final[daily.FIELD]['plan']['source']['answer_id'], second['id'])
        fixed = self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的封口完整。重算。')
        self.assertEqual(fixed['content'], 'X')
        bundle = create_backup(self.store.path, self.path / 'multiple.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        for answer in (initial, second, final, fixed):
            self.assertEqual(daily.read(restored, answer['id']), answer[daily.FIELD])
        context = restored.answer_context(initial['id'])
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False, indent=2), initial['id']))
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            daily.read(restored, fixed['id'])
        self.engine.answer.assert_not_called()

    def test_multiple_reference_scope_and_route_guards(self):
        for question in (
            MULTI_SELECTIVE.replace(SEPARATOR + '。', ''),
            MULTI_SELECTIVE.replace(SEPARATOR, SEPARATOR.replace('當次', '上次')),
            MULTI_SELECTIVE.replace(REFERENCE, '同一物件的識別卡已有章'),
            MULTI_SELECTIVE.replace(REFERENCE, '它的編號已入冊'),
            MULTI_SELECTIVE + '再把檔案寄給同事。',
        ):
            self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], '模型替身')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), MULTI_SELECTIVE)['content'], '模型替身')
        sid = self.store.new_session()
        self.app.ask(sid, MULTI_SELECTIVE)
        self.assertEqual(self.app.ask(self.store.new_session(), '北側檢測盒')['content'], '模型替身')
        path = self.path / 'notes.txt'
        path.write_text('測試附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, '北側檢測盒')['content'], '模型替身')

    def test_resolved_choice_survives_retry_updates_and_restore_with_verifiable_source(self):
        sid = self.store.new_session()
        asked = self.app.ask(sid, AMBIGUOUS)
        evaluate = daily.evaluate

        def stop_resolution(plan, cancel=None):
            if plan['schema'] == daily.RESOLVE_PLAN:
                raise EngineError('停止', 'cancelled')
            return evaluate(plan, cancel)

        with patch.object(daily, 'evaluate', side_effect=stop_resolution):
            with self.assertRaises(EngineError):
                self.app.ask(sid, '南側檢測盒')
        resolved = self.app.retry(sid)
        self.assertEqual(resolved['content'], 'X')
        inquiry = self.app.ask(sid, '還要核對什麼？')
        self.assertIn('北側檢測盒', inquiry['content'])
        fixed = self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的識別卡已有章。重算。')
        self.assertEqual(fixed['content'], 'R')
        new_round = self.app.ask(sid, '沿用上一輪規則，開始新一輪核對。' + ANCHOR + '。')
        self.assertEqual(new_round['content'], 'X')
        bundle = create_backup(self.store.path, self.path / 'clarification.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        for answer in (asked, resolved, inquiry, fixed, new_round):
            self.assertEqual(daily.read(restored, answer['id']), answer[daily.FIELD])
        context = restored.answer_context(asked['id'])
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False, indent=2), asked['id']))
        self.assertEqual(daily.read(restored, asked['id']), asked[daily.FIELD])
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            daily.read(restored, new_round['id'])
        self.engine.answer.assert_not_called()

    def test_retry_correction_and_new_round_rebuild_original_reference_after_restore(self):
        sid = self.store.new_session()
        with patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, ASK)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], 'R')
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, '沿用上一輪條件：本輪確認它的識別卡沒有章。重算。')
        correction = self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的識別卡沒有章。重算。')
        self.assertEqual(correction['content'], 'B')
        self.assertNotIn('observation_anchor', daily.effective(correction[daily.FIELD]['plan'])['conditions'][1])
        started = self.app.ask(sid, '沿用上一輪規則，開始新一輪核對。' + ANCHOR + '。' + REFERENCE + '。')
        self.assertEqual(started['content'], 'R')
        bundle = create_backup(self.store.path, self.path / 'reference.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily.read(restored, started['id']), started[daily.FIELD])
        context = restored.answer_context(first['id'])
        context[daily.FIELD]['plan']['conditions'][1]['observation_anchor'] = '本輪確認南側檢測盒的封口完整'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily.read(restored, started['id'])
        self.engine.answer.assert_not_called()

    def test_attachment_and_raw_model_runs_do_not_use_reference_resolution(self):
        sid = self.store.new_session()
        path = self.path / 'notes.txt'
        path.write_text('測試附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, ASK)['content'], '模型替身')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), ASK)['content'], '模型替身')
