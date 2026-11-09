import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant import answer_runs
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary
from scripts.daily_expenses import DailyAssistant


HEAD = '使用轉運台要有已校驗的水位計和有效的操作證。'
LABELS = '確定能用回「可用」，確定不能用回「停用」，尚不能判定回「待查」。請只輸出結論。'
FALSE_AND_UNKNOWN = HEAD + '水位計尚未校驗，唯一的操作證已確定作廢。' + LABELS
PHYSICAL_HEAD = '啟用沖洗站須同時確認進水接頭已接妥、出水閥已開，兩項就是全部要求。'
PHYSICAL_CODES = '請只回 R 代表能、B 代表不能、X 代表未定。'
PHYSICAL_PENDING = PHYSICAL_HEAD + '進水接頭鎖好，出水閥未檢查。' + PHYSICAL_CODES
PHYSICAL_SEPARATE = '只輸出指定代碼。確定可行回 R，確定不可行回 B，尚無法判定回 X。'
DECLARED_HEAD = ('判斷「送交維護報告」須同時符合「檔案已完成簽署」及（「原件已登錄」或「副本已核准」），'
                 '這些就是全部要求。')
DECLARED_PENDING = (DECLARED_HEAD + '已確認「檔案已完成簽署」成立，已確認「原件已登錄」不成立，'
                    '「副本已核准」尚未核對。' + PHYSICAL_SEPARATE)


class DailyDeclaredPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '一般模型回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_declared_groups_match_independent_worlds_and_preserve_atomic_names(self):
        names = ('檔案已完成簽署', '原件已登錄', '副本已核准')
        for states in product(('T', 'F', 'U'), repeat=3):
            observations = []
            for name, state in zip(names, states, strict=True):
                if state == 'U':
                    observations.append(f'「{name}」尚未核對')
                else:
                    observations.append(f'已確認「{name}」' + ('成立' if state == 'T' else '不成立'))
            possibilities = {'T': (True,), 'F': (False,), 'U': (True, False)}
            worlds = {first and (second or third) for first, second, third in product(
                *(possibilities[state] for state in states))}
            expected = 'R' if worlds == {True} else ('B' if worlds == {False} else 'X')
            request = DECLARED_HEAD + '，'.join(observations) + '。' + PHYSICAL_SEPARATE
            self.assertEqual(daily_necessary.evaluate(daily_necessary.parse(request))[0], expected)
        compound = DECLARED_PENDING.replace('檔案已完成簽署', '圖面和報告均已簽署')
        reply = self.app.ask(self.store.new_session(), compound)
        receipt = daily_necessary.read(self.store, reply['id'])
        self.assertEqual(receipt['plan']['groups'], [['圖面和報告均已簽署'], ['原件已登錄', '副本已核准']])
        self.assertEqual(reply['content'], 'X')
        atoms = [f'檢核 {i}' for i in range(1, 8)]
        groups = ['（' + '或'.join(f'「{name}」' for name in atoms[i:i + 3]) + '）' for i in (0, 3)]
        rule = '判斷「可交付」須同時符合' + '及'.join(groups)
        facts = '，'.join(f'已確認「{name}」成立' for name in atoms[:6])
        self.assertEqual(daily_necessary.evaluate(daily_necessary.parse(
            rule + '，這些就是全部要求。' + facts + '。' + PHYSICAL_SEPARATE))[0], 'R')
        self.assertIsNone(daily_necessary.parse(rule + '及「檢核 7」，這些就是全部要求。' + facts
                                                + '，已確認「檢核 7」成立。' + PHYSICAL_SEPARATE))
        self.engine.answer.assert_not_called()

    def test_rule_and_named_sources_reject_ambiguous_mixed_or_wrong_scope_input(self):
        for text in (
            DECLARED_PENDING.replace('（', '').replace('）', ''),
            DECLARED_PENDING.replace('副本已核准', '原件已登錄'),
            DECLARED_PENDING.replace('「副本已核准」尚未核對', '「別份副本已核准」尚未核對'),
            DECLARED_PENDING.replace('「副本已核准」尚未核對', '猜測「副本已核准」不成立'),
            DECLARED_PENDING.replace('尚未核對。', '尚未核對，已確認「副本已核准」成立。'),
            DECLARED_PENDING + '順便寄出檔案。',
            PHYSICAL_SEPARATE + DECLARED_PENDING,
        ):
            with self.subTest(text=text):
                self.assertIsNone(daily_necessary.parse(text))
                self.assertEqual(self.app.ask(self.store.new_session(), text)['content'], '一般模型回答')
        sid = self.store.new_session()
        path = self.path / 'proof.txt'
        path.write_text('另一份維護紀錄。', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, DECLARED_PENDING)['content'], '一般模型回答')

    def test_quoted_revision_and_check_order_recompute_after_restore(self):
        sid = self.store.new_session()
        original = DECLARED_PENDING.replace('副本已核准', 'B2 副本已核准')
        first = self.app.ask(sid, original)
        self.assertEqual(first['content'], 'X')
        inquiry = self.app.ask(sid, '還要核對什麼？')
        self.assertIn('B2 副本已核准', inquiry['content'])
        order = self.app.ask(sid, '沿用上一輪條件：檢查時間：「B2 副本已核准」3分鐘。先查哪一項最省時間？')
        self.assertIn('B2 副本已核准', order['content'])
        revised = self.app.ask(sid, '沿用上一輪條件：已確認「B2 副本已核准」成立。重算。')
        self.assertEqual(revised['content'], 'R')
        backup = create_backup(self.store.path, self.path / 'declared.paibackup')
        restore_backup(backup['path'], self.path / 'restored-declared')
        restored = Store(self.path / 'restored-declared/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, revised['id']), revised[daily_necessary.FIELD])
        context = restored.answer_context(first['id'])
        context[daily_necessary.FIELD]['plan']['groups'][1].reverse()
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily_necessary.read(restored, revised['id'])
        self.engine.answer.assert_not_called()


class DailyPhysicalPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型原答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_physical_observations_use_three_distinct_states_and_caller_codes(self):
        connectors = ('進水接頭鎖好', '唯一進水接頭已卸下', '進水接頭未核對')
        valves = ('出水閥也已全開', '出水閥確定仍關閉', '出水閥位置尚未查到')
        expected = (('R', 'B', 'X'), ('B', 'B', 'B'), ('X', 'B', 'X'))
        for (i, connector), (j, valve) in product(enumerate(connectors), enumerate(valves)):
            question = PHYSICAL_HEAD + connector + '，' + valve + '。' + PHYSICAL_CODES
            with self.subTest(question=question):
                self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], expected[i][j])
        sufficient = PHYSICAL_HEAD + '已確認接頭鎖好，出水閥也已全開。' + PHYSICAL_CODES
        self.assertEqual(self.app.ask(self.store.new_session(), sufficient)['content'], 'R')
        self.assertEqual(self.app.ask(self.store.new_session(),
                                      sufficient.replace('就是全部要求', '都是必要條件'))['content'], 'X')
        relabeled = PHYSICAL_PENDING.replace(PHYSICAL_CODES, '只回 Q 代表未定、J 代表不能、Y 代表能。')
        self.assertEqual(self.app.ask(self.store.new_session(), relabeled)['content'], 'Q')
        self.engine.answer.assert_not_called()

    def test_ambiguous_short_names_wrong_scope_and_attachments_preserve_normal_route(self):
        for request in (
            PHYSICAL_PENDING.replace('出水閥未檢查', '回水閥未檢查'),
            PHYSICAL_PENDING.replace('出水閥未檢查', '昨日出水閥未檢查'),
            PHYSICAL_PENDING.replace('出水閥未檢查', '出水閥已開，出水閥未檢查'),
            PHYSICAL_PENDING.replace('出水閥未檢查', '出水閥可能關閉'),
            PHYSICAL_PENDING.replace('、出水閥已開', '或出水閥已開'),
            PHYSICAL_PENDING.replace('X 代表未定', 'R 代表未定'),
            PHYSICAL_HEAD.replace('出水閥已開', '備用接頭已接妥')
            + '已確認接頭鎖好，備用接頭已接妥。' + PHYSICAL_CODES,
            PHYSICAL_PENDING + '並寄出通知。',
        ):
            with self.subTest(request=request):
                self.assertIsNone(daily_necessary.parse(request))
                self.assertEqual(self.app.ask(self.store.new_session(), request)['content'], '模型原答')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), PHYSICAL_PENDING)['content'], '模型原答')
        sid = self.store.new_session()
        document = self.path / 'checklist.txt'
        document.write_text('另附檢查紀錄。', encoding='utf-8')
        self.store.attach_document(sid, document)
        self.assertEqual(self.app.ask(sid, PHYSICAL_PENDING)['content'], '模型原答')
        late_sid = self.store.new_session()
        original_run = self.app._run_recorded

        def attach_before_run(*args, **kwargs):
            self.store.attach_document(late_sid, document)
            return original_run(*args, **kwargs)

        with patch.object(self.app, '_run_recorded', side_effect=attach_before_run):
            with self.assertRaisesRegex(EngineError, '提交前已加入附件'):
                self.app.ask(late_sid, PHYSICAL_PENDING)
        self.assertEqual([row['role'] for row in self.store.messages(late_sid, 5)], ['user'])

    def test_retry_revision_and_backup_recheck_complete_observations(self):
        sid = self.store.new_session()
        with patch.object(daily_necessary, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, PHYSICAL_PENDING)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], 'X')
        self.assertEqual(len(self.store.messages(sid, 10)), 2)
        self.assertIn('出水閥未檢查 → 尚未核實', daily_necessary.describe(daily_necessary.read(self.store, first['id'])))
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, '沿用上一輪條件：已確認接頭鎖好。重算。')
        revised = self.app.ask(sid, '沿用上一輪條件：出水閥已開。重算。')
        self.assertEqual(revised['content'], 'R')
        receipt = daily_necessary.read(self.store, revised['id'])
        saved = create_backup(self.store.path, self.path / 'proof.paibackup')
        restore_backup(saved['path'], self.path / 'restored')
        other = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(other, revised['id']), receipt)
        context = other.answer_context(first['id'])
        context[daily_necessary.FIELD]['plan']['conditions'][1]['state'] = 'false'
        with other.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily_necessary.read(other, revised['id'])
        self.engine.answer.assert_not_called()

    def test_separate_code_mapping_can_precede_or_follow_the_same_observations(self):
        facts = PHYSICAL_PENDING.removesuffix(PHYSICAL_CODES)
        remapped = '請只輸出代碼。資料不足回 Q，確定能回 X，確定不能回 R。'
        for text, expected in (
            (PHYSICAL_SEPARATE + facts, 'X'),
            (facts + PHYSICAL_SEPARATE, 'X'),
            (remapped + facts, 'Q'),
            (facts.replace('出水閥未檢查', '出水閥已開') + remapped, 'X'),
            (remapped + facts.replace('出水閥未檢查', '出水閥確定仍關閉'), 'R'),
        ):
            with self.subTest(text=text):
                reply = self.app.ask(self.store.new_session(), text)
                receipt = daily_necessary.read(self.store, reply['id'])
                self.assertEqual(reply['content'], expected)
                self.assertEqual(receipt['plan']['schema'], daily_necessary.V6)
                self.assertIn('回 ', receipt['plan']['output'])
        self.engine.answer.assert_not_called()

    def test_separate_mapping_rejects_second_directive_wrong_action_and_attachments(self):
        facts = PHYSICAL_PENDING.removesuffix(PHYSICAL_CODES)
        for text in (
            PHYSICAL_SEPARATE + facts + PHYSICAL_SEPARATE,
            PHYSICAL_SEPARATE.replace('確定可行', '確定可以停止') + facts,
            PHYSICAL_SEPARATE.replace('回 X', '回 R') + facts,
            PHYSICAL_SEPARATE + facts + '再執行啟用。',
            PHYSICAL_SEPARATE + facts.replace('，出水閥未檢查', ''),
        ):
            with self.subTest(text=text):
                self.assertIsNone(daily_necessary.parse(text))
                self.assertEqual(self.app.ask(self.store.new_session(), text)['content'], '模型原答')
        sid = self.store.new_session()
        path = self.path / 'codes.txt'
        path.write_text('另一份代碼和設備紀錄。', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, PHYSICAL_SEPARATE + facts)['content'], '模型原答')

    def test_v6_followup_and_backup_revalidate_the_initial_mapping(self):
        sid = self.store.new_session()
        initial = self.app.ask(sid, PHYSICAL_SEPARATE + PHYSICAL_PENDING.removesuffix(PHYSICAL_CODES))
        followup = self.app.ask(sid, '沿用上一輪條件：出水閥已開。重算。')
        self.assertEqual((initial['content'], followup['content']), ('X', 'R'))
        backup = create_backup(self.store.path, self.path / 'codes.paibackup')
        restore_backup(backup['path'], self.path / 'restored-codes')
        restored = Store(self.path / 'restored-codes/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, followup['id']), followup[daily_necessary.FIELD])
        context = restored.answer_context(initial['id'])
        context[daily_necessary.FIELD]['plan']['labels']['undetermined'] = 'R'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), initial['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily_necessary.read(restored, followup['id'])
        self.engine.answer.assert_not_called()


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


class DailyNecessaryRevisionTests(unittest.TestCase):
    INITIAL = ('啟用量測台的必要且充分條件是同時具備有效的操作證和（可用的北機或可用的南機）。'
               '唯一的操作證已確定失效，北機尚未檢查，南機已確認可用。' + LABELS)
    ENABLE = '沿用上一輪條件：操作證已確認有效。重算。'
    SOUTH_OUT = '沿用上一輪條件，唯一的南機已確定不可用，重新核對。'
    NORTH_READY = '沿用上一輪必要條件：北機已確認可用。請只回結論。'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '普通模型回答'
        self.app = DailyAssistant(self.store, self.engine)
        self.first = self.app.ask(self.sid, self.INITIAL)

    def test_multi_turn_revisions_preserve_other_members_and_prior_receipts(self):
        answers = [self.first]
        for prompt, expected in [(self.ENABLE, '可用'), (self.SOUTH_OUT, '待查'), (self.NORTH_READY, '可用')]:
            reply = self.app.ask(self.sid, prompt)
            self.assertEqual(reply['content'], expected)
            receipt = daily_necessary.read(self.store, reply['id'])
            self.assertEqual(receipt['plan']['source']['answer_id'], answers[-1]['id'])
            self.assertEqual(receipt['plan']['original']['request'], self.INITIAL)
            answers.append(reply)
        self.assertEqual(self.first['content'], '停用')
        final = daily_necessary.read(self.store, answers[-1]['id'])
        state = daily_necessary.effective(final['plan'])
        self.assertEqual(state['groups'], [['操作證'], ['北機', '南機']])
        self.assertEqual([c['state'] for c in state['conditions']], ['true', 'true', 'false'])
        self.assertIn('沿用上一則回答', daily_necessary.describe(final))
        self.engine.answer.assert_not_called()

    def test_previous_context_must_be_exact_and_in_the_same_session(self):
        for text in ('沿用上一輪條件：舊北機已確認可用。',
                     '沿用上一輪條件：北機可能可用。',
                     '沿用上一輪條件：北機已確認可用。並寄出通知。'):
            with self.subTest(text=text), self.assertRaises(EngineError) as exc:
                self.app.ask(self.sid, text)
            self.assertEqual(exc.exception.code, 'evidence_invalid')
        with self.assertRaises(EngineError):
            self.app.ask(self.store.new_session(), self.ENABLE)
        self.app.ask(self.sid, '換個話題。')
        self.assertEqual(self.engine.answer.call_count, 1)
        with self.assertRaises(EngineError):
            self.app.ask(self.sid, self.ENABLE)
        self.assertEqual(self.engine.answer.call_count, 1)

    def test_retry_checks_same_predecessor_and_no_duplicate_user_turn(self):
        evaluate = daily_necessary.evaluate
        def cancelled(plan, cancel=None):
            if plan['schema'] == daily_necessary.REVISION_PLAN:
                raise EngineError('停止', 'cancelled')
            return evaluate(plan, cancel)

        with patch.object(daily_necessary, 'evaluate', side_effect=cancelled):
            with self.assertRaises(EngineError):
                self.app.ask(self.sid, self.ENABLE)
        result = self.app.retry(self.sid)
        self.assertEqual(result['content'], '可用')
        self.assertEqual(len(self.store.messages(self.sid, 10)), 4)
        self.assertEqual(daily_necessary.read(self.store, result['id'])['plan']['source']['answer_id'], self.first['id'])

    def test_source_change_before_begin_blocks_commit_and_raw_mode_stays_raw(self):
        begin = answer_runs.begin
        def race(*args, **kwargs):
            self.store.add_message(self.sid, 'user', '這是另一則訊息')
            return begin(*args, **kwargs)
        with patch.object(answer_runs, 'begin', side_effect=race), self.assertRaises(EngineError) as exc:
            self.app.ask(self.sid, self.ENABLE)
        self.assertEqual(exc.exception.code, 'evidence_invalid')
        self.assertEqual(answer_runs.runs(self.store, self.sid)[0]['status'], 'failed')
        self.assertEqual(len([m for m in self.store.messages(self.sid, 10) if m['role'] == 'assistant']), 1)
        raw = DailyAssistant(self.store, self.engine, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), self.ENABLE)['content'], '普通模型回答')

    def test_restore_revalidates_each_predecessor(self):
        revised = self.app.ask(self.sid, self.ENABLE)
        newest = self.app.ask(self.sid, self.SOUTH_OUT)
        original = daily_necessary.read(self.store, newest['id'])
        bundle = create_backup(self.store.path, self.path / 'sample.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, newest['id']), original)
        context = restored.answer_context(self.first['id'])
        context['unrelated_change'] = True
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), self.first['id']))
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            daily_necessary.read(restored, newest['id'])
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            daily_necessary.read(restored, revised['id'])


