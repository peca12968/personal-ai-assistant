import unittest

from scripts.daily_clearance import opening_reference, proof_format, verified_answer


class OpeningReferenceTests(unittest.TestCase):
    question = ('剛性樣品是一個硬質正方體木箱，邊長 0.62 公尺，不可拆箱。'
                '唯一的門高 1.9 公尺，淨寬尚未量測，門前後空曠。能否搬入？')

    def previous(self, q=None):
        return dict(recent_conversation=[dict(role='user', message_id=1, content=q or self.question, truncated=False),
                                         dict(role='assistant', message_id=2, content='我猜 9 毫米', truncated=False)],
                    conversation_tail=dict(message_id=2))

    def test_units_and_hypotheses_remain_separate_from_measurements(self):
        ref = opening_reference(dict(current_request=self.question))
        self.assertEqual(ref['箱邊毫米'], '620.00')
        self.assertEqual(opening_reference(dict(current_request=self.question.replace('公尺，淨寬','公尺、淨寬'))), ref)
        self.assertEqual(ref['假設示例非實測'], ['310.00','1240.00'])
        actual = opening_reference(dict(current_request='實測門淨寬 0.59 公尺。', **self.previous()))
        self.assertEqual((actual['來源使用者訊息'], actual['本輪淨寬毫米']), (1,'590.00'))
        self.assertNotIn('假設示例非實測', actual)

    def test_equal_size_does_not_claim_operating_clearance(self):
        ref = opening_reference(dict(current_request='門的淨寬是 620 毫米。', **self.previous()))
        self.assertIn('等於', ref['本輪尺寸比較'])
        self.assertIn('間隙', ref['本輪尺寸比較'])

    def test_verifier_requires_a_complete_unobstructed_scene(self):
        q = ('一個不可拆的硬質正方體木箱，邊長 0.62 公尺，要原樣搬進倉庫。'
             '唯一的門高 1.9 公尺，淨寬尚未量測，門前後空曠。能否把箱子搬進去？請只問我還要量哪個尺寸。')
        ctx = dict(current_request=q)
        ctx['door_measurements'] = opening_reference(ctx)
        answer, receipt = verified_answer(ctx, '不可行。')
        self.assertEqual((answer, receipt['raw_answer']), ('門的淨寬是多少？','不可行。'))
        for bad in (q.replace('門前後空曠','門已上鎖'), '翻譯：'+q, q.replace('唯一的門','其中一扇門')):
            self.assertIsNone(proof_format(bad))

    def test_known_obstacles_take_priority_over_unknown_width(self):
        base = ('一個不可拆的硬質正方體木箱，邊長 0.5 公尺，要原樣搬進展廳。'
                '唯一的門高 800 毫米，淨寬不詳，門前後空曠。能否通過？只回「可行」、「不可行」或「資料不足」。')
        self.assertEqual(verified_answer(dict(current_request=base, door_measurements=opening_reference(
            dict(current_request=base))), '可行')[0], '資料不足')
        for q in (base.replace('800 毫米','49 公分'), base.replace('門前後空曠','門已封閉，目前禁止通行'),
                  base.replace('門前後空曠','門已上鎖，現在無法打開')):
            ref = opening_reference(dict(current_request=q))
            self.assertNotIn('尚缺尺寸', ref)
            self.assertNotIn('假設示例非實測', ref)
            answer, audit = verified_answer(dict(current_request=q, door_measurements=ref), '資料不足。')
            self.assertEqual((answer, audit['schema']), ('不可行','door_passage_blocked_v1'))
            updated = opening_reference(dict(current_request='門的淨寬量到 2 公尺。', **self.previous(q)))
            self.assertEqual((updated['阻礙原因'],updated['本輪淨寬毫米']), (ref['阻礙原因'],'2000'))
        self.assertIsNone(opening_reference(dict(current_request=base.replace('800 毫米','50 公分'))))
        for status in ('門曾經上鎖，現在已解鎖','如果上鎖就無法打開','門已上鎖，但鑰匙就在手上',
                       '這扇門現在無法打開，但旁邊另有入口'):
            q = base.replace('門前後空曠',status)
            self.assertIsNone(proof_format(q))
            self.assertNotIn('阻礙原因',opening_reference(dict(current_request=q)) or {})

    def test_daily_answer_persists_model_disagreement_and_retries_without_duplicating_turn(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from threading import Event
        from assistant.engine import EngineError
        from assistant.store import Store
        from scripts.daily_delivery import DailyLocalEngine
        from scripts.daily_expenses import DailyAssistant

        class Raw:
            display_name = 'fixture'
            def describe(self):
                return {'kind': 'fixture'}
            def answer(self, prompt, cancel=None):
                if cancel is not None:
                    cancel.set()
                return '不可行'

        q = ('搬運員要把一個硬質正方體箱子送進展廳。箱子每邊 400 毫米，不可拆卸。'
             '唯一的門高 2 公尺、寬度不詳，兩側空曠平坦且無其他障礙。能否搬入？只回「可行」、「不可行」或「資料不足」。')
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, DailyLocalEngine(Raw()))
            with self.assertRaises(EngineError):
                app.ask(sid, q, cancel=Event())
            reply = app.retry(sid)
            self.assertEqual((reply['content'],len(store.messages(sid,10))), ('資料不足',2))
            saved = store.answer_context(reply['id'])
            self.assertEqual(saved['clearance_check']['raw_answer'], '不可行')
            self.assertFalse(saved['clearance_check']['raw_exact'])
            self.assertEqual(saved['door_measurements']['假設示例非實測'], ['200','800'])
            width = '門的淨寬量到 450 毫米。現在能通過嗎？只回「可行」、「不可行」或「資料不足」。'
            with self.assertRaises(EngineError):
                app.ask(sid, width, cancel=Event())
            measured = app.retry(sid)
            self.assertEqual((measured['content'], measured['clearance_check']['schema']), ('可行','door_measured_passage_v1'))
            self.assertEqual(store.answer_context(measured['id'])['clearance_source']['answer_id'], reply['id'])
            self.assertEqual(len(store.messages(sid,10)), 4)
            self.assertNotIn('clearance_check', app.ask(store.new_session(), width))
            equal_sid = store.new_session()
            app.ask(equal_sid, q)
            self.assertNotIn('clearance_check', app.ask(equal_sid, width.replace('450','400')))
            untreated = DailyAssistant(store,DailyLocalEngine(Raw()),allow_local_calculation=False)
            self.assertNotIn('clearance_check', untreated.ask(store.new_session(), q))

    def test_incomplete_changed_or_ambiguous_input_skips_reference(self):
        for q in (self.question.replace('正方體','長方體'), self.question.replace('1.9 公尺','20 公分'),
                  self.question.replace('0.62','大約 0.62'), self.question.replace('唯一的門','其中一扇門'),
                  self.question.replace('門前後空曠','另有一道門淨寬 1 公尺')):
            self.assertIsNone(opening_reference(dict(current_request=q)))
        for ctx in ({}, self.previous('換個話題'), dict(self.previous(), conversation_tail={'message_id':9}),
                    dict(self.previous(), documents={'catalog':[1]})):
            self.assertIsNone(opening_reference(dict(current_request='門的淨寬量到 590 毫米。', **ctx)))
        for q in ('門框外寬量到 590 毫米。','門的淨寬大約 590 毫米。','朋友的門淨寬 590 毫米。'):
            self.assertIsNone(opening_reference(dict(current_request=q, **self.previous())))
