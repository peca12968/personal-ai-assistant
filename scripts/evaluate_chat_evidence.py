"""Exercise the local condition checker through saved chat answers."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import answer_runs, chat_evidence
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant


class NoModel:
    def answer(self, *args, **kwargs):
        raise AssertionError('本機條件核對不應呼叫模型。')

    def describe(self):
        raise AssertionError('本機條件核對不應查詢模型。')


def run():
    cases = json.loads((ROOT / 'evals/chat_evidence.json').read_bytes())
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'chat.sqlite3')
        for entry in (Assistant, DailyAssistant):
            app = entry(store, NoModel())
            for case in cases:
                sid = store.new_session()
                try:
                    answer = app.ask(sid, case['request'])
                except EngineError as exc:
                    if case.get('error') != exc.code:
                        raise
                    assert not any(row['role'] == 'assistant' for row in store.messages(sid)), case['id']
                    assert answer_runs.runs(store, sid)[0]['status'] == 'failed', case['id']
                    continue
                assert 'error' not in case, case['id']
                receipt = chat_evidence.read(store, answer['id'])
                assert receipt['output'] == answer['content'], case['id']
                result = receipt['analysis']
                assert result['status'] == case['status'], case['id']
                assert result['compatible_worlds'] == case['worlds'], case['id']
                if 'literal' in case:
                    assert result['extraction']['query']['literal'] == case['literal'], case['id']
                if 'distinguishes' in case:
                    assert [row['claim'] for row in result['observation_checks']
                            if row['distinguishes']] == case['distinguishes'], case['id']
            print(f'{entry.__name__}: {len(cases)}/{len(cases)} local condition checks and guards passed')


if __name__ == '__main__':
    run()
