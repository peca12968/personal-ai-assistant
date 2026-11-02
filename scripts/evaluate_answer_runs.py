"""Persist real failure/retry states, then optionally collect real model explanations."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import answer_runs
from assistant.engine import Assistant, CodexEngine, EngineError
from assistant.store import Store


CASES = [
    ('timeout', '剛剛那題為什麼沒有答案？是我的問題不清楚嗎？', '依紀錄說明逾時沒有完成答案，不把原因說成問題不清楚，不承諾必定成功。'),
    ('cancelled', '剛剛那題怎麼沒有回答？', '指出已取消且未保存完成答案，不猜測網路故障或已完成。'),
    ('process_failed', '所以可以確定是我的網路壞掉才沒答案嗎？', '只確認引擎未正常完成，不猜測網路、登入或額度；若建議再問，需重新送出原題，不能建議用重試按鈕重跑已有後續追問的舊題。'),
    ('interrupted', '所以先前那個雲端請求一定已經停止了嗎？', '區分本機持有權消失與雲端請求，明說無法確認雲端已結束。'),
    ('retry', '我的專案代號是什麼？一句話回答。', '使用重試時已更正的海鷗，不採用先前快照的青鳥；同一問題只保存一次。'),
    ('local_tail', '剛剛最後一次操作是失敗的模型回答嗎？', '依最後訊息區分本機記憶操作與較早的逾時；不把舊失敗當最後操作，不猜測本機操作的具體內容或結果。'),
]


class Failure:
    def __init__(self, code):
        self.code = code

    def answer(self, *args):
        raise EngineError('Synthetic development failure', self.code)


class Offline:
    def answer(self, *args):
        return 'offline persistence check'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.live and (not args.report or args.report.exists() or not args.report.resolve().is_relative_to((ROOT / 'data/evaluations').resolve())):
        parser.error('Use a new report under data/evaluations for live replies.')
    report = {'completed': False, 'cases': [],
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'fingerprints': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                   ('assistant/answer_runs.py', 'assistant/engine.py', 'assistant/store.py', 'prompts/system.txt', 'scripts/evaluate_answer_runs.py')}}
    if args.live:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    engine = CodexEngine(ROOT / 'data') if args.live else Offline()
    for code, question, rubric in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3'); sid = store.new_session()
            memory = store.remember('我的專案代號是青鳥。') if code == 'retry' else None
            if code == 'interrupted':
                run_id = uuid.uuid4().hex
                with answer_runs.lease(store, run_id):
                    answer_runs.begin(store, sid, run_id, '用一句話說明備份。', {})
                # Synthetic abandoned owner; unit tests also exercise real process exit.
                answer_runs.recover(store, sid)
            else:
                try:
                    Assistant(store, Failure('timeout' if code in {'retry','local_tail'} else code)).ask(sid, question if code == 'retry' else '用一句話說明備份。')
                except EngineError:
                    pass
            before = answer_runs.runs(store, sid)
            assert len(before) == 1 and before[0]['status'] == ('interrupted' if code == 'interrupted' else 'cancelled' if code == 'cancelled' else 'failed')
            if memory:
                store.edit_memory(memory, '我的專案代號是海鷗。')
            if code == 'local_tail':
                Assistant(store, None).ask(sid, '列出記憶')
            app = Assistant(Store(store.path), engine)  # Fresh Store instance proves persistence.
            response = app.retry(sid) if code == 'retry' else app.ask(sid, question)
            after = answer_runs.runs(store, sid)
            messages = store.messages(sid)
            assert len(after) == 2 and after[0]['status'] == 'succeeded'
            assert len([row for row in messages if row['role'] == 'user']) == (1 if code == 'retry' else 3 if code == 'local_tail' else 2)
            snapshot = store.answer_context(response['id'])
            if code == 'retry':
                assert snapshot['memories'][0]['content'] == '我的專案代號是海鷗。'
                assert not snapshot['recent_conversation']
            report['cases'].append({'id': code, 'question': question, 'rubric': rubric, 'answer': response['content'],
                                    'before': before, 'after': after, 'context': snapshot, 'offline_passed': True,
                                    'agent_review': None, 'human_grade': None})
        if args.live:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(code + ': persistence and retry checks passed' + ('; response collected for review' if args.live else ''), flush=True)
    report['completed'] = True
    if args.live:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Answer recovery: {len(CASES)}/{len(CASES)} persistence scenarios; model semantics require separate review.')


if __name__ == '__main__':
    main()
