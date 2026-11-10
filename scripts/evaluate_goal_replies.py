"""Fixed ask/reply/resume evaluations. Offline checks never grade model quality."""
import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_guidance, goal_replies
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, fingerprint, load_report, save_report


def signature(suite):
    return {**fingerprint(suite), 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def prepare(path, case):
    store = Store(path)
    sid = store.new_session()
    documents = case.get('documents', [])
    if not isinstance(documents, list) or len(documents) > 8:
        raise ValueError('每題最多八份合成文字文件。')
    names = set()
    for document in documents:
        if (not isinstance(document, dict) or set(document) != {'name', 'content'}
                or not isinstance(document['name'], str)
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,48}\.txt', document['name'])
                or document['name'] in names):
            raise ValueError('合成文件名稱或欄位不符。')
        autonomy.text(document['content'], 12000)
        names.add(document['name'])
        source = path.with_name(f'{path.stem}-{document["name"]}')
        with source.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(document['content'])
        store.attach_document(sid, source)
    return store, sid, autonomy.create_goal(store, sid, case['prompt'],
                                           file_delivery=case.get('file_delivery', False))


def replies_for(case):
    if 'replies' in case:
        replies = case['replies']
        if 'reply' in case or not isinstance(replies, list) or not 1 <= len(replies) <= 4:
            raise ValueError('每題只能指定一種補充格式，分次補充最多四則。')
    else:
        replies = [case['reply']] if 'reply' in case else []
    for reply in replies:
        autonomy.text(reply, 4000)
    return replies


def apply_amendments(store, sid, gid, case):
    if 'amendments' not in case and 'prior_read_ranges' not in case:
        return None
    amendments = case.get('amendments')
    if not isinstance(amendments, list) or not 1 <= len(amendments) <= 4:
        raise ValueError('更正案例需指定一至四則主動補充。')
    for content in amendments:
        autonomy.text(content, 4000)
    action = {'tool': 'read_ranges', 'args': {'ranges': case.get('prior_read_ranges')}, 'note': '核對初始文件'}
    engine = SimpleNamespace(answer=lambda prompt, cancel: json.dumps(action, ensure_ascii=False))
    read = autonomy.run_goal(store, sid, gid, engine, max_steps=1)
    if read['status'] != 'paused' or read['steps'][0]['action']['tool'] != 'read_ranges':
        raise ValueError('更正案例未完成初始批次查讀。')
    saved = []
    for content in amendments:
        review = goal_guidance.snapshot(store, sid, gid)
        saved.append(goal_guidance.save(store, sid, gid, content, expected_fingerprint=review['fingerprint']))
        if saved[-1]['step_count'] != len(read['steps']) or saved[-1]['goal']['status'] != 'paused':
            raise ValueError('主動更正應保存原進度與暫停狀態。')
    return {'before': read, 'saved': saved}


def waiting_change(case):
    if 'waiting_change' not in case:
        return None
    change = case['waiting_change']
    if (not isinstance(change, dict) or set(change) != {'correction', 'withdraw'}
            or type(change['withdraw']) is not bool):
        raise ValueError('等待更正案例須指定原話與是否撤下問題。')
    autonomy.text(change['correction'], 4000)
    return change


