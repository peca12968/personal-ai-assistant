"""Text-only local inference in an owned, cancellable optional-environment child."""
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .engine import EngineError, CodexEngine, MAX_PROMPT_BYTES, MAX_ANSWER_BYTES, answer_directory
from . import document_partial, local_chat_budget, local_models, learned_models
from .learning_batches import PhaseExit

ERRORS = {
    'system_memory': 'Windows 回報記憶體／分頁檔可用額度不足（1455），本機模型未完成作答；請先釋放記憶體或檢查分頁檔設定。沒有自動重試或改送其他引擎。',
    'gpu_busy': '本機模型正在被另一個推理或學習程序使用，這次沒有送到其他引擎。',
    'model_changed': '本機模型或研究參數的檔案／驗證記錄已變更，沒有使用未核對的版本。',
    'environment': '本機模型環境尚未就緒或版本不符，請依本機學習研究說明安裝。',
    'gpu_unavailable': '目前沒有可用且支援BF16的CUDA顯示卡。',
    'gpu_memory': '本機推理顯存不足，這次沒有保存答案；請縮短內容或釋放顯存後重試。',
    'input_too_large': '完整提示超過本機模型的24576 token上限，未裁切或改送其他引擎。',
    'document_citation_invalid': '本機文件問答的引用超出這次提供的範圍，沒有保存完成答案。可核對來源編號與行號後再提問。',
    'document_partial_invalid': '本機文件部分回答的內容或來源代號無法核對，沒有保存完成答案。',
    'generation_limit': '本機回答達到512 token上限仍未結束，未把截斷內容保存為完成答案。',
    'empty_response': '本機模型沒有傳回有效答案。',
    'invalid_request': '本機推理請求格式不符，沒有執行。',
    'inference_failed': '本機推理未完成，這次沒有保存答案。',
}


