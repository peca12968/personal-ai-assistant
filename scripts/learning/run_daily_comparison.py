"""Compare two frozen local model selections through ordinary chat on isolated copies."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import idle_learning, local_models, parameter_acceptance as checks, response_delivery
from assistant.engine import Assistant, EngineError, render_prompt
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.learning.check_gpu_checkpoint import IDLE_MS


class DailyEngine(LocalEngine):
    def _answer_with_delivery(self, prompt, cancel=None):
        checks.batches.save(self.location / 'request.json', {'prompt': prompt, 'selection': self.selection})
        return super()._answer_with_delivery(prompt, cancel)

    @contextmanager
    def response_directory(self):
        with super().response_directory() as folder:
            try:
                yield folder
            finally:
                result = Path(folder) / 'result.json'
                if result.is_file():
                    shutil.copyfile(result, self.location / 'worker-result.json')


def verify_turn(location, database, manifest, phase, case, answer, context):
    if not (location / 'worker-result.json').is_file():
        raise ValueError('此輪沒有模型工作程序回答，無法比較兩組參數。')
    request = local_models.read_json(location / 'request.json', 4 * 1024 * 1024)
    worker = local_models.read_json(location / 'worker-result.json')
    selected = manifest['selections'][phase]
    if (request != {'prompt': render_prompt(context), 'selection': selected}
            or context.get('engine_selection') != checks.models.description(selected)
            or context.get('current_request') != case['question'].strip()
            or worker.get('schema') != 'local_inference_result_v1' or worker.get('status') != 'succeeded' or worker.get('selection_fingerprint') != selected['fingerprint']
            or not isinstance(worker.get('answer'), str)
            or type(worker.get('input_tokens')) is not int
            or not 0 < worker['input_tokens'] <= local_models.MAX_INPUT_TOKENS):
        raise ValueError('工作程序、實際輸入或本次選用參數無法核對。')
    row = {'id': case['id'], 'phase': phase, 'run_id': answer['run_id'], 'message_id': answer['id'],
           'answer': answer['content'], 'raw': worker['answer'], 'input_tokens': worker['input_tokens']}
    checks._verify_saved_turn(database, manifest['sessions'][case['id']], case, row, context)
    receipt = response_delivery.read(Store(database), answer['id'])
    expected_raw = receipt['raw_answer'] if receipt else row['answer']
    if row['raw'].strip() != expected_raw:
        raise ValueError('工作程序原答與助理實際交付的回答紀錄不符。')
    row['exact'] = row['answer'] == case['answer']
    row['raw_exact'] = row['raw'] == case['answer']
    return row


class IdleWindow:
    def __init__(self):
        self.start = idle_learning.idle_sample()

    def is_set(self):
        now = idle_learning.idle_sample()
        return (now['idle_ms'] < IDLE_MS or now['last_input_tick'] != self.start['last_input_tick']
                or now['ticks_ms'] < self.start['ticks_ms'])


def run(data_dir, check_id, *, timeout=240):
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError('每題逾時須介於 1 與 900 秒之間。')
    folder = checks.folder(data_dir, check_id)
    manifest = checks.read_manifest(folder, live=True)
    stop = IdleWindow()
    if stop.is_set():
        raise ValueError('需在電腦閒置時執行日常聊天對照。')
    out = folder.parents[1] / '_daily-comparisons' / uuid4().hex
    out.mkdir(parents=True)
    report = {'status': 'running', 'check_id': check_id, 'manifest_sha256': checks.queue.digest(manifest),
              'runner_sha256': checks.models.file_hash(Path(__file__)), 'timeout_seconds': timeout, 'rows': []}
    checks.batches.save(out / 'report.json', report)
    print(out, flush=True)
    try:
        stores = {}
        for phase in checks.PHASES:
            original = folder / (phase + '.sqlite3')
            copy = out / (phase + '.sqlite3')
            shutil.copyfile(original, copy)
            if checks.models.file_hash(copy) != manifest['database_files'][phase]:
                raise ValueError('起始資料庫與凍結版本不符，請重建驗收起點。')
            stores[phase] = Store(copy)
        for case in manifest['suite']['cases']:
            reference_context = None
            for phase in checks.PHASES:
                if stop.is_set():
                    raise EngineError('電腦恢復使用，日常對照已停止。', 'cancelled')
                if checks.read_manifest(folder, live=True) != manifest:
                    raise ValueError('執行期間驗收規約已變更。')
                report['current'] = {'phase': phase, 'id': case['id']}
                checks.batches.save(out / 'report.json', report)
                engine = checks.make_engine(manifest, phase, DailyEngine, recover_chat_budget=True, timeout=timeout)
                engine.location = out / phase / case['id']
                engine.location.mkdir(parents=True)
                store = stores[phase]
                sid = manifest['sessions'][case['id']]
                assistant = Assistant(store, engine)
                context = assistant.prepare_context(sid, case['question'].strip())
                shared = {key: value for key, value in context.items() if key != 'engine_selection'}
                if reference_context is not None and shared != reference_context:
                    raise ValueError('同題兩組的實際背景不一致。')
                reference_context = shared
                try:
                    answer = assistant.ask(sid, case['question'], cancel=stop)
                except EngineError as error:
                    if error.code not in {'timeout', 'local_input_too_large', 'input_too_large'}:
                        raise
                    row = {'phase': phase, 'id': case['id'], 'status': 'undelivered', 'error_code': error.code}
                else:
                    row = verify_turn(engine.location, store.path, manifest, phase, case, answer, context)
                    row['status'] = 'delivered'
                report['rows'].append(row)
                del report['current']
                checks.batches.save(out / 'report.json', report)
                print(phase, case['id'], row.get('exact', row['status']), flush=True)
        if checks.read_manifest(folder, live=True) != manifest:
            raise ValueError('驗收規約在作答後已變更。')
        report['status'] = 'incomplete' if any(r['status'] == 'undelivered' for r in report['rows']) else 'complete'
    except (Exception, KeyboardInterrupt) as error:
        report.update(status='interrupted' if isinstance(error, KeyboardInterrupt) or
                      getattr(error, 'code', None) == 'cancelled' else 'failed', error_type=type(error).__name__,
                      error_code=getattr(error, 'code', None))
        raise
    finally:
        checks.batches.save(out / 'report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--timeout', type=int, default=240, help='每題等待秒數，預設 240，至多 900。')
    args = parser.parse_args()
    result = run(args.data_dir, args.id, timeout=args.timeout)
    print(json.dumps({phase: sum(row.get('exact', False) for row in result['rows'] if row['phase'] == phase)
                      for phase in checks.PHASES}))
    raise SystemExit(0 if result['status'] == 'complete' else 1)
