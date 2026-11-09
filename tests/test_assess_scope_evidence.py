from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from assistant.store import Store
from scripts.learning import assess_scope_evidence as study


class ScopeEvidencePairTests(unittest.TestCase):
    def test_prompt_headings_preserve_every_record_and_pair_only_changes_one_current_line(self):
        cases = study.cases()
        for first, second in zip(cases[0:6:2], cases[1:6:2], strict=True):
            self.assertEqual(first['history'], second['history'])
            self.assertEqual(sum(a != b for a, b in zip(first['records'], second['records'], strict=True)), 1)
            self.assertNotEqual(first['answer'], second['answer'])
        for case in cases:
            if case['group'] == 'control':
                self.assertEqual(case['question'], case['semantic_question'])
                continue
            indexed = case['semantic_question']
            for heading in ('規則\n', '\n舊輪紀錄\n', '\n本輪觀察\n'):
                indexed = indexed.replace(heading, '', 1)
            self.assertEqual(indexed.replace('\n', ''), case['question'])
            self.assertIn('。'.join(case['records'][:3]), case['history'][0]['content'])

    def test_old_rule_and_other_object_citations_do_not_earn_grounding_credit(self):
        case = next(c for c in study.cases() if c['id'] == 'gate_open')

        def reply(a, b, ended=True):
            raw = json.dumps({'A': {'state': 'T', 'source': a}, 'B': {'state': 'T', 'source': b}})
            return study.grade(case, dict(raw=raw, ended=ended))

        old = reply(1, 3)
        self.assertTrue(old['correct'])
        self.assertFalse(old['current_only'])
        self.assertEqual((old['rule_citations'], old['prior_citations'], old['exact_facts']), (1, 1, 0))
        other = reply(4, 6)
        self.assertTrue(other['correct'])
        self.assertTrue(other['current_only'])
        self.assertEqual(other['exact_facts'], 1)
        self.assertEqual(reply(5, 6)['exact_facts'], 2)
        for a, b, ended in ((True, 6, True), (5, 7, True), (5, 6, False)):
            self.assertFalse(reply(a, b, ended)['valid'])

    def test_paired_score_requires_both_answers_and_preserves_same_prompt_control(self):
        first, second = study.cases()[:2]
        control = study.cases()[-1]

        def response(case, facts):
            raw = json.dumps({key: {'state': state, 'source': source}
                              for key, (state, source) in zip(('A', 'B'), facts, strict=True)})
            return dict(id=case['id'], raw=raw, ended=True)

        identical = dict(id=control['id'], raw=control['answer'], ended=True, delivered=control['answer'],
                         input_tokens=100, wire_sha='same')
        rows = {'direct': [response(first, first['gold']), response(second, first['gold']), identical],
                'split': [response(first, first['gold']), response(second, second['gold']), identical]}
        report = study.summarize([first, second, control], rows)
        self.assertEqual(report['pairs']['rail'], {'direct': {'correct': False, 'exact': False},
                                                   'split': {'correct': True, 'exact': True}})
        self.assertFalse(report['automatic_activation'])
        rows['split'][-1] = dict(identical, raw='changed')
        with self.assertRaisesRegex(ValueError, '一般輸出控制'):
            study.summarize([first, second, control], rows)

    def test_assistant_keeps_history_and_original_question_while_model_sees_grouped_request(self):
        case = study.cases()[0]
        cid = case['id']
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'assistant.sqlite3')
            sid = store.new_session()
            for row in case['history']:
                store.add_message(sid, row['role'], row['content'])
            requested = []

            def infer(prompt, cancel):
                requested.append(prompt)
                return '{"A":{"state":"T","source":4},"B":{"state":"T","source":5}}', True, 100

            observer = study.full.Observed(dict(description={'research_adapter': True}), case, 'split', infer)
            app = study.full.assistant(store, observer)
            context = app.prepare_context(sid, case['question'])
            prompts = study.full.prompt_pair(context, case)
            observer.plan.update(contexts={cid: context}, prompts={cid: {arm: study.digest(prompt)
                                                                         for arm, prompt in prompts.items()}})
            reply = app.ask(sid, case['question'])
            self.assertEqual(requested, [prompts['split']])
            self.assertEqual(store.answer_context(reply['id']), context)
            self.assertEqual([row['content'] for row in context['recent_conversation']],
                             [row['content'] for row in case['history']])
            self.assertEqual(store.messages(sid, 10)[-2]['content'], case['question'])
            self.assertFalse(observer.record['mapped'])


    def test_prepare_freezes_multiturn_inputs_without_changing_older_assay_configuration(self):
        full = study.full
        earlier = (full.SCHEMA, full.location, full.cases, full.sources)
        description = {'research_adapter': True}
        reference = dict(reference='source', model='test-model', models={'parent': {'files': {}}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = root / 'sealed.json'
            sealed.write_text('{"full_transfer":{"cases":[]}}', encoding='utf-8')
            with patch.object(study, 'PARENT', root / 'new'), \
                 patch.object(study, 'sources', return_value={'fixture': 'abc'}), \
                 patch.object(full, 'reference', return_value=(reference, {}, description)), \
                 patch.object(full.diagnosis.transfer, 'frozen', return_value={'cases': [], 'training': []}), \
                 patch.object(full.prior, 'SEALED_FULL', sealed):
                plan = study.prepare('test-scope', 'sample-reference')
                self.assertEqual(study.frozen(study.location('test-scope')), plan)
            case = plan['cases'][0]
            self.assertEqual(plan['contexts'][case['id']]['current_request'], case['question'])
            self.assertEqual(len(plan['contexts'][case['id']]['recent_conversation']), 2)
        self.assertEqual((full.SCHEMA, full.location, full.cases, full.sources), earlier)
