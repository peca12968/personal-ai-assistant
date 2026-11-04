import unittest

from scripts import daily_dialogue as dialogue


class DailyDialogueTests(unittest.TestCase):
    def setUp(self):
        self.previous = [dict(role='user', content='展區有白廳和青廳。', truncated=False)]

    def context(self, q):
        return dict(current_request=q, recent_conversation=self.previous)

    def test_revision_repeats_only_the_current_user_statements(self):
        q = '更正，紅色已售完；藍色剛到貨。金額上限照舊。哪個選項適用？只回名稱。'
        self.assertEqual(dialogue.revision_focus(self.context(q))['本輪使用者更新'],
                         ['更正，紅色已售完；藍色剛到貨', '金額上限照舊'])
        for ctx in (self.context('請翻譯：' + q), self.context('「' + q + '」'),
                    dict(current_request=q, recent_conversation=[]),
                    dict(self.context(q), documents={'catalog':[1]})):
            self.assertIsNone(dialogue.revision_focus(ctx))

    def test_named_topic_recovers_user_facts_and_subsequent_corrections(self):
        q = '回到剛才借書的事。改由妹妹辦理，她沒有帶證。'
        history = []
        for question in ('借書需要學生證。','更新，訪客也能用臨時證。','換個話題。今天天氣如何？'):
            history.append(dict(role='user', message_id=len(history)+1, truncated=False, content=question))
            history.append(dict(role='assistant', message_id=len(history)+1, truncated=False, content='猜測：不必帶證。'))
        ctx = dict(current_request=q, recent_conversation=history, conversation_tail=dict(message_id=6))
        focus = dialogue.resumed_topic(ctx)
        self.assertEqual(focus['話題'], '借書')
        self.assertEqual(focus['先前使用者原話'], [dict(訊息=r['message_id'], 原文=r['content']) for r in history[:4:2]])
        self.assertEqual(focus['本輪接續'], '改由妹妹辦理，她沒有帶證。')
        for change in (dict(current_request='請翻譯「'+q+'」'), dict(conversation_tail=dict(message_id=8)),
                       dict(documents={'catalog':[1]}), dict(current_request=q.replace('借書','旅遊'))):
            self.assertIsNone(dialogue.resumed_topic({**ctx, **change}))
        history[0]['truncated'] = True
        self.assertIsNone(dialogue.resumed_topic(ctx))
        history[0]['truncated'] = False
        history[4]['content'] += '今天要借書嗎？'
        self.assertIsNone(dialogue.resumed_topic(ctx))

    def test_short_names_need_a_user_source_and_a_standalone_conclusion(self):
        q = '請選一處。只回展區名稱。'
        ctx = self.context(q)
        self.assertEqual(dialogue.short_label(ctx,'青廳。'), '青廳')
        self.assertEqual(dialogue.short_label(ctx,q+'\n\n青廳！'), '青廳')
        self.assertEqual(dialogue.short_label(ctx,'白廳已關閉。因此，現在應選青廳。'), '青廳')
        self.assertEqual(dialogue.short_label(self.context('換個話題。'+q),q+'\n青廳。'), '青廳')
        for answer in ('如果今天沒有休館。因此，應選青廳。', '所以，應選白廳或青廳。', '若能訂票就選青廳。','西廳。','白廳或青廳。','青廳。還要核對現場。'):
            self.assertIsNone(dialogue.short_label(ctx,answer))
        self.assertIsNone(dialogue.short_label(self.context('請先解釋。只回展區名稱。'),'青廳。'))
        self.assertIsNone(dialogue.short_label(dict(ctx,recent_conversation=[dict(self.previous[0],truncated=True)]),'青廳。'))

    def test_feasibility_sources_preserve_explicit_capability_and_pending_status(self):
        fact = '東梯能載這個機台，但今天是否開放還未查到'
        ctx = self.context(fact+'。能否運送？')
        focus = dialogue.feasibility_evidence(ctx)
        self.assertNotIn('通行能力的原話', focus)
        self.assertIn('未開放', focus['情況核對'])
        supported = self.context('坡道可讓這個機台直接通過。能否運送？')
        self.assertEqual(dialogue.feasibility_evidence(supported)['通行能力的原話'], ['坡道可讓這個機台直接通過'])
        self.assertEqual(focus['尚未核實的原話'], [fact])
        for q in ('請翻譯：'+ctx['current_request'], '先前有人說過能通過。現在幾點？',
                  '假設通道能讓車通過。能否運送？', '這台設備不一定能載貨。能否運送？',
                  '這台設備不能載貨。能否運送？'):
            self.assertIsNone(dialogue.feasibility_evidence(self.context(q)))
        self.assertIsNone(dialogue.feasibility_evidence(dict(ctx,documents={'catalog':[1]})))
        self.assertIsNone(dialogue.feasibility_evidence(self.context('能否通過？')))

    def test_only_a_complete_requested_choice_can_be_shortened(self):
        ctx = self.context('現在能決定嗎？只回答「通過」、「拒絕」或「待補件」。')
        self.assertEqual(dialogue.short_label(ctx, '待補件。'), '待補件')
        for draft in ('待查。', '原則上通過。', '通過或拒絕。'):
            self.assertIsNone(dialogue.short_label(ctx,draft))

    def test_choice_format_preserves_the_model_decision(self):
        ctx=self.context('兩人同意了嗎？只回「同意」或「不同意」。')
        self.assertEqual(dialogue.short_label(ctx,'不同意。'), '不同意')
        self.assertIsNone(dialogue.short_label(ctx,'尚未確認是否同意。'))
        self.assertIsNone(dialogue.short_label(ctx,'反對。'))
