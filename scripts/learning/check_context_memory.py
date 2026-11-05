"""Run the fixed long-context GPU regression with a local model and synthetic history."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import answer_runs
from assistant.engine import Assistant, EngineError, render_prompt
from assistant.parameter_acceptance import TracedEngine
from assistant.store import Store
from scripts.evaluate_understanding import prepare_case

MIN_TEST_TOKENS = 23000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path, help='本機引擎的資料目錄')
    parser.add_argument('--batch', help='已確認批次 ID，省略時測試原始底座')
    args = parser.parse_args()
    suite = (ROOT / 'evals/referent_history.json').read_bytes()
    case, = [row for row in json.loads(suite) if row['id'] == 'ambiguous_station']
    parent = ROOT / 'data/learning/context-memory-checks'
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
    print(f'本機檢查紀錄 {folder}', flush=True)
    store = Store(folder / 'synthetic.sqlite3')
    sid = prepare_case(store, case)
    model = TracedEngine(args.data_dir, batch=args.batch)
    app = Assistant(store, model, allow_local_transcription=False, allow_local_evidence=False)
    model.destination = folder
    model.expected_prompt = render_prompt(app.prepare_context(sid, case['prompt']))
    report = {'case': case['id'], 'expected': case['expected_answer'], 'minimum_input_tokens': MIN_TEST_TOKENS,
              'suite_sha256': hashlib.sha256(suite).hexdigest(),
              'request_sha256': hashlib.sha256(model.expected_prompt.encode()).hexdigest(),
              'selection_fingerprint': model.selection['fingerprint']}
    try:
        reply = app.ask(sid, case['prompt'])
    except EngineError as error:
        report.update(resource_status='failed', error_code=error.code)
    else:
        result = json.loads((folder / 'worker/result.json').read_text(encoding='utf-8'))
        run, = answer_runs.runs(store, sid)
        if (run['status'] != 'succeeded' or result['answer'] != reply['content']
                or store.messages(sid)[-1]['content'] != reply['content']
                or render_prompt(store.answer_context(reply['id'])) != model.expected_prompt):
            raise ValueError('模型輸出與保存的回答紀錄不一致。')
        status = 'completed' if result['input_tokens'] >= MIN_TEST_TOKENS else 'below_test_length'
        report.update(resource_status=status, input_tokens=result['input_tokens'],
                      output_tokens=result['output_tokens'], peak_gpu_bytes=result['peak_gpu_bytes'],
                      answer_matches_expected=reply['content'] == case['expected_answer'])
    output = json.dumps(report, ensure_ascii=False, indent=2)
    (folder / 'report.json').write_text(output, encoding='utf-8')
    print(output)
    return 0 if report['resource_status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
