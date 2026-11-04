"""Actual local arithmetic/persistence regression; never a model-quality score."""
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if __package__ in (None,''):sys.path.insert(0,str(ROOT))
from assistant.engine import Assistant,EngineError
from assistant.store import Store
from assistant import answer_runs


def main():
    cases=json.loads((ROOT/'evals/chat_calculation.json').read_text(encoding='utf-8'))
    class NoModel:
        def answer(self,*args,**kwargs):raise AssertionError('Local arithmetic invoked a model')
        def describe(self):raise AssertionError('Local arithmetic selected a model')
    with tempfile.TemporaryDirectory() as directory:
        store=Store(Path(directory)/'assistant.sqlite3');app=Assistant(store,NoModel())
        for case in cases:
            sid=store.new_session(case['id'])
            try:answer=app.ask(sid,case['request'])
            except EngineError as error:
                assert case.get('error')==error.code,(case['id'],str(error))
                assert [r['role'] for r in store.messages(sid)]==['user']
                assert answer_runs.runs(store,sid)[0]['status']=='failed'
            else:
                assert 'error' not in case and answer['content']==case['answer'],case['id']
                context=store.answer_context(answer['id'])
                assert context['response_mode']=='local_calculation'
                assert context['calculation']['result']['exact']==case['exact']
                assert context['calculation']['model_called'] is False and 'engine_selection' not in context
                assert answer_runs.runs(store,sid)[0]['status']=='succeeded'
    print(f'Local arithmetic: {len(cases)}/{len(cases)} actual calculation/persistence scenarios; no model reasoning or parameter-learning score.')


if __name__=='__main__':main()