class DailyNecessaryInquiryTests(unittest.TestCase):
    ASK = '還要核對什麼？'
    EXPLICIT = '沿用上一輪條件：還要核對什麼？'
    START = ('啟用量測台的必要且充分條件是同時具備有效的操作證和（可用的北機或可用的南機）。'
             '操作證尚未確認，北機尚未檢查，南機尚未檢查。' + LABELS)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '普通模型回答'
        self.app = DailyAssistant(self.store, self.engine)

    def test_pending_groups_describe_all_minimal_decisive_observations(self):
        groups = [['甲', '乙'], ['丙', '丁'], ['戊']]
        names = sum(groups, [])
        index_groups = [(0, 1), (2, 3), (4,)]
        worlds = list(product((False, True), repeat=5))
        valid = {world for world in worlds if all(any(world[i] for i in group) for group in index_groups)}

        def verdict(known, sufficient):
            possible = {world for world in worlds if all(value is None or value == world[i]
                                                          for i, value in enumerate(known))}
            if not possible & valid:
                return 'refuted'
            if sufficient and possible <= valid:
                return 'supported'
            return 'undetermined'

        for known in product((None, False, True), repeat=5):
            unknown = [i for i, value in enumerate(known) if value is None]
            conditions = [dict(name=name, state={None: 'unknown', False: 'false', True: 'true'}[value])
                          for name, value in zip(names, known, strict=True)]
            for sufficient in (False, True):
                current = verdict(known, sufficient)
                state = dict(groups=groups, conditions=conditions, sufficient=sufficient)
                result = daily_necessary.inquiry_result(state, current)
                with self.subTest(known=known, sufficient=sufficient):
                    self.assertEqual(result['needs_sufficiency'], not sufficient and current == 'undetermined')
                    if current != 'undetermined':
                        self.assertEqual(result['support'], [])
                        self.assertEqual(result['refute'], [])
                        self.assertEqual(bool(result['blockers']), current == 'refuted')
                        continue
                    minimal = {'refuted': set(), 'supported': set()}
                    for checks in product((None, False, True), repeat=len(unknown)):
                        updated = list(known)
                        changes = {i: value for i, value in zip(unknown, checks, strict=True) if value is not None}
                        for i, value in changes.items():
                            updated[i] = value
                        outcome = verdict(updated, sufficient)
                        if outcome == 'undetermined':
                            continue
                        for i in changes:
                            reduced = updated.copy()
                            reduced[i] = None
                            if verdict(reduced, sufficient) == outcome:
                                break
                        else:
                            minimal[outcome].add(frozenset((names[i], value) for i, value in changes.items()))
                    reported_refute = {frozenset((name, False) for name in group) for group in result['refute']}
                    reported_support = {frozenset((name, True) for name in choice)
                                        for choice in product(*result['support'])} if result['support'] else set()
                    self.assertEqual(reported_refute, minimal['refuted'])
                    self.assertEqual(reported_support, minimal['supported'])

    def test_read_only_inquiry_can_be_followed_by_correction_and_another_inquiry(self):
        first = self.app.ask(self.sid, self.START)
        asked = self.app.ask(self.sid, self.ASK)
        receipt = daily_necessary.read(self.store, asked['id'])
        self.assertEqual(receipt['plan']['source']['answer_id'], first['id'])
        self.assertEqual(receipt['inquiry']['refute'], [['操作證'], ['北機', '南機']])
        self.assertIn('可用的北機 或 可用的南機', asked['content'])
        self.assertIn('沿用上一則回答', daily_necessary.describe(receipt))
        with self.assertRaisesRegex(EngineError, '上一則已列出'):
            self.app.ask(self.sid, self.EXPLICIT)
        revised = self.app.ask(self.sid, '沿用上一輪條件：操作證已確認有效。重算。')
        self.assertEqual(revised['content'], '待查')
        self.assertEqual(daily_necessary.read(self.store, revised['id'])['plan']['source']['answer_id'], asked['id'])
        newest = self.app.ask(self.sid, self.EXPLICIT)
        latest = daily_necessary.read(self.store, newest['id'])
        self.assertEqual(latest['inquiry']['support'], [['北機', '南機']])
        self.assertEqual(daily_necessary.read(self.store, asked['id']), receipt)
        bundle = create_backup(self.store.path, self.path / 'inquiries.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, newest['id']), latest)
        prior = restored.answer_context(asked['id'])
        prior['unrelated_change'] = True
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(prior, ensure_ascii=False), asked['id']))
        with self.assertRaisesRegex(ValueError, '來源已變更'):
            daily_necessary.read(restored, newest['id'])
        self.engine.answer.assert_not_called()

    def test_chain_at_the_revision_limit_still_verifies_last_inquiry(self):
        self.app.ask(self.sid, self.START)
        self.app.ask(self.sid, self.ASK)
        correction = '沿用上一輪條件：操作證已確認有效。重算。'
        for _ in range(daily_necessary.MAX_REVISIONS):
            self.app.ask(self.sid, correction)
            latest = self.app.ask(self.sid, self.EXPLICIT)
        plan = daily_necessary.read(self.store, latest['id'])['plan']
        self.assertEqual(len(plan['basis']['revisions']), 8)
        with self.assertRaisesRegex(EngineError, '八次更正上限'):
            self.app.ask(self.sid, correction)
        self.engine.answer.assert_not_called()

    def test_bare_inquiry_needs_current_anchor_and_respects_raw_and_attachment_routes(self):
        self.app.ask(self.sid, self.START)
        self.assertEqual(self.app.ask(self.store.new_session(), self.ASK)['content'], '普通模型回答')
        raw = DailyAssistant(self.store, self.engine, allow_local_evidence=False)
        raw_sid = self.store.new_session()
        self.app.ask(raw_sid, self.START)
        self.assertEqual(raw.ask(raw_sid, self.EXPLICIT)['content'], '普通模型回答')
        path = self.path / 'sample.txt'
        path.write_text('測試用附件', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        with self.assertRaisesRegex(EngineError, '只接受純文字'):
            self.app.ask(self.sid, self.EXPLICIT)
        self.assertEqual(self.app.ask(self.sid, self.ASK)['content'], '普通模型回答')
        self.assertEqual(self.engine.answer.call_count, 3)

    def test_cancelled_inquiry_can_retry_and_receipt_is_rederived(self):
        first = self.app.ask(self.sid, self.START)
        evaluate = daily_necessary.evaluate

        def cancelled(plan, cancel=None):
            if plan['schema'] == daily_necessary.INQUIRY_PLAN:
                raise EngineError('停止', 'cancelled')
            return evaluate(plan, cancel)

        with patch.object(daily_necessary, 'evaluate', side_effect=cancelled), self.assertRaisesRegex(EngineError, '停止'):
            self.app.ask(self.sid, self.ASK)
        result = self.app.retry(self.sid)
        self.assertEqual(len(self.store.messages(self.sid, 10)), 4)
        self.assertEqual(daily_necessary.read(self.store, result['id'])['plan']['source']['answer_id'], first['id'])
        context = self.store.answer_context(result['id'])
        context[daily_necessary.FIELD]['inquiry']['refute'] = [['南機']]
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), result['id']))
        with self.assertRaisesRegex(ValueError, '原問或推論不符'):
            daily_necessary.read(self.store, result['id'])
        self.engine.answer.assert_not_called()


