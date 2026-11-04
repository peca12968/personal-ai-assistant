import tempfile
import unittest
from pathlib import Path

from assistant.store import Store
from scripts import daily_stock as stock
from scripts.daily_expenses import DailyAssistant

BASE = '紙箱原有 27 支蠟筆。發出了 6 支，之後買進 4 支。現在箱裡有幾支？只回數字。'
EDIT = '更正，實際發出的是 2 支，買進數量照舊。現在剩幾支？只給數字。'
UNKNOWN = '也許另有 0 支被領走，但尚未證實。能確定唯一的剩餘數量嗎？只回答「能」或「不能」。'


def history(*questions):
    rows = []
    for question in questions:
        rows.append(dict(role='user', content=question, message_id=len(rows)+1, truncated=False))
        rows.append(dict(role='assistant', content='我猜總數是 900。', message_id=len(rows)+1, truncated=False))
    return dict(recent_conversation=rows, conversation_tail={'message_id':len(rows)})


class StockContextTests(unittest.TestCase):
    def test_reconstruct_only_user_events_and_preserve_zero_hypothesis(self):
        seed = stock.context_hint(dict(current_request=BASE))
        self.assertEqual(seed['確認異動後的算式'], '27 - 6 + 4')
        revised = stock.context_hint(dict(current_request=EDIT, **history(BASE)))
        self.assertEqual(revised['確認異動後的算式'], '27 - 2 + 4')
        fork = stock.context_hint(dict(current_request=UNKNOWN, **history(BASE,EDIT)))
        self.assertEqual(fork['事件未發生'], '27 - 2 + 4')
        self.assertEqual(fork['事件有發生'], '(27 - 2 + 4) + (0)')
        self.assertEqual(fork['來源訊息編號'], [1,3])

    def test_uncertain_input_quotes_or_different_unit_not_treated_as_confirmed(self):
        for q in (BASE.replace('發出了', '可能發出了'), BASE.replace('6 支', '6 盒'),
                  '翻譯「' + BASE + '」', BASE.replace('箱裡有', '店裡有'),
                  BASE.replace('買進 4 支', '買進 4 支或 7 支')):
            self.assertIsNone(stock.context_hint(dict(current_request=q)))
        for ctx in (history(BASE, '換個話題'), {**history(BASE), 'conversation_tail':{'message_id':9}},
                    {'documents':{'catalog':[1]}, **history(BASE)}, {}):
            self.assertIsNone(stock.context_hint(dict(current_request=EDIT, **ctx)))
        source = history(BASE)
        source['recent_conversation'][0]['truncated'] = True
        self.assertIsNone(stock.context_hint(dict(current_request=EDIT, **source)))

    def test_correction_targets_event_kind_not_merely_its_sign(self):
        self.assertIsNone(stock.context_hint(dict(current_request=EDIT.replace('發出的','售出的'), **history(BASE))))
        self.assertIsNone(stock.context_hint(dict(current_request=EDIT.replace('買進數量','借入數量'), **history(BASE))))
        duplicate = BASE.replace('發出了 6 支，','發出了 6 支，再發出了 1 支，')
        self.assertIsNone(stock.context_hint(dict(current_request=EDIT, **history(duplicate))))
        self.assertIsNone(stock.context_hint(dict(current_request=BASE.replace('6 支','36 支'))))
        self.assertIsNone(stock.context_hint(dict(current_request=UNKNOWN.replace('0 支','75 支'), **history(BASE))))
        other_unit = UNKNOWN.replace('能確定唯一的剩餘數量嗎', '現在的盒數能唯一確定嗎')
        self.assertIsNone(stock.context_hint(dict(current_request=other_unit, **history(BASE))))

    def test_render_needs_a_matching_model_conclusion(self):
        ctx = dict(current_request=BASE)
        ctx['inventory_reasoning'] = stock.context_hint(ctx)
        self.assertEqual(stock.concise(ctx, '27 - 6 + 4 = 25\n\n現在箱裡有 25 支。'), '25')
        self.assertEqual(stock.concise(ctx,'27'), '本機核對：27 - 6 + 4 = 25 支。模型原答為 27 支。')
        for draft in ('現在店裡有 25 支。', '現在箱裡有 25 盒。', '如果確認，現在箱裡有 25 支。'):
            self.assertIsNone(stock.concise(ctx,draft))
        ctx = dict(current_request=UNKNOWN, **history(BASE))
        ctx['inventory_reasoning'] = stock.context_hint(ctx)
        self.assertEqual(stock.concise(ctx, '能。'), '能')
        self.assertIn('答案是「能」', stock.concise(ctx, '不能。'))

    def test_different_orders_and_quantities_agree_with_independent_counts(self):
        import random
        rng = random.Random(74)
        for i in range(80):
            initial = rng.randint(12, 99)
            incoming = rng.randint(0, 15)
            outgoing = rng.randint(0, initial)
            actions = [f'借入了 {incoming} 件', f'借出了 {outgoing} 件']
            if i % 2:
                actions.reverse()
            q = f'工作室原有 {initial} 件道具。' + '，'.join(actions) + '。現在工作室有幾件？只給數字。'
            ctx = dict(current_request=q)
            ctx['inventory_reasoning'] = stock.context_hint(ctx)
            wanted = initial + incoming - outgoing
            self.assertEqual(stock.concise(ctx, f'工作室有 {wanted} 件。'), str(wanted))
            self.assertIn(f'= {wanted} 件', stock.concise(ctx, f'工作室有 {wanted+1} 件。'))

    def test_daily_delivery_checks_the_number_after_extracting_a_well_formed_equation(self):
        from scripts.daily_delivery import DailyLocalEngine
        class Raw:
            display_name = 'fixture'
            def describe(self):
                return {'kind':'fixture'}
            def answer(self,prompt,cancel=None):
                return '27 + 6 + 4 = 37'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            result = DailyAssistant(store,DailyLocalEngine(Raw())).ask(store.new_session(),BASE)
            self.assertEqual(result['content'], '本機核對：27 - 6 + 4 = 25 支。模型原答為 37 支。')
            self.assertEqual(store.answer_context(result['id'])['inventory_reasoning']['確認異動後的算式'],
                             '27 - 6 + 4')

    def test_audit_is_saved_in_daily_context_and_skipped_for_unassisted_evaluation(self):
        class Raw:
            def answer(self,prompt,cancel=None):
                return '測試回答'
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3');sid=store.new_session()
            app=DailyAssistant(store,Raw())
            reply=app.ask(sid,BASE)
            self.assertEqual(store.answer_context(reply['id'])['inventory_reasoning']['觀察位置'],'紙箱')
            self.assertNotIn('inventory_reasoning',DailyAssistant(store,Raw(),allow_local_calculation=False).prepare_context(sid,EDIT))
