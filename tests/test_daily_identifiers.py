from pathlib import Path
import tempfile
import unittest

from assistant.store import Store
from scripts.daily_expenses import DailyAssistant
from scripts.daily_identifiers import missing_literal, request_focus, supplied_code


class DailyIdentifierTests(unittest.TestCase):
    def test_target_is_the_requested_pair_not_the_first_device(self):
        q = '澄河機型號 T3。松山機的標籤不在。請問松山機的型號是什麼？'
        focus = request_focus(q)
        self.assertEqual((focus['target'], focus['field']), ('松山機', '型號'))
        self.assertNotIn('T3', str(focus))

    def test_missing_literal_requires_a_conclusion_about_the_requested_object(self):
        q = '我想查樓下主機的序號。若無法得知，只回覆「資訊不足」。'
        raw = '樓下主機的序號無法得知。因此，我只能回答「資訊不足」。'
        self.assertEqual(missing_literal(q, raw), '資訊不足')
        for unsafe in (raw.replace('樓下', '二樓'), raw.replace('因此，我只能', '若無法得知，我只能'),
                       raw + '後來已確認是 T82。', '樓下主機的序號是 T82。'):
            self.assertIsNone(missing_literal(q, unsafe))

    def test_untrusted_examples_revisions_and_ambiguous_pairs_are_skipped(self):
        for q in ('轉述這句：我要的是白盒的編號。', '「請問白盒的編號？」',
                  '我要的是兩者的代碼。', '我要的是白盒或灰盒的編號。',
                  '我要的是白盒的型號。更正，改問黑盒的價格。',
                  '請查灰盒的序號。請查白盒的代碼。', '有這樣的例子嗎？'):
            with self.subTest(q=q):
                self.assertIsNone(request_focus(q))

    def test_exact_render_requires_agreeing_user_and_model_values(self):
        q = '銀色電錶的型號是 Z-805。我要的是銀色電錶的型號。只給型號。'
        draft = '銀色電錶的型號是 **Z-805**。'
        ctx = {'current_request': q}
        self.assertEqual(supplied_code(ctx, draft), 'Z-805')
        for variant in (draft.replace('型號', '序號'),draft.replace('銀色', '黑色'), draft + '另一款是 Z-804。',
                        draft + '這個值仍待確認。',draft.replace('Z-805','Z-508')):
            self.assertIsNone(supplied_code(ctx, variant))
        for variant in (q.replace('銀色電錶的型號是', '黑色電錶的型號是'),
                        q.replace('只給型號', '請解釋'), q.replace('Z-805。', 'Z-805。登記值已作廢。'),
                        q.replace('銀色電錶的型號是 Z-805。', '引文「內容。銀色電錶的型號是 Z-805。」。')):
            self.assertIsNone(supplied_code({'current_request': variant}, draft))

    def test_packaging_reply_must_match_immediately_preceding_target_and_field(self):
        ctx = {'current_request':'我在淡紫盒的包裝上讀到 F97。只給序號。',
               'conversation_tail':{'message_id':22}, 'recent_conversation':[
                   {'role':'user','message_id':21,'content':'我要的是淡紫盒的序號。','truncated':False},
                   {'role':'assistant','message_id':22,'content':'請提供淡紫盒包裝上的序號。','truncated':False}]}
        draft = '淡紫盒的序號為 F97。'
        self.assertEqual(supplied_code(ctx,draft), 'F97')
        for change in ({'conversation_tail':{'message_id':23}}, {'recent_conversation':[]},
                       {'current_request':ctx['current_request'].replace('淡紫盒','淺灰盒')},
                       {'current_request':ctx['current_request'].replace('序號','編號')}):
            self.assertIsNone(supplied_code({**ctx, **change}, draft))
        ctx['recent_conversation'][-1]['truncated'] = True
        self.assertIsNone(supplied_code(ctx, draft))

    def test_focus_is_saved_only_on_the_relevant_daily_model_turn(self):
        class Raw:
            def answer(self, prompt, cancel=None):
                self.prompt = prompt
                return '請核對目標物件的標籤。'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3'); raw = Raw()
            sid = store.new_session(); daily = DailyAssistant(store, raw)
            q = '我要的是南倉讀卡機的序號。'
            result = daily.ask(sid, q)
            self.assertIn('identifier_request', raw.prompt)
            saved = store.answer_context(result['id'])
            self.assertEqual(saved['identifier_request']['target'], '南倉讀卡機')
            self.assertEqual(saved['current_request'], q)
            self.assertNotIn('identifier_request', daily.prepare_context(sid, '改談別的。'))
            pure = DailyAssistant(store, raw, allow_local_calculation=False)
            self.assertNotIn('identifier_request', pure.prepare_context(sid, q))
