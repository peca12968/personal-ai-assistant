"""Desktop entry point for local daily chat. Keep raw benchmark engines available."""
import argparse
from pathlib import Path
import sqlite3
import sys

from assistant.engine import Assistant, ROOT
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--engine', choices=('local', 'codex'))
    parser.add_argument('--qualified-segment', action='store_true')
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--local-experiment')
    choices.add_argument('--local-batch')
    choices.add_argument('--local-active', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--session')
    parser.add_argument('--file', type=Path, action='append', default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--ask')
    mode.add_argument('--retry-answer', action='store_true')
    args, other = parser.parse_known_args()

    if args.qualified_segment:
        if args.engine or args.local_experiment or args.local_batch or args.local_active:
            parser.error('分段模型與其他模型選項只能選一種。')
        if other:
            from scripts.learning.segment_chat import main as segment_main
            return segment_main()
    elif args.engine != 'local' or other:
        from assistant.__main__ import main as ordinary_main
        return ordinary_main()
    if args.retry_answer and (not args.session or args.file):
        parser.error('--retry-answer 需要 --session，且不能再附加文件。')
    if args.file and not args.ask:
        parser.error('--file 需搭配 --ask。')
    if args.qualified_segment and args.session and not (args.ask or args.retry_answer):
        parser.error('--session 需搭配 --ask 或 --retry-answer。')
    try:
        if args.qualified_segment:
            from scripts.learning.segment_chat import SegmentEngine
            raw = SegmentEngine(args.data_dir)
        else:
            raw = LocalEngine(args.data_dir, experiment=args.local_experiment, batch=args.local_batch, active=args.local_active)
        engine = DailyLocalEngine(raw)
        store = Store(args.data_dir / 'assistant.sqlite3')
        if args.ask or args.retry_answer:
            sid = args.session or store.new_session()
            for path in args.file:
                store.attach_document(sid, path)
            app = Assistant(store, engine)
            reply = app.retry(sid) if args.retry_answer else app.ask(sid, args.ask)
            print(reply['content'])
            print('\nSession:', sid)
        else:
            from assistant.gui import run
            run(store, engine)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(1, '本機助理未完成：' + str(exc) + '\n')


if __name__ == '__main__':
    main()