class DailyNecessaryDisputeTests(unittest.TestCase):
    INITIAL = DailyNecessaryInquiryTests.START
    ASK = DailyNecessaryInquiryTests.ASK
    PAIR = ('沿用上一輪條件：巡檢表回報「北機已確認可用」，'
            '維修表回報「唯一的北機已確定不可用」。保留分歧。')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '普通模型回答'
        self.app = DailyAssistant(self.store, self.engine)

    def test_paired_reports_preserve_both_sides_and_only_the_named_condition(self):
        initial = self.app.ask(self.sid, self.INITIAL)
        answer = self.app.ask(self.sid, self.PAIR)
        receipt = daily_necessary.read(self.store, answer['id'])
        self.assertEqual(receipt['analysis']['status'], 'undetermined')
        self.assertEqual(receipt['plan']['source']['answer_id'], initial['id'])
        self.assertEqual(receipt['conflicts'], [dict(name='北機', reports=[
            dict(source='巡檢表', observation='北機已確認可用', state='true'),
            dict(source='維修表', observation='唯一的北機已確定不可用', state='false'),
        ])])
        self.assertNotIn('已確認「條件2 北機可用」', receipt['canonical'])
        self.assertNotIn('已確認並非「條件2 北機可用」', receipt['canonical'])
        state = daily_necessary.effective(receipt['plan'])
        self.assertEqual([c['observation'] for c in state['conditions']],
                         ['操作證尚未確認', '北機尚未檢查', '南機尚未檢查'])
        self.assertIn('巡檢表「北機已確認可用」', answer['content'])
        self.assertIn('維修表「唯一的北機已確定不可用」', daily_necessary.describe(receipt))
        self.engine.answer.assert_not_called()

    def test_a_conflict_does_not_hide_an_independent_blocker_or_disqualify_a_backup(self):
        for permit, south in product(('true', 'false'), ('true', 'false', 'unknown')):
            # The north machine can take either value. Determine the expected result independently.
            possibilities = {permit == 'true' and (north or backup)
                             for north in (True, False)
                             for backup in {'true': (True,), 'false': (False,), 'unknown': (True, False)}[south]}
            status = {frozenset({True}): 'supported', frozenset({False}): 'refuted',
                      frozenset({False, True}): 'undetermined'}[frozenset(possibilities)]
            with self.subTest(permit=permit, south=south):
                sid = self.store.new_session()
                permit_text = {'true': '操作證已確認有效', 'false': '唯一的操作證已確定失效'}[permit]
                south_text = {'true': '南機已確認可用', 'false': '唯一的南機已確定不可用',
                              'unknown': '南機尚未檢查'}[south]
                text = self.INITIAL.replace('操作證尚未確認', permit_text).replace('南機尚未檢查', south_text)
                self.app.ask(sid, text)
                answer = self.app.ask(sid, self.PAIR)
                check = self.app.ask(sid, self.ASK)
                receipt = daily_necessary.read(self.store, answer['id'])
                inquiry = daily_necessary.read(self.store, check['id'])
                self.assertEqual(receipt['analysis']['status'], status)
                self.assertEqual(inquiry['inquiry']['conclusion'], status)
                self.assertEqual(inquiry['conflicts'], receipt['conflicts'])
                self.assertIn('北機 · 巡檢表', check['content'])
                if status == 'supported':
                    self.assertEqual(inquiry['inquiry']['support'], [])
                elif status == 'refuted':
                    self.assertEqual(inquiry['inquiry']['blockers'], [['操作證']])
        self.engine.answer.assert_not_called()

    def test_single_report_requires_a_conflicting_prior_confirmation_and_only_known_correction_clears_it(self):
        initial = self.app.ask(self.sid, self.INITIAL)
        report = '沿用上一輪條件：交班表回報「操作證已確認有效」。保留分歧。'
        with self.assertRaisesRegex(EngineError, '一正一反'):
            self.app.ask(self.sid, report)
        correction = '沿用上一輪條件：唯一的操作證已確定失效。重算。'
        self.app.ask(self.sid, correction)
        disputed = self.app.ask(self.sid, report)
        receipt = daily_necessary.read(self.store, disputed['id'])
        self.assertEqual(receipt['conflicts'][0]['reports'][1], dict(
            source=None, observation='唯一的操作證已確定失效', state='false'))
        self.assertEqual(receipt['plan']['original']['request'], self.INITIAL)
        self.assertEqual(receipt['analysis']['status'], 'undetermined')
        with self.assertRaisesRegex(EngineError, '仍有分歧'):
            self.app.ask(self.sid, '沿用上一輪條件：操作證尚未確認。重算。')
        with self.assertRaisesRegex(EngineError, '已有待核對'):
            self.app.ask(self.sid, report)
        self.app.ask(self.sid, self.ASK)
        resolved = self.app.ask(self.sid, correction)
        latest = daily_necessary.read(self.store, resolved['id'])
        self.assertEqual(resolved['content'], '停用')
        self.assertNotIn('conflicts', latest)
        self.assertIn(report, latest['plan']['revisions'])
        self.assertEqual(daily_necessary.read(self.store, disputed['id']), receipt)
        self.assertEqual(daily_necessary.read(self.store, initial['id'])['plan']['request'], self.INITIAL)
        self.engine.answer.assert_not_called()

    def test_ambiguous_reports_and_mixed_actions_never_change_the_saved_state(self):
        first = self.app.ask(self.sid, self.INITIAL)
        for text in (self.PAIR.replace('巡檢表', 'ABC').replace('維修表', 'abc'),
                     self.PAIR.replace('唯一的北機已確定不可用', '唯一的南機已確定不可用'),
                     self.PAIR.replace('唯一的北機已確定不可用', '北機已確認可用'),
                     self.PAIR.replace('唯一的北機已確定不可用', '北機尚未檢查'),
                     self.PAIR.replace('北機已確認可用', '舊北機已確認可用'),
                     self.PAIR + '並啟動機組。'):
            with self.subTest(text=text), self.assertRaises(EngineError) as exc:
                self.app.ask(self.sid, text)
            self.assertEqual(exc.exception.code, 'evidence_invalid')
        self.assertEqual(self.store.messages(self.sid, 1)[0]['id'], first['id'])
        with self.assertRaises(EngineError):
            self.app.ask(self.store.new_session(), self.PAIR)
        raw = DailyAssistant(self.store, self.engine, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.sid, self.PAIR)['content'], '普通模型回答')
        self.engine.answer.assert_called_once()

    def test_two_disputed_conditions_keep_independent_resolution(self):
        self.app.ask(self.sid, self.INITIAL)
        self.app.ask(self.sid, self.PAIR)
        permit = self.app.ask(self.sid, '沿用上一輪條件：交班表回報「操作證已確認有效」，'
                              '查驗單回報「唯一的操作證已確定失效」。保留分歧。')
        receipt = daily_necessary.read(self.store, permit['id'])
        self.assertEqual([item['name'] for item in receipt['conflicts']], ['操作證', '北機'])
        corrected = self.app.ask(self.sid, '沿用上一輪條件：北機已確認可用。重算。')
        current = daily_necessary.read(self.store, corrected['id'])
        self.assertEqual([item['name'] for item in current['conflicts']], ['操作證'])
        self.assertEqual(corrected['content'], '待查')
        self.assertIn('交班表「操作證已確認有效」', daily_necessary.describe(current))
        self.engine.answer.assert_not_called()

    def test_restore_rederives_dispute_provenance_and_rejects_altered_reports(self):
        self.app.ask(self.sid, self.INITIAL)
        disputed = self.app.ask(self.sid, self.PAIR)
        latest = self.app.ask(self.sid, self.ASK)
        receipt = daily_necessary.read(self.store, latest['id'])
        backup = create_backup(self.store.path, self.path / 'test.paibackup')
        restore_backup(backup['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(restored, latest['id']), receipt)
        context = restored.answer_context(disputed['id'])
        context[daily_necessary.FIELD]['conflicts'][0]['reports'][1]['source'] = '巡檢表'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), disputed['id']))
        with self.assertRaisesRegex(ValueError, '原問或推論不符'):
            daily_necessary.read(restored, latest['id'])
