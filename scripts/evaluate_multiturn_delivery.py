"""Measure real goal delivery, clarification and source retention on fresh synthetic goals."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import artifacts, autonomy, goal_guidance, goal_replies
from assistant.acceptance import strict_json
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_cross_task_boundaries import prepare
from scripts.evaluate_understanding import fingerprint, save_report

SUITE = ROOT / 'evals/goal_multiturn_delivery.json'
CONTEXT = '\n\n目標與已觀察資料：\n'
CITATION = re.compile(r'\[D(\d+):L(\d+)(?:-L?(\d+))?\]')
USER_CITATION = re.compile(r'\[(?:補充|更正)#\d+\]')
MAX_GOAL_STEPS = 24


def signature(suite):
    paths = (Path(__file__), ROOT / 'scripts/evaluate_cross_task_boundaries.py')
    return {**fingerprint(suite), 'collector_sha256': hashlib.sha256(b''.join(path.read_bytes() for path in paths)).hexdigest()}


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('多輪任務題庫不能為空。')
    ids = set()
    for case in cases:
        if case['id'] in ids or not re.fullmatch('[a-z][a-z0-9_]+', case['id']):
            raise ValueError('案例編號重複或格式不符。')
        ids.add(case['id'])
        autonomy.text(case['prompt'], 4000)
        if 'guidance' in case:
            autonomy.text(case['guidance'], 4000)
            if not 1 <= case['first_steps'] <= 6:
                raise ValueError('分段更正必須設定前置步數。')
        if waiting := case.get('waiting_guidance'):
            if 'guidance' in case or not waiting['question_has'] or type(waiting['withdraw']) is not bool:
                raise ValueError('待答更正須指定原問題及是否撤下。')
            autonomy.text(waiting['text'], 4000)
            for pattern in waiting['question_has']:
                re.compile(pattern)
            if 'remaining_question' in waiting:
                autonomy.text(waiting['remaining_question'], 800)
                if not waiting['withdraw']:
                    raise ValueError('驗收保留的分問時，須同時驗收原問題已撤下。')
        if 'final_file_delivery' in case and type(case['final_file_delivery']) is not bool:
            raise ValueError('最終交付方式須為布林值。')
        if case['file_delivery']:
            artifacts.filename(case['artifact'])
        if 'expected_question' in case:
            if case.get('expected_json') is not None or case.get('source_lines'):
                raise ValueError('等待補充案例應直接核對問題與暫表文字。')
            question = case['expected_question']
            if not question['question_has']:
                raise ValueError('等待補充案例須明列必要的問題內容。')
            for pattern in question['question_has'] + question.get('question_excludes', []):
                re.compile(pattern)
        if not (case.get('must_match') or 'expected_json' in case):
            raise ValueError('每案須有可核對的交付條件。')
        replies = case.get('replies', ())
        if len(replies) > 6:
            raise ValueError('每案最多六次固定答覆。')
        for reply in replies:
            autonomy.text(reply['answer'], 4000)
            if not reply['question_has']:
                raise ValueError('預設答覆須有可核對的提問條件。')
            for pattern in reply['question_has'] + reply.get('question_excludes', []):
                re.compile(pattern)
            if 'partial_has' in reply and not reply['partial_has']:
                raise ValueError('暫表驗收須明列必要內容。')
            if 'restore_before' in reply and (reply['restore_before'] is not True or 'partial_has' not in reply):
                raise ValueError('須指定待答暫表，才能在答覆前驗收備份還原。')
            for pattern in reply.get('partial_has', []) + reply.get('partial_omits', []):
                re.compile(pattern)
        for pattern in case.get('must_match', []) + case.get('must_omit', []):
            re.compile(pattern)
        for source in case.get('source_lines', ()):
            document = case['documents'][source.get('doc', 0)]
            if not source['text'] or source['text'] not in document['content'].splitlines()[source['line'] - 1]:
                raise ValueError('核對條件未指向合成文件的指定行。')
        for claim in case.get('claim_sources', ()):
            re.compile(claim['claim'])
            if not claim['claim'] or not claim['sources'] or 'expected_json' in case:
                raise ValueError('同列引用驗收須明列主張及其支持來源，並使用文字成果。')
            for source in [*claim['sources'], *claim.get('forbidden_sources', ())]:
                kind = source['kind']
                if kind == 'document':
                    if not 0 <= source['doc'] < len(case.get('documents', ())):
                        raise ValueError('主張的文件編號超出題庫範圍。')
                    document = case['documents'][source['doc']]['content'].splitlines()
                    if not 1 <= source['line'] <= len(document):
                        raise ValueError('主張的文件行號超出題庫範圍。')
                    original = document[source['line'] - 1]
                elif kind == 'reply':
                    if not 0 <= source['index'] < len(replies):
                        raise ValueError('主張的答覆序號超出題庫範圍。')
                    original = replies[source['index']]['answer']
                elif kind == 'guidance':
                    original = case['guidance'] if 'guidance' in case else case['waiting_guidance']['text']
                else:
                    raise ValueError('主張支持來源類型不符。')
                if not source['text'] or source['text'] not in original:
                    raise ValueError('主張支持內容不在指定合成來源中。')
    return cases


def question_matches(question, rule):
    return all(re.search(pattern, question) for pattern in rule['question_has'])


def claim_support(case, final, report, turns):
    replies = [turn for turn in turns if turn['kind'] == 'reply' and turn.get('saved')]

    def supports(source, line):
        if source['kind'] == 'document':
            source_id = final['sources'][source['doc']]['document_id']
            return any(int(doc) == source_id and int(start) <= source['line'] <= int(end or start)
                       for doc, start, end in CITATION.findall(line))
        if source['kind'] == 'guidance':
            guidance = case['guidance'] if 'guidance' in case else case['waiting_guidance']['text']
            return any(note['content'] == guidance and note['citation'] in line
                       for note in final.get('guidance_sources', ()))
        index = source['index']
        if index >= len(replies):
            return False
        sequence = replies[index]['before']['steps'][-1]['sequence']
        return any(reply['ask_sequence'] == sequence and reply['answer'] == case['replies'][index]['answer']
                   and reply['citation'] in line for reply in final.get('reply_sources', ()))

    result = []
    for claim in case.get('claim_sources', ()):
        matching = [line for line in report.splitlines()
                    if re.search(claim['claim'], USER_CITATION.sub('', CITATION.sub('', line)))]
        result.append(bool(matching) and all(
            all(supports(source, line) for source in claim['sources'])
            and not any(supports(source, line) for source in claim.get('forbidden_sources', ()))
            for line in matching))
    return result


def score(case, final, files, turns, calls):
    delivery = final['steps'][-1] if final['status'] == 'report_ready' else None
    file_delivery = case.get('final_file_delivery', case['file_delivery'])
    report = (files[0]['content'] if len(files) == 1 else '') if file_delivery else final['result']
    question = case.get('expected_question')
    if question:
        report = '\n'.join((final['question'], final.get('partial_result', '')))
    cited = [(int(doc), int(start), int(end or start)) for doc, start, end in CITATION.findall(report)]
    sources = final['sources']
    coverage = [any(doc == sources[row.get('doc', 0)]['document_id'] and start <= row['line'] <= end
                    for doc, start, end in cited) for row in case.get('source_lines', ())]
    checks = {
        'delivered': bool(delivery and report and (len(files) == 1 and files[0]['name'] == case['artifact']
                           if file_delivery else delivery['action']['tool'] == 'finish')),
        'content': bool(report and all(re.search(p, report) for p in case.get('must_match', ()))
                        and not any(re.search(p, report) for p in case.get('must_omit', ()))),
        'sources': all(coverage),
        'required_tools': set(case.get('required_tools', ())) <= {step['action']['tool'] for step in final['steps']},
        'applied_turns': len([turn for turn in turns if turn['kind'] == 'reply' and turn.get('saved')]) == len(case.get('replies', ()))
                         and ('guidance' not in case or any(turn['kind'] == 'guidance' and turn.get('saved') for turn in turns)),
    }
    if case.get('claim_sources'):
        claim_report = final.get('partial_result', '') if question else report
        supported = claim_support(case, final, claim_report, turns)
        checks['claim_sources'] = all(supported)
    reply_events = [turn for turn in turns if turn['kind'] == 'reply' and turn.get('saved')]
    checkpoints = [(index, rule) for index, rule in enumerate(case.get('replies', ())) if 'partial_has' in rule]
    if checkpoints:
        def checkpoint_passes(index, rule):
            if index >= len(reply_events):
                return False
            before = reply_events[index]['before']
            partial = before.get('partial_result', '')
            return (before['status'] == 'needs_input' and not before['artifacts'] and not before['result']
                    and all(re.search(p, partial) for p in rule['partial_has'])
                    and not any(re.search(p, partial) for p in rule.get('partial_omits', ())))

        checks['intermediate_waiting'] = all(checkpoint_passes(index, rule) for index, rule in checkpoints)
    restores = [index for index, rule in checkpoints if rule.get('restore_before')]
    if restores:
        checks['restored_waiting'] = all(index < len(reply_events) and reply_events[index].get('restored') is True
                                         for index in restores)
    if waiting := case.get('waiting_guidance'):
        event = next((turn for turn in turns if turn['kind'] == 'waiting_guidance'), {})
        saved = event.get('saved')
        checks['restored_guidance'] = bool(event.get('restored') and saved
                                         and saved['guidance_sources'][-1]['content'] == waiting['text'])
        before = event.get('before')
        withdrawals = final.get('withdrawn_questions', ())
        checks['old_question'] = bool(before and saved and (
            any(row['ask_sequence'] == before['steps'][-1]['sequence'] and row['question'] == before['question']
                and row['guidance_citation'] == saved['guidance_sources'][-1]['citation']
                for row in withdrawals) if waiting['withdraw'] else (
                    not withdrawals and saved['status'] == 'needs_input' and saved['question'] == before['question'])))
        if waiting['withdraw']:
            remaining = waiting.get('remaining_question', '')
            status = 'needs_input' if remaining else 'paused'
            checks['one_step_withdrawal'] = bool(checks['old_question'] and saved['status'] == status
                                                 and saved['question'] == remaining and 'after_withdrawal' not in event)
    if 'final_file_delivery' in case:
        checks['delivery_mode'] = final['file_delivery'] is file_delivery and (file_delivery or not final['artifacts'])
    if question:
        checks.pop('delivered')
        checks['waiting_for_required_data'] = (final['status'] == 'needs_input' and not final['artifacts'] and not final['result']
                                               and question_matches(final['question'], question)
                                               and not any(re.search(p, final['question']) for p in question.get('question_excludes', ())))
    if 'expected_json' in case:
        try:
            actual = strict_json(report)
            expected = case['expected_json']
            checks['content'] = actual == expected and all(type(actual[key]) is type(value) for key, value in expected.items())
        except (TypeError, ValueError):
            checks['content'] = False
    asks = [step['observation']['question'] for step in final['steps'] if step['action']['tool'] == 'ask']
    allowed_asks = len(case.get('replies', ())) + bool(question) + bool(case.get('waiting_guidance', {}).get('withdraw'))
    metrics = {'question_count': len(asks), 'extra_questions': max(0, len(asks) - allowed_asks),
               'repeated_questions': len(asks) - len(set(asks)), 'sources_retained': sum(coverage), 'sources_required': len(coverage),
               'model_calls': len(calls), 'engine_errors': sum(call['status'] == 'error' for call in calls)}
    if case.get('claim_sources'):
        metrics.update(claim_sources_supported=sum(supported), claim_sources_required=len(supported))
    return checks, metrics


def collect(path, case, suite, frozen, *, engine_factory=CodexEngine):
    store, sid, gid = prepare(path, case)
    engine = engine_factory(path / 'engine')
    trace = path / 'trace.json'
    row = {'id': case['id'], 'calls': [], 'turns': [], 'goal_id': gid, 'session_id': sid}

    class Recorded:
        def answer(self, prompt, cancel=None):
            if signature(suite) != frozen:
                raise ValueError('來源、題庫或評分程式已變更。')
            context = strict_json(prompt.rsplit(CONTEXT, 1)[1])
            if context['objective'] != case['prompt'] or context['conversation_context'] or context['conversation_history']:
                raise ValueError('實際請求混入題庫以外的目標或對話。')
            call = {'prompt': prompt, 'status': 'running'}
            row['calls'].append(call)
            save_report(trace, row)
            try:
                call.update(raw=engine.answer(prompt, cancel), status='returned')
            except (EngineError, OSError, ValueError) as exc:
                call.update(status='error', error=str(exc))
                save_report(trace, row)
                raise
            save_report(trace, row)
            return call['raw']

    recorded = Recorded()

    def restore_waiting(event):
        nonlocal store
        before = event['before']
        fingerprint = goal_replies.snapshot(store, sid, gid)['fingerprint']
        ordinal = len(row['turns'])
        backup = create_backup(store.path, path / f'waiting-{ordinal}.paibackup')
        destination = path / f'restored-{ordinal}'
        restore_backup(backup['path'], destination)
        store = Store(destination / 'assistant.sqlite3')
        if (autonomy.get_goal(store, sid, gid) != before
                or goal_replies.snapshot(store, sid, gid)['fingerprint'] != fingerprint):
            raise ValueError('還原後的待答狀態或問題指紋不符。')
        event['restored'] = True
        row['database'] = store.path.relative_to(path).as_posix()

    state = row['initial'] = autonomy.run_goal(store, sid, gid, recorded, max_steps=case.get('first_steps', 8))
    if case.get('guidance') and state['status'] in ('paused', 'needs_input'):
        view = goal_guidance.snapshot(store, sid, gid)
        event = {'kind': 'guidance', 'text': case['guidance'], 'before': state}
        row['turns'].append(event)
        try:
            goal_guidance.save(store, sid, gid, event['text'], expected_fingerprint=view['fingerprint'])
        except ValueError as exc:
            event['error'] = str(exc)
        else:
            state = event['saved'] = autonomy.get_goal(store, sid, gid)
    waiting = case.get('waiting_guidance')
    if waiting and state['status'] == 'needs_input' and question_matches(state['question'], waiting):
        event = {'kind': 'waiting_guidance', 'text': waiting['text'], 'before': state}
        row['turns'].append(event)
        restore_waiting(event)
        view = goal_guidance.snapshot(store, sid, gid)
        goal_guidance.save(store, sid, gid, waiting['text'], expected_fingerprint=view['fingerprint'])
        state = event['saved'] = autonomy.get_goal(store, sid, gid)
        if waiting['withdraw'] and state['status'] == 'needs_input' and state['question'] == event['before']['question']:
            view = goal_replies.snapshot(store, sid, gid)
            number = state['guidance_sources'][-1]['number']
            goal_replies.withdraw(store, sid, gid, number, expected_fingerprint=view['fingerprint'])
            state = event['after_withdrawal'] = autonomy.get_goal(store, sid, gid)
    replies = iter(case.get('replies', ()))
    while len(state['steps']) < MAX_GOAL_STEPS:
        if state['status'] == 'needs_input':
            rule = next(replies, None)
            if not rule or not question_matches(state['question'], rule):
                break
            event = {'kind': 'reply', 'text': rule['answer'], 'before': state, 'extra_parts':
                     [p for p in rule.get('question_excludes', ()) if re.search(p, state['question'])]}
            row['turns'].append(event)
            if rule.get('restore_before'):
                restore_waiting(event)
            view = goal_replies.snapshot(store, sid, gid)
            try:
                goal_replies.save(store, sid, gid, event['text'], expected_fingerprint=view['fingerprint'])
            except ValueError as exc:
                event['error'] = str(exc)
                break
            state = event['saved'] = autonomy.get_goal(store, sid, gid)
        elif state['status'] == 'paused':
            previous_steps = len(state['steps'])
            state = autonomy.run_goal(store, sid, gid, recorded, max_steps=min(8, MAX_GOAL_STEPS - previous_steps))
            if len(state['steps']) == previous_steps and state['status'] == 'paused':
                break
        else:
            break
    row['final'] = state
    files = row['files'] = []
    if state['status'] == 'report_ready' and state['steps'][-1]['action']['tool'] == 'deliver':
        for receipt in state['steps'][-1]['observation']['artifacts']:
            content = (artifacts.directory(store, gid) / receipt['name']).read_bytes()
            if hashlib.sha256(content).hexdigest() != receipt['sha256']:
                raise ValueError('已交付檔案與保存指紋不一致。')
            files.append({**receipt, 'content': content.decode('utf-8')})
    row['checks'], row['metrics'] = score(case, state, files, row['turns'], row['calls'])
    row['metrics']['unneeded_question_parts'] = sum(len(turn.get('extra_parts', ())) for turn in row['turns'])
    validate_database(store.path)
    save_report(trace, row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--output', type=Path, help='新建本機資料夾；省略時只核對案例及建立起點。')
    args = parser.parse_args()
    cases = validate_cases(strict_json(args.suite.read_text(encoding='utf-8')))
    if args.output is None:
        with TemporaryDirectory() as scratch:
            for index, case in enumerate(cases):
                store, _, _ = prepare(Path(scratch) / str(index), case)
                validate_database(store.path)
        print(f'Validated {len(cases)} fresh multiturn delivery fixtures without model calls.')
        return
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'data/evaluations').resolve()) or output.exists():
        parser.error('實測需使用 data/evaluations 下尚未使用的目錄。')
    output.mkdir(parents=True)
    frozen = signature(args.suite)
    report = {'signature': frozen, 'cases': [], 'complete': False}
    save_report(output / 'report.json', report)
    for index, case in enumerate(cases):
        row = collect(output / str(index), case, args.suite, frozen)
        report['cases'].append(row)
        save_report(output / 'report.json', report)
        print(case['id'], row['final']['status'], all(row['checks'].values()), row['metrics'], flush=True)
    report['complete'] = True
    save_report(output / 'report.json', report)


if __name__ == '__main__':
    main()
