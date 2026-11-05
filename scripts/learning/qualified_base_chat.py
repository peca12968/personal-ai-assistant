"""Explicit per-launch use of a currently qualified base checkpoint."""
from pathlib import Path
import os
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from assistant import document_partial, local_chat_budget, local_models, local_worker, response_delivery
from assistant.engine import EngineError, MAX_ANSWER_BYTES, MAX_PROMPT_BYTES, answer_directory
from assistant.local_engine import ERRORS
from scripts.learning import assess_base_assistant as assay

EVIDENCE_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error)
RESULT_FIELDS = {'schema', 'status', 'answer', 'selection_fingerprint', 'input_tokens', 'output_tokens', 'peak_gpu_bytes'}


def qualify(name):
    folder = assay.location(name)
    meta, state = assay.frozen(folder)
    if any(assay.batches.file_hash(folder / 'sources' / name) != sha for name, sha in meta['sources'].items()):
        raise ValueError('完整助理驗收的來源副本已變更。')
    run, rows = assay.read(folder / 'run.json'), assay.read(folder / 'responses.json', 8 * 1024 * 1024)
    review_path = folder / 'review.json'
    report = assay.review(folder, meta, run, rows)
    if report != assay.read(review_path) or report['passed'] is not True or report['automatic_activation'] is not False:
        raise ValueError('完整助理驗收未通過或證據已變更。')
    assay.segments.pin_blob(state.series, state.head['blob'])
    return selection_for(name, meta, state, report), state.adapter


def selection_for(name, meta, state, report):
    selection = dict(state.selections['candidate'])
    del selection['fingerprint']
    selection.update(qualification=name, full_manifest_sha256=assay.digest(meta),
                     full_review_sha256=assay.digest(report))
    return {**selection, 'fingerprint': assay.digest(selection)}


def current(name):
    folder = assay.location(name)
    meta, state = assay.frozen(folder)
    return selection_for(name, meta, state, assay.read(folder / 'review.json'))


def description(selection):
    return {**assay.description(selection), 'qualified_assay': selection['qualification']}


class QualifiedBaseEngine:
    display_name = '本機 Qwen3-4B · 已驗收底座候選'

    def __init__(self, data_dir, name):
        self.name = name
        self.selection, _ = qualify(name)
        self.workspace = Path(data_dir).resolve() / 'local-engine-workspace'
        self.workspace.mkdir(parents=True, exist_ok=True)

    def describe(self):
        return description(self.selection)

    def answer(self, prompt, cancel=None):
        return self.answer_with_delivery(prompt, cancel)[0]

    def answer_with_delivery(self, prompt, cancel=None):
        deadline = time.monotonic() + 240

        def guard():
            if cancel and cancel.is_set():
                raise EngineError('已停止本機回答。', 'cancelled')
            if time.monotonic() >= deadline:
                raise EngineError('本機回答逾時。', 'timeout')

        def verify(check):
            try:
                if check(self.name) != self.selection:
                    raise ValueError('驗收版已變更。')
            except EVIDENCE_ERRORS:
                raise EngineError(ERRORS['model_changed'], 'local_model_changed') from None

        guard()
        try:
            if not isinstance(prompt, str) or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
                raise EngineError('本機提示無效或超過大小上限。', 'invalid_input')
        except UnicodeError:
            raise EngineError('本機提示包含無效文字。', 'invalid_input') from None
        verify(lambda name: qualify(name)[0])
        guard()
        python = assay.ROOT / 'data/learning/runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            raise EngineError(ERRORS['environment'], 'local_environment')
        try:
            with answer_directory(self.workspace) as directory:
                folder = Path(directory).resolve()
                request = dict(schema=local_chat_budget.REQUEST_SCHEMA, prompt=prompt, selection=self.selection)
                assay.save(folder / 'request.json', request)
                if (folder / 'request.json').stat().st_size > local_models.MAX_REQUEST_BYTES:
                    raise EngineError('本機請求超過大小上限。', 'input_too_large')

                def live():
                    verify(current)
                    if (folder / 'result.log').stat().st_size > 1024 * 1024:
                        raise EngineError('本機診斷輸出超限。', 'local_log_limit')

                command = [str(python), '-I', str(Path(__file__).resolve()), self.name, str(folder)]
                guard()
                assay.gpu.run_owned(command, folder, 'result', guard, live)
                live()
                verify(lambda name: qualify(name)[0])
                guard()
                result = local_models.read_json(folder / 'result.json', local_models.MAX_RESULT_BYTES)
                return deliver(prompt, self.selection, result)
        except (ValueError, OSError, sqlite3.Error, TimeoutError) as exc:
            raise EngineError('本機驗收模型回答未完成：' + str(exc), 'local_qualified_failed') from exc


def deliver(prompt, selection, result):
    if not isinstance(result, dict) or result.get('schema') != 'local_inference_result_v1':
        raise EngineError('本機引擎回覆格式無效。', 'invalid_response')
    if result.get('status') == 'failed':
        code = result.get('code')
        if set(result) == {'schema', 'status', 'code'} and isinstance(code, str) and code in ERRORS:
            raise EngineError(ERRORS[code], 'local_' + code)
        raise EngineError('本機引擎錯誤格式無效。', 'invalid_response')

    partial = result.get('format') == document_partial.METHOD
    answer = result.get('answer')
    if (set(result) != (RESULT_FIELDS | {'format'} if partial else RESULT_FIELDS)
            or result['status'] != 'succeeded' or result['selection_fingerprint'] != selection['fingerprint']
            or type(result['input_tokens']) is not int or not 1 <= result['input_tokens'] <= local_models.MAX_INPUT_TOKENS
            or type(result['output_tokens']) is not int or not 1 <= result['output_tokens'] <= local_models.MAX_OUTPUT_TOKENS
            or type(result['peak_gpu_bytes']) is not int or result['peak_gpu_bytes'] < 0
            or not isinstance(answer, str) or not answer.strip() or len(answer.encode('utf-8')) > MAX_ANSWER_BYTES):
        raise EngineError('本機引擎回覆或模型版本核對失敗。', 'invalid_response')
    raw = answer.strip()
    if not partial:
        return raw, None
    recovery = local_chat_budget.recovery_plan(prompt)
    try:
        if recovery is None or recovery[2] is None:
            raise ValueError
        delivered = document_partial.render(raw, recovery[2])
        return delivered, response_delivery.make_receipt(raw, delivered, method=document_partial.METHOD)
    except (ValueError, RecursionError):
        raise EngineError(ERRORS['document_partial_invalid'], 'local_document_partial_invalid') from None


def worker(name, folder):
    expected, adapter = qualify(name)

    def validate(selection):
        if selection != expected or qualify(name) != (expected, adapter):
            raise ValueError('驗收版已變更。')

    def assets(selection):
        validate(selection)
        base, _ = local_models.verify_assets(expected['base'])
        return base, adapter

    # Scope the candidate provider to this child. The stock worker owns
    # framing, GPU locking, loading, weight checks and result serialization.
    provider = SimpleNamespace(SCHEMA=assay.CANDIDATE, sqlite3=sqlite3, description=description,
                               validate=validate, verify_assets=assets)
    with (folder / 'request.json').open(encoding='utf-8') as request:
        with patch.object(local_worker, 'learned_models', provider), patch.object(sys, 'stdin', request), \
                patch.object(sys, 'argv', [sys.argv[0], '--output', str(folder / 'result.json')]):
            local_worker.main()


if __name__ == '__main__':
    worker(sys.argv[1], Path(sys.argv[2]))
