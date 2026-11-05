import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant, render_prompt
from assistant.evolution_status import improvement_context, status_request
from assistant.store import Store


class EvolutionStatusScopeTests(unittest.TestCase):
    def test_explicit_own_status_requests_keep_history_available(self):
        for question in ('你是不是改了程式？','妳剛剛改了程式嗎？',
                         '修改你的程式後有何進展？','你修改了自己的程式嗎？',
                         '本助理最近修改了自己的程式嗎？','自我改進',
                         '自我改進的狀態','self-improvement status'):
            with self.subTest(question=question):
                self.assertTrue(status_request(question))

    def test_code_work_and_quoted_data_do_not_open_private_history(self):
        questions=('請修正Python函式，說明修改原因，不要執行程式。',
                   '請你修改這段程式，保留0和空字串。','你幫我改了程式嗎？',
                   '請解釋「你改了程式」的文法。','請翻譯「自我改進」。',
                   '> 自我改進\n請翻譯上面文字。',
                   '```python\n# 自我改進\n```\n請解釋這段註解。',
                   '````python\n# 自我改進\n```\n未關閉區塊','「自我改進',
                   'x'*16001)
        with patch('assistant.evolution_status.Path',side_effect=AssertionError('unneeded private history read')):
            for question in questions:
                with self.subTest(question=question[:80]):
                    self.assertFalse(status_request(question))
                    self.assertEqual(improvement_context('unused',question),[])

    def test_real_assistant_preparation_keeps_code_and_removes_unrelated_history(self):
        question=('請修正這個Python函式：\ndef value_or_default(value, default):\n'
                  '    return value or default\n只有None才用default。解釋修改原因，不要執行程式。')
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'assistant.sqlite3');sid=store.new_session()
            with patch('assistant.evolution_status.Path',side_effect=AssertionError('unneeded private history read')):
                context=Assistant(store,None,allow_local_transcription=False).prepare_context(sid,question)
            self.assertEqual(context['current_request'],question)
            self.assertEqual(context['self_improvement'],[])
            marker='\n\n以下 JSON 是使用者資料與當前問題：\n'
            self.assertEqual(json.loads(render_prompt(context).rpartition(marker)[2]),context)


if __name__=='__main__':unittest.main()
