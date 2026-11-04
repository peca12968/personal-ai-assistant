import unittest

from scripts.daily_conditions import capacity_trial


class ConditionFocusTests(unittest.TestCase):
    def focus(self, text, **context):
        q = text + '小林能參加嗎？只回「能」、「不能」或「資料不足」。'
        return capacity_trial(dict(current_request=q, **context))

    def test_the_same_unknown_is_varied_while_the_known_status_is_kept(self):
        for status in ('活動已取消', '活動照常舉行'):
            focus = self.focus(status+'。小林不知道預約名額還剩幾個。')
            self.assertEqual(focus['兩種情況都保留的原文'], [status])
            self.assertEqual(focus['對照假設'], ['至少還有一個預約名額','沒有剩餘預約名額'])
            self.assertEqual(focus['待查原文'], '小林不知道預約名額還剩幾個')

    def test_does_not_invent_an_alternative_for_ambiguous_or_quoted_requests(self):
        for text in ('上午可能停辦。小林不知道名額還剩幾個。', '甲機是否有電還沒確認。乙機是否有電還沒確認。',
                     '今天照常。沒查到登記費。', '翻譯「活動照常」。小林不知道名額還剩幾個。'):
            self.assertIsNone(self.focus(text))
        self.assertIsNone(self.focus('活動照常。小林不知道名額還剩幾個。',documents={'catalog':[1]}))

    def test_unrelated_state_and_unresolved_context_do_not_take_the_capacity_route(self):
        self.assertIsNone(self.focus('甲機是否有電尚未確認；乙機已可直接使用。'))
        source = '活動已取消。小林不知道名額還剩幾個。'
        self.assertIsNone(self.focus(source, memories=['saved']))
        self.assertIsNone(self.focus(source, conversation_tail={'message_id': 8}))
        focus = self.focus('展館今日休館。小林不知道主展還剩幾張票。')
        self.assertEqual(focus['對照假設'], ['主展至少還有一張票','主展沒有剩餘票'])

    def test_branches_require_two_ordered_labels_and_no_unparsed_caveats(self):
        from scripts.daily_conditions import branch_labels
        self.assertEqual(branch_labels('一：能。  \n二：不能'), ['能','不能'])
        for raw in ('能', '二：能\n一：不能', '一：能\n二：不能，除非取消', '一：能\n一：不能'):
            self.assertIsNone(branch_labels(raw))

    def test_daily_entry_keeps_the_trial_separate_from_actual_user_history(self):
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
                return {'kind':'fixture'}
            def answer(self,prompt,cancel=None):
                if cancel:
                    cancel.set()
                return '一：不能\n二：不能'

        q = '今天活動已取消。小林不知道名額還剩幾個。小林今天能參加嗎？只回「能」、「不能」或「資料不足」。'
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            engine = Raw()
            app = DailyAssistant(store,DailyLocalEngine(engine))
            sid = store.new_session()
            with self.assertRaises(EngineError):
                app.ask(sid,q,cancel=Event())
            reply = app.retry(sid)
            self.assertEqual(reply['content'], '不能')
            saved = store.answer_context(reply['id'])
            self.assertEqual(saved['capacity_check']['branches'],['不能','不能'])
            self.assertEqual([m['content'] for m in store.messages(sid)], [q,'不能'])
            self.assertNotIn('capacity_check', app.ask(sid,q))
            drafts=iter(['兩種情況我沒有分開說明','資料不足。'])
            engine.answer=lambda *a,**kw:next(drafts)
            reply=app.ask(store.new_session(),q)
            self.assertEqual((reply['content'],reply['capacity_check']['fallback_answer']),('資料不足','資料不足。'))
