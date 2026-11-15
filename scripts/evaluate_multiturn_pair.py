"""Compare direct answers with the guarded work loop using identical public scenarios."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.backup import validate_database
from assistant.engine import EngineError
from scripts import evaluate_multiturn_delivery as multi
from scripts.evaluate_reasoning_pair import PinnedEngine


SUITE = ROOT / 'evals/goal_multiturn_paired.json'
INSTRUCTIONS = (
    '依原始任務及最新對話回覆。文件行已編號。引用原行用 [D1:L1]，引用使用者答覆用訊息所附的編號。'
    '只輸出一個JSON物件。需補資料時用 {"status":"ask","question":"問題","partial_result":"已可提供的暫表"}，'
    '暫表可省略。可交付時用 {"status":"finish","report":"完整成果"}；若要求文件，另加 filename，report 必須是完整文件內容。'
)
ARMS = ('direct', 'assistant')
FIELDS = {'id', 'prompt', 'documents', 'csv', 'file_delivery', 'artifact', 'replies', 'expected_question',
          'expected_json', 'must_match', 'must_omit', 'claim_sources', 'source_lines'}


def signature(suite):
    runner = Path(__file__)
    calibration = runner.with_name('evaluate_reasoning_pair.py')
    return {**multi.signature(suite), 'pair_sha256': hashlib.sha256(
        runner.read_bytes() + calibration.read_bytes()).hexdigest()}


def cases_for(suite):
    cases = multi.validate_cases(multi.strict_json(suite.read_text(encoding='utf-8')))
    for case in cases:
        if case.keys() - FIELDS or (case.get('expected_question') and case['file_delivery']):
            raise ValueError('配對題只接受相同公開回合、共同成果及待答條件。')
        if any(reply.keys() - {'question_has', 'question_excludes', 'answer', 'partial_has', 'partial_omits'}
               for reply in case.get('replies', ())):
            raise ValueError('配對題不使用還原等產品專屬條件。')
    return cases


def parse_action(raw, file_delivery):
    action = multi.strict_json(raw)
    if not isinstance(action, dict):
        raise ValueError('直接作答必須輸出JSON物件。')
    if action.get('status') == 'ask':
        if {'status', 'question'} <= action.keys() <= {'status', 'question', 'partial_result'} and (
                isinstance(action['question'], str) and action['question'].strip() and len(action['question']) <= 800
                and isinstance(action.get('partial_result', ''), str) and len(action.get('partial_result', '')) <= 12000):
            return action
    if action.get('status') == 'finish':
        keys = {'status', 'report', 'filename'} if file_delivery else {'status', 'report'}
        if action.keys() == keys and isinstance(action['report'], str) and 0 < len(action['report'].strip()) <= 12000:
            if file_delivery:
                multi.artifacts.filename(action['filename'])
            return action
    raise ValueError('直接作答的提問或成果格式不符。')


def collect_direct(path, case, suite, frozen, engine):
    documents = case.get('documents', []) + ([case['csv']] if 'csv' in case else [])
    numbered = [{'document_id': number, 'name': doc['name'], 'lines': list(enumerate(doc['content'].splitlines(), 1))}
                for number, doc in enumerate(documents, 1)]
    transcript = []
    trace = path / 'trace.json'
    row = {'id': case['id'], 'calls': [], 'turns': []}
    state = {'status': 'paused', 'steps': [], 'sources': [{'document_id': doc['document_id']} for doc in numbered],
             'reply_sources': [], 'question': '', 'result': '', 'artifacts': []}
    replies = iter(case.get('replies', ()))
    files = []
    for _ in range(len(case.get('replies', ())) + 1):
        if signature(suite) != frozen:
            raise ValueError('來源、題庫或評分程式已變更。')
        prompt = INSTRUCTIONS + '\n' + json.dumps({'objective': case['prompt'], 'documents': numbered,
                                                     'conversation': transcript}, ensure_ascii=False)
        call = {'prompt': prompt, 'status': 'running'}
        row['calls'].append(call)
        multi.save_report(trace, row)
        try:
            call.update(raw=engine.answer(prompt), status='returned')
        except (EngineError, OSError) as error:
            call.update(status='error', error=str(error))
            break
        try:
            action = parse_action(call['raw'], case['file_delivery'])
        except (TypeError, ValueError) as error:
            row['format_error'] = str(error)
            state['status'] = 'failed'
            break
        if action['status'] == 'finish':
            state.update(status='report_ready', result=action['report'], question='', partial_result='')
            state['steps'].append({'action': {'tool': 'finish'}})
            if case['file_delivery']:
                files.append({'name': action['filename'], 'content': action['report']})
            break
        question = action['question']
        partial = action.get('partial_result', '')
        sequence = len(state['steps']) + 1
        step = {'sequence': sequence, 'action': {'tool': 'ask'}, 'observation': {'question': question}}
        state['steps'].append(step)
        state.update(status='needs_input', question=question, partial_result=partial)
        rule = next(replies, None)
        if not rule or not multi.question_matches(question, rule):
            break
        source = {'citation': f'[補充#{sequence}]', 'answer': rule['answer'], 'ask_sequence': sequence}
        state['reply_sources'].append(source)
        before = {'status': 'needs_input', 'steps': [step], 'partial_result': partial, 'result': '', 'artifacts': []}
        row['turns'].append({'kind': 'reply', 'saved': True, 'before': before, 'text': rule['answer'],
                             'extra_parts': [pattern for pattern in rule.get('question_excludes', ())
                                             if re.search(pattern, question)]})
        transcript.extend(({'assistant': action}, {'user': source}))
        multi.save_report(trace, row)
    row.update(final=state, files=files)
    row['checks'], row['metrics'] = multi.score(case, state, files, row['turns'], row['calls'])
    multi.save_report(trace, row)
    return row


class RunEngine(PinnedEngine):
    def __init__(self, path, model, effort, timeout):
        super().__init__(path, model, effort, timeout)
        self.receipts = []

    def answer(self, prompt, cancel=None):
        self.metadata.clear()
        try:
            return super().answer(prompt, cancel)
        finally:
            self.receipts.append({'settings': self.metadata.copy(),
                                  'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()})


def collect_arm(path, arm, case, suite, frozen, protocol):
    if signature(suite) != frozen:
        raise ValueError('來源、題庫或評分程式已變更。')
    engine = None

    def make_engine(folder):
        nonlocal engine
        engine = RunEngine(folder, **protocol)
        return engine

    if arm == 'direct':
        path.mkdir()
        row = collect_direct(path, case, suite, frozen, make_engine(path / 'engine'))
    else:
        collector_scope = {key: value for key, value in frozen.items() if key != 'pair_sha256'}
        row = multi.collect(path, case, suite, collector_scope, engine_factory=make_engine)
    calls = row['calls']
    if len(engine.receipts) != len(calls):
        raise ValueError('模型設定紀錄與呼叫數不符。')
    for receipt, call in zip(engine.receipts, calls, strict=True):
        if receipt['prompt_sha256'] != hashlib.sha256(call['prompt'].encode('utf-8')).hexdigest():
            raise ValueError('模型設定紀錄未對應實際請求。')
    row['model_receipts'] = engine.receipts
    if arm == 'direct' and case['file_delivery']:
        row['checks']['output_content'] = row['checks'].pop('delivered')
    row['checks'].pop('required_tools')
    row['metrics']['unneeded_question_parts'] = sum(len(turn.get('extra_parts', ())) for turn in row['turns'])
    row['checks']['clarification'] = not any(row['metrics'][name] for name in
                                           ('extra_questions', 'repeated_questions', 'unneeded_question_parts'))
    row['valid_model_calls'] = bool(calls) and all(call['status'] == 'returned' for call in calls)
    if row.get('format_error'):
        row['checks']['format'] = False
    multi.save_report(path / 'trace.json', row)
    return {'status': row['final']['status'], 'checks': row['checks'], 'metrics': row['metrics'],
            'valid_model_calls': row['valid_model_calls']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--output', type=Path, help='不指定時只驗證題庫。')
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--effort', default='medium')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    cases = cases_for(args.suite)
    if args.output is None:
        with TemporaryDirectory() as scratch:
            for case in cases:
                store, _, _ = multi.prepare(Path(scratch) / case['id'], case)
                validate_database(store.path)
        print(f'Validated {len(cases)} paired multiturn fixtures; no model calls.')
        return
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'data/evaluations').resolve()) or output.exists():
        parser.error('實測需在 data/evaluations 下提供尚未存在的目錄。')
    output.mkdir(parents=True)
    frozen = signature(args.suite)
    protocol = {'model': args.model, 'effort': args.effort, 'timeout': args.timeout}
    report = {'signature': frozen, 'protocol': protocol, 'case_ids': [case['id'] for case in cases],
              'results': [], 'complete': False}
    report_path = output / 'report.json'
    multi.save_report(report_path, report)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [{arm: pool.submit(collect_arm, output / f'{case["id"]}-{arm}', arm, case, args.suite, frozen, protocol)
                 for arm in (ARMS if index % 2 == 0 else tuple(reversed(ARMS)))}
                for index, case in enumerate(cases)]
        for case, pair in zip(cases, jobs, strict=True):
            row = {'id': case['id'], **{arm: pair[arm].result() for arm in ARMS}}
            report['results'].append(row)
            multi.save_report(report_path, report)
            print(case['id'], {arm: row[arm]['checks'] for arm in ARMS}, flush=True)
    if signature(args.suite) != frozen:
        raise ValueError('來源、題庫或評分程式已變更。')
    report['complete'] = True
    multi.save_report(report_path, report)


if __name__ == '__main__':
    main()
