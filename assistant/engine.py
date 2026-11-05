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
from contextlib import contextmanager
from pathlib import Path

from .context import correction_context, followup_query, make_context, select_excerpts
from .documents import check_citations, retrieve_documents
from .tasks import task_context
from .communication import conversation_style
from .evolution_status import improvement_context
from .memory_commands import handle_command, parse_command
from . import answer_feedback
from . import answer_runs
from .memory_aliases import GUIDANCE as MEMORY_ALIAS_GUIDANCE
from .autonomy_status import goal_context
from .companion_profile import GUIDANCE as COMPANION_GUIDANCE, get_profile

ROOT = Path(__file__).resolve().parents[1]
MAX_QUESTION_CHARS = 16000
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MAX_ANSWER_BYTES = 2 * 1024 * 1024


class EngineError(RuntimeError):
    def __init__(self, message, code='engine_error'):
        super().__init__(message)
        self.code = code


@contextmanager
def answer_directory(workdir):
    temporary = tempfile.TemporaryDirectory(prefix='answer-', dir=workdir)
    original_error = None
    try:
        yield temporary.name
    except Exception as exc:
        original_error = exc
        raise
    finally:
        # Windows may briefly retain a file handle after process exit. Retry only
        # cleanup of this owned directory, with a short finite bound.
        for attempt in range(5):
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.1)
                    continue
            except OSError:
                pass
            prefix = str(original_error) + ' ' if isinstance(original_error, EngineError) else ''
            raise EngineError(prefix + '推理暫存資料未能完全清理；本次不標記完成。', 'temp_cleanup_failed') from None


def find_codex():
    executable = shutil.which("codex")
    if executable:
        return executable
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise EngineError("找不到 Codex CLI。請先安裝 Codex 並執行 codex login。")


def render_prompt_parts(context):
    instructions = (ROOT / "prompts/system.txt").read_text(encoding="utf-8")
    from .response_delivery import GUIDANCE, PROMPT_MARKER, FOCUSED_GUIDANCE, POLICY_MARKER, focused_help, policy_reference
    from .prompt_scope import (TRANSCRIPTION_GUIDANCE, transcription_focus, transcription_format,
                              OUTER_GUIDANCE, transcription_source, SOURCE_GUIDANCE, transcription_block)
    policy = policy_reference(context)
    copy_focus = transcription_focus(context)
    copy_format = transcription_format(context) if copy_focus else None
    copy_source = transcription_source(context) if copy_focus else None
    if 'transcription_format' in context and context['transcription_format'] != copy_format:
        copy_focus = False
    if 'transcription_source' in context and context['transcription_source'] != copy_source:
        copy_focus = False
    if copy_focus:
        instructions = TRANSCRIPTION_GUIDANCE
    else:
        instructions = FOCUSED_GUIDANCE if policy is not None and focused_help(context) else instructions.replace(PROMPT_MARKER, GUIDANCE)
    instructions += '\n' + (ROOT / 'prompts/communication.txt').read_text(encoding='utf-8')
    instructions += '\n' + COMPANION_GUIDANCE
    instructions += '\n' + answer_feedback.GUIDANCE
    instructions += '\n' + MEMORY_ALIAS_GUIDANCE
    if policy is not None:
        instructions += POLICY_MARKER + json.dumps(policy, ensure_ascii=False)
    instructions += ('\n交付檢查：遵守本輪明確格式；逐字照抄或轉錄只輸出指定內容，界定範圍的引號不算內容。'
                     '精簡仍須回答各個所問部分，包括適用範圍與判斷所需證據。')
    if copy_focus and copy_format is not None:
        instructions += '\n' + OUTER_GUIDANCE[copy_format['outer_quotes']]
    literal_data = ''
    if copy_focus and copy_source is not None and context.get('transcription_source') == copy_source:
        instructions += '\n' + SOURCE_GUIDANCE
        literal_data = transcription_block(context) or ''
    data = literal_data + "\n\n以下 JSON 是使用者資料與當前問題：\n" + json.dumps(context, ensure_ascii=False)
    return instructions, data


