"""Collect actual communication responses; semantic grades require explicit review."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine
from assistant.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    cases = json.loads((ROOT / 'evals/communication.json').read_text(encoding='utf-8'))
    if not args.live:
        print(f'{len(cases)} communication scenarios available; no answers collected or graded.')
        return
    if not args.report:
        parser.error('--live requires --report under data/evaluations')
    destination = args.report.resolve()
    if not destination.is_relative_to((ROOT / 'data/evaluations').resolve()) or destination.exists():
        parser.error('Use a new report path under data/evaluations; existing evidence is preserved.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                   ('assistant/communication.py', 'assistant/engine.py', 'assistant/evolution_status.py', 'prompts/system.txt',
                    'prompts/communication.txt', 'evals/communication.json')}
    report = {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'fingerprint': fingerprint, 'completed': False, 'results': []}
    engine = CodexEngine(ROOT / 'data')
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            if case.get('preference'):
                store.remember(case['preference'], 'preference')
            for role, content in case.get('history', []):
                store.add_message(sid, role, content)
            answer = Assistant(store, engine).ask(sid, case['question'])
            report['results'].append({**case, 'answer': answer['content'], 'agent_review': None,
                                      'human_grade': None, 'context': store.answer_context(answer['id'])})
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(case['id'] + ': response collected, review pending', flush=True)
    report['completed'] = True
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
