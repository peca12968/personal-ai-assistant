"""Offline retrieval regression; optional live answers for human rubric review."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import CodexEngine, build_prompt
from assistant.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="呼叫 Codex；會使用帳號額度")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--report", type=Path, help="保存離線評估結果 JSON")
    parser.add_argument("--suite", choices=("basic", "memory"), default="basic")
    args = parser.parse_args()
    cases = json.loads((ROOT / "evals/retrieval.json").read_text(encoding="utf-8"))
    passed = 0
    checks = []
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "eval.sqlite3")
            expected = store.remember(case["memory"])
            store.remember(case["distractor"])
            actual = store.retrieve(case["query"], 1)
            ok = bool(actual and actual[0]["id"] == expected) if case.get("expect_match", True) else not actual
            passed += ok
            checks.append({"query": case["query"], "passed": ok,
                           "selected": actual[0]["content"] if actual else None})
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"passed": passed, "total": len(cases), "checks": checks},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Retrieval regression: {passed}/{len(cases)} passed (not a model intelligence score).")
    if passed != len(cases):
        sys.exit(1)
    if args.live:
        engine = CodexEngine(ROOT / "data")
        suite_file = "memory_behavior.json" if args.suite == "memory" else "behavior.json"
        cases = json.loads((ROOT / "evals" / suite_file).read_text(encoding="utf-8"))
        results = []
        path = ROOT / "data/evaluations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.suite + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        for case in cases[:args.limit]:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "live-eval.sqlite3")
                for memory in case.get("memories", []):
                    store.remember(**memory)
                selected = store.retrieve(case["prompt"])
                answer = engine.answer(build_prompt(case["prompt"], case.get("history", []), selected))
            results.append({**case, "selected_memory_ids": [m["id"] for m in selected],
                            "answer": answer, "human_grade": None})
            path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["id"], "answered; pending human rubric review", flush=True)
        print("Report:", path)


if __name__ == "__main__":
    main()