def render_prompt(context):
    return ''.join(render_prompt_parts(context))


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
        # Model output is text only. Scoped autonomy tools are dispatched by the host.
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

    def answer_images(self, prompt, images, cancel=None):
        return self.answer(prompt, cancel, images=images)

    def answer(self, prompt, cancel: threading.Event | None = None, *, images=()):
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
        from .image_inputs import MAX_ATTACHMENTS, inspect_bytes
        if not isinstance(images, (tuple, list)) or len(images) > MAX_ATTACHMENTS:
            raise EngineError('每次最多附上4張圖片。', 'invalid_input')
        if images:
            images = tuple(images)
            for data in images:
                check_running()
                inspect_bytes(data)
        try:
            return self._answer(payload, check_running, images) if images else self._answer(payload, check_running)
        except OSError:
            raise EngineError('無法讀寫本機推理暫存資料，請檢查磁碟空間與存取權限。', 'local_io') from None

    def _answer(self, payload, check_running, images=()):
        with answer_directory(self.workdir) as tmp:
            output = Path(tmp) / "answer.txt"
            # A seekable stdin file avoids a blocked parent-side pipe write when the
            # child never reads. All temporary prompt/output files close before cleanup.
            with tempfile.TemporaryFile(dir=tmp) as source, (Path(tmp) / "stdout.log").open("wb") as stdout, (Path(tmp) / "stderr.log").open("wb") as stderr:
                source.write(payload)
                source.seek(0)
                check_running()
                command = self.command(output)
                if images:
                    from .image_inputs import inspect_bytes
                    if command[-1] != '-':
                        raise EngineError('圖片引擎命令缺少標準輸入位置。', 'invalid_input')
                    flags = []
                    for position, data in enumerate(images, 1):
                        check_running()
                        extension = 'png' if inspect_bytes(data)['format'] == 'PNG' else 'jpg'
                        target = Path(tmp) / f'image-{position:02}.{extension}'
                        target.write_bytes(data)
                        # Generated relative names avoid CLI comma splitting of original paths.
                        flags.extend(['--image', str(target.relative_to(self.workdir))])
                    command = command[:-1] + flags + ['--', '-']
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
    def __init__(self, store, engine, *, allow_local_calculation=True, allow_local_transcription=True,
                 allow_local_evidence=True):
        self.store, self.engine = store, engine
        self.allow_local_calculation = allow_local_calculation
        if type(allow_local_transcription) is not bool:
            raise ValueError('本機轉錄開關需為布林值。')
        self.allow_local_transcription = allow_local_transcription
        if type(allow_local_evidence) is not bool:
            raise ValueError('本機條件核對開關需為布林值。')
        self.allow_local_evidence = allow_local_evidence

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
        excerpts = select_excerpts(question, older, followup=followup)
        context = make_context(question, history, memories, self.store.session_brief(sid), excerpts)
        corrections = self.store.earlier_correction_messages(sid, history[0]['id'], exclude_message_id) if history else []
        context['answer_feedback'] = answer_feedback.for_chat(self.store, sid, question)
        context['earlier_user_corrections'] = correction_context(question, corrections)
        documents = self.store.documents(sid, include_content=True)
        context["documents"] = retrieve_documents(question + ("\n" + followup if followup else ""), documents,
                                                  reference_query=question)
        context['task_runs'] = task_context(self.store, sid, question)
        context['autonomous_goals'] = goal_context(self.store, sid)
        context['communication'] = conversation_style(question, history)
        context['companion_profile'] = get_profile(self.store)
        context['self_improvement'] = improvement_context(ROOT, question)
        context['answer_attempts'] = answer_runs.context_status(self.store, sid)
        context['conversation_tail'] = answer_runs.conversation_tail(self.store, sid)
        describe = getattr(self.engine, 'describe', None)
        if callable(describe):
            context['engine_selection'] = describe()
        from .learning_queue import help_context
        learning_help = help_context(question + ('\n' + followup if followup else ''))
        if learning_help is not None:
            from .idle_learning import capabilities
            learning_help.update(capabilities())
            context['learning_capabilities'] = learning_help
        from .response_delivery import help_context as delivery_help_context
        if delivery_help := delivery_help_context(question):
            context['delivery_capabilities'] = delivery_help
        if not self.allow_local_calculation:
            context['calculation_mode'] = 'model_only_evaluation'
        if not self.allow_local_transcription:
            context['transcription_mode'] = 'model_only_evaluation'
        from .prompt_scope import transcription_format, transcription_source
        if copy_format := transcription_format(context):
            context['transcription_format'] = copy_format
        if copy_source := transcription_source(context):
            context['transcription_source'] = copy_source
        return context

    def ask(self, sid, question, cancel=None, *, draft=None, image_ids=()):
        return self._ask(sid, question, cancel, draft=draft, image_ids=image_ids)

    def retry(self, sid, cancel=None):
        target = answer_runs.retry_target(self.store, sid)
        return self._ask(sid, target['question'], cancel, retry=target)

    def _ask(self, sid, question, cancel=None, retry=None, draft=None, image_ids=()):
        if cancel and cancel.is_set():
            raise EngineError('已停止這次回答。', 'cancelled')
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise ValueError("問題需為 1–16000 字。")
        command = parse_command(question)
        if command is not None and retry is None:
            if image_ids:
                raise ValueError('本機記憶指令不能同時附圖；請先取消圖片選取。')
            return handle_command(self.store, sid, question, command, cancel, draft=draft)
        from .image_inputs import prepare, validate_metadata, GUIDANCE
        historical = None
        if retry:
            with self.store.connect() as db:
                saved = db.execute('SELECT context_json FROM answer_runs WHERE id=? AND session_id=?', (retry['id'], sid)).fetchone()
                if saved is None:
                    raise ValueError('原回答紀錄已不存在，沒有重試。')
                previous = json.loads(saved[0])
            historical = previous.get('image_inputs')
            if historical is not None:
                validate_metadata(historical)
                image_ids = tuple(item['id'] for item in historical)
        image_metadata, image_payloads = prepare(self.store, sid, image_ids, historical=historical)
        from . import chat_calculation
        calculation = chat_calculation.parse_request(question) if self.allow_local_calculation else None
        from . import document_calculation
        document_plan = (document_calculation.parse_live_request(question)
                         if self.allow_local_calculation and not image_metadata else None)
        from . import chat_transcription
        transcription = (chat_transcription.parse_request(question)
                         if self.allow_local_transcription and not image_metadata else None)
        from . import chat_evidence
        evidence = chat_evidence.parse_request(question) if self.allow_local_evidence else None
        if evidence is not None and image_metadata:
            raise ValueError('本機條件核對使用本輪文字，請先取消圖片選取。')
        if calculation is not None and image_metadata:
            raise ValueError('本機精確計算指令不能同時附圖；請先取消圖片選取。')
        if image_payloads and not callable(getattr(self.engine, 'answer_images', None)):
            raise ValueError('目前引擎不支援圖片，沒有送出問題或忽略附件。')
        answer_runs.recover(self.store, sid)
        if evidence is not None:
            context = make_context(question, [], [])
            context.update(response_mode=chat_evidence.MODE, evidence=evidence,
                           documents={'catalog': [], 'excerpts': []})
        elif transcription is not None:
            context = make_context(question, [], [])
            context.update(response_mode=chat_transcription.MODE,transcription=transcription,
                           documents={'catalog':[],'excerpts':[]})
        elif calculation is not None:
            context = make_context(question, [], [])
            context.update(response_mode='local_calculation', calculation=calculation,
                           documents={'catalog':[], 'excerpts':[]})
        elif document_plan is not None:
            previous_plan = previous.get(document_calculation.FIELD) if retry else None
            document_plan = document_calculation.bind_live_request(document_plan, previous_plan)
            sources = document_calculation.snapshot(self.store, sid, document_plan)
            document_plan['documents'] = sources
            context = make_context(question, [], [])
            context.update(response_mode=document_calculation.MODE, document_calculation=document_plan, documents=sources)
        else:
            context = self.prepare_context(sid, question, exclude_message_id=retry['user_message_id'] if retry else None)
        if image_metadata:
            context['image_inputs'] = image_metadata
            context['image_input_guidance'] = GUIDANCE
        if retry:
            context['retry_of'] = {'run_id': retry['id'], 'previous_status': retry['status'], 'uses_current_references': True}
        run_id = uuid.uuid4().hex
        with answer_runs.lease(self.store, run_id):
            uid = answer_runs.begin(self.store, sid, run_id, question, context, retry, draft=draft)
            try:
                delivery = None
                if evidence is not None:
                    try:
                        answer, receipt = chat_evidence.evaluate(evidence, cancel)
                    except ValueError as exc:
                        raise EngineError('本機條件核對未完成：' + str(exc), 'evidence_invalid') from None
                    context['evidence'] = receipt
                elif transcription is not None:
                    try:
                        answer,receipt = chat_transcription.evaluate(transcription,cancel)
                        chat_transcription.validate(receipt,answer,question)
                    except ValueError as exc:
                        raise EngineError('本機逐字轉錄未完成：'+str(exc),'transcription_invalid') from None
                    context['transcription'] = receipt
                elif calculation is not None:
                    try:
                        answer, receipt = chat_calculation.evaluate(calculation, cancel)
                    except ValueError as exc:
                        raise EngineError('本機精確計算未完成：' + str(exc), 'calculation_invalid') from None
                    context['calculation'] = receipt
                elif document_plan is not None:
                    try:
                        answer, receipt = document_calculation.evaluate(document_plan, cancel)
                    except ValueError as exc:
                        raise EngineError('本機文件計算未完成：' + str(exc), 'document_calculation_invalid') from None
                    context[document_calculation.FIELD] = receipt
                else:
                    if (self.allow_local_calculation and not image_payloads
                            and callable(getattr(self.engine, 'answer_with_delivery', None))):
                        answer, delivery = self.engine.answer_with_delivery(render_prompt(context), cancel)
                    else:
                        answer = (self.engine.answer_images(render_prompt(context), image_payloads, cancel) if image_payloads
                                  else self.engine.answer(render_prompt(context), cancel))
                if cancel and cancel.is_set():
                    raise EngineError('已停止這次回答。', 'cancelled')
                from .prompt_scope import transcription_focus
                # A citation-looking fragment in an explicit literal is data,
                # not a claim of support from an imported document.
                checks = [] if evidence is not None or transcription is not None or transcription_focus(context) else check_citations(answer, context['documents']['excerpts'])
                if any(not check['range_valid'] for check in checks):
                    answer += '\n\n來源檢查：有引用無法對照本次文件片段，請勿將該引用視為已驗證。'
                mid = answer_runs.finish(self.store, run_id, answer, context, checks,
                                         **({'delivery': delivery} if delivery is not None else {}))
            except Exception as exc:
                answer_runs.fail(self.store, run_id, getattr(exc, 'code', 'unexpected_error'))
                raise
        return {"id": mid, "content": answer, "memory_ids": [m["id"] for m in context["memories"]],
                "excerpt_ids": [row["message_id"] for row in context["earlier_user_excerpts"]],
                "document_count": len(context["documents"]["catalog"]), "citation_checks": checks,
                'run_id': run_id, 'user_message_id': uid,
                **({'response_mode':'local_calculation', 'calculation':receipt} if calculation is not None else {}),
                **({'response_mode': document_calculation.MODE, document_calculation.FIELD: receipt} if document_plan is not None else {}),
                **({'response_mode':chat_transcription.MODE, 'transcription':receipt} if transcription is not None else {}),
                **({'response_mode': chat_evidence.MODE, 'evidence': receipt} if evidence is not None else {}),
                **({'image_count': len(image_metadata)} if image_metadata else {})}
