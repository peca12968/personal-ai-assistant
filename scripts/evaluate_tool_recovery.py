"""Real continuations after controlled tool failures, never an end-to-end score."""
import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, artifacts, goal_guidance
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, fingerprint, load_report, save_report

OLD_CONTENT = '原稿：已確認項目維持不變。\n'
NEW_CONTENT = '修正版：保留代碼 K-731；第二行待核對。\n附件數量：17。\n'


def action(tool, **args):
    return {'tool': tool, 'args': args, 'note': '執行已規劃的步驟'}


class Scripted:
    def __init__(self, actions):
        self.responses = iter(actions)

    def answer(self, prompt, cancel=None):
        return json.dumps(next(self.responses), ensure_ascii=False)


def prepare_handoff(store, case, sid):
    documents = case.get('documents')
    if not isinstance(documents, list) or not 1 <= len(documents) <= 8:
        raise ValueError('跨階段案例需指定1至8份合成來源。')
    files = []
    for index, document in enumerate(documents):
        if (not isinstance(document, dict) or set(document) != {'name', 'before', 'after'}
                or not isinstance(document['name'], str)
                or not re.fullmatch(r'[a-z][a-z0-9_-]{0,48}\.txt', document['name'])
                or (document['before'] is None and document['after'] is None)):
            raise ValueError('跨階段合成文件格式不符。')
        for version in (document['before'], document['after']):
            if version is not None:
                autonomy.text(version, 12000)
        folder = store.path.with_name(f'{store.path.stem}-source-{index}')
        folder.mkdir(exist_ok=False)
        path = folder / document['name']
        did = None
        if document['before'] is not None:
            path.write_text(document['before'], encoding='utf-8', newline='\n')
            did = store.attach_document(sid, path)
        files.append((path, did, document))
    seed = case.get('seed')
    if not isinstance(seed, list) or not 1 <= len(seed) <= 8:
        raise ValueError('跨階段案例需指定1至8個受控原步驟。')
    if any(not isinstance(step, dict) or step.get('tool') not in
           {'read', 'read_ranges', 'calculate', 'calculate_expression'} for step in seed):
        raise ValueError('跨階段原步驟只支援讀取及精確計算。')
    gid = autonomy.create_goal(store, sid, case['parent_prompt'])
    initial = autonomy.run_goal(store, sid, gid, Scripted(seed), max_steps=len(seed))
    if initial['status'] != 'paused' or len(initial['steps']) != len(seed):
        raise ValueError('原階段未保留預期的受控步驟。')
    for path, did, document in files:
        if document['after'] is None:
            store.detach_document(sid, did)
        elif document['after'] != document['before']:
            path.write_text(document['after'], encoding='utf-8', newline='\n')
            store.attach_document(sid, path)
    stopped = autonomy.run_goal(store, sid, gid, Scripted(()), max_steps=1)
    if stopped['steps'] != initial['steps'] or stopped['status'] != 'paused' or '參考文件已改變' not in stopped['notice']:
        raise ValueError('原階段未因來源改版停止。')
    child = autonomy.create_goal(store, sid, case['prompt'], parent_id=gid)
    return sid, child, seed, {'parent_before': initial, 'parent_stopped': stopped}


def prepare(store, case):
    sid = store.new_session()
    if case['fixture'] == 'source_handoff':
        return prepare_handoff(store, case, sid)
    file_delivery = case['fixture'] == 'file_conflict'
    if case['fixture'] not in ('quotient', 'file_conflict'):
        raise ValueError('未知的失敗測試來源。')
    gid = autonomy.create_goal(store, sid, case['prompt'], file_delivery=file_delivery)
    seed = [action('write_artifact', filename='original.txt', content=OLD_CONTENT),
            action('write_artifact', filename='original.txt', content=NEW_CONTENT)] if file_delivery else [
            action('calculate', operation='quotient', values=['731.25', '0'])]
    goal = autonomy.run_goal(store, sid, gid, Scripted(seed), max_steps=len(seed))
    if goal['status'] != 'paused' or goal['steps'][-1]['action']['tool'] != 'invalid':
        raise ValueError('未重現預期的受控工具失敗。')
    goal_guidance.save(store, sid, gid, case['guidance'],
                       expected_fingerprint=goal_guidance.snapshot(store, sid, gid)['fingerprint'])
    return sid, gid, seed, None


def signature(suite):
    return {**fingerprint(suite), 'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/tool_recovery.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        if not args.live:
            if args.report: raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as tmp:
                for i, case in enumerate(cases): prepare(Store(Path(tmp)/f'{i}.sqlite3'), case)
            print(f'Validated {len(cases)} controlled failure/steering fixtures; no model answers graded.')
            return
        if not args.report: raise ValueError('需指定新的本機報告路徑。')
        frozen = signature(args.suite)
        report = load_report(args.report, frozen, [c['id'] for c in cases], False)
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        def unchanged():
            if signature(args.suite) != frozen: raise ValueError('來源、提示、腳本或案例已變更。')
        for i, case in enumerate(cases):
            unchanged()
            store = Store(folder/f'{i}.sqlite3')
            sid, gid, seed, stage_setup = prepare(store, case)
            row = {'id':case['id'], 'category':case['category'], 'criteria':case['criteria'],
                   'human_grade':None, 'seed_kind':'scripted_failure_not_model_generated',
                   'seed_actions':seed, 'seed_goal':autonomy.get_goal(store,sid,gid),
                   'goal_id':gid, 'session_id':sid, 'state_path':str(store.path), 'calls':[]}
            if stage_setup is not None:
                row.update(stage_setup=stage_setup, seed_kind='scripted_parent_not_model_generated', fixture=case)
            engine = CodexEngine(folder/f'engine-{i}')
            trace_path = folder/f'{i}.request.json'
            class Recorded:
                def answer(self, prompt, cancel=None):
                    unchanged()
                    call = {'prompt':prompt,'status':'calling'}
                    row['calls'].append(call)
                    save_report(trace_path, row)
                    try:
                        raw = engine.answer(prompt, cancel)
                    except (ValueError, EngineError, OSError) as exc:
                        call.update(status='error', error=str(exc))
                        save_report(trace_path, row)
                        raise
                    call.update(status='returned', raw=raw)
                    save_report(trace_path, row)
                    unchanged()
                    return raw
            try:
                goal = autonomy.run_goal(store,sid,gid,Recorded(),max_steps=5)
            except (ValueError,EngineError) as exc:
                failed = True; row['error'] = str(exc)
                goal = autonomy.get_goal(store,sid,gid)
            unchanged()
            row.update(goal=goal, answer=goal['result'] or goal['question'] or goal['notice'])
            if case['fixture'] == 'file_conflict':
                path = artifacts.directory(store,gid)
                row['mechanical_checks'] = {
                    'original_unchanged': (path/'original.txt').read_text(encoding='utf-8') == OLD_CONTENT,
                    'revised_exact': (path/'revised.txt').is_file() and (path/'revised.txt').read_bytes() == NEW_CONTENT.encode('utf-8')}
            save_report(trace_path, row)
            report['results'].append(row); save_report(args.report, report)
            print(case['id']+': '+goal['status']+'; review pending', flush=True)
        if failed: parser.exit(1,'部分流程失敗，未自動重試。\n')
    except (ValueError,OSError) as exc: parser.exit(1,str(exc)+'\n')


if __name__ == '__main__': main()
