"""Check information actually sent through Assistant.ask, without calling a model."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine, build_prompt
from assistant.store import Store


class CaptureEngine:
    def answer(self, prompt, cancel=None):
        self.prompt = prompt
        return "測試回覆"


def prepare_case(store, case):
    sid = store.new_session()
    for text in case.get("history", []):
        store.add_message(sid, "user", text)
        store.add_message(sid, "assistant", "已收到。")
    for index in range(case.get("filler_pairs", 0)):
        store.add_message(sid, "user", f"閒聊第 {index} 回：今天心情不錯。")
        store.add_message(sid, "assistant", "謝謝分享。")
    if case.get("memory"):
        store.remember(case["memory"])
    return sid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--strategy", choices=("current", "recent16"), default="current")
    parser.add_argument("--ids", nargs="+", help="限定真實回答案例 ID")
    args = parser.parse_args()
    results = []
    cases = json.loads((ROOT / "evals/context.json").read_text(encoding="utf-8"))
    if args.live:
        if args.ids:
            unknown = set(args.ids) - {case["id"] for case in cases}
            if unknown:
                parser.error("未知案例 ID：" + ", ".join(sorted(unknown)))
            cases = [case for case in cases if case["id"] in args.ids]
        engine = CodexEngine(ROOT / "data")
        path = args.report or ROOT / "data/evaluations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-context-" + args.strategy + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        for case in cases:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "live-context.sqlite3")
                sid = prepare_case(store, case)
                if args.strategy == "recent16":
                    # Same current instructions; isolate the old context-selection strategy.
                    answer = engine.answer(build_prompt(case["prompt"], store.messages(sid, 16), store.retrieve(case["prompt"])))
                else:
                    answer = Assistant(store, engine).ask(sid, case["prompt"])["content"]
                results.append({"id": case["id"], "strategy": args.strategy, "answer": answer,
                                "expected_information": case.get("must_include", []), "human_grade": None})
                path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                print(case["id"], "answered; pending rubric review", flush=True)
        print("Report:", path)
        return
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "context.sqlite3")
            sid = prepare_case(store, case)
            engine = CaptureEngine()
            Assistant(store, engine).ask(sid, case["prompt"])
            ok = all(text in engine.prompt for text in case.get("must_include", [])) and all(
                text not in engine.prompt for text in case.get("must_exclude", []))
            results.append({"id": case["id"], "passed": ok})
    report = {"passed": sum(r["passed"] for r in results), "total": len(results), "checks": results}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f'Context regression: {report["passed"]}/{report["total"]} (context inclusion, not answer correctness).')
    if report["passed"] != report["total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
