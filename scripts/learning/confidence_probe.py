"""Finite answer-then-confidence experiment on a fresh local synthetic chat store."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from statistics import mean
import uuid

from assistant import answer_runs, local_models
from assistant.engine import Assistant, EngineError, parse_command
from assistant.store import Store
from scripts.learning import qualified_segment as qualified
from scripts.learning.segment_chat import SegmentEngine

ROOT = qualified.ROOT
PARENT = ROOT / 'data/learning/confidence-probes'
POLICY = {'schema': 'exact_answer_confidence_v1', 'high_percent': 80,
          'confidence_instruction': '以下是原題與助理實際給出的完整回答。評估該回答完全正確且符合原題格式的機率。'
                                    '只回覆 0 到 100 的一個整數，不要百分號或解釋。'}
FORMATS = {'integer': r'(?:0|-?[1-9][0-9]*)', 'choice': r'[ABC]'}
save = qualified.segments.gpu.save
digest = qualified.segments.digest
read = local_models.read_json


def cases(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 32:
        raise ValueError('需要 2 至 32 題。')
    seen = set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'question', 'answer', 'format'}
                or not isinstance(row['question'], str) or not 1 <= len(row['question'].strip()) <= 2000
                or row['question'] != row['question'].strip() or row['question'] in seen
                or parse_command(row['question']) is not None or not isinstance(row['format'], str)
                or row['format'] not in FORMATS or not isinstance(row['answer'], str)
                or not re.fullmatch(FORMATS[row['format']], row['answer'])):
            raise ValueError('題目、正解或答案格式不符。')
        seen.add(row['question'])
    return value


def confidence_question(question, answer):
    return POLICY['confidence_instruction'] + '\n' + json.dumps(
        {'原題': question, '實際回答': answer}, ensure_ascii=False)


def metrics(corpus, paired):
    counted, correct, malformed, missing = [], 0, 0, 0
    invalid_confidence = missing_confidence = 0
    for case, (answer, rating) in zip(cases(corpus), paired, strict=True):
        if answer is None:
            if rating is not None:
                raise ValueError('無答案卻有信心評分。')
            missing += 1
            continue
        ok = answer == case['answer']
        correct += ok
        malformed += not bool(re.fullmatch(FORMATS[case['format']], answer))
        if rating is None:
            missing_confidence += 1
        elif not re.fullmatch(r'(?:0|[1-9][0-9]?|100)', rating):
            invalid_confidence += 1
        else:
            counted.append((int(rating), ok))
    right = [p for p, ok in counted if ok]
    wrong = [p for p, ok in counted if not ok]
    high = [ok for p, ok in counted if p >= POLICY['high_percent']]
    return dict(total=len(corpus), correct=correct, answer_errors=missing, answer_format_errors=malformed,
                answered_incorrect=len(corpus)-missing-correct, confidence_errors=missing_confidence,
                confidence_format_errors=invalid_confidence, scored_pairs=len(counted),
                accuracy=correct/len(corpus), brier=mean((p/100-ok)**2 for p, ok in counted) if counted else None,
                ranking=mean((a>b) + 0.5*(a==b) for a in right for b in wrong) if right and wrong else None,
                high_confidence_count=len(high), high_confidence_coverage=len(high)/len(corpus),
                high_confidence_accuracy=mean(high) if high else None)


def sources():
    paths = [Path(__file__), Path(qualified.__file__), Path(__file__).with_name('segment_chat.py'),
             Path(__file__).with_name('segment_chat_worker.py')]
    return {p.name: qualified.batches.file_hash(p) for p in paths}


def ask(app, store, question):
    sid = store.new_session()
    try:
        reply = app.ask(sid, question)
    except EngineError as exc:
        return {'session': sid, 'error': exc.code}
    return {'session': sid, 'run': reply['run_id'], 'answer': reply['content']}


def verify_answer(db, row, question, description):
    record = db.execute('SELECT r.id,r.status,r.error_code,r.context_json,q.content,a.content FROM answer_runs r '
                        'JOIN messages q ON q.id=r.user_message_id LEFT JOIN messages a ON a.id=r.answer_message_id '
                        'WHERE r.session_id=?', (row['session'],)).fetchall()
    if len(record) != 1:
        raise ValueError('驗收工作紀錄數量不符。')
    run, status, error, ctx, prompt, actual = record[0]
    context = json.loads(ctx)
    if (context.get('engine_selection') != description or context.get('calculation_mode') != 'model_only_evaluation'
            or prompt != question):
        raise ValueError('題目、模型或計算模式與規約不符。')
    if 'error' in row:
        # The standard store normalizes engine-specific failure codes on persistence.
        raw_code = row['error']
        stored_code = raw_code if raw_code in answer_runs.ERROR_LABELS and raw_code != 'owner_lost' else 'unexpected_error'
        if status != 'failed' or stored_code != error or actual is not None:
            raise ValueError('失敗紀錄不符。')
        return None
    if status != 'succeeded' or row['run'] != run or actual != row['answer']:
        raise ValueError('原始答案與對話資料庫不符。')
    return actual


def review(folder, fingerprint):
    plan = read(folder / 'plan.json')
    observed = sources()
    observed[Path(__file__).name] = qualified.batches.file_hash(folder / 'collector.py')
    if digest(plan) != fingerprint or plan['policy'] != POLICY or observed != plan['sources']:
        raise ValueError('實驗規約、來源或指紋已變更。')
    rows = read(folder / 'responses.json')
    paired = []
    used = set()
    with closing(sqlite3.connect((folder / 'assistant.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        for case, row in zip(cases(plan['cases']), rows, strict=True):
            answer = verify_answer(db, row['response'], case['question'], plan['description'])
            occurrences = [row['response']['session']]
            rating = None
            if answer is not None:
                rating = verify_answer(db, row['confidence'], confidence_question(case['question'], answer), plan['description'])
                occurrences.append(row['confidence']['session'])
            elif row.get('confidence') is not None:
                raise ValueError('無實際答案可評分。')
            for sid in occurrences:
                if sid in used:
                    raise ValueError('評估對話重複。')
                used.add(sid)
            paired.append((answer, rating))
    result = metrics(plan['cases'], paired)
    result['reviewer_sha256'] = qualified.batches.file_hash(Path(__file__))
    return result


def collect(corpus):
    folder = PARENT / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    engine = SegmentEngine(folder)
    training = {item['prompt'] for item in engine.batch['items']}
    if any(c['question'] in training for c in corpus):
        raise ValueError('題目與本批最佳化資料相同。')
    plan = dict(policy=POLICY, cases=corpus, description=engine.describe(), sources=sources())
    signature = digest(plan)
    (folder / 'collector.py').write_bytes(Path(__file__).read_bytes())
    save(folder / 'plan.json', plan)
    print(folder.name, signature, flush=True)
    store = Store(folder / 'assistant.sqlite3')
    app = Assistant(store, engine, allow_local_calculation=False, allow_local_transcription=False, allow_local_evidence=False)
    rows = []
    for n, case in enumerate(corpus, 1):
        if digest(read(folder / 'plan.json')) != signature or sources() != plan['sources']:
            raise ValueError('執行中規約或來源改變。')
        result = ask(app, store, case['question'])
        row = {'response': result, 'confidence': None}
        rows.append(row)
        save(folder / 'responses.json', rows)
        if 'answer' in result:
            row['confidence'] = ask(app, store, confidence_question(case['question'], result['answer']))
            save(folder / 'responses.json', rows)
        print(f'{n}/{len(corpus)}', flush=True)
    return review(folder, signature)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('collect').add_argument('suite', type=Path)
    check = sub.add_parser('review')
    check.add_argument('id')
    check.add_argument('fingerprint')
    args = parser.parse_args()
    if args.command == 'collect':
        result = collect(cases(read(args.suite)))
    else:
        result = review(PARENT / qualified.segments.identifier(args.id), args.fingerprint)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
