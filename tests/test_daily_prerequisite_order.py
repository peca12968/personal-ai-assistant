import json
import tempfile
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary, daily_prerequisite_order
from scripts.daily_expenses import DailyAssistant


class DailyPrerequisiteOrderTests(unittest.TestCase):
    INITIAL = ('啟用測試台的必要且充分條件是同時具備有效的操作證和（可用的東泵或可用的西泵）。'
               '操作證尚未確認，東泵尚未檢查，西泵尚未檢查。'
               '確定能啟用回「啟用」，確定不能啟用回「停用」，資料不足回「待查」。請只輸出結論。')
    ASK = '還要核對什麼？'
    ORDER = '沿用上一輪條件：檢查時間：操作證4分鐘，東泵2分鐘，西泵7分鐘。先查哪一項最省時間？'
    VERIFIED = '沿用上一輪條件：操作證已確認有效。重算。'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '普通模型回答'
        self.app = DailyAssistant(self.store, self.engine)

    def test_tree_matches_exhaustive_policy_search_and_each_world(self):
        worlds = list(product((False, True), repeat=4))
        names = ('甲', '乙', '丙', '丁')
        layouts = (((0,), (1, 2, 3)), ((0, 1), (2, 3)), ((0,), (1,), (2,), (3,)))
        profiles = ((1, 1, 1, 1), (4, 2, 7, 3), (999, 1, 999, 9), (3, 5, 1, 2))
        for layout, times, sufficient in product(layouts, profiles, (True, False)):
            def outcome(world):
                if not all(any(world[i] for i in group) for group in layout):
                    return 'refuted'
                return 'supported' if sufficient else 'undetermined'

            def best(remaining, candidates):
                if len({outcome(world) for world in remaining}) == 1:
                    return Fraction(0), 0
                scores = []
                for i in candidates:
                    rest = tuple(k for k in candidates if k != i)
                    left = [world for world in remaining if not world[i]]
                    right = [world for world in remaining if world[i]]
                    left_average, left_max = best(left, rest)
                    right_average, right_max = best(right, rest)
                    average = (len(left) * left_average + len(right) * right_average) / len(remaining)
                    scores.append((times[i] + average, times[i] + max(left_max, right_max)))
                return min(scores)

            groups = [[names[i] for i in group] for group in layout]
            minutes = dict(zip(names, times, strict=True))
            order = daily_prerequisite_order.plan_order(groups, minutes, 'undetermined', sufficient)
            with self.subTest(layout=layout, times=times, sufficient=sufficient):
                score = Fraction(*order['average_minutes']), order['max_minutes']
                self.assertEqual(score, best(worlds, tuple(range(4))))
                durations = []
                for world in worlds:
                    checked = set()
                    node = order['tree']
                    while 'check' in node:
                        name = node['check']
                        self.assertNotIn(name, checked)
                        self.assertEqual(node['minutes'], minutes[name])
                        checked.add(name)
                        node = node['if_true' if world[names.index(name)] else 'if_false']
                    self.assertEqual(node['stop'], outcome(world))
                    durations.append(sum(minutes[name] for name in checked))
                self.assertEqual(score, (Fraction(sum(durations), len(worlds)), max(durations)))

    def test_followup_can_plan_after_inquiry_then_replan_after_observation(self):
        self.app.ask(self.sid, self.INITIAL)
        inquiry = self.app.ask(self.sid, self.ASK)
        result = self.app.ask(self.sid, self.ORDER)
        receipt = daily_necessary.read(self.store, result['id'])
        order = receipt['check_order']
        self.assertEqual(receipt['plan']['source']['answer_id'], inquiry['id'])
        self.assertEqual(order['average_minutes'], [27, 4])
        self.assertEqual(order['max_minutes'], 13)
        self.assertEqual(order['tree']['check'], '操作證')
        self.assertEqual(order['tree']['if_false'], {'stop': 'refuted'})
        self.assertEqual(order['tree']['if_true']['check'], '東泵')
        self.assertIn('完整檢查順序', daily_necessary.describe(receipt))
        self.assertIn('各種結果組合等權', result['content'])
        with self.assertRaisesRegex(EngineError, '已列出檢查順序'):
            self.app.ask(self.sid, self.ORDER)
        revised = self.app.ask(self.sid, self.VERIFIED)
        self.assertEqual(revised['content'], '待查')
        planned = self.app.ask(self.sid, self.ORDER)
        current = daily_necessary.read(self.store, planned['id'])['check_order']
        self.assertEqual(current['average_minutes'], [11, 2])
        self.assertEqual(current['tree']['check'], '東泵')
        self.assertEqual(daily_necessary.read(self.store, result['id']), receipt)
        self.engine.answer.assert_not_called()

    def test_skips_irrelevant_unknowns_and_retains_necessary_only_boundary(self):
        blocked = self.INITIAL.replace('操作證尚未確認', '唯一的操作證已確定失效')
        self.app.ask(self.sid, blocked)
        short = self.ORDER.replace('操作證4分鐘，東泵2分鐘，西泵7分鐘', '北機1分鐘')
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(self.sid, short)
        result = self.app.ask(self.sid, short.replace('北機', '東泵'))
        self.assertEqual(daily_necessary.read(self.store, result['id'])['check_order'],
                         dict(tree={'stop': 'refuted'}, average_minutes=[0, 1], max_minutes=0))
        sid = self.store.new_session()
        necessary = self.INITIAL.replace('的必要且充分條件是同時具備', '須同時具備')
        self.app.ask(sid, necessary)
        answer = self.app.ask(sid, self.ORDER)
        node = daily_necessary.read(self.store, answer['id'])['check_order']['tree']
        self.assertEqual(node['if_true']['if_true'], {'stop': 'undetermined'})
        self.app.ask(sid, self.VERIFIED)
        self.app.ask(sid, '沿用上一輪條件：東泵已確認可用。重算。')
        final = self.app.ask(sid, short.replace('北機', '東泵'))
        self.assertIn('需要充分條件或直接證據', final['content'])
        self.engine.answer.assert_not_called()

    def test_invalid_costs_do_not_consume_the_source_and_raw_route_remains_available(self):
        first = self.app.ask(self.sid, self.INITIAL)
        for request in (self.ORDER.replace('，西泵7分鐘', ''),
                        self.ORDER.replace('西泵7分鐘', '東泵7分鐘'),
                        self.ORDER.replace('西泵7分鐘', '西泵0分鐘'),
                        self.ORDER.replace('西泵7分鐘', '西泵1000分鐘'),
                        self.ORDER.replace('西泵7分鐘', '西泵1.5分鐘'),
                        self.ORDER.replace('西泵7分鐘', '西泵7秒'),
                        self.ORDER + '直接開啟設備。'):
            with self.subTest(request=request), self.assertRaises(EngineError):
                self.app.ask(self.sid, request)
        self.assertEqual(self.store.messages(self.sid, 1)[0]['id'], first['id'])
        raw = DailyAssistant(self.store, self.engine, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.sid, self.ORDER)['content'], '普通模型回答')
        self.engine.answer.assert_called_once()

    def test_retry_restore_and_changed_tree_detection(self):
        self.app.ask(self.sid, self.INITIAL)
        evaluate = daily_necessary.evaluate

        def cancelled(plan, cancel=None):
            if plan['schema'] == daily_necessary.COST_PLAN:
                raise EngineError('停止', 'cancelled')
            return evaluate(plan, cancel)

        with patch.object(daily_necessary, 'evaluate', side_effect=cancelled), self.assertRaises(EngineError):
            self.app.ask(self.sid, self.ORDER)
        answer = self.app.retry(self.sid)
        self.assertEqual(len(self.store.messages(self.sid, 10)), 4)
        receipt = daily_necessary.read(self.store, answer['id'])
        backup = create_backup(self.store.path, self.path / 'order.paibackup')
        restore_backup(backup['path'], self.path / 'restored')
        store = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_necessary.read(store, answer['id']), receipt)
        snapshot = store.answer_context(answer['id'])
        snapshot[daily_necessary.FIELD]['check_order']['tree']['if_false']['stop'] = 'supported'
        with store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(snapshot, ensure_ascii=False), answer['id']))
        with self.assertRaisesRegex(ValueError, '原問或推論不符'):
            daily_necessary.read(store, answer['id'])
        self.engine.answer.assert_not_called()

    def test_maximum_history_with_two_read_only_followups_per_state(self):
        self.app.ask(self.sid, self.INITIAL)
        self.app.ask(self.sid, self.ASK)
        self.app.ask(self.sid, self.ORDER)
        for _ in range(daily_necessary.MAX_REVISIONS):
            self.app.ask(self.sid, self.VERIFIED)
            self.app.ask(self.sid, self.ASK)
            answer = self.app.ask(self.sid, self.ORDER)
        latest = daily_necessary.read(self.store, answer['id'])
        self.assertEqual(len(latest['plan']['basis']['revisions']), daily_necessary.MAX_REVISIONS)
        self.assertEqual(latest['check_order']['tree']['check'], '東泵')
        with self.assertRaisesRegex(EngineError, '八次更正上限'):
            self.app.ask(self.sid, self.VERIFIED)
        self.engine.answer.assert_not_called()
