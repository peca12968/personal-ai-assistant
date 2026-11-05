"""Compare saved numeric confidence with two counterbalanced code assignments."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import uuid

from scripts.learning import confidence_probe as probe

PARENT = probe.ROOT / 'data/learning/confidence-codes'
HIGH = '至少八成'
LOW = '低於八成'
hash_file = probe.qualified.batches.file_hash


def prompt(question, answer, high_code):
    legend = '甲＝' + (HIGH if high_code == '甲' else LOW) + '，乙＝' + (HIGH if high_code == '乙' else LOW)
    return ('評估這份實際回答完全正確且符合原題格式的機率。依照本輪對照表，只填一個代碼。'
            + legend + '。\n' + json.dumps({'原題': question, '實際回答': answer}, ensure_ascii=False))


def protocol(source_id, signature):
    source = probe.PARENT / probe.qualified.segments.identifier(source_id)
    answers_file = source / 'responses.json'
    before = hash_file(answers_file)
    probe.review(source, signature)
    previous = probe.read(source / 'plan.json')
    records = probe.read(answers_file)
    if probe.digest(previous) != signature or hash_file(answers_file) != before:
        raise ValueError('來源在核對期間改變。')
    trials = []
    for index, (case, row) in enumerate(zip(previous['cases'], records, strict=True)):
        if 'answer' not in row['response']:
            continue
        answer = row['response']['answer']
        number = row['confidence'].get('answer')
        high = int(number) >= 80 if number and re.fullmatch(r'0|[1-9][0-9]?|100', number) else None
        for high_code in ('甲乙' if index % 2 == 0 else '乙甲'):
            trials.append(dict(case=index, high_code=high_code, question=prompt(case['question'], answer, high_code),
                               numeric_high=high, exact=answer == case['answer']))
    if not trials:
        raise ValueError('來源沒有可重評的回答。')
    return dict(schema='counterbalanced_confidence_v1', source=source_id, signature=signature, baseline=before,
                cases=len(records), description=previous['description'], trials=trials,
                sources={**probe.sources(), Path(__file__).name: hash_file(Path(__file__))})


def scores(trials, answers):
    pairs = {}
    high_by_correctness = {'correct': [0, 0], 'incorrect': [0, 0]}
    missing = invalid = valid = numeric_matches = numeric_compared = 0
    for trial, answer in zip(trials, answers, strict=True):
        pair = pairs.setdefault(trial['case'], [])
        if answer is None:
            missing += 1
        elif answer not in ('甲', '乙'):
            invalid += 1
        else:
            high = answer == trial['high_code']
            pair.append(high)
            counts = high_by_correctness['correct' if trial['exact'] else 'incorrect']
            counts[0] += high
            counts[1] += 1
            valid += 1
            if trial['numeric_high'] is not None:
                numeric_compared += 1
                numeric_matches += high == trial['numeric_high']
    complete = [p for p in pairs.values() if len(p) == 2]
    return dict(attempts=len(trials), valid=valid, format_failures=invalid, engine_errors=missing,
                paired=len(complete), consistent=sum(p[0] == p[1] for p in complete),
                numeric_compared=numeric_compared, numeric_matches=numeric_matches,
                high_and_valid_counts=high_by_correctness)


def review(folder, digest):
    plan = probe.read(folder / 'plan.json')
    if probe.digest(plan) != digest or plan != protocol(plan['source'], plan['signature']):
        raise ValueError('對照資料已變動。')
    rows = probe.read(folder / 'responses.json')
    if len({r['session'] for r in rows}) != len(rows):
        raise ValueError('對話不可重複。')
    with closing(sqlite3.connect((folder / 'assistant.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        answers = [probe.verify_answer(db, row, trial['question'], plan['description'])
                   for trial, row in zip(plan['trials'], rows, strict=True)]
    return scores(plan['trials'], answers)


def collect(source, signature):
    plan = protocol(source, signature)
    folder = PARENT / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    engine = probe.SegmentEngine(folder)
    if engine.describe() != plan['description']:
        raise ValueError('目前模型與原答的參數版本不同。')
    digest = probe.digest(plan)
    probe.save(folder / 'plan.json', plan)
    print(folder.name, digest, flush=True)
    store = probe.Store(folder / 'assistant.sqlite3')
    app = probe.Assistant(store, engine, allow_local_calculation=False, allow_local_transcription=False, allow_local_evidence=False)
    rows = []
    for trial in plan['trials']:
        rows.append(probe.ask(app, store, trial['question']))
        probe.save(folder / 'responses.json', rows)
        print(len(rows), '/', len(plan['trials']), flush=True)
    return review(folder, digest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'review'])
    parser.add_argument('id')
    parser.add_argument('digest')
    args = parser.parse_args()
    if args.action == 'collect':
        result = collect(args.id, args.digest)
    else:
        result = review(PARENT / probe.qualified.segments.identifier(args.id), args.digest)
    print(json.dumps(result, indent=2))
