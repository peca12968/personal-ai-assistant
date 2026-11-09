"""Compare literal citations, fixed source IDs and a separate time-eligibility gate."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import assess_natural_scope as natural

scope, full, polarity = natural.scope, natural.full, natural.polarity
short, gpu, batches = natural.short, natural.gpu, natural.batches
read, save, digest = natural.read, natural.save, natural.digest
SCHEMA = 'indexed_scope_evidence_v1'
PARENT = ROOT / 'data/learning/indexed-scope-evidence'
FIXTURE = ROOT / 'evals/indexed_scope_evidence.json'
DIRECTIVE = ('對 A、B 各回當前狀態及對應原句。T 是確認成立，F 是確認不成立，U 是未能確認。'
             'source 填同一則問題中對應整句的整數編號。如沒有可核對的當前觀察，填 U，source 填 null。'
             '只回 JSON，例如 {"A":{"state":"T","source":2},"B":{"state":"U","source":null}}。'
             '例中的狀態與編號僅示意。')
UNDATED_MEDIA = re.compile(
    r'^[^「『“（(。！？；，,]{0,42}(?:無日期|未標日期|未標示日期|日期不明|未標時間|未標示拍攝時間|拍攝時間不明)'
    r'的?(?:照片|相片|截圖|影像)')


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def manifest(case):
    sources = []
    for index, span in enumerate(case['spans'], 1):
        quote = natural.source_text(case['text'], span)
        reason = span['kind'] if span['kind'] in ('rule', 'earlier') else None
        if reason is None and UNDATED_MEDIA.match(quote):
            reason = 'undated_media'
        sources.append(dict(id=index, start=span['start'], end=span['end'], quote=quote, excluded=reason))
    return sources


def index_case(case):
    if case['group'] == 'control':
        return case
    sources = manifest(case)
    text = case['text']
    indexed = ''.join(f'[{row["id"]}] ' + text[row['start']:row['end']] for row in sources)
    directive = case['question'][len(text):]
    if not directive.endswith(natural.DIRECTIVE):
        raise ValueError('原引句規約無法核對。')
    directive = directive[:-len(natural.DIRECTIVE)] + DIRECTIVE
    return {**case, 'semantic_question': indexed + directive, 'source_manifest': sources}


def cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('固定索引題庫格式不符。')
    baseline = natural.cases()
    rows = data['cases']
    new = [natural.compile_case(row) for row in rows]
    if ([row['id'] for row in rows] != ['cabinet_photo_unknown', 'cabinet_photo_current',
                                       'panel_old_test', 'panel_current_veto']
            or any(row['group'] != 'fresh' for row in rows)):
        raise ValueError('固定索引的兩組新題不符。')
    for first, second in zip(new[::2], new[1::2], strict=True):
        left, right = manifest(first), manifest(second)
        if (first['history'] != second['history'] or len(left) != len(right)
                or sum(a['quote'] != b['quote'] for a, b in zip(left, right, strict=True)) != 1
                or first['answer'] == second['answer']):
            raise ValueError('新配對須保留舊輪且只換一條觀察。')
    return [index_case({**case, 'group': 'replay' if case['group'] == 'fresh' else case['group']})
            for case in baseline] + [index_case(case) for case in new]


def sources():
    hashes = natural.sources()
    for path in (FIXTURE, Path(__file__).resolve()):
        hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def baseline(name):
    folder = natural.location(name)
    plan = natural.frozen(folder)
    run = read(folder / 'run.json')
    report = natural.result(folder, plan, run)
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('原引句測量缺少可重算的完成證據。')
    receipt = dict(name=name, protocol_sha256=digest(plan), report_sha=report['report_sha'], exit_sha=report['exit_sha'])
    return plan, receipt, read(folder / 'paired.json')['rows']['direct']


def bind_baseline(plan, old_plan):
    old_cases = old_plan['cases']
    if (any(plan[key] != old_plan[key] for key in ('model', 'selection', 'description', 'files', 'reference_protocol'))
            or [row['id'] for row in plan['cases'][:len(old_cases)]] != [row['id'] for row in old_cases]):
        raise ValueError('原引句比較使用的題目或權重不符。')
    for case, old in zip(plan['cases'][:len(old_cases)], old_cases, strict=True):
        cid = case['id']
        if (case['question'] != old['question'] or plan['contexts'][cid] != old_plan['contexts'][cid]
                or plan['prompts'][cid]['direct'] != old_plan['prompts'][cid]['direct']):
            raise ValueError('原引句的完整提示未重現。')


def prepare(name, reference, previous):
    old_plan, receipt, _ = baseline(previous)
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources, location=location):
        plan = full.prepare(name, reference)
    bind_baseline(plan, old_plan)
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
    old, receipt, _ = baseline(plan['baseline']['name'])
    bind_baseline(plan, old)
    if plan['baseline'] != receipt:
        raise ValueError('原引句測量的凍結來源已變更。')
    return plan


def parse_indices(row):
    raw = row['raw']
    if not row['ended'] or not 1 <= len(raw) <= 2048:
        return None
    try:
        obj = polarity.strict_json(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.keys() != {'A', 'B'}:
        return None
    output = []
    for slot in ('A', 'B'):
        item = obj[slot]
        if (not isinstance(item, dict) or item.keys() != {'state', 'source'} or item['state'] not in natural.STATES
                or (item['source'] is not None and type(item['source']) is not int)):
            return None
        output.append([item['state'], item['source']])
    return output


def resolve(case, facts):
    sources = {row['id']: row for row in case['source_manifest']}
    restored, guarded, rejected = [], [], []
    for state, source in facts:
        entry = sources.get(source)
        quote = entry['quote'] if entry else None
        reason = entry['excluded'] if entry else (
            'out_of_range' if source is not None else 'missing_source' if state != 'U' else None)
        restored.append([state, quote])
        guarded.append(['U', None] if reason else [state, quote])
        rejected.append(reason)
    return restored, guarded, rejected


def grade(case, row):
    if case['group'] == 'control':
        answer = natural.grade(case, row)
        return dict(**answer, gated_correct=answer['correct'])
    facts = parse_indices(row)
    restored, guarded, rejected = resolve(case, facts) if facts else ([], [], [])
    raw = polarity.conclusion([item[0] for item in restored]) if facts else None
    gated = polarity.conclusion([item[0] for item in guarded]) if facts else None
    return dict(facts=facts, restored=restored, guarded=guarded, rejected=rejected, conclusion=raw,
                correct=raw == case['answer'], gated_conclusion=gated, gated_correct=gated == case['answer'],
                states=sum(a[0] == b[0] for a, b in zip(restored, case['gold'])),
                exact=sum(a == b and reason != 'out_of_range' for a, b, reason in zip(restored, case['gold'], rejected)),
                gated_exact=sum(a == b for a, b in zip(guarded, case['gold'])),
                rejected_count=sum(reason is not None for reason in rejected), valid=facts is not None)


def summarize(fixture, rows):
    groups, details = {}, {}
    for case, direct, split in zip(fixture, rows['direct'], rows['split'], strict=True):
        cid, group = case['id'], case['group']
        if direct['id'] != cid or split['id'] != cid:
            raise ValueError('固定索引的案例順序不符。')
        if group == 'control' and any(direct[key] != split[key] for key in
                                     ('raw', 'ended', 'delivered', 'input_tokens', 'wire_sha')):
            raise ValueError('一般輸出控制未重現原提示與原答。')
        details[cid] = dict(expected=case['answer'], gold=case.get('gold'),
                            direct=natural.grade(case, direct), split=grade(case, split))
        for arm in ('direct', 'split'):
            outcome = details[cid][arm]
            totals = groups.setdefault(group, {}).setdefault(arm, {'count': 0})
            totals['count'] += 1
            for key in ('correct', 'states', 'exact', 'valid', 'gated_correct', 'gated_exact', 'rejected_count'):
                if key in outcome:
                    totals[key] = totals.get(key, 0) + outcome[key]
    return dict(groups=groups, details=details, automatic_activation=False)


def result(folder, plan, run):
    with patch.multiple(scope, SCHEMA=SCHEMA, summarize=summarize):
        report = scope.result(folder, plan, run)
    _, receipt, old_rows = baseline(plan['baseline']['name'])
    if receipt != plan['baseline']:
        raise ValueError('原引句測量的證據已變更。')
    rows = read(folder / 'paired.json')['rows']['direct']
    for now, old in zip(rows[:len(old_rows)], old_rows, strict=True):
        if any(now[key] != old[key] for key in ('id', 'raw', 'ended', 'delivered', 'wire_sha', 'input_tokens')):
            raise ValueError('同一引句提示未重現原作答與輸入。')
    return dict(**report, reproduced_direct=len(old_rows))


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
                raise TimeoutError('固定索引對照已達時限。')
            if read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('固定索引對照的來源或規約已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, digest(run)]
            check()
            gpu.run_owned(command, folder, scope.PHASE, lambda: gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('固定索引對照的參考來源已變更。')
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
        raise ValueError('固定索引報告與重算證據不符。')
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
        print(len(cases()))
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
