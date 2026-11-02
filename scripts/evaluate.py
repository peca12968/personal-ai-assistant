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
    args = parser.parse_args()
    cases = json.loads((ROOT / "evals/retrieval.json").read_text(encoding="utf-8"))
    passed = 0
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "eval.sqlite3")
            expected = store.remember(case["memory"])
            store.remember(case["distractor"])
            actual = store.retrieve(case["query"], 1)
            passed += bool(actual and actual[0]["id"] == expected)
    print(f"Retrieval regression: {passed}/{len(cases)} passed (not a model intelligence score).")
    if passed != len(cases):
        sys.exit(1)
    if args.live:
        engine = CodexEngine(ROOT / "data")
        cases = json.loads((ROOT / "evals/behavior.json").read_text(encoding="utf-8"))
        results = []
        path = ROOT / "data/evaluations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        for case in cases[:args.limit]:
            answer = engine.answer(build_prompt(case["prompt"], [], []))
            results.append({**case, "answer": answer, "human_grade": None})
            path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["id"], "answered; pending human rubric review", flush=True)
        print("Report:", path)


if __name__ == "__main__":
    main()
