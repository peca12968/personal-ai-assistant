"""Verify persisted memory operations and collect real replies separately from grading."""
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


class NoModel:
    def answer(self, *args):
        raise AssertionError('Local memory commands unexpectedly called model')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.live and (not args.report or args.report.exists() or
                      not args.report.resolve().is_relative_to((ROOT / 'data/evaluations').resolve())):
        parser.error('Live runs require a new report under data/evaluations.')
    cases = json.loads((ROOT / 'evals/memory_commands.json').read_text(encoding='utf-8'))['cases']
    fingerprints = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                    ('assistant/memory_commands.py', 'assistant/store.py', 'assistant/engine.py',
                     'assistant/context.py', 'assistant/retrieval.py', 'prompts/system.txt',
                     'prompts/communication.txt', 'evals/memory_commands.json')}
    report = {'completed': False, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'fingerprints': fingerprints, 'cases': []}
    engine = CodexEngine(ROOT / 'data') if args.live else None
    if args.live:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            local = Assistant(store, NoModel())
            receipts = [local.ask(sid, command) for command in case['commands']]
            if case['new_session']:
                sid = store.new_session()
            context = local.prepare_context(sid, case['question'])
            expected = [row['content'] for row in context['memories']]
            if expected != case['memories'] or context['recent_conversation'] or context['earlier_user_excerpts']:
                raise AssertionError('Persistence/context expectation failed: ' + case['id'])
            row = {'id': case['id'], 'question': case['question'], 'rubric': case['rubric'],
                   'receipts': receipts, 'database_memories': store.memories(), 'offline_passed': True,
                   'context': context, 'agent_review': None, 'human_grade': None}
            if args.live:
                before = store.memories()
                try:
                    response = Assistant(store, engine).ask(sid, case['question'])
                    row['answer'] = response['content']
                    row['context'] = store.answer_context(response['id'])
                    row['model_did_not_mutate_memories'] = store.memories() == before
                except Exception as exc:
                    row['error'] = str(exc)
            report['cases'].append(row)
            if args.live:
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(case['id'] + ': local persistence/context verified' + ('; reply collected for review' if args.live else ''), flush=True)
    report['completed'] = True
    if args.live:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Memory command scenarios: {len(cases)}/{len(cases)} actual database/context checks. Model answers require separate review.')
    if any('error' in row or row.get('model_did_not_mutate_memories') is False for row in report['cases']):
        sys.exit(1)


if __name__ == '__main__':
    main()
