"""Repeat the object/property assay with a flat, explicit JSON contract."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import assess_object_attribute as first

full, scope, polarity, runtime = first.full, first.scope, first.polarity, first.runtime
read, save, digest = first.read, first.save, first.digest
SCHEMA = 'object_attribute_flat_v2'
PARENT = ROOT / 'data/learning/object-attribute-flat'
FLAT = '回單一平面 JSON，不使用巢狀結構。A_state、B_state 各只能填 T、F 或 U。'
DIRECT = FLAT + '例如 {"A_state":"F","B_state":"U"}。例子只示意格式。'
SPLIT = (FLAT + '各自判斷原句是否指向該物件，以及是否觀察該屬性。'
         '物件不同時，原句仍可能涉及同一屬性。狀態相反時，屬性仍相同。'
         'A_object、A_property、B_object、B_property 只填 true 或 false。'
         '例如 {"A_state":"F","B_state":"U","A_object":true,"A_property":true,'
         '"B_object":true,"B_property":false}。例子只示意格式。')
STATE_KEYS = {'A_state', 'B_state'}
FLAGS = {'A_object', 'A_property', 'B_object', 'B_property'}


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def cases():
    output = []
    for case in first.cases():
        if case['group'] != 'control':
            text = case['question'].rsplit('\n', 1)[0]
            case = {**case, 'question': text + '\n' + DIRECT, 'semantic_question': text + '\n' + SPLIT}
        output.append(case)
    return output


def sources():
    hashes = first.sources()
    path = Path(__file__).resolve()
    hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def baseline(name):
    folder = first.location(name)
    plan = first.frozen(folder)
    report = first.result(folder, plan, read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('原巢狀對照缺少完整證據。')
    return dict(name=name, protocol_sha256=digest(plan), report_sha=report['report_sha'], exit_sha=report['exit_sha']), plan


def bind(plan, old):
    first.recovery.same_weights(plan, old)
    for case, earlier in zip(plan['cases'], old['cases'], strict=True):
        if (case['id'] != earlier['id'] or case['history'] != earlier['history']
                or case.get('gold') != earlier.get('gold') or case.get('records') != earlier.get('records')
                or case['question'].rsplit('\n', 1)[0] != earlier['question'].rsplit('\n', 1)[0]):
            raise ValueError('平面格式對照的原句或題目已變更。')


def prepare(name, reference, previous):
    receipt, old = baseline(previous)
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources, location=location):
        plan = full.prepare(name, reference)
    bind(plan, old)
    plan['baseline'] = receipt
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
    receipt, old = baseline(plan['baseline']['name'])
    bind(plan, old)
    if plan['baseline'] != receipt:
        raise ValueError('原巢狀對照的證據已變更。')
    return plan


def normalize(row, *, split):
    raw = row['raw']
    if not row['ended'] or not 1 <= len(raw) <= 2048:
        return ''
    try:
        obj = polarity.strict_json(raw)
    except (ValueError, RecursionError):
        return ''
    keys = STATE_KEYS | FLAGS if split else STATE_KEYS
    if (not isinstance(obj, dict) or obj.keys() != keys or any(obj[key] not in first.STATES for key in STATE_KEYS)
            or (split and any(type(obj[key]) is not bool for key in FLAGS))):
        return ''
    fields = ('state', 'object', 'property') if split else ('state',)
    return json.dumps({slot: {field: obj[f'{slot}_{field}'] for field in fields} for slot in first.SLOTS})


def grade(case, row, *, split):
    if case['group'] != 'control':
        row = {**row, 'raw': normalize(row, split=split)}
    return ORIGINAL_GRADE(case, row, split=split)


ORIGINAL_GRADE = first.grade


def summarize(fixture, rows):
    with patch.object(first, 'grade', grade):
        return first.summarize(fixture, rows)


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
