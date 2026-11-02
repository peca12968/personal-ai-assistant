import argparse
import json
import sys
import sqlite3
import zipfile
from pathlib import Path

from .engine import Assistant, CodexEngine, ROOT, find_codex
from .store import Store


def main():
    # Stable Unicode for JSON pipelines on Windows; pythonw has no console streams.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="個人 AI 助理")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ask", help="單次提問")
    mode.add_argument("--backup", action="store_true", help="建立目前資料的本機備份")
    mode.add_argument("--inspect-backup", type=Path, help="檢查備份完整性與內容筆數")
    mode.add_argument("--restore", type=Path, help="還原備份到 --data-dir 指定的新資料夾")
    mode.add_argument("--search-folder", type=Path, help="在指定資料夾本機唯讀搜尋，不呼叫模型")
    mode.add_argument('--plan-csv', type=Path, help='建立 CSV 分組加總計畫，尚不執行')
    mode.add_argument('--run-task', help='執行已建立的任務 ID；需 --session')
    mode.add_argument('--task-status', help='讀取任務結果；需 --session')
    mode.add_argument('--export-task', help='匯出已完成報告；需 --session 與 --output')
    mode.add_argument('--improve', help='指定方向，自我修改限定程式、驗證並發布一輪')
    mode.add_argument('--rollback-improvement', help='回復仍為最新版本的自我改進紀錄 ID 並發布')
    parser.add_argument('--edit', action='append', help='搭配 --improve 指定可修改模組，可重複')
    parser.add_argument('--group-column')
    parser.add_argument('--value-column')
    parser.add_argument('--output', type=Path)
    parser.add_argument("--query", help="搭配 --search-folder 的搜尋詞")
    parser.add_argument("--session", help="延續指定對話 ID")
    mode.add_argument("--doctor", action="store_true", help="檢查環境")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--file", type=Path, action="append", default=[], help="匯入文字文件供本次對話分析；可重複指定")
    args = parser.parse_args()
    if (args.improve or args.rollback_improvement) and any((args.file, args.session, args.output,
                                                         args.group_column, args.value_column, args.query)):
        parser.error('自我改進／回復不能混用對話、文件或 CSV 工作參數。')
    if args.edit and not args.improve:
        parser.error('--edit 需搭配 --improve。')
    if args.rollback_improvement:
        from .evolution import rollback
        try:
            print(json.dumps(rollback(args.rollback_improvement), ensure_ascii=False, indent=2))
        except Exception as exc:
            parser.exit(1, '版本回復未完成：' + str(exc) + '\n')
        return
    if args.improve:
        from .evolution import improve
        try:
            result = improve(args.improve, args.edit, progress=lambda state: print(state, flush=True))
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
    if args.output and not args.export_task:
        parser.error('--output 僅搭配 --export-task。')
    if args.plan_csv or args.run_task or args.task_status or args.export_task:
        from .tasks import execute_task, export_task, plan_csv
        if args.plan_csv and (not args.group_column or not args.value_column):
            parser.error('建立計畫需 --group-column 與 --value-column。')
        if not args.plan_csv and not args.session:
            parser.error('操作既有任務需 --session。')
        if args.export_task and not args.output:
            parser.error('匯出需 --output 指定新 JSON 檔案。')
        try:
            store = Store(args.data_dir / 'assistant.sqlite3')
            sid = args.session or store.new_session('CSV 工作')
            if args.plan_csv:
                result = plan_csv(store, sid, args.plan_csv, args.group_column, args.value_column)
            elif args.run_task:
                result = execute_task(store, sid, args.run_task)
            elif args.export_task:
                result = export_task(store, sid, args.export_task, args.output)
            else:
                result = store.task(sid, args.task_status)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.run_task and result['status'] != 'succeeded':
                parser.exit(1)
        except (ValueError, OSError, sqlite3.Error) as exc:
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
        print("Codex:", find_codex())
        return
    store = Store(args.data_dir / "assistant.sqlite3")
    engine = CodexEngine(args.data_dir)
    if args.ask:
        sid = args.session or store.new_session()
        for path in args.file:
            store.attach_document(sid, path)
        result = Assistant(store, engine).ask(sid, args.ask)
        print(result["content"])
        print("\nSession:", sid)
    else:
        from .gui import run
        run(store, engine)


if __name__ == "__main__":
    main()
