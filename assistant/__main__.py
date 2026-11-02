import argparse
from pathlib import Path

from .engine import Assistant, CodexEngine, ROOT, find_codex
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="個人 AI 助理")
    parser.add_argument("--ask", help="單次提問")
    parser.add_argument("--session", help="延續指定對話 ID")
    parser.add_argument("--doctor", action="store_true", help="檢查環境")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if args.doctor:
        print("Project:", ROOT)
        print("Data:", args.data_dir.resolve())
        print("Codex:", find_codex())
        return
    store = Store(args.data_dir / "assistant.sqlite3")
    engine = CodexEngine(args.data_dir)
    if args.ask:
        sid = args.session or store.new_session()
        result = Assistant(store, engine).ask(sid, args.ask)
        print(result["content"])
        print("\nSession:", sid)
    else:
        from .gui import run
        run(store, engine)


if __name__ == "__main__":
    main()
