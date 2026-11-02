import argparse
import json
import sqlite3
import zipfile
from pathlib import Path

from .engine import Assistant, CodexEngine, ROOT, find_codex
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="個人 AI 助理")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ask", help="單次提問")
    mode.add_argument("--backup", action="store_true", help="建立目前資料的本機備份")
    mode.add_argument("--inspect-backup", type=Path, help="檢查備份完整性與內容筆數")
    mode.add_argument("--restore", type=Path, help="還原備份到 --data-dir 指定的新資料夾")
    mode.add_argument("--search-folder", type=Path, help="在指定資料夾本機唯讀搜尋，不呼叫模型")
    parser.add_argument("--query", help="搭配 --search-folder 的搜尋詞")
    parser.add_argument("--session", help="延續指定對話 ID")
    mode.add_argument("--doctor", action="store_true", help="檢查環境")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--file", type=Path, action="append", default=[], help="匯入文字文件供本次對話分析；可重複指定")
    args = parser.parse_args()
    if args.file and not args.ask:
        parser.error("--file 需搭配 --ask；桌面版請使用「參考文件」。")
    if bool(args.search_folder) != bool(args.query):
        parser.error('--search-folder 與 --query 需一起使用。')
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
