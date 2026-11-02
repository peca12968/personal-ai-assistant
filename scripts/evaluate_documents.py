"""Document evidence selection offline; optional live task answers with review rubrics."""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine
from assistant.store import Store


def prepare_case(store, directory, case):
    sid = store.new_session()
    for file in case["files"]:
        path = directory / file["name"]
        content = "".join(f"一般背景資料第 {i + 1} 行。\n" for i in range(file.get("filler_before", 0))) + file["text"]
        path.write_text(content, encoding="utf-8")
        store.attach_document(sid, path)
    return sid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="呼叫 Codex，使用帳號額度")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    cases = json.loads((ROOT / "evals/documents.json").read_text(encoding="utf-8"))
    if args.ids:
        unknown = set(args.ids) - {case["id"] for case in cases}
        if unknown:
            parser.error("未知案例：" + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.ids]
    engine = CodexEngine(ROOT / "data") if args.live else None
    results = []
    path = args.report
    if args.live and not path:
        path = ROOT / "data/evaluations" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-documents.json")
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = Store(directory / "state.sqlite3")
            sid = prepare_case(store, directory, case)
            app = Assistant(store, engine)
            context = app.prepare_context(sid, case["prompt"])
            selected_text = "\n".join(row["text"] for row in context["documents"]["excerpts"])
            passed = all(text in selected_text for text in case["evidence"])
            passed = passed and all(text not in selected_text for text in case.get('absent_evidence', []))
            if 'coverage_limited' in case:
                passed = passed and context['documents']['coverage_limited'] == case['coverage_limited']
            if case["id"] == "partial_coverage":
                passed = passed and context["documents"]["coverage_limited"]
            result = {"id": case["id"], "evidence_present": passed, "rubric": case["rubric"]}
            if args.live:
                answer = app.ask(sid, case["prompt"])
                result.update(answer=answer["content"], citation_checks=answer["citation_checks"], human_grade=None)
                print(case["id"], "answered; pending rubric review", flush=True)
            results.append(result)
            if path:
                path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(row["evidence_present"] for row in results)
    print(f"Document evidence regression: {passed}/{len(results)} (not answer correctness).")
    if path:
        print("Report:", path)
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
