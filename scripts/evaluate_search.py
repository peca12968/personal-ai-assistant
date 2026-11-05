"""Synthetic folder-search regression and optional real grounded-answer evaluation."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine
from assistant.folder_search import attach_result, search_folder
from assistant.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.live and not args.report:
        parser.error('--live 需提供 --report，將實際回答存放在本機 data/evaluations。')
    cases = json.loads((ROOT / 'evals/folder_search.json').read_text(encoding='utf-8'))
    if args.ids:
        unknown = set(args.ids) - {case['id'] for case in cases}
        if unknown:
            parser.error('未知案例：' + ', '.join(sorted(unknown)))
        cases = [case for case in cases if case['id'] in args.ids]
    engine = CodexEngine(ROOT / 'data') if args.live else None
    results = []
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'selected'
            for name, content in case['files'].items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                filler = ''.join(f'一般背景第 {i+1} 行。\n' for i in range(case.get('filler', {}).get(name, 0)))
                path.write_text(filler + content, encoding=case.get('encoding', 'utf-8'))
            report = search_folder(root, case['query'])
            selected = report['results'][:len(case['expected'])] if case['expected'] else report['results']
            found = [row['relative_path'] for row in selected]
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            for row in selected:
                attach_result(store, sid, report, row)
            app = Assistant(store, engine, allow_local_transcription=False)
            context = app.prepare_context(sid, case['prompt'])
            text = '\n'.join(row['text'] for row in context['documents']['excerpts'])
            passed = set(found) == set(case['expected']) and all(term in text for term in case['evidence'])
            result = {'id': case['id'], 'passed': passed, 'selected': found, 'rubric': case['rubric']}
            if args.live and selected:
                answer = app.ask(sid, case['prompt'])
                result.update(answer=answer['content'], citation_checks=answer['citation_checks'], human_grade=None)
            results.append(result)
            print(case['id'], 'selection pass' if passed else 'FAIL', flush=True)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    passed = sum(row['passed'] for row in results)
    print(f'Folder search regression: {passed}/{len(results)} (selection, not answer correctness).')
    if passed != len(results):
        sys.exit(1)


if __name__ == '__main__':
    main()
