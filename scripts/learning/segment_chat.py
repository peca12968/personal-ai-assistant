"""Explicit per-launch selection of the locally qualified segmented release."""
import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from assistant.engine import Assistant, EngineError, MAX_ANSWER_BYTES, MAX_PROMPT_BYTES, answer_directory
from assistant.local_engine import ERRORS
from assistant import local_models
from assistant.store import Store
from scripts.learning import qualified_segment as qualified


class SegmentEngine:
    display_name = '本機 Qwen3-4B · 分段學習驗收版'

    def __init__(self, data_dir):
        self.plan, self.batch, self.description, self.parent, self.weights = qualified.qualify()
        self.workspace = Path(data_dir) / 'local-engine-workspace'
        self.workspace.mkdir(exist_ok=True, parents=True)

    def describe(self):
        return dict(self.description)

    def answer(self, prompt, cancel=None):
        deadline = time.monotonic() + 240
        def guard():
            if cancel and cancel.is_set():
                raise EngineError('已停止本機回答。', 'cancelled')
            if time.monotonic() >= deadline:
                raise EngineError('本機回答逾時。', 'timeout')

        try:
            guard()
            if not isinstance(prompt, str) or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
                raise EngineError('本機提示無效或超過大小上限。', 'invalid_input')
            if qualified.qualify()[2] != self.description:
                raise EngineError(ERRORS['model_changed'], 'local_model_changed')
            python = qualified.ROOT / 'data/learning/runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            if not python.is_file():
                raise EngineError(ERRORS['environment'], 'local_environment')
            with answer_directory(self.workspace) as tmp:
                folder = Path(tmp).resolve()
                qualified.segments.gpu.save(folder / 'request.json', dict(prompt=prompt, description=self.description))
                if (folder / 'request.json').stat().st_size > local_models.MAX_REQUEST_BYTES:
                    raise EngineError('本機請求超過大小上限。', 'input_too_large')

                def live():
                    qualified.current(self.plan, self.batch, self.parent)
                    if (folder / 'chat.log').stat().st_size > 1024 * 1024:
                        raise EngineError('本機診斷輸出超限。', 'local_log_limit')

                cmd = [str(python), '-I', str(Path(__file__).with_name('segment_chat_worker.py').resolve()), str(folder)]
                guard()
                qualified.segments.gpu.run_owned(cmd, folder, 'chat', guard, live)
                guard()
                result = local_models.read_json(folder / 'result.json', local_models.MAX_RESULT_BYTES)
                if set(result) == {'error'} and result['error'] in ERRORS:
                    raise EngineError(ERRORS[result['error']], 'local_' + result['error'])
                answer = result.get('answer')
                if (set(result) != {'fingerprint', 'answer'} or result['fingerprint'] != self.description['selection_fingerprint']
                        or not isinstance(answer, str) or not answer.strip() or len(answer.encode('utf-8')) > MAX_ANSWER_BYTES):
                    raise EngineError('本機回答或模型指紋無效。', 'invalid_response')
                if qualified.qualify()[2] != self.description:
                    raise EngineError(ERRORS['model_changed'], 'local_model_changed')
                guard()
                return answer.strip()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            raise EngineError('本機分段模型回答未完成：' + str(exc), 'local_segment_failed') from exc


def main():
    parser = argparse.ArgumentParser(description='本次開啟使用已通過驗收的分段學習參數。')
    parser.add_argument('--qualified-segment', action='store_true', required=True)
    parser.add_argument('--ask')
    parser.add_argument('--session')
    parser.add_argument('--data-dir', type=Path, default=qualified.ROOT / 'data')
    args = parser.parse_args()
    if args.session and not args.ask:
        parser.error('--session 需搭配 --ask。')
    try:
        engine = SegmentEngine(args.data_dir)
        store = Store(args.data_dir / 'assistant.sqlite3')
        if args.ask:
            sid = args.session or store.new_session()
            print(Assistant(store, engine).ask(sid, args.ask)['content'])
            print('Session:', sid)
        else:
            from assistant.gui import run
            run(store, engine)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(1, '分段模型未完成：' + str(exc) + '\n')


if __name__ == '__main__':
    main()

