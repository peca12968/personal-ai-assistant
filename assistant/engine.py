from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .context import followup_query, make_context, select_excerpts
from .documents import check_citations, retrieve_documents
from .tasks import task_context
from .communication import conversation_style
from .evolution_status import improvement_context
from .memory_commands import handle_command, parse_command
from . import answer_runs

ROOT = Path(__file__).resolve().parents[1]
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MAX_ANSWER_BYTES = 2 * 1024 * 1024


class EngineError(RuntimeError):
    def __init__(self, message, code='engine_error'):
        super().__init__(message)
        self.code = code


def find_codex():
    executable = shutil.which("codex")
    if executable:
        return executable
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise EngineError("找不到 Codex CLI。請先安裝 Codex 並執行 codex login。")


def render_prompt(context):
    instructions = (ROOT / "prompts/system.txt").read_text(encoding="utf-8")
    instructions += '\n' + (ROOT / 'prompts/communication.txt').read_text(encoding='utf-8')
    return instructions + "\n\n以下 JSON 是使用者資料與當前問題：\n" + json.dumps(context, ensure_ascii=False)


def build_prompt(question, history, memories, brief=None, excerpts=None):
    return render_prompt(make_context(question, history, memories, brief, excerpts))


class CodexEngine:
    def __init__(self, data_dir: Path, timeout=240):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('引擎逾時需為有限的正秒數。')
        # CLI -C and output paths must remain valid after Popen changes cwd.
        self.workdir = Path(data_dir).resolve() / "engine-workspace"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def command(self, output):
        cmd = [find_codex(), "exec", "--ignore-user-config", "--ephemeral",
               "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
               "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
               "-c", "project_doc_max_bytes=0",
               "-C", str(self.workdir), "--output-last-message", str(output)]
        # This assistant produces advice only; tools belong in a later, scoped execution mode.
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent",
                        "browser_use", "computer_use", "image_generation", "skill_search"):
            cmd.extend(["--disable", feature])
        return cmd + ["-"]

    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Still try to terminate the exact process we created.
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def answer(self, prompt, cancel: threading.Event | None = None):
        deadline = time.monotonic() + self.timeout

        def check_running():
            if cancel and cancel.is_set():
                raise EngineError('已停止這次回答。', 'cancelled')
            if time.monotonic() >= deadline:
                raise EngineError('回答逾時（含啟動與傳送問題），請稍後重試或縮短內容。', 'timeout')

        check_running()
        if not isinstance(prompt, str):
            raise EngineError('推理輸入必須是文字。', 'invalid_input')
        try:
            payload = prompt.encode('utf-8')
        except UnicodeError:
            raise EngineError('推理輸入包含無效文字。', 'invalid_input') from None
        if len(payload) > MAX_PROMPT_BYTES:
            raise EngineError('本次參考內容超過 2 MiB，請減少文件或縮短內容。', 'input_too_large')
        try:
            return self._answer(payload, check_running)
        except OSError:
            raise EngineError('無法讀寫本機推理暫存資料，請檢查磁碟空間與存取權限。', 'local_io') from None

    def _answer(self, payload, check_running):
        with tempfile.TemporaryDirectory(prefix="answer-", dir=self.workdir) as tmp:
            output = Path(tmp) / "answer.txt"
            # A seekable stdin file avoids a blocked parent-side pipe write when the
            # child never reads. All temporary prompt/output files close before cleanup.
            with tempfile.TemporaryFile(dir=tmp) as source, (Path(tmp) / "stdout.log").open("wb") as stdout, (Path(tmp) / "stderr.log").open("wb") as stderr:
                source.write(payload)
                source.seek(0)
                check_running()
                command = self.command(output)
                check_running()
                try:
                    process = subprocess.Popen(command, stdin=source,
                        stdout=stdout, stderr=stderr, cwd=self.workdir,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except OSError:
                    raise EngineError('無法啟動推理引擎，請確認 Codex CLI 仍可執行，或執行 --doctor 檢查。', 'launch_failed') from None
                try:
                    while process.poll() is None:
                        check_running()
                        time.sleep(0.15)
                    check_running()  # Cancellation/timeout also wins over a just-finished output.
                    if process.returncode != 0:
                        raise EngineError(f'推理引擎提前結束（退出碼 {process.returncode}），沒有保存答案。請檢查登入、網路或使用額度；目前無法確認具體原因。', 'process_failed')
                    if not output.exists():
                        raise EngineError('推理引擎未傳回答案，請重試。', 'empty_response')
                    with output.open('rb') as result:
                        raw = result.read(MAX_ANSWER_BYTES + 1)
                    if len(raw) > MAX_ANSWER_BYTES:
                        raise EngineError('引擎回覆超過 2 MiB，未保存為完成答案；請縮小需求。', 'output_too_large')
                    try:
                        answer = raw.decode('utf-8').strip()
                    except UnicodeError:
                        raise EngineError('引擎回覆不是有效 UTF-8 文字，未保存答案。', 'invalid_response') from None
                    if not answer:
                        raise EngineError('推理引擎傳回空白答案，請重試。', 'empty_response')
                    check_running()
                    return answer
                finally:
                    try:
                        self.stop(process)
                        process.wait(timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        raise EngineError('已要求停止引擎，但尚無法確認程序結束；本次不標記完成。', 'cleanup_unconfirmed') from None


class Assistant:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine

    def prepare_context(self, sid, question, exclude_message_id=None):
        history = self.store.messages(sid, 16, exclude_local=True, exclude_message_id=exclude_message_id)
        memories = self.store.retrieve(question)
        followup = followup_query(question, history)
        if followup:
            seen = {row["id"] for row in memories}
            for row in self.store.retrieve(followup):
                if row["id"] not in seen:
                    memories.append(row)
                    seen.add(row["id"])
        older = self.store.earlier_user_messages(sid, history[0]["id"], exclude_local=True, exclude_message_id=exclude_message_id) if history else []
        context = make_context(question, history, memories, self.store.session_brief(sid), select_excerpts(question, older))
        documents = self.store.documents(sid, include_content=True)
        context["documents"] = retrieve_documents(question + ("\n" + followup if followup else ""), documents)
        context['task_runs'] = task_context(self.store, sid, question)
        context['communication'] = conversation_style(question, history)
        context['self_improvement'] = improvement_context(ROOT, question)
        context['answer_attempts'] = answer_runs.context_status(self.store, sid)
        context['conversation_tail'] = answer_runs.conversation_tail(self.store, sid)
        return context

    def ask(self, sid, question, cancel=None):
        return self._ask(sid, question, cancel)

    def retry(self, sid, cancel=None):
        target = answer_runs.retry_target(self.store, sid)
        return self._ask(sid, target['question'], cancel, retry=target)

    def _ask(self, sid, question, cancel=None, retry=None):
        if cancel and cancel.is_set():
            raise EngineError('已停止這次回答。', 'cancelled')
        question = question.strip()
        if not question or len(question) > 16000:
            raise ValueError("問題需為 1–16000 字。")
        command = parse_command(question)
        if command is not None and retry is None:
            return handle_command(self.store, sid, question, command, cancel)
        answer_runs.recover(self.store, sid)
        context = self.prepare_context(sid, question, exclude_message_id=retry['user_message_id'] if retry else None)
        if retry:
            context['retry_of'] = {'run_id': retry['id'], 'previous_status': retry['status'], 'uses_current_references': True}
        run_id = uuid.uuid4().hex
        with answer_runs.lease(self.store, run_id):
            uid = answer_runs.begin(self.store, sid, run_id, question, context, retry)
            try:
                answer = self.engine.answer(render_prompt(context), cancel)
                if cancel and cancel.is_set():
                    raise EngineError('已停止這次回答。', 'cancelled')
                checks = check_citations(answer, context['documents']['excerpts'])
                if any(not check['range_valid'] for check in checks):
                    answer += '\n\n來源檢查：有引用無法對照本次文件片段，請勿將該引用視為已驗證。'
                mid = answer_runs.finish(self.store, run_id, answer, context, checks)
            except Exception as exc:
                answer_runs.fail(self.store, run_id, getattr(exc, 'code', 'unexpected_error'))
                raise
        return {"id": mid, "content": answer, "memory_ids": [m["id"] for m in context["memories"]],
                "excerpt_ids": [row["message_id"] for row in context["earlier_user_excerpts"]],
                "document_count": len(context["documents"]["catalog"]), "citation_checks": checks,
                'run_id': run_id, 'user_message_id': uid}
