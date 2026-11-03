import copy
import json
import tempfile
import unittest
from pathlib import Path
from assistant.context import context_reset,followup_query,_instruction_text
from assistant.engine import Assistant
from assistant.store import Store


class NeverCalled:
    def answer(self,*args,**kwargs):raise AssertionError('Context inspection must not call a model')


class QuoteContinuityTests(unittest.TestCase):
    def test_all_reviewed_followup_and_real_reset_cases(self):
        cases=json.loads((Path(__file__).resolve().parents[1]/'evals/quote_continuity_revisions.json').read_text('utf-8'))
        self.assertEqual(len(cases),60)
        for case in cases:
            with self.subTest(id=case['id']):
                self.assertEqual(followup_query(case['question'],case['history']),case['expect'])
                self.assertEqual(context_reset(case['question']),case['expect'] is None)

    def test_adjacent_quote_mask_preserves_shape_and_external_reset(self):
        questions=[
            '接著把‘換個話題’放在文末。\n保留原報價。',
            'Then append ‘change the subject’Now continue the quote.',
            'Then append ‘the user’s words: change the subject’Next to the total.',
            "Then append 'change the subject'Next to the total."]
        for question in questions:
            with self.subTest(question=question):
                masked=_instruction_text(question)
                self.assertEqual(len(masked),len(question))
                self.assertEqual([i for i,c in enumerate(masked) if c=='\n'],[i for i,c in enumerate(question) if c=='\n'])
                self.assertNotIn('換個話題',masked);self.assertNotIn('change the subject',masked)
                self.assertFalse(context_reset(question))
                extended=question+'\n換個話題，談音樂。'
                self.assertTrue(context_reset(extended));self.assertIn('換個話題',_instruction_text(extended))

    def test_real_context_keeps_old_quote_corrections_feedback_and_current_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session();other=store.new_session()
            opening=store.add_message(sid,'user','包裝報價：7份，每份90元，運費30元。')
            answer=store.add_message(sid,'assistant','先前總額660元。')
            correction='更正：改為9份、每份125元、運費40元，以這份為準。'
            corrected=store.add_message(sid,'user',correction)
            store.rate(answer,-1,'數量改9份、單價125元、運費40元；不是660元。')
            store.add_message(other,'user','更正：外部對話秘密，不應帶入。')
            for i in range(12):
                store.add_message(sid,'user',f'背景討論{i}')
                store.add_message(sid,'assistant','收到背景。')
            app=Assistant(store,NeverCalled());question='接著把‘換個話題’放在末尾；這次只將數量改為10份，其餘依更正。'
            before=copy.deepcopy(store.messages(sid,100))
            context=app.prepare_context(sid,question)
            self.assertIn(opening,[r['message_id'] for r in context['earlier_user_excerpts']])
            self.assertEqual(context['earlier_user_corrections']['excerpts'],[{'message_id':corrected,'role':'user','content':correction,'truncated':False}])
            self.assertEqual(context['answer_feedback']['items'][0]['answer_message_id'],answer)
            self.assertEqual(context['current_request'],question)
            self.assertNotIn('外部對話秘密',json.dumps(context,ensure_ascii=False))
            reset=app.prepare_context(sid,question+'\n換個話題，只算3乘4。')
            self.assertEqual(reset['earlier_user_excerpts'],[])
            self.assertEqual(reset['earlier_user_corrections']['excerpts'],[])
            self.assertEqual(reset['answer_feedback']['items'],[])
            self.assertEqual(store.messages(sid,100),before)
            self.assertEqual(store.retrieve('包裝'),[])


if __name__=='__main__':unittest.main()
