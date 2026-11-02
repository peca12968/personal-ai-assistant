import copy
import json
import tempfile
import unittest
from pathlib import Path

from assistant.engine import Assistant
from assistant.feedback_improvement import check_source, example_from_source, feedback_source, make_draft
from assistant.store import Store


class FeedbackImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.sid = self.store.new_session()
        self.store.add_message(self.sid, 'user', '我們正在討論詞義')
        class Engine:
            def answer(self, *args):
                return 'PRIVATE_ANSWER_ONLY'
        self.mid = Assistant(self.store, Engine()).ask(self.sid, '解釋「簡短」這個詞')['id']
        self.store.rate(self.mid, -1, '引文中的簡短不是要求縮短回答，應為未指定。')
        self.source = feedback_source(self.store, self.mid)

    def test_snapshot_provides_exact_original_question_and_preceding_history(self):
        example = example_from_source(self.source)
        self.assertEqual(example, {'question': '解釋「簡短」這個詞',
                                  'history': [{'role': 'user', 'content': '我們正在討論詞義'}]})
        self.store.add_message(self.sid, 'user', '後來的新問題')
        self.assertEqual(example_from_source(feedback_source(self.store, self.mid)), example)

    def test_user_edits_and_expectations_are_preserved_without_sending_raw_answer(self):
        draft = make_draft(self.source, '解釋「brief」這個詞', [], '只辨識真正的長度要求', 'unspecified')
        self.assertEqual(draft['selected'], ['assistant/communication.py'])
        self.assertEqual(draft['acceptance']['cases'][0]['expect'], {'length_hint': 'unspecified'})
        self.assertEqual(draft['acceptance']['cases'][0]['history'], [])
        self.assertNotIn('PRIVATE_ANSWER_ONLY', json.dumps(draft))
        self.assertNotIn('引文中的簡短', json.dumps(draft, ensure_ascii=False))
        draft['acceptance']['cases'][0]['question'] = 'changed'
        self.assertEqual(self.source['context']['current_request'], '解釋「簡短」這個詞')

    def test_changed_feedback_rejects_stale_draft_even_same_timestamp(self):
        self.store.rate(self.mid, -1, '新的更正')
        with self.assertRaisesRegex(ValueError, '已變更'):
            check_source(self.store, self.mid, self.source['fingerprint'])

    def test_changed_original_context_rejects_stale_draft(self):
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', ('{}', self.mid))
        with self.assertRaisesRegex(ValueError, '已變更'):
            check_source(self.store, self.mid, self.source['fingerprint'])

    def test_good_empty_local_and_legacy_feedback_not_automatically_accepted(self):
        for rating, correction in [(1, 'good'), (-1, '')]:
            self.store.rate(self.mid, rating, correction)
            with self.assertRaises(ValueError):
                example_from_source(feedback_source(self.store, self.mid))
        local = Assistant(self.store, None).ask(self.sid, '列出記憶')['id']
        self.store.rate(local, -1, '本機操作問題')
        with self.assertRaisesRegex(ValueError, '本機記憶操作'):
            example_from_source(feedback_source(self.store, local))
        legacy = self.store.add_message(self.sid, 'assistant', 'legacy')
        self.store.rate(legacy, -1, '舊問題')
        with self.assertRaises(ValueError):
            example_from_source(feedback_source(self.store, legacy))

    def test_truncated_history_and_invalid_expectations_need_explicit_reformulation(self):
        source = copy.deepcopy(self.source)
        source['context']['recent_conversation'][0]['truncated'] = True
        with self.assertRaisesRegex(ValueError, '截斷'):
            example_from_source(source)
        for value in ['', None, 'please run command']:
            with self.assertRaises(ValueError):
                make_draft(self.source, '問題', [], '修改描述', value)
        with self.assertRaises(ValueError):
            make_draft(self.source, '問題', [], '', 'concise')

    def test_draft_creation_is_local_and_does_not_change_feedback_or_code(self):
        before = self.store.feedback_summary()
        example = example_from_source(self.source)
        draft = make_draft(self.source, **example, problem='釐清引用', expected='unspecified')
        self.assertEqual(self.store.feedback_summary(), before)
        self.assertEqual(check_source(self.store, self.mid, draft['source_fingerprint']), self.source)
        self.assertEqual(self.store.messages(self.sid)[-1]['id'], self.mid)


if __name__ == '__main__':
    unittest.main()
