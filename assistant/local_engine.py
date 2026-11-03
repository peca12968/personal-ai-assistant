"""Text-only local inference in an owned, cancellable optional-environment child."""
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .engine import EngineError, CodexEngine, MAX_PROMPT_BYTES, MAX_ANSWER_BYTES, answer_directory
from . import local_models

ERRORS = {
    'gpu_busy': '本機模型正在被另一個推理或學習程序使用，這次沒有送到其他引擎。',
    'model_changed': '本機模型或研究參數的檔案／驗證記錄已變更，沒有使用未核對的版本。',
    'environment': '本機模型環境尚未就緒或版本不符，請依本機學習研究說明安裝。',
    'gpu_unavailable': '目前沒有可用且支援BF16的CUDA顯示卡。',
    'gpu_memory': '本機推理顯存不足，這次沒有保存答案；請縮短內容或釋放顯存後重試。',
    'input_too_large': '完整提示超過本機模型的24576 token上限，未裁切或改送其他引擎。',
    'generation_limit': '本機回答達到512 token上限仍未結束，未把截斷內容保存為完成答案。',
    'empty_response': '本機模型沒有傳回有效答案。',
    'invalid_request': '本機推理請求格式不符，沒有執行。',
    'inference_failed': '本機推理未完成，這次沒有保存答案。',
}


class LocalEngine:
    def __init__(self, data_dir, *, experiment=None, timeout=240):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 900:
            raise ValueError('本機引擎逾時需為大於0且不超過900秒的有限值。')
        try:
            self._selection_json = json.dumps(local_models.select(experiment), ensure_ascii=False)
        except (ValueError, OSError, KeyError, TypeError):
            raise EngineError(ERRORS['model_changed'], 'local_model_changed') from None
        self.timeout = timeout
        self.workdir = Path(data_dir).resolve()/'local-engine-workspace'
        self.workdir.mkdir(parents=True, exist_ok=True)
        folder = local_models.ROOT/'data/learning/runtime'
        self.python = folder/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        self.display_name = '本機 Qwen3-4B · ' + ('研究參數' if experiment else '原始參數')

    def describe(self):
        return local_models.description(self.selection)

    @property
    def selection(self):
        return json.loads(self._selection_json)

    def command(self, output):
        if not self.python.is_file():
            raise EngineError(ERRORS['environment'], 'local_environment')
        return [str(self.python), '-I', str(local_models.ROOT/'assistant/local_worker.py'), '--output', str(output)]

    def answer(self, prompt, cancel=None):
        deadline = time.monotonic() + self.timeout
        def check():
            if cancel and cancel.is_set():
                raise EngineError('已停止本機回答。', 'cancelled')
            if time.monotonic() >= deadline:
                raise EngineError('本機回答逾時，已要求停止程序。', 'timeout')
        check()
        selection = self.selection
        if not isinstance(prompt, str):
            raise EngineError('推理輸入必須是文字。', 'invalid_input')
        try:
            if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
                raise EngineError('本機推理輸入超過2MiB。', 'input_too_large')
            request = json.dumps({'schema': 'local_inference_request_v1', 'prompt': prompt,
                                  'selection': selection}, ensure_ascii=False).encode('utf-8')
        except UnicodeError:
            raise EngineError('推理輸入包含無效文字。', 'invalid_input') from None
        if len(request) > local_models.MAX_REQUEST_BYTES:
            raise EngineError('本機推理請求超過大小上限。', 'input_too_large')
        try:
            with answer_directory(self.workdir) as temporary:
                folder = Path(temporary)
                output, log = folder/'result.json', folder/'worker.log'
                with tempfile.TemporaryFile(dir=folder) as source, log.open('wb') as stderr:
                    source.write(request); source.seek(0)
                    command = self.command(output)
                    check()
                    process = subprocess.Popen(command, stdin=source, stdout=subprocess.DEVNULL, stderr=stderr,
                        cwd=local_models.ROOT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    try:
                        while process.poll() is None:
                            check()
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
                        return result['answer'].strip()
                    finally:
                        CodexEngine.stop(process)
                        try:
                            process.wait(timeout=10)
                        except (OSError, subprocess.TimeoutExpired):
                            raise EngineError('已要求停止，但無法確認本機程序已結束。', 'cleanup_unconfirmed') from None
        except OSError:
            raise EngineError('無法啟動本機引擎或讀寫推理暫存資料。', 'local_io') from None
