import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary
from scripts.daily_expenses import DailyAssistant


HEAD = '使用轉運台要有已校驗的水位計和有效的操作證。'
LABELS = '確定能用回「可用」，確定不能用回「停用」，尚不能判定回「待查」。請只輸出結論。'
FALSE_AND_UNKNOWN = HEAD + '水位計尚未校驗，唯一的操作證已確定作廢。' + LABELS


class DailyNecessaryTests(unittest.TestCase):
    def test_required_vs_sufficient_and_unknown_are_distinct(self):
        for rule, facts, expected in (
            (HEAD, '水位計尚未校驗，唯一的操作證已確定作廢。', '停用'),
            (HEAD, '水位計尚未校驗，操作證已確認有效。', '待查'),
            (HEAD, '水位計已校驗，操作證已確認有效。', '待查'),
            (HEAD.replace('要有', '的必要且充分條件是同時具備'),
             '水位計已校驗，操作證已確認有效。', '可用'),
            (HEAD, '水位計已校驗，已確定沒有有效的操作證。', '停用'),
        ):
            with self.subTest(rule=rule, facts=facts):
                plan = daily_necessary.parse(rule + facts + LABELS)
                output, receipt = daily_necessary.evaluate(plan)
                self.assertEqual(output, expected)
                self.assertFalse(receipt['model_called'])
                self.assertEqual(receipt['plan']['request'], rule + facts + LABELS)

    def test_complete_condition_names_keep_their_final_character(self):
        question = ('啟用試驗台的必要且充分條件是同時具備已核准的檢測單與可用的識別碼。'
                    '檢測單已確認核准，識別碼已確認可用。' + LABELS)
        plan = daily_necessary.parse(question)
        self.assertEqual([row['name'] for row in plan['conditions']], ['檢測單', '識別碼'])
        self.assertEqual(daily_necessary.evaluate(plan)[0], '可用')

    def test_ambiguous_or_mixed_requests_keep_their_model_route(self):
        q = FALSE_AND_UNKNOWN
        for prompt in (
            q.replace('唯一的操作證', '操作證'), q.replace('操作證已確定', '舊操作證已確定'),
            q.replace('作廢。', '作廢？'), q.replace('已確定作廢', '可能作廢'),
            q.replace('作廢。', '作廢，操作證已確認有效。'), q.replace('要有', '只要有'),
            q.replace('要有', '僅要有'), q.replace('要有', '通常要有'),
            q.replace('使用轉運台', '使用它'),
            q.replace('「待查」', '「停用」'), q + '也寄出通知。', '請引用「' + q + '」',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(daily_necessary.parse(prompt))

    def test_daily_receipt_recomputes_after_restore_and_raw_bypasses_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '模型原答'
            answer = DailyAssistant(store, engine).ask(store.new_session(), FALSE_AND_UNKNOWN)
            engine.answer.assert_not_called()
            self.assertEqual(answer['content'], '停用')
            receipt = daily_necessary.read(store, answer['id'])
            self.assertEqual(receipt['analysis']['status'], 'refuted')
            saved = create_backup(store.path, root / 'sample.paibackup')
            restore_backup(saved['path'], root / 'restore')
            other = Store(root / 'restore/assistant.sqlite3')
            self.assertEqual(daily_necessary.read(other, answer['id']), receipt)
            with other.connect() as db:
                context = other.answer_context(answer['id'])
                context[daily_necessary.FIELD]['analysis']['status'] = 'undetermined'
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(context, ensure_ascii=False), answer['id']))
            with self.assertRaisesRegex(ValueError, '必要條件紀錄'):
                daily_necessary.read(other, answer['id'])
            raw = DailyAssistant(store, engine, allow_local_evidence=False).ask(store.new_session(), FALSE_AND_UNKNOWN)
            self.assertEqual(raw['content'], '模型原答')
            self.assertIsNone(daily_necessary.read(store, raw['id']))

    def test_retry_uses_original_plan_and_attached_sources_block_new_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '模型原答'
            app = DailyAssistant(store, engine)
            with patch.object(daily_necessary, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
                with self.assertRaises(EngineError):
                    app.ask(sid, FALSE_AND_UNKNOWN)
            result = app.retry(sid)
            self.assertEqual(result['content'], '停用')
            self.assertEqual(len(store.messages(sid, 5)), 2)
            engine.answer.assert_not_called()
            document = root / 'record.txt'
            document.write_text('另有一張未核實的證件。', encoding='utf-8')
            store.attach_document(sid, document)
            self.assertEqual(app.ask(sid, FALSE_AND_UNKNOWN)['content'], '模型原答')
            engine.answer.assert_called_once()


class DailyAlternativeTests(unittest.TestCase):
    TERMS = '有效的值班許可與（可用的北機或可用的南機）'
    FACTS = (
        ('唯一的值班許可已確定失效', '值班許可尚未核實', '值班許可已確認有效'),
        ('唯一的北機已確定不可用', '北機尚未檢查', '北機已確認可用'),
        ('唯一的南機已確定不可用', '南機尚未測試', '南機已確認可用'),
    )

    def question(self, sufficient, facts=(2, 1, 2)):
        rule = '的必要且充分條件是同時具備' if sufficient else '必須同時有'
        return '啟用監測台' + rule + self.TERMS + '。' + '，'.join(
            words[index] for words, index in zip(self.FACTS, facts, strict=True)) + '。' + LABELS

    def test_disjunction_matches_independent_truth_table_in_both_directions(self):
        possible = [(False,), (False, True), (True,)]
        for sufficient, *facts in product((False, True), range(3), range(3), range(3)):
            with self.subTest(sufficient=sufficient, facts=facts):
                request = self.question(sufficient, facts)
                plan = daily_necessary.parse(request)
                self.assertEqual(plan['schema'], daily_necessary.V2)
                self.assertEqual(plan['groups'], [['值班許可'], ['北機', '南機']])
                answer, proof = daily_necessary.evaluate(plan)
                outcomes = []
                for gate, first, second in product(*(possible[index] for index in facts)):
                    enabled = gate and (first or second)
                    outcomes.extend((enabled,) if sufficient or not enabled else (False, True))
                wanted = '可用' if all(outcomes) else '停用' if not any(outcomes) else '待查'
                self.assertEqual(answer, wanted)
                self.assertEqual(proof['analysis']['compatible_worlds'], len(outcomes))

    def test_ambiguous_grouping_and_missing_names_keep_the_model_route(self):
        q = self.question(True)
        for prompt in (
            q.replace('（', '').replace('）', ''), q.replace('）', ')'),
            q.replace('北機或', '北機與'), q.replace('北機或可用的南機', '北機'),
            q.replace('北機或可用的南機', '北機或可用的北機'),
            q.replace('北機尚未檢查', '舊北機尚未檢查'), q + '寄送通知。',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(daily_necessary.parse(prompt))

    def test_mixed_receipts_survive_restore_and_group_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            app = DailyAssistant(store, None)
            original = app.ask(store.new_session(), FALSE_AND_UNKNOWN)
            group = app.ask(store.new_session(), self.question(True))
            legacy = daily_necessary.read(store, original['id'])
            newer = daily_necessary.read(store, group['id'])
            self.assertEqual(legacy['schema'], 'daily_necessary_receipt_v1')
            self.assertEqual(newer['schema'], 'daily_necessary_receipt_v2')
            self.assertIn('備援（北機或南機） → 已確認滿足', daily_necessary.describe(newer))
            backup = create_backup(store.path, root / 'sample.paibackup')
            restore_backup(backup['path'], root / 'restore')
            restored = Store(root / 'restore/assistant.sqlite3')
            self.assertEqual(daily_necessary.read(restored, original['id']), legacy)
            self.assertEqual(daily_necessary.read(restored, group['id']), newer)
            context = restored.answer_context(group['id'])
            context[daily_necessary.FIELD]['plan']['groups'] = [['值班許可', '北機'], ['南機']]
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(context, ensure_ascii=False), group['id']))
            with self.assertRaisesRegex(ValueError, '規約'):
                daily_necessary.read(restored, group['id'])
