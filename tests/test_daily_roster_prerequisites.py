from itertools import product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary
from scripts.daily_expenses import DailyAssistant


EVAL_PATH = Path(__file__).resolve().parents[1] / 'evals/base_assistant_generalization.json'
RECORDING = next(case for case in json.loads(EVAL_PATH.read_text(encoding='utf-8'))['cases']
                 if case['id'] == 'recording_consent_revoked')
GROUPED_PATH = EVAL_PATH.with_name('prerequisite_contrast_assistant_transfer.json')
GROUPED = [case for case in json.loads(GROUPED_PATH.read_text(encoding='utf-8'))['cases']
           if case['id'].startswith('map_room_')]
PERMITS = '本次三位訪客都必須有有效的入場證才可入場。'
LABELS = '可入場回「可進」，確定不能入場回「不可進」，證據不足回「待核實」。請只輸出一個標籤。'


class DailyRosterPrerequisiteTests(unittest.TestCase):
    def test_recording_binds_the_known_blocker_and_unknown_separately(self):
        plan = daily_necessary.parse(RECORDING['question'])
        answer, receipt = daily_necessary.evaluate(plan)
        self.assertEqual(plan['schema'], daily_necessary.V3)
        self.assertEqual(answer, RECORDING['answer'])
        self.assertEqual(receipt['analysis']['status'], 'refuted')
        self.assertEqual(receipt['schema'], 'daily_necessary_roster_receipt_v3')
        self.assertFalse(receipt['model_called'])
        self.assertEqual([(c['name'], c['state']) for c in plan['conditions']],
                         [('第一位同意書', 'unknown'), ('第二位同意書', 'false')])
        self.assertIn('第二位已撤回唯一的同意書 → 已確認不滿足', daily_necessary.describe(receipt))

    def test_three_people_use_all_facts_without_reversing_necessary_only_rule(self):
        states = ('的入場證已確認有效', '的入場證尚未核實', '已確定沒有有效的入場證')
        possibilities = [(True,), (True, False), (False,)]
        for values in product(range(3), repeat=3):
            with self.subTest(values=values):
                ordered = list(zip('一二三', values, strict=True))
                facts = '，'.join('第' + ordinal + '位' + states[value] for ordinal, value in reversed(ordered))
                plan = daily_necessary.parse(PERMITS.replace('三位', '3 位') + facts + '。' + LABELS)
                self.assertEqual([c['name'] for c in plan['conditions']], [f'第{o}位入場證' for o in '一二三'])
                allowed = {act for data in product(*(possibilities[value] for value in values))
                           for act in (False, True) if not act or all(data)}
                expected = '不可進' if allowed == {False} else '待核實'
                self.assertEqual(daily_necessary.evaluate(plan)[0], expected)
        eighth = '。'.join('第' + o + '位的入場證已確認有效' for o in '一二三四五六七')
        eighth += '。第八位的唯一入場證已確定失效。'
        plan = daily_necessary.parse(PERMITS.replace('三位', '八位') + eighth + LABELS)
        self.assertEqual(len(plan['conditions']), 8)
        self.assertEqual(daily_necessary.evaluate(plan)[0], '不可進')

    def test_unbound_people_evidence_and_mixed_questions_do_not_use_roster_route(self):
        question = RECORDING['question']
        for prompt in (
            question.replace('兩位講者', '三位講者'),
            question.replace('第二位已撤回', '第一位已撤回'),
            question.replace('唯一的同意書', '同意書'),
            question.replace('唯一的同意書', '唯一的入場證'),
            question.replace('第一位今天', '第一位昨天'),
            question.replace('已撤回唯一的同意書', '可能撤回唯一的同意書'),
            question.replace('第一位今天的文件還沒核對', '第一位今天的文件已確認有效'),
            question.replace('確定不能錄回', '確定不能入場回'),
            question + '再寄出通知。',
            '請引用「' + question + '」',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(daily_necessary.parse(prompt))

    def test_default_retry_followup_and_restore_reverify_the_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            sid = store.new_session()
            for message in RECORDING['history']:
                store.add_message(sid, message['role'], message['content'])
            with patch.object(daily_necessary, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
                with self.assertRaises(EngineError):
                    app.ask(sid, RECORDING['question'])
            first = app.retry(sid)
            self.assertEqual(first['content'], '不可錄')
            inquiry = app.ask(sid, '沿用上一輪條件：還要核對什麼？')
            self.assertIn('第二位有有效的同意書', inquiry['content'])
            with self.assertRaisesRegex(EngineError, '完整條件名稱'):
                app.ask(sid, '沿用上一輪條件：第二位今天的同意書已確認有效。重算。')
            corrected = app.ask(sid, '沿用上一輪條件：第二位的同意書已確認有效。重算。')
            self.assertEqual(corrected['content'], '待核')
            backup = create_backup(store.path, root / 'roster.paibackup')
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored/assistant.sqlite3')
            receipt = daily_necessary.read(restored, corrected['id'])
            self.assertEqual(receipt['analysis']['status'], 'undetermined')
            self.assertEqual(receipt['plan']['original']['schema'], daily_necessary.V3)
            context = restored.answer_context(first['id'])
            context[daily_necessary.FIELD]['plan']['conditions'][0]['scope'] = '昨天'
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(context, ensure_ascii=False), first['id']))
            with self.assertRaisesRegex(ValueError, '必要條件規約'):
                daily_necessary.read(restored, corrected['id'])
            engine.answer.assert_not_called()

    def test_raw_evaluation_and_attached_requests_use_model_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '模型原答'
            raw = DailyAssistant(store, engine, default_chat=True, allow_local_evidence=False)
            self.assertEqual(raw.ask(store.new_session(), RECORDING['question'])['content'], '模型原答')
            sid = store.new_session()
            note = root / 'document.txt'
            note.write_text('另一份尚待查證的紀錄。', encoding='utf-8')
            store.attach_document(sid, note)
            default = DailyAssistant(store, engine, default_chat=True)
            result = default.ask(sid, RECORDING['question'])
            self.assertEqual(result['content'], '模型原答')
            self.assertIsNone(daily_necessary.read(store, result['id']))
            self.assertEqual(engine.answer.call_count, 2)


class DailyGroupedRosterPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型原答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_frozen_complete_dialogues_use_original_grouped_evidence(self):
        states = [('true', 'true', 'true', 'true'), ('true', 'true', 'false', 'unknown'),
                  ('true', 'true', 'unknown', 'true')]
        for case, expected_states in zip(GROUPED, states, strict=True):
            with self.subTest(case=case['id']):
                sid = self.store.new_session()
                for row in case['history']:
                    self.store.add_message(sid, row['role'], row['content'])
                reply = self.app.ask(sid, case['question'])
                receipt = daily_necessary.read(self.store, reply['id'])
                plan = receipt['plan']
                self.assertEqual(reply['content'], case['answer'])
                self.assertEqual(plan['schema'], daily_necessary.V4)
                self.assertEqual(tuple(c['state'] for c in plan['conditions']), expected_states)
                self.assertTrue(all(c['scope'] == '今天' for c in plan['conditions']))
                self.assertIs(receipt['model_called'], False)
                self.assertEqual(self.store.answer_context(reply['id'])['recent_conversation'], [])
        self.engine.answer.assert_not_called()

    def test_grouped_facts_respect_necessity_sufficiency_and_known_failure(self):
        rule = '三位維護員都必須持有本次有效的區域通行證'
        labels = '能使用回「通行」，不能使用回「停用」，資料不足回「待核」。只回標籤。'
        details = ('已核對有效', '尚未核對', '唯一的通行證已失效')
        possibilities = ((True,), (False, True), (False,))
        for sufficient, values in product((False, True), product(range(3), repeat=3)):
            with self.subTest(sufficient=sufficient, values=values):
                clauses = []
                for state, detail in enumerate(details):
                    group = [f'第{n}' for n, value in enumerate(values, 1) if value == state]
                    if state == 2:
                        clauses.extend(person + '位' + detail for person in group)
                    elif group:
                        clauses.append('、'.join(group) + '位' + detail)
                text = rule + ('，三位都有效就是全部要求' if sufficient else '')
                text += '。本次' + '，'.join(clauses) + '。' + labels
                allowed = {action for observations in product(*(possibilities[v] for v in values))
                           for action in (True, False)
                           if (action == all(observations) if sufficient else not action or all(observations))}
                expected = '通行' if allowed == {True} else '停用' if allowed == {False} else '待核'
                self.assertEqual(daily_necessary.evaluate(daily_necessary.parse(text))[0], expected)
        maximum = ('八位維護員都必須持有本次有效的區域通行證，八位都有效就是全部要求。'
                   '本次第1、第2、第3、第4、第5、第6、第7及第8位均已核對有效。' + labels)
        self.assertEqual(daily_necessary.evaluate(daily_necessary.parse(maximum))[0], '通行')
        self.assertIsNone(daily_necessary.parse(maximum.replace('八位', '九位')))

    def test_ambiguous_roster_or_credential_facts_and_attached_requests_fall_back(self):
        question = GROUPED[1]['question']
        controls = (
            question.replace('，第四位尚未核對', ''),
            question.replace('第四位尚未核對', '第二位尚未核對'),
            question.replace('第四位尚未核對', '第五位尚未核對'),
            question.replace('唯一的使用碼', '唯一的門禁碼'),
            question.replace('唯一的使用碼', '使用碼'),
            question.replace('第三位唯一', '第三位昨天唯一'),
            question.replace('四名都有效', '三名都有效'),
            question.replace('今天的模擬規則', '本次的模擬規則'),
            question.replace('第三位唯一', '第三及第四位唯一').replace('，第四位尚未核對', ''),
            question + '再替我登入。',
            GROUPED[0]['question'].replace('，全都有效', ''),
        )
        for prompt in controls:
            with self.subTest(prompt=prompt):
                self.assertIsNone(daily_necessary.parse(prompt))
                self.assertEqual(self.app.ask(self.store.new_session(), prompt)['content'], '模型原答')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), question)['content'], '模型原答')
        sid = self.store.new_session()
        note = self.root / 'observations.txt'
        note.write_text('補充本次使用碼資料', encoding='utf-8')
        self.store.attach_document(sid, note)
        self.assertEqual(self.app.ask(sid, question)['content'], '模型原答')

    def test_grouped_receipt_retry_correction_and_backup_preserve_person_bindings(self):
        question = GROUPED[1]['question']
        sid = self.store.new_session()
        with patch.object(daily_necessary, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, question)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], '禁止')
        receipt = daily_necessary.read(self.store, first['id'])
        self.assertIn('第三位｜第三位唯一的使用碼已作廢 → 已確認不滿足', daily_necessary.describe(receipt))
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, '沿用上一輪條件：第三位的使用碼已確認有效。重算。')
        correction = self.app.ask(sid, '沿用上一輪條件：第三位的繪圖室使用碼已確認有效。重算。')
        self.assertEqual(correction['content'], '未定')
        final = self.app.ask(sid, '沿用上一輪條件：第四位的繪圖室使用碼已確認有效。重算。')
        self.assertEqual(final['content'], '准用')
        bundle = create_backup(self.store.path, self.root / 'grouped.paibackup')
        restore_backup(bundle['path'], self.root / 'restored')
        restored = Store(self.root / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, final['id'])['plan']['original']['schema'], daily_necessary.V4)
        context = restored.answer_context(first['id'])
        context[daily_necessary.FIELD]['plan']['conditions'][2]['state'] = 'unknown'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily_necessary.read(restored, final['id'])
        self.engine.answer.assert_not_called()
