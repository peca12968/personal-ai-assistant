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

    def test_choice_format_preserves_the_model_decision(self):
        ctx=self.context('兩人同意了嗎？只回「同意」或「不同意」。')
        self.assertEqual(dialogue.short_label(ctx,'不同意。'), '不同意')
        self.assertIsNone(dialogue.short_label(ctx,'尚未確認是否同意。'))
        self.assertIsNone(dialogue.short_label(ctx,'反對。'))
