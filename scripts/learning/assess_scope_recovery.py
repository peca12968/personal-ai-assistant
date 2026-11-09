"""Evaluate one-pass recovery after an indexed observation has been rejected."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import assess_indexed_scope as indexed

natural, scope, full, polarity = indexed.natural, indexed.scope, indexed.full, indexed.polarity
short, gpu, batches = indexed.short, indexed.gpu, indexed.batches
read, save, digest = indexed.read, indexed.save, indexed.digest
SCHEMA = 'scope_evidence_recovery_v1'
PARENT = ROOT / 'data/learning/scope-evidence-recovery'
FIXTURE = ROOT / 'evals/scope_evidence_recovery.json'
REASONS = {'rule': '規則文字', 'earlier': '較早的紀錄', 'undated_media': '拍攝時間不明',
           'out_of_range': '找不到這個編號', 'missing_source': '未提供原句'}
DIRECTIVE = ('核對同一物件及本輪實際觀察。T 表示確認成立，F 表示確認不成立，U 表示待查。'
             '有對應的本輪原句時填該整句編號。沒有當輪證據時填 U 和 null。'
             '只回 JSON，格式為 {"state":"T","source":3}。例中的狀態與編號僅示意。')


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def baseline(name):
    folder = indexed.location(name)
    plan = indexed.frozen(folder)
    report = indexed.result(folder, plan, read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('索引測量缺少可重算的完成證據。')
    rejected = [dict(id=case['id'], facts=report['details'][case['id']]['split']['facts'])
                for case in plan['cases'] if report['details'][case['id']]['split'].get('rejected_count', 0)]
    return dict(name=name, protocol_sha256=digest(plan), report_sha=report['report_sha'], exit_sha=report['exit_sha'],
                targets=rejected), plan


def compile_recovery(row, facts, group):
    case = indexed.index_case(natural.compile_case(row))
    restored, guarded, rejected = indexed.resolve(case, facts)
    targets = [i for i, reason in enumerate(rejected) if reason is not None]
    if len(targets) != 1:
        raise ValueError('每題須恰有一個被排除的欄位。')
    slot = targets[0]
    previous_id = facts[slot][1]
    failed = f'原引用編號 {previous_id}' if previous_id is not None else '原引用'
    intro = '重新核對命題「' + row['claims'][slot] + '」。' + failed + '已排除，原因是' + REASONS[rejected[slot]] + '。'

    def prompt(records):
        lines = '\n'.join(f'[{item["id"]}] {case["text"][item["start"]:item["end"]]}' for item in records)
        return intro + '\n' + lines + '\n' + DIRECTIVE

    entries = case['source_manifest']
    allowed = [item for item in entries if item['excluded'] is None and item['id'] != previous_id]
    return {**case, 'group': group, 'question': prompt(entries), 'semantic_question': prompt(allowed), 'target': slot,
            'initial': facts, 'initial_restored': restored, 'initial_guarded': guarded, 'initial_rejected': rejected,
            'allowed_ids': [item['id'] for item in allowed]}


def fresh_cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('證據重查題庫格式不符。')
    raw = data['cases']
    if [row['id'] for row in raw] != ['silver_veto', 'silver_pending', 'strap_missing', 'strap_current']:
        raise ValueError('證據重查的兩組新題不符。')
    compiled = [compile_recovery({k: v for k, v in row.items() if k != 'initial'}, row['initial'], 'fresh') for row in raw]
    for a, b in zip(compiled[::2], compiled[1::2], strict=True):
        if (a['history'] != b['history'] or a['initial'] != b['initial'] or a['target'] != b['target']
                or sum(left['quote'] != right['quote'] for left, right in
                       zip(a['source_manifest'], b['source_manifest'], strict=True)) != 1 or a['answer'] == b['answer']):
            raise ValueError('新配對須保留相同前文與初始引用，且只替換一筆觀察。')
    return compiled


def cases(receipt):
    original = {row['id']: row for fixture in (natural.FIXTURE, indexed.FIXTURE) for row in read(fixture)['cases']}
    targets = receipt['targets']
    if [item['id'] for item in targets] != ['catch_repaired', 'undated_photo', 'cabinet_photo_unknown']:
        raise ValueError('須保留前次全部實際被排除的案例。')
    previous = [compile_recovery(original[row['id']], row['facts'], 'replay') for row in targets]
    control = natural.compile_case(original['literal_control'])
    return [*previous, *fresh_cases(), control]


def sources():
    hashes = indexed.sources()
    for path in (FIXTURE, Path(__file__).resolve()):
        hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def same_weights(plan, previous):
    if any(plan[key] != previous[key] for key in ('model', 'selection', 'description', 'files', 'reference_protocol')):
        raise ValueError('證據重查的參考權重不符。')


def prepare(name, reference, previous):
    receipt, old = baseline(previous)
    with patch.multiple(full, SCHEMA=SCHEMA, cases=lambda: cases(receipt), sources=sources, location=location):
        plan = full.prepare(name, reference)
    same_weights(plan, old)
    plan['baseline'] = receipt
    folder = location(name)
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    receipt = read(folder / 'protocol.json')['baseline']
    with patch.multiple(full, SCHEMA=SCHEMA, cases=lambda: cases(receipt), sources=sources):
        return full.frozen(folder)


def checked(folder):
    with patch.object(scope, 'frozen', frozen):
        plan = scope.checked(folder)
    receipt, old = baseline(plan['baseline']['name'])
    same_weights(plan, old)
    if receipt != plan['baseline']:
        raise ValueError('先前索引或排除結果已變更。')
    return plan


def parse_fact(row):
    raw = row['raw']
    if not row['ended'] or not 1 <= len(raw) <= 2048:
        return None
    try:
        item = polarity.strict_json(raw)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(item, dict) or item.keys() != {'state', 'source'} or item['state'] not in natural.STATES
            or (item['source'] is not None and type(item['source']) is not int)):
        return None
    return [item['state'], item['source']]


def grade(case, row):
    if case['group'] == 'control':
        return natural.grade(case, row)
    target = case['target']
    fact = parse_fact(row)
    updated = list(case['initial_guarded'])
    reason = 'invalid_reply'
    if fact is not None:
        _, guarded, reasons = indexed.resolve(case, [fact])
        reason = reasons[0]
        if reason is None and fact[1] is not None and fact[1] not in case['allowed_ids']:
            reason = 'outside_recovery_set'
        if reason is None:
            updated[target] = guarded[0]
    verdict = polarity.conclusion([item[0] for item in updated])
    before = polarity.conclusion([item[0] for item in case['initial_guarded']])
    return dict(fact=fact, reason=reason, states=updated, conclusion=verdict, correct=verdict == case['answer'],
                before=before, before_correct=before == case['answer'], exact_target=updated[target] == case['gold'][target],
                changed=updated[target] != case['initial_guarded'][target], valid=fact is not None)


def summarize(fixture, rows):
    groups, details = {}, {}
    for case, direct, split in zip(fixture, rows['direct'], rows['split'], strict=True):
        cid, group = case['id'], case['group']
        if direct['id'] != cid or split['id'] != cid:
            raise ValueError('證據重查案例順序不符。')
        if group == 'control' and any(direct[key] != split[key] for key in
                                     ('raw', 'ended', 'delivered', 'input_tokens', 'wire_sha')):
            raise ValueError('一般控制未重現同一提示與原答。')
        details[cid] = dict(expected=case['answer'], gold=case.get('gold'),
                            direct=grade(case, direct), split=grade(case, split))
        for arm in ('direct', 'split'):
            outcome = details[cid][arm]
            totals = groups.setdefault(group, {}).setdefault(arm, {'count': 0})
            totals['count'] += 1
            for metric in ('correct', 'before_correct', 'exact_target', 'changed', 'valid'):
                if metric in outcome:
                    totals[metric] = totals.get(metric, 0) + outcome[metric]
    return dict(groups=groups, details=details, automatic_activation=False)


def result(folder, plan, run):
    with patch.multiple(scope, SCHEMA=SCHEMA, summarize=summarize):
        return scope.result(folder, plan, run)


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        plan = checked(folder)
        admitted = short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(plan), admitted=admitted)
        save(folder / 'run.json', run)
        deadline = time.monotonic() + polarity.LIMIT_SECONDS

        def check():
            gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('證據重查已達時限。')
            if read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('證據重查的來源或規約已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, digest(run)]
            check()
            gpu.run_owned(command, folder, scope.PHASE, lambda: gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('證據重查的先前證據已變更。')
            report = result(folder, plan, run)
            save(folder / 'review.json', report)
            return report
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    report = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('證據重查報告與重算證據不符。')
    return report


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
        print(len(fresh_cases()))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference, args.previous)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        with patch.object(polarity, 'frozen', frozen):
            polarity.worker(location(args.name), args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