def change_while_waiting(store, sid, gid, state, change):
    if state['status'] != 'needs_input':
        raise ValueError('原工作未停下提問，未追加更正。')
    review = goal_replies.snapshot(store, sid, gid)
    saved = goal_guidance.save(store, sid, gid, change['correction'], expected_fingerprint=review['fingerprint'])
    amended = autonomy.get_goal(store, sid, gid)
    if (amended['status'] != 'needs_input' or amended['steps'] != state['steps']
            or amended['question'] != state['question']):
        raise ValueError('只保存更正時應保留原問題及步驟。')
    result = {'asked': state, 'amended': amended}
    if change['withdraw']:
        withdraw = getattr(goal_replies, 'withdraw', None)
        if withdraw is None:
            result['withdrawal_unavailable'] = True
        else:
            number = saved['goal']['guidance_sources'][-1]['number']
            withdraw(store, sid, gid, number, expected_fingerprint=saved['fingerprint'])
            withdrawn = autonomy.get_goal(store, sid, gid)
            if withdrawn['status'] != 'paused' or withdrawn['steps'] != state['steps'] or withdrawn['question']:
                raise ValueError('撤下後應只解除等待，原步驟需保留。')
            result['withdrawn'] = withdrawn
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/goal_replies.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        for case in cases:
            autonomy.text(case['prompt'], 4000)
            replies_for(case)
            waiting_change(case)
        if not args.live:
            if args.report: raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as temp:
                for index, case in enumerate(cases):
                    store, sid, gid = prepare(Path(temp)/f'{index}.sqlite3', case)
                    assert goal_replies.snapshot(store, sid, gid)['goal']['status'] == 'pending'
                    apply_amendments(store, sid, gid, case)
                    change = waiting_change(case)
                    if change is not None:
                        action = {'tool': 'ask', 'args': {'question': '初始等待補充？'}, 'note': '合成等待狀態'}
                        engine = SimpleNamespace(answer=lambda prompt, cancel: json.dumps(action, ensure_ascii=False))
                        state = autonomy.run_goal(store, sid, gid, engine, max_steps=1)
                        change_while_waiting(store, sid, gid, state, change)
            print(f'Validated {len(cases)} reply scenarios; no model calls or quality grades.')
            return
        if not args.report: raise ValueError('真實評估需指定新的本機報告路徑。')
        frozen = signature(args.suite)
        report = load_report(args.report, frozen, [case['id'] for case in cases], False)
        report['mode'] = 'goal_reply'
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        def unchanged():
            if signature(args.suite) != frozen: raise ValueError('來源或案例已變，停止收集。')
        for index, case in enumerate(cases):
            unchanged()
            path = folder/f'{index}.sqlite3'
            store, sid, gid = prepare(path, case)
            row = {'id':case['id'], 'category':case['category'], 'criteria':case['criteria'],
                   'human_grade':None, 'state_path':str(path), 'session_id':sid, 'goal_id':gid, 'calls':[],
                   'fixture':case}
            setup = apply_amendments(store, sid, gid, case)
            if setup is not None:
                row['amendment_setup'] = setup
            engine = CodexEngine(folder/f'engine-{index}')
            trace_path = folder/f'{index}.request.json'
            class Recorded:
                def answer(self, prompt, cancel=None):
                    unchanged()
                    call = {'prompt':prompt, 'status':'calling'}
                    row['calls'].append(call)
                    save_report(trace_path, row)
                    try:
                        raw = engine.answer(prompt, cancel)
                    except (EngineError, ValueError, OSError) as exc:
                        call.update(status='error', error=str(exc))
                        save_report(trace_path, row)
                        raise
                    call.update(status='returned', raw=raw)
                    save_report(trace_path, row)
                    unchanged()
                    return raw
            try:
                state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
                replies = replies_for(case)
                change = waiting_change(case)
                if change is not None:
                    calls_before = len(row['calls'])
                    setup = change_while_waiting(store, sid, gid, state, change)
                    assert len(row['calls']) == calls_before
                    row['waiting_change'] = setup
                    state = setup.get('withdrawn', setup['amended'])
                    save_report(trace_path, row)
                    if change['withdraw']:
                        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
                        if setup.get('withdrawal_unavailable'):
                            if state != setup['amended'] or len(row['calls']) != calls_before:
                                raise ValueError('舊入口未維持原等待狀態。')
                            replies = []
                if 'replies' in case:
                    row['rounds'] = []
                for reply in replies:
                    turn = {'asked': state}
                    if 'rounds' in row:
                        row['rounds'].append(turn)
                    else:
                        row.update(turn)
                        turn = row
                    if state['status'] != 'needs_input':
                        raise ValueError('初始執行未停下提問；未提交預設回答。')
                    observed = goal_replies.snapshot(store, sid, gid)
                    before_calls = len(row['calls'])
                    turn['reply'] = reply
                    turn['saved'] = goal_replies.save(store, sid, gid, reply, expected_fingerprint=observed['fingerprint'])
                    assert turn['saved']['goal']['status'] == 'paused' and len(row['calls']) == before_calls
                    save_report(trace_path, row)
                    state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
                row['final'] = state
                row['answer'] = row['final']['result'] or row['final']['question'] or row['final']['notice']
            except (ValueError, EngineError) as exc:
                failed = True; row.update(answer='', error=str(exc), final=autonomy.get_goal(store, sid, gid))
            save_report(trace_path, row)
            unchanged(); report['results'].append(row); save_report(args.report, report)
            print(case['id']+(' failed; not retried' if 'error' in row else ' collected; review pending'), flush=True)
        if failed: parser.exit(1, '部分流程失敗；保留證據且未重試。\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__': main()
