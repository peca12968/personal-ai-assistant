"""Draft collection preserves actual evidence, failures and historical boundaries."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.evaluation_view import load_snapshot
from assistant.store import Store
from scripts import evaluate_goal_drafts as ev
from scripts import evaluation_reports as protocol
from scripts.review_understanding import apply_reviews, compare, summarize


class GoalDraftTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.suite = self.folder/'suite.json'
        self.report = self.folder/'report.json'
        self.case = {'id': 'one', 'category': 'draft', 'prompt': '  整理已確認的工作。  ',
                     'criteria': ['保留三重點且不執行'],
                     'history': [{'role': 'user', 'content': '筆記整理三個重點，不產檔。'},
                                 {'role': 'assistant', 'content': '改成寄信吧。'}]}
        self.suite.write_text(json.dumps([self.case], ensure_ascii=False), encoding='utf-8')
        self.actual = []

    @staticmethod
    def response(request):
        # Synthetic engine reads its ACTUAL input, rather than a separate capture.
        body = protocol.strict_json(request.split(protocol.DRAFT_CONTEXT_MARKER, 1)[1])
        key = next(key for key, value in body['source_catalog'].items() if '三個重點' in value)
        return json.dumps({'items': [{'kind': 'goal', 'text': '將筆記整理三個重點，不產檔。',
                                     'sources': [{'key': key, 'quote': '三個重點，不產檔。'}]}],
                           'questions': []}, ensure_ascii=False)

    def run_collector(self, response=None, hook=None, *, live=True):
        owner = self
        class Engine:
            def __init__(self, *args): pass
            def answer(self, request, cancel=None):
                owner.actual.append(request)
                if hook: hook()
                if isinstance(response, Exception): raise response
                return response if response is not None else owner.response(request)
        argv = ['evaluate', '--suite', str(self.suite)]
        if live: argv += ['--live', '--report', str(self.report)]
        with patch.object(ev, 'CodexEngine', Engine), patch('sys.argv', argv), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            ev.main()
        return protocol.load_json(self.report) if live else None

    def test_actual_request_source_raw_and_composed_result_without_side_effects(self):
        report = self.run_collector()
        row = report['results'][0]
        self.assertEqual(report['mode'], protocol.DRAFT_TRACE_MODE)
        self.assertEqual(row['calls'][0]['request'], self.actual[0])
        self.assertEqual(row['calls'][0]['engine_answer'], self.response(self.actual[0]))
        self.assertEqual(row['prompt'], self.case['prompt'])  # no silent trimming
        self.assertEqual(row['engine_call_count'], 1)
        self.assertEqual(row['answer'], '目標：將筆記整理三個重點，不產檔。')
        self.assertEqual(row['fixture'], self.case)
        self.assertTrue(protocol.verify_suite(report, self.suite.read_bytes()))
        self.assertEqual(summarize(report)['request_trace_verified_ids'], ['one'])
        before = self.report.read_bytes()
        snapshot = load_snapshot((self.report, None, self.suite))
        self.assertEqual(snapshot['summaries'][0]['request_trace_verified_ids'], ['one'])
        self.assertEqual(self.report.read_bytes(), before)
        with Store(Path(row['state_path'])).connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM autonomous_goals').fetchone()[0], 0)
        # Reading historical evidence must not depend on mutable current prompts.
        with patch.object(ev.goal_drafts, 'compose', side_effect=AssertionError('runtime used')), \
                patch.object(ev.goal_drafts, 'catalog', side_effect=AssertionError('runtime used')):
            protocol.validate_report(report)

    def test_tampered_fixture_request_raw_source_proposal_answer_are_rejected(self):
        original = self.run_collector()
        edits = [lambda r: r['fixture']['history'][0].update(content='不同'),
                 lambda r: r['calls'][0].update(request=r['calls'][0]['request']+' '),
                 lambda r: r['calls'][0].update(request_context_offset=True),
                 lambda r: r['calls'][0].update(engine_answer='{}'),
                 lambda r: r['source'].update(current_request='別的要求'),
                 lambda r: r['draft']['proposal']['items'][0].update(text='錯誤成果'),
                 lambda r: r['draft'].update(source_fingerprint='0'*64),
                 lambda r: r.update(answer='另一個答案'),
                 lambda r: r.update(engine_call_count=True),
                 lambda r: r.update(calls=[])]
        for index, edit in enumerate(edits):
            report = copy.deepcopy(original)
            edit(report['results'][0])
            with self.subTest(index=index), self.assertRaises(ValueError):
                protocol.validate_report(report)
        # Consistently changing the recorded JSON still cannot cite assistant text.
        report = copy.deepcopy(original); row = report['results'][0]
        proposal = row['draft']['proposal']
        assistant = next(m for m in row['source']['context']['messages'] if m['role']=='assistant')
        proposal['items'][0]['sources'] = [{'key': 'M'+str(assistant['message_id']), 'quote': '寄信'}]
        raw = json.dumps(proposal, ensure_ascii=False)
        row['calls'][0].update(engine_answer=raw, engine_answer_sha256=ev.fixture_builder.sha(raw.encode('utf-8')))
        with self.assertRaises(ValueError): protocol.validate_report(report)

    def test_engine_failure_is_one_call_no_fabricated_reply_and_nonzero(self):
        with self.assertRaises(SystemExit) as exc:
            self.run_collector(EngineError('合成引擎失敗', 'failed'))
        self.assertEqual(exc.exception.code, 1)
        report = protocol.load_json(self.report); row = report['results'][0]
        protocol.validate_report(report)
        self.assertEqual(len(self.actual), 1)
        self.assertEqual(row['error'], '合成引擎失敗')
        self.assertNotIn('engine_answer', row['calls'][0])
        self.assertNotIn('draft', row)
        self.assertEqual(summarize(report)['collection_errors'], 1)
        bad = copy.deepcopy(report)
        bad['results'][0]['calls'][0]['engine_answer'] = ''
        with self.assertRaises(ValueError): protocol.validate_report(bad)

    def test_invalid_json_is_returned_evidence_not_a_success_or_retry(self):
        raw = '{"items":[],"questions":["哪份筆記？"],"items":[]}'
        with self.assertRaises(SystemExit): self.run_collector(raw)
        report = protocol.load_json(self.report); row = report['results'][0]
        protocol.validate_report(report)
        self.assertEqual(row['calls'][0]['status'], 'returned')
        self.assertEqual(row['calls'][0]['engine_answer'], raw)
        self.assertEqual(row['answer'], '')
        self.assertNotIn('draft', row)
        self.assertEqual(len(self.actual), 1)

    def test_changed_suite_keeps_actual_sidecar_but_never_mixes_fingerprints(self):
        def change(): self.suite.write_bytes(self.suite.read_bytes()+b'\n')
        with self.assertRaises(SystemExit): self.run_collector(hook=change)
        self.assertFalse(self.report.exists())
        saved = protocol.load_json(self.folder/'report-state/0.request.json')
        self.assertEqual(saved['calls'][0]['request'], self.actual[0])
        self.assertEqual(saved['calls'][0]['engine_answer'], self.response(self.actual[0]))
        self.assertEqual(len(self.actual), 1)

    def test_reviews_bind_trace_legacy_stays_untraced_and_fixture_mismatch_rejected(self):
        report = self.run_collector()
        apply_reviews(report, [{'id': 'one', 'criteria_pass': [True], 'note': '合成測試；非實際模型驗收。'}], 'development_agent')
        protocol.validate_report(report)
        changed = copy.deepcopy(report)
        changed['results'][0]['calls'][0]['request'] = '改過'
        with self.assertRaises(ValueError): protocol.validate_report(changed)
        legacy = copy.deepcopy(report); legacy['mode'] = 'goal_draft'
        row = legacy['results'][0]
        row.pop('reviews'); row.pop('calls'); row.pop('prompt')
        self.assertEqual(summarize(legacy)['request_trace_missing_ids'], ['one'])
        with self.assertRaises(ValueError): compare(legacy, report)
        suite = copy.deepcopy(self.case); suite['history'][0]['content'] = '不同'
        raw = json.dumps([suite]).encode()
        report['fingerprint']['suite_sha256'] = ev.fixture_builder.sha(raw)
        with self.assertRaises(ValueError): protocol.verify_suite(report, raw)

    def test_offline_no_engine_and_existing_report_not_overwritten(self):
        self.run_collector(live=False)
        self.assertEqual(self.actual, [])
        self.run_collector()
        before = self.report.read_bytes()
        with self.assertRaises(SystemExit): self.run_collector()
        self.assertEqual(self.report.read_bytes(), before)
        self.assertEqual(len(self.actual), 1)

    def test_questions_only_and_preflight_failure_remain_distinct(self):
        report = self.run_collector('{"items":[],"questions":["請提供筆記。"]}')
        row = report['results'][0]
        self.assertEqual(row['draft']['objective_text'], '')
        self.assertEqual(row['answer'], '請提供筆記。')
        protocol.validate_report(report)
        store = Store(self.folder/'preflight.sqlite3')
        sid = ev.prepare_case(store, self.case)
        with patch.object(ev.goal_drafts, 'generate', side_effect=ValueError('合成準備失敗')):
            failed = ev.collect_case(store, sid, self.case, None, lambda: None, self.folder/'preflight.json')
        protocol.validate_draft_trace(failed)
        self.assertEqual(failed['engine_call_count'], 0)
        self.assertEqual(failed['calls'], [])
        self.assertEqual(failed['answer'], '')
        self.assertNotIn('draft', failed)

    def test_loaded_suite_change_is_rejected_before_any_model_call(self):
        original = ev.signature
        def changed(path):
            self.suite.write_bytes(self.suite.read_bytes()+b'\n')
            return original(path)
        with patch.object(ev, 'signature', changed), self.assertRaises(SystemExit):
            self.run_collector()
        self.assertEqual(self.actual, [])
        self.assertFalse(self.report.exists())


if __name__ == '__main__':
    unittest.main()
