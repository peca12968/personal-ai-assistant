import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from assistant.__main__ import main as assistant_main
from assistant.engine import Assistant, EngineError, render_prompt
from assistant.store import Store
from scripts import daily_chat, daily_meeting, daily_necessary


class DefaultPrerequisiteChatTests(unittest.TestCase):
    INITIAL = ('啟用量測台的必要且充分條件是同時具備有效的操作證和（可用的北機或可用的南機）。'
               '操作證尚未確認，北機尚未檢查，南機尚未檢查。'
               '確定能啟用回「啟用」，確定不能啟用回「停用」，資料不足回「待查」。請只輸出結論。')
    UPDATE = '沿用上一輪條件：操作證已確認有效。重算。'
    ORDER = '沿用上一輪條件：檢查時間：操作證4分鐘，北機2分鐘，南機7分鐘。先查哪一項最省時間？'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer', 'answer_images'])
        self.engine.answer.return_value = '一般文字回答'
        self.engine.answer_images.return_value = '圖片回答'

    def run_cli(self, *args, entry=daily_chat.main):
        output = io.StringIO()
        with patch('sys.argv', ['assistant', '--data-dir', str(self.path), *args]), \
                patch('assistant.__main__.CodexEngine', return_value=self.engine), \
                patch.object(daily_chat, 'CodexEngine', return_value=self.engine), redirect_stdout(output):
            entry()
        return output.getvalue()

    def test_default_and_explicit_codex_continue_the_same_local_evidence_chain(self):
        sid = self.store.new_session()
        self.assertIn('待查', self.run_cli('--ask', self.INITIAL, '--session', sid))
        first = self.store.messages(sid, 1)[0]
        self.assertIn('先查「操作證」', self.run_cli('--ask', self.ORDER, '--session', sid))
        order = self.store.messages(sid, 1)[0]
        plan = daily_necessary.read(self.store, order['id'])
        self.assertEqual(plan['plan']['source']['answer_id'], first['id'])
        self.assertEqual(plan['check_order']['average_minutes'], [27, 4])
        self.assertIn('待查', self.run_cli('--engine', 'codex', '--ask', self.UPDATE, '--session', sid))
        corrected = self.store.messages(sid, 1)[0]
        self.assertEqual(daily_necessary.read(self.store, corrected['id'])['plan']['source']['answer_id'], order['id'])
        self.engine.answer.assert_not_called()
        self.engine.answer_images.assert_not_called()

    def test_ordinary_and_isolated_prompts_keep_core_assistant_context(self):
        for question in ('幫我把下週的討論邀請改得簡短禮貌。',
                         '還要核對什麼？', '只根據這則訊息回答。幫我把討論邀請寫短一點。'):
            with self.subTest(question=question):
                sid = self.store.new_session()
                self.store.add_message(sid, 'user', '我們在談設計討論。')
                expected = render_prompt(Assistant(self.store, self.engine).prepare_context(sid, question))
                self.assertIn('一般文字回答', self.run_cli('--ask', question, '--session', sid))
                self.assertEqual(self.engine.answer.call_args.args[0], expected)
                answer = self.store.messages(sid, 1)[0]
                self.assertIsNone(daily_necessary.read(self.store, answer['id']))
        self.assertEqual(self.engine.answer.call_count, 3)

    def test_attached_document_uses_standard_codex_input_even_on_condition_followup(self):
        sid = self.store.new_session()
        self.run_cli('--ask', self.INITIAL, '--session', sid)
        document = self.path / 'evidence.txt'
        document.write_text('測試文件中的當班狀態。', encoding='utf-8')
        self.assertIn('一般文字回答', self.run_cli('--ask', self.UPDATE, '--session', sid, '--file', str(document)))
        self.assertIn('一般文字回答', self.run_cli('--ask', self.UPDATE, '--session', sid))
        self.assertIn('測試文件中的當班狀態。', self.engine.answer.call_args.args[0])
        answer = self.store.messages(sid, 1)[0]
        self.assertIsNone(daily_necessary.read(self.store, answer['id']))

    def test_image_followup_retries_with_the_saved_image_and_never_creates_local_evidence(self):
        sid = self.store.new_session()
        self.run_cli('--ask', self.INITIAL, '--session', sid)
        image = self.path / 'sample.png'
        Image.new('RGB', (10, 10), 'green').save(image)
        self.engine.answer_images.side_effect = [EngineError('停止', 'cancelled'), '圖片回答']
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli('--ask', self.UPDATE, '--session', sid, '--image', str(image))
        self.assertIn('圖片回答', self.run_cli('--retry-answer', '--session', sid))
        attempts = self.engine.answer_images.call_args_list
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].args[1], attempts[1].args[1])
        answer = self.store.messages(sid, 1)[0]
        self.assertIsNone(daily_necessary.read(self.store, answer['id']))
        self.assertEqual(len(self.store.messages(sid, 10)), 4)
        self.engine.answer.assert_not_called()

    def test_default_desktop_and_non_chat_commands_keep_distinct_paths(self):
        with patch.object(daily_chat, 'run_daily_window') as run:
            self.run_cli()
            self.assertEqual(run.call_args.kwargs, {'default_chat': True})
            self.assertIs(run.call_args.args[1], self.engine)
            self.assertIn('revision', self.run_cli('--show-companion'))
            run.assert_called_once()
        self.engine.answer.assert_not_called()

    def test_direct_core_entrypoint_keeps_existing_model_path(self):
        sid = self.store.new_session()
        self.assertIn('一般文字回答', self.run_cli('--ask', self.INITIAL, '--session', sid, entry=assistant_main))
        answer = self.store.messages(sid, 1)[0]
        self.assertIsNone(daily_necessary.read(self.store, answer['id']))
        self.engine.answer.assert_called_once()

    def test_default_meeting_uses_saved_same_day_slots_and_keeps_model_fallback(self):
        sid = self.store.new_session()
        question = ('安禾可談 11:20-12:40 和 14:00-14:50，佳瑜可談 11:40-12:30 或 14:20-15:00。'
                    '安禾的上午時段已取消。兩人要談 20 分鐘。只輸出最早可行時段，格式 HH:MM-HH:MM。')
        followup = '上一場會議，佳瑜取消 12:00-12:15，重算。'
        self.assertIn('12:00-12:20', self.run_cli('--ask', question, '--session', sid))
        first = self.store.messages(sid, 1)[0]
        self.assertIn('14:20-14:40', self.run_cli('--ask', followup, '--session', sid))
        latest = self.store.messages(sid, 1)[0]
        self.assertEqual(daily_meeting.read(self.store, latest['id'])['meeting_source']['answer_id'], first['id'])
        self.engine.answer.assert_not_called()
        self.assertIn('一般文字回答', self.run_cli('--ask', followup, '--session', self.store.new_session()))
        document = self.path / 'calendar.txt'
        document.write_text('額外時段限制。', encoding='utf-8')
        self.assertIn('一般文字回答', self.run_cli('--ask', followup, '--session', sid, '--file', str(document)))
        self.assertEqual(self.engine.answer.call_count, 2)
