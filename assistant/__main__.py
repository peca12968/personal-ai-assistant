import argparse
import json
import sys
import sqlite3
import zipfile
from pathlib import Path

from .engine import Assistant, CodexEngine, EngineError, ROOT, find_codex
from .store import Store


def main():
    # Stable Unicode for JSON pipelines on Windows; pythonw has no console streams.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="個人 AI 助理")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ask", help="單次提問")
    mode.add_argument('--retry-answer', action='store_true', help='重試指定對話最後一題，需 --session；不重複新增問題')
    mode.add_argument('--show-companion', action='store_true', help='查看本機夥伴設定，不呼叫模型')
    mode.add_argument('--set-companion', type=Path, help='從UTF-8 JSON儲存夥伴設定；需 --profile-revision')
    mode.add_argument('--reset-companion', action='store_true', help='恢復預設並儲存；需 --profile-revision')
    parser.add_argument('--profile-revision', type=int, help='設定讀取時的revision，避免覆寫其他視窗的更新')
    mode.add_argument('--show-memory-aliases', action='store_true', help='查看本機記憶搜尋稱呼，不呼叫模型')
    mode.add_argument('--set-memory-aliases', type=Path, help='從JSON儲存記憶搜尋稱呼；需 --aliases-revision，空清單可撤回')
    parser.add_argument('--aliases-revision', type=int, help='從 --show-memory-aliases 取得的revision')
    mode.add_argument("--backup", action="store_true", help="建立目前資料的本機備份")
    mode.add_argument("--inspect-backup", type=Path, help="檢查備份完整性與內容筆數")
    mode.add_argument("--restore", type=Path, help="還原備份到 --data-dir 指定的新資料夾")
    mode.add_argument("--search-folder", type=Path, help="在指定資料夾本機唯讀搜尋，不呼叫模型")
    mode.add_argument('--plan-csv', type=Path, help='建立 CSV 分組加總計畫，尚不執行')
    mode.add_argument('--plan-workflow', type=Path, help='依 --workflow-spec 建立 CSV 多步計畫')
    mode.add_argument('--propose-csv', type=Path, help='依 --request 請模型規劃 CSV，尚不執行')
    parser.add_argument('--workflow-spec', type=Path)
    parser.add_argument('--request', help='搭配 --propose-csv 的工作需求')
    mode.add_argument('--run-task', help='執行已建立的任務 ID；需 --session')
    mode.add_argument('--task-status', help='讀取任務結果；需 --session')
    mode.add_argument('--new-goal', help='保存自主目標；需 --session，使用目前已匯入的參考文件')
    mode.add_argument('--draft-goal', help='依目前輸入與指定對話整理目標草稿；需 --session，輸出JSON但不保存或開始工作')
    parser.add_argument('--from-chat', action='store_true', help='搭配 --new-goal，固定帶入同對話的有限近期討論、更早更正及已保存目標')
    parser.add_argument('--history-search', action='store_true', help='搭配 --new-goal 與 --from-chat，另存當時本對話的有界歷史供自主查找')
    parser.add_argument('--csv-document', type=int, help='搭配 --new-goal，選取此對話已匯入CSV編號，允許開始後自主規劃及執行分析')
    mode.add_argument('--export-goal-csv', help='匯出已完成自主CSV計畫ID；需 --csv-goal、--session、--output')
    parser.add_argument('--csv-goal', help='搭配 --export-goal-csv 指定所屬目標ID')
    mode.add_argument('--review-goals', action='store_true', help='本機跨對話回顧自主目標，不呼叫模型或啟動工作')
    parser.add_argument('--review-hidden', action='store_true', help='搭配 --review-goals，也列出暫時收起項目')
    parser.add_argument('--review-offset', type=int, help='搭配 --review-goals，跳過指定數量，每頁100項')
    parser.add_argument('--deliver-files', action='store_true', help='搭配 --new-goal，要求在該目標本機成果資料夾交付Markdown、TXT或JSON')
    mode.add_argument('--rebuild-goal-files', help='核對／重建指定目標已保存文件；需 --session，不呼叫模型')
    parser.add_argument('--from-goal', help='搭配 --new-goal，帶入同對話已交付成果，或已有步驟且已停止目標的有限進度及使用者補充；不自動開始')
    mode.add_argument('--run-goal', help='開始／接續自主目標 ID；需 --session')
    parser.add_argument('--continuous-steps', type=int, help='搭配 --run-goal，持續執行新增最多1到32步；預設30分鐘，累計仍最多32步')
    parser.add_argument('--continuous-minutes', type=int, help='搭配 --continuous-steps，時間上限1到120分鐘')
    mode.add_argument('--goal-status', help='讀取自主目標與行動紀錄；需 --session')
    mode.add_argument('--reply-goal', help='補充自主目標問題；需 --session、--goal-reply 與 --goal-fingerprint')
    parser.add_argument('--goal-reply', help='搭配 --reply-goal 的回答')
    mode.add_argument('--goal-guidance-status', '--goal-reply-status', help='查看目標、目前問題、補充與指紋；需 --session')
    mode.add_argument('--amend-goal', help='追加目標更正；需 --session、--goal-guidance、--goal-fingerprint')
    parser.add_argument('--goal-guidance', help='追加到目標的使用者更正或條件')
    parser.add_argument('--goal-fingerprint', help='從 --goal-guidance-status 或 --goal-reply-status 取得的指紋')
    parser.add_argument('--goal-steps', type=int, help='搭配 --run-goal，每輪最多1到12步，預設6步')
    mode.add_argument('--export-task', help='匯出已完成報告；需 --session 與 --output')
    mode.add_argument('--improve', help='指定方向，自我修改限定程式、驗證並發布一輪')
    mode.add_argument('--rollback-improvement', help='回復仍為最新版本的自我改進紀錄 ID 並發布')
    parser.add_argument('--edit', action='append', help='搭配 --improve 指定可修改模組，可重複')
    parser.add_argument('--acceptance', type=Path, help='搭配 --improve 指定 JSON 驗收案例，核對改前／改後')
    parser.add_argument('--holdout', type=Path, help='搭配 --improve 與 --acceptance 的保留案例，不加入提案模型提示')
    parser.add_argument('--group-column')
    parser.add_argument('--value-column')
    parser.add_argument('--output', type=Path)
    parser.add_argument("--query", help="搭配 --search-folder 的搜尋詞")
    parser.add_argument("--session", help="延續指定對話 ID")
    mode.add_argument("--doctor", action="store_true", help="檢查環境")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument('--engine', choices=('codex', 'local'), default='codex', help='選用推理引擎；local為需另裝環境的本機Qwen3-4B實驗引擎')
    parser.add_argument('--local-experiment', help='搭配--engine local，明確選用已驗證研究參數；預設為未微調底座')
    parser.add_argument('--local-batch', help='搭配--engine local，選用此資料目錄內仍有效的已驗證確認批次')
    parser.add_argument('--local-active', action='store_true', help='搭配--engine local，載入明確保存的模型選擇；變更後需重新開啟')
    parser.add_argument("--file", type=Path, action="append", default=[], help="匯入文字文件供本次對話分析；可重複指定")
    parser.add_argument('--image', type=Path, action='append', default=[], help='搭配--ask匯入並附上PNG／JPEG，可重複指定，最多4張')
    args = parser.parse_args()
    if any((args.local_experiment is not None,args.local_batch is not None,args.local_active)) and args.engine != 'local':
        parser.error('本機參數選項需搭配--engine local。')
    if sum((args.local_experiment is not None,args.local_batch is not None,args.local_active))>1:
        parser.error('--local-experiment、--local-batch與--local-active只能選一個。')
    if args.engine == 'local' and (args.improve or args.rollback_improvement):
        parser.error('程式自我改進目前使用Codex；本機參數學習是另一個流程，不能混用--engine local。')
    def make_engine():
        if args.engine == 'local':
            from .local_engine import LocalEngine
            return LocalEngine(args.data_dir, experiment=args.local_experiment,batch=args.local_batch,active=args.local_active)
        return CodexEngine(args.data_dir)
    if args.image and (not args.ask or len(args.image) > 4):
        parser.error('--image需搭配--ask，最多4張；重試沿用原題圖片。')
    alias_change = args.set_memory_aliases is not None
    alias_mode = args.show_memory_aliases or alias_change
    if alias_change and (args.aliases_revision is None or args.aliases_revision < 0):
        parser.error('儲存搜尋稱呼需要 --aliases-revision，請先用 --show-memory-aliases 查看。')
    if args.aliases_revision is not None and not alias_change:
        parser.error('--aliases-revision 只能搭配 --set-memory-aliases。')
    if alias_mode and (args.session or args.file):
        parser.error('搜尋稱呼套用此資料庫，不搭配 --session 或 --file。')
    if args.history_search and (args.new_goal is None or not args.from_chat):
        parser.error('--history-search需搭配 --new-goal 與 --from-chat。')
    if args.from_chat and args.new_goal is None:
        parser.error('--from-chat只能搭配 --new-goal。')
    if args.csv_document is not None and args.new_goal is None:
        parser.error('--csv-document只能搭配 --new-goal。')
    if args.csv_goal is not None and args.export_goal_csv is None:
        parser.error('--csv-goal只能搭配 --export-goal-csv。')
    if args.export_goal_csv is not None and not (args.csv_goal and args.session and args.output):
        parser.error('匯出自主CSV需要 --csv-goal、--session 與 --output。')
    if (args.review_hidden or args.review_offset is not None) and not args.review_goals:
        parser.error('回顧分頁選項只能搭配 --review-goals。')
    if args.review_goals and (args.session or args.file or (args.review_offset is not None and args.review_offset < 0)):
        parser.error('回顧涵蓋本機所有對話，不搭配 --session 或 --file；offset須非負數。')
    profile_change = args.set_companion is not None or args.reset_companion
    profile_mode = args.show_companion or profile_change
    if profile_change and (args.profile_revision is None or args.profile_revision < 0):
        parser.error('儲存／重設夥伴設定需要 --profile-revision，請先用 --show-companion 查看。')
    if args.profile_revision is not None and not profile_change:
        parser.error('--profile-revision 只能搭配儲存／重設夥伴設定。')
    if profile_mode and (args.session or args.file):
        parser.error('夥伴設定套用整份資料庫，不搭配 --session 或 --file。')
    autonomous = any(value is not None for value in (args.draft_goal, args.new_goal, args.run_goal, args.goal_status, args.reply_goal, args.rebuild_goal_files, args.goal_guidance_status, args.amend_goal))
    if args.amend_goal is not None:
        if args.goal_guidance is None or args.goal_fingerprint is None:
            parser.error('--amend-goal需要 --goal-guidance 與 --goal-fingerprint。')
    elif args.goal_guidance is not None or (args.goal_fingerprint is not None and args.reply_goal is None):
        parser.error('補充內容需搭配 --amend-goal；指紋需搭配 --amend-goal 或 --reply-goal。')
    if args.reply_goal is not None and args.goal_fingerprint is None:
        parser.error('--reply-goal需先以 --goal-reply-status 讀取問題，並提供 --goal-fingerprint。')
    if (args.amend_goal is not None or args.goal_guidance_status is not None or args.reply_goal is not None) and args.file:
        parser.error('補充目標不會匯入或替換來源，不能搭配 --file。')
    if autonomous and not args.session:
        parser.error('自主目標需要 --session 指定對話。')
    if args.from_goal is not None and args.new_goal is None:
        parser.error('--from-goal 只能搭配 --new-goal。')
    if args.deliver_files and args.new_goal is None:
        parser.error('--deliver-files 只能搭配 --new-goal。')
    if (args.reply_goal is not None) != (args.goal_reply is not None):
        parser.error('--reply-goal 與 --goal-reply 需一起使用。')
    if args.goal_steps is not None and (args.run_goal is None or not 1 <= args.goal_steps <= 12):
        parser.error('--goal-steps 需搭配 --run-goal，範圍1到12。')
    if args.continuous_steps is not None and (args.run_goal is None or args.goal_steps is not None or not 1 <= args.continuous_steps <= 32):
        parser.error('--continuous-steps需搭配 --run-goal，範圍1到32，不能混用 --goal-steps。')
    if args.continuous_minutes is not None and (args.continuous_steps is None or not 1 <= args.continuous_minutes <= 120):
        parser.error('--continuous-minutes需搭配 --continuous-steps，範圍1到120。')
    if args.retry_answer and (not args.session or args.file):
        parser.error('--retry-answer 需要 --session，不能同時匯入文件；會使用目前已保存的參考資料。')
    if bool(args.plan_workflow) != bool(args.workflow_spec):
        parser.error('--plan-workflow 與 --workflow-spec 需一起使用。')
    if bool(args.propose_csv) != bool(args.request):
        parser.error('--propose-csv 與 --request 需一起使用。')
    if (args.improve or args.rollback_improvement) and any((args.file, args.session, args.output,
                                                         args.group_column, args.value_column, args.query)):
        parser.error('自我改進／回復不能混用對話、文件或 CSV 工作參數。')
    if args.edit and not args.improve:
        parser.error('--edit 需搭配 --improve。')
    if args.acceptance and not args.improve:
        parser.error('--acceptance 需搭配 --improve。')
    if args.holdout and (not args.improve or not args.acceptance):
        parser.error('--holdout需搭配 --improve 與 --acceptance。')
    if args.rollback_improvement:
        from .evolution import rollback
        try:
            print(json.dumps(rollback(args.rollback_improvement), ensure_ascii=False, indent=2))
        except Exception as exc:
            parser.exit(1, '版本回復未完成：' + str(exc) + '\n')
        return
    if args.improve:
        from .evolution import improve
        from .acceptance import load_suite
        try:
            suite = load_suite(args.acceptance) if args.acceptance else None
            withheld = load_suite(args.holdout) if args.holdout else None
            result = improve(args.improve, args.edit, progress=lambda state: print(state, flush=True), acceptance=suite, holdout=withheld)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            parser.exit(1, '自我改進未完成：' + str(exc) + '\n')
        return
    if args.file and not args.ask:
        parser.error("--file 需搭配 --ask；桌面版請使用「參考文件」。")
    if bool(args.search_folder) != bool(args.query):
        parser.error('--search-folder 與 --query 需一起使用。')
    if (args.group_column is not None or args.value_column is not None) and not args.plan_csv:
        parser.error('欄位參數僅搭配 --plan-csv。')
    if args.output and not (args.export_task or args.export_goal_csv):
        parser.error('--output 僅搭配 --export-task 或 --export-goal-csv。')
    if args.plan_csv or args.plan_workflow or args.propose_csv or args.run_task or args.task_status or args.export_task:
        from .tasks import execute_task, export_task, plan_csv
        if args.plan_csv and (not args.group_column or not args.value_column):
            parser.error('建立計畫需 --group-column 與 --value-column。')
        if not (args.plan_csv or args.plan_workflow or args.propose_csv) and not args.session:
            parser.error('操作既有任務需 --session。')
        if args.export_task and not args.output:
            parser.error('匯出需 --output 指定新 JSON 檔案。')
        try:
            store = Store(args.data_dir / 'assistant.sqlite3')
            sid = args.session or store.new_session('CSV 工作')
            if args.plan_csv:
                result = plan_csv(store, sid, args.plan_csv, args.group_column, args.value_column)
            elif args.plan_workflow:
                from .workflows import plan_workflow, read_spec
                result = plan_workflow(store, sid, args.plan_workflow, read_spec(args.workflow_spec))
            elif args.propose_csv:
                from .workflows import propose_workflow
                result = propose_workflow(store, sid, args.propose_csv, args.request, make_engine())
                result['session_id'] = sid
            elif args.run_task:
                result = execute_task(store, sid, args.run_task)
            elif args.export_task:
                result = export_task(store, sid, args.export_task, args.output)
            else:
                result = store.task(sid, args.task_status)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.run_task and result['status'] != 'succeeded':
                parser.exit(1)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            parser.exit(1, '工作未完成：' + str(exc) + '\n')
        return
    if args.search_folder:
        from .folder_search import search_folder
        try:
            print(json.dumps(search_folder(args.search_folder, args.query), ensure_ascii=False, indent=2))
        except (ValueError, OSError) as exc:
            parser.exit(1, '搜尋失敗：' + str(exc) + '\n')
        return
    if args.backup or args.inspect_backup or args.restore:
        from .backup import create_backup, inspect_backup, restore_backup
        try:
            if args.backup:
                result = create_backup(args.data_dir / 'assistant.sqlite3')
            elif args.inspect_backup:
                result = inspect_backup(args.inspect_backup)
            else:
                result = restore_backup(args.restore, args.data_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, TimeoutError) as exc:
            parser.exit(1, '備份／還原失敗：' + str(exc) + '\n')
        return
    if args.doctor:
        print("Project:", ROOT)
        print("Data:", args.data_dir.resolve())
        if args.engine == 'local':
            try:
                from .local_engine import LocalEngine
                selected = make_engine()
                print('Local model:', json.dumps(selected.describe(), ensure_ascii=False))
                print('Optional runtime present:', selected.python.is_file())
                print('完整套件、模型雜湊與GPU檢查會在實際回答前執行；本次沒有載入模型。')
            except (ValueError, OSError, RuntimeError) as exc:
                parser.exit(1, '本機引擎檢查未完成：' + str(exc) + '\n')
        else:
            print("Codex:", find_codex())
        try:
            import PIL
            print('Image decoder: Pillow', PIL.__version__)
        except ImportError:
            print('Image decoder: missing; run .venv\\Scripts\\python -m pip install -r requirements.txt')
        try:
            import pypdf
            print('PDF text decoder: pypdf', pypdf.__version__)
        except ImportError:
            print('PDF text decoder: missing; run .venv\\Scripts\\python -m pip install -r requirements.txt')
        return
    if args.export_goal_csv is not None:
        from .autonomy_csv import export
        try:
            receipt = export(Store(args.data_dir / 'assistant.sqlite3'), args.session, args.csv_goal,
                             args.export_goal_csv, args.output)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            parser.exit(1, 'CSV匯出未完成：' + str(exc) + '\n')
        return
    if args.review_goals:
        from .goal_review import snapshot
        try:
            result = snapshot(Store(args.data_dir / 'assistant.sqlite3'), include_hidden=args.review_hidden,
                              offset=args.review_offset or 0)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, '回顧未完成：' + str(exc) + '\n')
        return
    if alias_mode:
        from . import memory_aliases
        try:
            store = Store(args.data_dir / 'assistant.sqlite3')
            result = (memory_aliases.save(store, memory_aliases.read_file(args.set_memory_aliases),
                      expected_revision=args.aliases_revision) if alias_change else memory_aliases.get(store))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, '搜尋稱呼未更新：' + str(exc) + '\n')
        return
    if profile_mode:
        from .companion_profile import get_profile, read_settings, reset_profile, save_profile
        try:
            store = Store(args.data_dir / 'assistant.sqlite3')
            if args.set_companion:
                result = save_profile(store, read_settings(args.set_companion), expected_revision=args.profile_revision)
            elif args.reset_companion:
                result = reset_profile(store, expected_revision=args.profile_revision)
            else:
                result = get_profile(store)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, '夥伴設定未完成：' + str(exc) + '\n')
        return
    if autonomous:
        from .autonomy import create_goal, get_goal, reply, run_goal
        try:
            store = Store(args.data_dir / 'assistant.sqlite3')
            if args.draft_goal is not None:
                from .goal_drafts import capture, generate
                result = generate(capture(store, args.session, args.draft_goal), make_engine())
            elif args.new_goal is not None:
                gid = create_goal(store, args.session, args.new_goal, parent_id=args.from_goal, file_delivery=args.deliver_files, csv_document_id=args.csv_document, from_chat=args.from_chat, history_search=args.history_search)
                result = get_goal(store, args.session, gid)
            elif args.run_goal is not None:
                if args.continuous_steps is not None:
                    from .continuous import run_continuous
                    result = run_continuous(store, args.session, args.run_goal, make_engine(),
                        max_steps=args.continuous_steps, max_seconds=(args.continuous_minutes or 30) * 60)
                else:
                    result = run_goal(store, args.session, args.run_goal, make_engine(),
                                      max_steps=args.goal_steps or 6)
            elif args.reply_goal is not None:
                reply(store, args.session, args.reply_goal, args.goal_reply, expected_fingerprint=args.goal_fingerprint)
                result = get_goal(store, args.session, args.reply_goal)
            elif args.rebuild_goal_files is not None:
                from .artifacts import rebuild
                result = rebuild(store, args.session, args.rebuild_goal_files)
            elif args.goal_guidance_status is not None:
                from .goal_guidance import snapshot
                result = snapshot(store, args.session, args.goal_guidance_status)
            elif args.amend_goal is not None:
                from .goal_guidance import save
                result = save(store, args.session, args.amend_goal, args.goal_guidance, expected_fingerprint=args.goal_fingerprint)
            else:
                result = get_goal(store, args.session, args.goal_status)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            parser.exit(1, '自主工作未完成：' + str(exc) + '\n')
        return
    try:
        engine = make_engine()
        store = Store(args.data_dir / "assistant.sqlite3")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, '助理未啟動：' + str(exc) + '\n')
    if args.ask or args.retry_answer:
        try:
            sid = args.session or store.new_session()
            for path in args.file:
                store.attach_document(sid, path)
            app = Assistant(store, engine)
            if args.image:
                from .image_inputs import attach_paths
                image_ids = attach_paths(store, sid, args.image)
                result = app.ask(sid, args.ask, image_ids=image_ids)
            else:
                result = app.retry(sid) if args.retry_answer else app.ask(sid, args.ask)
        except (EngineError, ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, '回答未完成：' + str(exc) + '\n')
        print(result["content"])
        print("\nSession:", sid)
    else:
        from .gui import run
        run(store, engine)


if __name__ == "__main__":
    main()
