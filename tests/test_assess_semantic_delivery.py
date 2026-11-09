from pathlib import Path
import tempfile
import unittest

from assistant.store import Store
from scripts.learning import assess_semantic_delivery as study


class FullSemanticDeliveryTests(unittest.TestCase):
    def test_cases_keep_unseen_work_separate_and_leave_control_questions_unchanged(self):
        rows = study.cases()
        self.assertEqual(len(rows), 10)
        self.assertEqual([sum(c['group'] == group for c in rows) for group in ('fresh', 'historical', 'controls')],
                         [6, 2, 2])
        for case in rows:
            if case['mapping']:
                self.assertNotEqual(case['question'], case['semantic_question'])
                self.assertNotIn(case['answer'], case['semantic_question'])
            else:
                self.assertEqual(case['question'], case['semantic_question'])

    def test_actual_assistant_preserves_original_request_and_saved_context(self):
        case = next(c for c in study.cases() if c['id'] == 'reading_zone')
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'assistant.sqlite3')
            sid = store.new_session()
            for message in case['history']:
                store.add_message(sid, message['role'], message['content'])
            request = []
            description = {'research_adapter': True}

            def infer(prompt, cancel):
                request.append(prompt)
                return '待查', True, 100

            observer = study.Observed(dict(description=description), case, 'split', infer)
            app = study.assistant(store, observer)
            context = app.prepare_context(sid, case['question'])
            prompts = study.prompt_pair(context, case)
            observer.plan.update(contexts={case['id']: context},
                                 prompts={case['id']: {phase: study.digest(value) for phase, value in prompts.items()}})
            reply = app.ask(sid, case['question'])
            self.assertEqual(reply['content'], case['answer'])
            self.assertEqual(request, [prompts['split']])
            self.assertNotIn(case['answer'], request[0])
            self.assertEqual(store.messages(sid, 10)[-2]['content'], case['question'])
            self.assertEqual(store.answer_context(reply['id']), context)
            self.assertTrue(observer.record['mapped'])
            self.assertEqual(observer.record['raw'], '待查')

    def test_invalid_semantic_words_do_not_map_and_controls_must_match(self):
        case = next(c for c in study.cases() if c['mapping'])
        self.assertEqual(study.deliver(case, 'split', '應該待查', True), ('應該待查', False))
        self.assertEqual(study.deliver(case, 'split', '待查', False), ('待查', False))
        self.assertEqual(study.deliver(case, 'direct', '待查', True), ('待查', False))
        control = next(c for c in study.cases() if c['group'] == 'controls')
        direct = dict(id=control['id'], raw=control['answer'], ended=True, delivered=control['answer'],
                      input_tokens=100, wire_sha='same', mapped=False)
        with self.assertRaisesRegex(ValueError, '控制題'):
            study.score([control], {'direct': {'rows': [direct]},
                                    'split': {'rows': [dict(direct, raw='different')]}})
