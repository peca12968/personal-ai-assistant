from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .context import followup_query, make_context, select_excerpts
from .documents import check_citations, retrieve_documents
from .tasks import task_context
from .communication import conversation_style
from .evolution_status import improvement_context

ROOT = Path(__file__).resolve().parents[1]


class EngineError(RuntimeError):
    pass


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
        self.workdir = Path(data_dir) / "engine-workspace"
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
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process.kill()

    def answer(self, prompt, cancel: threading.Event | None = None):
        with tempfile.TemporaryDirectory(prefix="answer-", dir=self.workdir) as tmp:
            output = Path(tmp) / "answer.txt"
            # Write stdout/stderr to temporary files to prevent pipe-buffer deadlocks.
            with (Path(tmp) / "stdout.log").open("wb") as stdout, (Path(tmp) / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(self.command(output), stdin=subprocess.PIPE,
                    stdout=stdout, stderr=stderr, cwd=self.workdir,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    process.stdin.close()
                    deadline = time.monotonic() + self.timeout
                    while process.poll() is None:
                        if cancel and cancel.is_set():
                            raise EngineError("已停止這次回答。")
                        if time.monotonic() >= deadline:
                            raise EngineError("回答逾時，請縮短問題後重試。")
                        time.sleep(0.15)
                    if process.returncode != 0:
                        raise EngineError("Codex 未完成回答。請執行 codex login status 確認登入，並檢查網路或使用額度。")
                    if not output.exists() or not output.read_text(encoding="utf-8").strip():
                        raise EngineError("推理引擎未傳回答案，請重試。")
                    return output.read_text(encoding="utf-8").strip()
                finally:
                    self.stop(process)
                    process.wait(timeout=10)


class Assistant:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine

    def prepare_context(self, sid, question):
        history = self.store.messages(sid, 16)
        memories = self.store.retrieve(question)
        followup = followup_query(question, history)
        if followup:
            seen = {row["id"] for row in memories}
            for row in self.store.retrieve(followup):
                if row["id"] not in seen:
                    memories.append(row)
                    seen.add(row["id"])
        older = self.store.earlier_user_messages(sid, history[0]["id"]) if history else []
        context = make_context(question, history, memories, self.store.session_brief(sid), select_excerpts(question, older))
        documents = self.store.documents(sid, include_content=True)
        context["documents"] = retrieve_documents(question + ("\n" + followup if followup else ""), documents)
        context['task_runs'] = task_context(self.store, sid, question)
        context['communication'] = conversation_style(question, history)
        context['self_improvement'] = improvement_context(ROOT, question)
        return context

    def ask(self, sid, question, cancel=None):
        question = question.strip()
        if not question or len(question) > 16000:
            raise ValueError("問題需為 1–16000 字。")
        context = self.prepare_context(sid, question)
        self.store.add_message(sid, "user", question)
        answer = self.engine.answer(render_prompt(context), cancel)
        checks = check_citations(answer, context["documents"]["excerpts"])
        if any(not check["range_valid"] for check in checks):
            answer += "\n\n來源檢查：有引用無法對照本次文件片段，請勿將該引用視為已驗證。"
        mid = self.store.add_answer(sid, answer, context, checks)
        return {"id": mid, "content": answer, "memory_ids": [m["id"] for m in context["memories"]],
                "excerpt_ids": [row["message_id"] for row in context["earlier_user_excerpts"]],
                "document_count": len(context["documents"]["catalog"]), "citation_checks": checks}