class LocalEngine:
    def __init__(self, data_dir, *, experiment=None, batch=None, active=False, timeout=240,
                 recover_chat_budget=False):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 900:
            raise ValueError('本機引擎逾時需為大於0且不超過900秒的有限值。')
        if type(recover_chat_budget) is not bool:
            raise ValueError('本機聊天提示排版開關需為布林值。')
        try:
            self._selection_json = json.dumps(learned_models.select(data_dir,experiment=experiment,batch=batch,active=active), ensure_ascii=False)
        except (ValueError, OSError, KeyError, TypeError, learned_models.sqlite3.Error):
            raise EngineError(ERRORS['model_changed'], 'local_model_changed') from None
        self.timeout = timeout
        self.recover_chat_budget = recover_chat_budget
        self.workdir = Path(data_dir).resolve()/'local-engine-workspace'
        self.workdir.mkdir(parents=True, exist_ok=True)
        folder = local_models.ROOT/'data/learning/runtime'
        self.python = folder/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        self.display_name = '本機 Qwen3-4B · ' + ('確認批次' if self.describe().get('confirmed_batch') else '研究參數' if experiment else '原始參數')

    def describe(self):
        return learned_models.description(self.selection)

    @property
    def selection(self):
        return json.loads(self._selection_json)

    def command(self, output):
        if not self.python.is_file():
            raise EngineError(ERRORS['environment'], 'local_environment')
        return [str(self.python), '-I', str(local_models.ROOT/'assistant/local_worker.py'), '--output', str(output)]

    def response_directory(self):
        return answer_directory(self.workdir)

    def answer(self, prompt, cancel=None):
        return self._answer_with_delivery(prompt, cancel)[0]

    @property
    def answer_with_delivery(self):
        return self._answer_with_delivery if self.recover_chat_budget else None

    def _answer_with_delivery(self, prompt, cancel=None):
        schema = local_chat_budget.REQUEST_SCHEMA if self.recover_chat_budget else 'local_inference_request_v1'
        return self._answer({'schema': schema, 'prompt': prompt}, (prompt,), cancel)

    def answer_roles(self, guide, data, cancel=None):
        """Explicit research entry point. The caller supplies the host-rendered guide."""
        if not guide or not data:
            raise EngineError('角色分離需要完整的指引與資料。', 'invalid_input')
        return self._answer({'schema': 'local_inference_request_v2', 'guide': guide, 'data': data}, (guide, data), cancel)[0]

    def _answer(self, payload, texts, cancel):
        deadline = time.monotonic() + self.timeout
        def check():
            if cancel and cancel.is_set():
                raise EngineError('已停止本機回答。', 'cancelled')
            if time.monotonic() >= deadline:
                raise EngineError('本機回答逾時，已要求停止程序。', 'timeout')
        check()
        selection = self.selection
        def verify_selection(*,full=False):
            try:
                (learned_models.validate if full else learned_models.check_context)(selection)
            except (ValueError,OSError,KeyError,TypeError,learned_models.sqlite3.Error):
                raise EngineError(ERRORS['model_changed'], 'local_model_changed') from None
        verify_selection(full=True)
        check()
        if any(not isinstance(text, str) for text in texts):
            raise EngineError('推理輸入必須是文字。', 'invalid_input')
        try:
            if sum(len(text.encode('utf-8')) for text in texts) > MAX_PROMPT_BYTES:
                raise EngineError('本機推理輸入超過2MiB。', 'input_too_large')
            request = json.dumps({**payload, 'selection': selection}, ensure_ascii=False).encode('utf-8')
        except UnicodeError:
            raise EngineError('推理輸入包含無效文字。', 'invalid_input') from None
        if len(request) > local_models.MAX_REQUEST_BYTES:
            raise EngineError('本機推理請求超過大小上限。', 'input_too_large')
        try:
            with self.response_directory() as temporary:
                folder = Path(temporary)
                output, log = folder/'result.json', folder/'worker.log'
                with tempfile.TemporaryFile(dir=folder) as source, log.open('wb') as stderr:
                    source.write(request); source.seek(0)
                    command = self.command(output)
                    check()
                    actual=PhaseExit(output.with_suffix('.started'))
                    process = subprocess.Popen(command, stdin=source, stdout=subprocess.DEVNULL, stderr=stderr,
                        cwd=local_models.ROOT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    try:
                        last_check=0
                        while process.poll() is None:
                            actual.observe()
                            check()
                            if time.monotonic()-last_check>=2:
                                verify_selection(); last_check=time.monotonic()
                            if log.stat().st_size > 1024 * 1024:
                                raise EngineError('本機推理診斷輸出超限，已要求停止。', 'local_log_limit')
                            time.sleep(0.1)
                        check()
                        if log.stat().st_size > 1024 * 1024:
                            raise EngineError('本機推理診斷輸出超限。', 'local_log_limit')
                        if not output.is_file():
                            raise EngineError(ERRORS['inference_failed'], 'local_process_failed')
                        try:
                            result = local_models.read_json(output, local_models.MAX_RESULT_BYTES)
                        except (ValueError, UnicodeError):
                            raise EngineError('本機引擎回覆格式無效。', 'invalid_response') from None
                        if not isinstance(result, dict) or result.get('schema') != 'local_inference_result_v1':
                            raise EngineError('本機引擎回覆格式無效。', 'invalid_response')
                        if result.get('status') == 'failed':
                            code = result.get('code')
                            if set(result) != {'schema', 'status', 'code'} or not isinstance(code, str) or code not in ERRORS:
                                raise EngineError('本機引擎錯誤格式無效。', 'invalid_response')
                            raise EngineError(ERRORS[code], 'local_'+code)
                        keys = {'schema', 'status', 'answer', 'selection_fingerprint', 'input_tokens', 'output_tokens', 'peak_gpu_bytes'}
                        partial_format = (payload['schema'] == local_chat_budget.REQUEST_SCHEMA
                                          and result.get('format') == document_partial.METHOD)
                        if partial_format:
                            keys.add('format')
                        if (process.returncode != 0 or set(result) != keys or result['status'] != 'succeeded'
                                or result['selection_fingerprint'] != selection['fingerprint']
                                or type(result['input_tokens']) is not int or not 1 <= result['input_tokens'] <= local_models.MAX_INPUT_TOKENS
                                or type(result['output_tokens']) is not int or not 1 <= result['output_tokens'] <= local_models.MAX_OUTPUT_TOKENS
                                or type(result['peak_gpu_bytes']) is not int or result['peak_gpu_bytes'] < 0
                                or not isinstance(result['answer'], str) or not result['answer'].strip()):
                            raise EngineError('本機引擎回覆或模型版本核對失敗。', 'invalid_response')
                        try:
                            answer_bytes = result['answer'].encode('utf-8')
                        except UnicodeError:
                            raise EngineError('本機回覆包含無效文字。', 'invalid_response') from None
                        if len(answer_bytes) > MAX_ANSWER_BYTES:
                            raise EngineError('本機回覆超過大小上限。', 'output_too_large')
                        check()
                        verify_selection(full=True)
                        raw = result['answer'].strip()
                        if not partial_format:
                            return raw, None
                        recovery = local_chat_budget.recovery_plan(payload['prompt'])
                        if recovery is None or recovery[2] is None:
                            raise EngineError(ERRORS['document_partial_invalid'], 'local_document_partial_invalid')
                        try:
                            delivered = document_partial.render(raw, recovery[2])
                            from .response_delivery import make_receipt
                            return delivered, make_receipt(raw, delivered, method=document_partial.METHOD)
                        except (ValueError, RecursionError):
                            raise EngineError(ERRORS['document_partial_invalid'], 'local_document_partial_invalid') from None
                    finally:
                        try:
                            try: actual.observe()
                            finally:
                                CodexEngine.stop(process); process.wait(timeout=10)
                            actual.wait()
                        except (OSError, ValueError, subprocess.TimeoutExpired):
                            raise EngineError('已要求停止，但無法確認本機程序已結束。', 'cleanup_unconfirmed') from None
                        finally: actual.close()
        except OSError:
            raise EngineError('無法啟動本機引擎或讀寫推理暫存資料。', 'local_io') from None
