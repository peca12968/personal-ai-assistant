"""Real continuations after controlled tool failures, never an end-to-end score."""
import argparse
import hashlib
import json
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


def prepare(store, case):
    sid = store.new_session()
    file_delivery = case['fixture'] == 'file_conflict'
    if case['fixture'] not in ('quotient', 'file_conflict'):
        raise ValueError('未知的失敗測試來源。')
    gid = autonomy.create_goal(store, sid, case['prompt'], file_delivery=file_delivery)
    seed = [action('write_artifact', filename='original.txt', content=OLD_CONTENT),
            action('write_artifact', filename='original.txt', content=NEW_CONTENT)] if file_delivery else [
            action('calculate', operation='quotient', values=['731.25', '0'])]
    class Scripted:
        def __init__(self): self.responses = iter(seed)
        def answer(self, prompt, cancel=None):
            return json.dumps(next(self.responses), ensure_ascii=False)
    goal = autonomy.run_goal(store, sid, gid, Scripted(), max_steps=len(seed))
    if goal['status'] != 'paused' or goal['steps'][-1]['action']['tool'] != 'invalid':
        raise ValueError('未重現預期的受控工具失敗。')
    goal_guidance.save(store, sid, gid, case['guidance'],
                       expected_fingerprint=goal_guidance.snapshot(store, sid, gid)['fingerprint'])
    return sid, gid, seed


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
            sid, gid, seed = prepare(store, case)
            row = {'id':case['id'], 'category':case['category'], 'criteria':case['criteria'],
                   'human_grade':None, 'seed_kind':'scripted_failure_not_model_generated',
                   'seed_actions':seed, 'seed_goal':autonomy.get_goal(store,sid,gid),
                   'goal_id':gid, 'session_id':sid, 'state_path':str(store.path), 'calls':[]}
            engine = CodexEngine(folder/f'engine-{i}')
            class Recorded:
                def answer(self, prompt, cancel=None):
                    unchanged(); raw = engine.answer(prompt,cancel); unchanged()
                    row['calls'].append({'prompt':prompt,'raw':raw})
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
            report['results'].append(row); save_report(args.report, report)
            print(case['id']+': '+goal['status']+'; review pending', flush=True)
        if failed: parser.exit(1,'部分流程失敗，未自動重試。\n')
    except (ValueError,OSError) as exc: parser.exit(1,str(exc)+'\n')


if __name__ == '__main__': main()
