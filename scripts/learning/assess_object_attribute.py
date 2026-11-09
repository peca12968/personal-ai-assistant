"""Compare combined judgment and explicit object/property checks on assigned evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import assess_scope_recovery as recovery
from scripts.learning import paired_evidence_runtime as runtime

scope, full, polarity = recovery.scope, recovery.full, recovery.polarity
read, save, digest = recovery.read, recovery.save, recovery.digest
SCHEMA = 'object_attribute_assay_v1'
PARENT = ROOT / 'data/learning/object-attribute-assay'
FIXTURE = ROOT / 'evals/object_attribute_assay.json'
SLOTS = ('A', 'B')
STATES = ('T', 'F', 'U')
INSTRUCTION = ('各自只用指定的編號觀察核對命題。T 表示確認成立，F 表示確認不成立，U 表示待查。'
               '不同物件或不同屬性的觀察不能確認命題。只回 JSON。')
DIRECT = 'JSON 必須有 A、B 兩個欄位，各含一個 state 字串。'
SPLIT = ('分別判斷物件是否吻合，以及觀察是否涉及同一屬性。屬性可吻合而物件不吻合，'
         '相反狀態仍屬同一屬性。object 和 property 只填 true 或 false，再填 state。'
         'JSON 必須有 A、B 兩個欄位，各含 object、property 兩個布林值和 state 字串。')


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def compile_case(row):
    cid, group = row['id'], row['group']
    if group == 'control':
        if set(row) != {'id', 'group', 'question', 'answer'}:
            raise ValueError('一般控制的格式不符。')
        return dict(id=cid, group=group, question=row['question'], semantic_question=row['question'],
                    history=[], mapping={}, answer=row['answer'])
    if (group not in ('fresh', 'historical') or set(row) != {'id', 'group', 'history', 'records', 'claims'}
            or not 1 <= len(row['records']) <= 3 or len(row['claims']) != 2):
        raise ValueError('須提供兩個待核對命題及指定原句。')
    records = row['records']
    for text in [row['history'], *records]:
        recovery.batches.queue.text(text, 180, '原文')
    claims, gold = [], []
    for slot, claim in zip(SLOTS, row['claims'], strict=True):
        if (set(claim) != {'object', 'property', 'source', 'gold'} or type(claim['source']) is not int
                or not 1 <= claim['source'] <= len(records)):
            raise ValueError('指定觀察編號無效。')
        for text in (claim['object'], claim['property']):
            recovery.batches.queue.text(text, 55, '命題')
        match = claim['gold']
        if (len(match) != 3 or any(type(flag) is not bool for flag in match[:2]) or match[2] not in STATES
                or (not all(match[:2]) and match[2] != 'U')):
            raise ValueError('預期物件、屬性及狀態不符。')
        claims.append(f'{slot} 核對「{claim["object"]}」的「{claim["property"]}」，指定觀察 [{claim["source"]}]。')
        gold.append(match)
    text = '\n'.join([*(f'[{i}] {record}' for i, record in enumerate(records, 1)), *claims, INSTRUCTION])
    return dict(id=cid, group=group, question=text + '\n' + DIRECT,
                semantic_question=text + '\n' + SPLIT, history=[dict(role='user', content=row['history']),
                                                                  dict(role='assistant', content='收到。')],
                mapping={}, records=records, gold=gold, answer=polarity.conclusion([item[2] for item in gold]))


def cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('物件與屬性題庫格式不符。')
    rows = data['cases']
    compiled = [compile_case(row) for row in rows]
    if ([row['id'] for row in compiled] != ['label_only', 'label_and_seal', 'similar_cabinet', 'same_cabinet_veto',
                                           'shared_opposite', 'shared_partial', 'strap_missing', 'literal_control']
            or [row['group'] for row in compiled] != ['fresh'] * 6 + ['historical', 'control']):
        raise ValueError('物件與屬性題組不符。')
    for a, b in zip(rows[:6:2], rows[1:6:2], strict=True):
        if (a['history'] != b['history']
                or [{key: value for key, value in claim.items() if key != 'gold'} for claim in a['claims']]
                != [{key: value for key, value in claim.items() if key != 'gold'} for claim in b['claims']]
                or sum(left != right for left, right in zip(a['records'], b['records'], strict=True)) != 1):
            raise ValueError('配對只能替換一則原句。')
    return compiled


def sources():
    hashes = recovery.sources()
    for path in (FIXTURE, Path(__file__).resolve(), Path(runtime.__file__)):
        hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def historical(name):
    folder = recovery.location(name)
    plan = recovery.frozen(folder)
    report = recovery.result(folder, plan, read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('先前重查測量缺少完成證據。')
    case = next(row for row in plan['cases'] if row['id'] == 'strap_missing')
    rejected_claim = report['details']['strap_missing']['split']
    state, source = rejected_claim['fact']
    selected = next(row for row in case['source_manifest'] if row['id'] == source)
    original = case['text'][selected['start']:selected['end']]
    if (state != 'T' or rejected_claim['correct'] or rejected_claim['conclusion'] != '可行'
            or cases()[-2]['records'] != [original]):
        raise ValueError('歷史對照未重現原錯誤引用。')
    return dict(name=name, protocol_sha256=digest(plan), report_sha=report['report_sha'],
                exit_sha=report['exit_sha']), plan


def prepare(name, reference, previous):
    evidence, old = historical(previous)
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources, location=location):
        plan = full.prepare(name, reference)
    recovery.same_weights(plan, old)
    plan['historical'] = evidence
    folder = location(name)
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources):
        return full.frozen(folder)


def checked(folder):
    with patch.object(scope, 'frozen', frozen):
        plan = scope.checked(folder)
    evidence, old = historical(plan['historical']['name'])
    recovery.same_weights(plan, old)
    if evidence != plan['historical']:
        raise ValueError('先前引用錯誤的實測證據已變更。')
    return plan


def parse(row, *, split):
    raw = row['raw']
    if not row['ended'] or not 1 <= len(raw) <= 2048:
        return None
    try:
        obj = polarity.strict_json(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.keys() != set(SLOTS):
        return None
    keys = {'object', 'property', 'state'} if split else {'state'}
    output = []
    for slot in SLOTS:
        item = obj[slot]
        if (not isinstance(item, dict) or item.keys() != keys or item['state'] not in STATES
                or (split and any(type(item[key]) is not bool for key in ('object', 'property')))):
            return None
        output.append(item)
    return output


def grade(case, row, *, split):
    if case['group'] == 'control':
        return recovery.natural.grade(case, row)
    facts = parse(row, split=split)
    states = [item['state'] for item in facts or ()]
    conclusion = polarity.conclusion(states) if facts else None
    gold = case['gold']
    outcome = dict(facts=facts, valid=facts is not None, conclusion=conclusion, correct=conclusion == case['answer'],
                   exact_states=sum(state == target[2] for state, target in zip(states, gold)))
    if split:
        guarded = [item['state'] if item['object'] and item['property'] else 'U' for item in facts or ()]
        verdict = polarity.conclusion(guarded) if facts else None
        outcome.update(guarded_states=guarded, guarded_conclusion=verdict, guarded_correct=verdict == case['answer'],
                       object_correct=sum(item['object'] == target[0] for item, target in zip(facts or (), gold)),
                       property_correct=sum(item['property'] == target[1] for item, target in zip(facts or (), gold)),
                       exact_guarded=sum(state == target[2] for state, target in zip(guarded, gold)))
    return outcome


def summarize(fixture, rows):
    groups, details = {}, {}
    for case, direct, split in zip(fixture, rows['direct'], rows['split'], strict=True):
        cid, group = case['id'], case['group']
        if direct['id'] != cid or split['id'] != cid:
            raise ValueError('物件與屬性案例順序不符。')
        if group == 'control' and any(direct[key] != split[key] for key in
                                     ('raw', 'ended', 'delivered', 'input_tokens', 'wire_sha')):
            raise ValueError('一般控制未重現同一提示與原答。')
        details[cid] = dict(expected=case['answer'], gold=case.get('gold'),
                            direct=grade(case, direct, split=False), split=grade(case, split, split=True))
        for arm in ('direct', 'split'):
            outcome = details[cid][arm]
            totals = groups.setdefault(group, {}).setdefault(arm, {'count': 0})
            totals['count'] += 1
            for metric in ('correct', 'valid', 'exact_states', 'guarded_correct', 'exact_guarded',
                           'object_correct', 'property_correct'):
                if metric in outcome:
                    totals[metric] = totals.get(metric, 0) + outcome[metric]
    return dict(groups=groups, details=details, automatic_activation=False)


def result(folder, plan, run):
    with patch.multiple(scope, SCHEMA=SCHEMA, summarize=summarize):
        return scope.result(folder, plan, run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('check')
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--reference', required=True)
    prep.add_argument('--previous', required=True)
    for mode in ('run', 'verify'):
        modes.add_parser(mode).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'check':
        print(len(cases()))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference, args.previous)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        with patch.object(polarity, 'frozen', frozen):
            polarity.worker(location(args.name), args.run_sha256)
    elif args.command == 'run':
        report = runtime.collect(location(args.name), args.name, __file__, frozen=frozen, checked=checked, result=result)
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(json.dumps(runtime.verify(location(args.name), checked=checked, result=result), ensure_ascii=False))


if __name__ == '__main__':
    main()
