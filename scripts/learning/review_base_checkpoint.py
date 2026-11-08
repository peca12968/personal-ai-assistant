"""Recompute a completed short checkpoint assay, including a rejected candidate."""
import argparse
from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evaluation_reports import strict_json
from scripts.learning import assess_base_checkpoint as assay
from scripts.learning.review_batch import read_phase


STATES = {'已確認成立', '已確認不成立', '未知'}
CODE = r'[A-Z0-9\u4e00-\u9fff]{1,4}'
CODE_DECLARATIONS = (
    re.compile(rf'確定可行回「(?P<supported>{CODE})」，確定不可行回「(?P<refuted>{CODE})」，'
               rf'現有資料不足回「(?P<undetermined>{CODE})」。只輸出指定代碼。'),
    re.compile(rf'回覆代碼「(?P<undetermined>{CODE})」表示待確認，「(?P<refuted>{CODE})」表示確定不能，'
               rf'「(?P<supported>{CODE})」表示確定能。整個回答只留代碼。'),
    re.compile(rf'能確定可以回 (?P<supported>{CODE})，能確定不可以回 (?P<refuted>{CODE})，'
               rf'還不能確定回 (?P<undetermined>{CODE})。只輸出代碼。'),
    re.compile(rf'只回一個代碼。(?P<undetermined>{CODE}) 表示資料不足，(?P<supported>{CODE}) 表示確定可行，'
               rf'(?P<refuted>{CODE}) 表示確定不可行。'),
)
CODE_REPLY = re.compile(rf'(?:({CODE})|「({CODE})」|『({CODE})』)。?')
RETENTION_GROUPS = {'heldout', 'transfer', 'guard'}


def outcome(expected, row):
    if not row['ended_with_eos']:
        return 'unfinished'
    output = row['output']
    if output == expected:
        return 'exact'
    if output.strip(' \t\r\n。「」『』') == expected:
        return 'literal_format'
    if expected.startswith(('{', '[')) and output.startswith(('{', '[')):
        try:
            if assay.digest(strict_json(expected.encode('utf-8'))) == assay.digest(strict_json(output.encode('utf-8'))):
                return 'literal_format'
        except (ValueError, UnicodeError, RecursionError):
            pass
    return 'different_state' if expected in STATES and output in STATES else 'other_output'


def declared_codes(prompt):
    matches = [match for rule in CODE_DECLARATIONS for match in rule.finditer(prompt)]
    if len(matches) != 1:
        return None
    match = matches[0]
    if match.start() != 0 and match.end() != len(prompt):
        return None
    codes = {literal: state for state, literal in match.groupdict().items()}
    evidence = prompt[:match.start()] + prompt[match.end():]
    return (codes, evidence) if len(codes) == 3 and evidence else None


def code_reply(row, codes):
    if not row['ended_with_eos']:
        return None
    match = CODE_REPLY.fullmatch(row['output'].strip(' \t\r\n'))
    return codes.get(next(code for code in match.groups() if code is not None)) if match else None


def code_diagnostics(cases, reports):
    groups, evidence_rows = {}, defaultdict(list)
    phases = ('baseline', 'reload')
    rows = [reports[phase]['rows'] for phase in phases]
    for case, *answers in zip(cases, *rows, strict=True):
        declaration = declared_codes(case['prompt'])
        if declaration is None:
            continue
        codes, evidence = declaration
        expected = codes.get(case['answer'])
        if expected is None:
            continue
        group = groups.setdefault(case['group'], {phase: {} for phase in phases})
        observed = {}
        for phase, answer in zip(phases, answers, strict=True):
            state = code_reply(answer, codes)
            exact = answer['ended_with_eos'] and answer['output'] == case['answer']
            if not answer['ended_with_eos']:
                category = 'unfinished'
            elif state is None:
                category = 'unrecognized_output'
            elif state != expected:
                category = 'wrong_declared_verdict'
            else:
                category = 'exact' if exact else 'correct_verdict_format_difference'
            group[phase].setdefault(expected, Counter())[category] += 1
            observed[phase] = state, exact
        evidence_rows[case['group'], evidence].append((case['id'], codes, expected, observed))
    remapped = defaultdict(list)
    for (group, _), variants in evidence_rows.items():
        for left, right in combinations(variants, 2):
            first_id, first_codes, expected, first_observed = left
            second_id, second_codes, target, second_observed = right
            if first_codes == second_codes or expected != target:
                continue
            pair = dict(case_ids=[first_id, second_id], expected=expected)
            for phase in phases:
                first_state, first_exact = first_observed[phase]
                second_state, second_exact = second_observed[phase]
                pair[phase] = dict(decoded=[first_state, second_state],
                                   both_correct=first_state == second_state == expected,
                                   both_exact=first_exact and second_exact)
            remapped[group].append(pair)
    return dict(by_expected_verdict=groups, same_evidence_remapped=dict(remapped))


def verify(folder):
    run, _ = read_phase(folder / 'run.json')
    protocol, series, plan, _, cases, head, record = assay.inputs(folder, run['receipt'])
    if (run['protocol_sha256'] != assay.digest(protocol)
            or assay.segments.idle.validate_sample(run['admitted'])['idle_ms'] < plan['idle_ms']):
        raise ValueError('短題驗收的規約或閒置收據不符。')
    run_hash = assay.digest(run)
    reports, report_hashes, exit_hashes = {}, {}, {}
    for phase in assay.PHASES:
        report, report_hashes[phase] = read_phase(folder / (phase + '.json'))
        exited, exit_hashes[phase] = read_phase(folder / (phase + '.exit.json'))
        with (folder / (phase + '.started')).open(encoding='ascii') as marker:
            started = marker.read(32)
        pid = report['pid']
        if (report['phase'] != phase or report['run_sha256'] != run_hash
                or type(pid) is not int or pid <= 0 or started != str(pid)
                or type(exited['worker']) is not int or exited['worker'] != pid
                or exited['exit_confirmed'] is not True
                or type(exited['returncode']) is not int or exited['returncode'] != 0):
            raise ValueError('短題工作程序的啟動或退出證據不符。')
        reports[phase] = report
    assay.segments.pin_blob(series, head['blob'])
    checked = assay.assess(cases, reports, record, assay.batches.adapter_files(folder / 'adapter'))
    expected = {**checked, 'run_sha256': run_hash, 'report_sha256': report_hashes, 'exit_sha256': exit_hashes}
    saved, _ = read_phase(folder / 'review.json')
    if assay.digest(saved) != assay.digest(expected):
        raise ValueError('保存的短題報告與重算結果不符。')
    return cases, reports, checked


def retention(previous, current):
    old_cases, old_reports, old_checked = previous
    new_cases, new_reports, new_checked = current
    available = {}
    for case, baseline in zip(new_cases, new_reports['baseline']['rows'], strict=True):
        if case['group'] in RETENTION_GROUPS:
            if case['prompt'] in available:
                raise ValueError('新驗收的同一保留題有多筆紀錄。')
            available[case['prompt']] = case, baseline
    groups, regressed, improved = {}, [], []
    for old, old_baseline in zip(old_cases, old_reports['baseline']['rows'], strict=True):
        group = old['group']
        if group not in RETENTION_GROUPS:
            continue
        match = available.pop(old['prompt'], None)
        if match is None:
            raise ValueError('新驗收缺少早期保留題、轉移題或基本控制。')
        new, new_baseline = match
        if (old['answer'] != new['answer'] or any(old_baseline[key] != new_baseline[key]
                                                for key in ('output', 'tokens', 'ended_with_eos'))):
            raise ValueError('兩次驗收的答案規約或原始底座回覆不一致。')
        before = old_checked['scores']['reload'][old['id']]
        after = new_checked['scores']['reload'][new['id']]
        counts = groups.setdefault(group, dict(count=0, prior=0, current=0))
        counts['count'] += 1
        counts['prior'] += before
        counts['current'] += after
        if before != after:
            (improved if after else regressed).append(dict(previous_id=old['id'], current_id=new['id'], group=group))
    if not groups:
        raise ValueError('早期驗收沒有可比較的保留題。')
    return dict(groups=groups, all_prior_correct_retained=not regressed,
                improved=improved, regressed=regressed)


def review(name, retained_from=None):
    verified = verify(assay.assay_folder(name))
    cases, reports, checked = verified
    groups = {}
    for case, baseline, reload in zip(cases, reports['baseline']['rows'], reports['reload']['rows'], strict=True):
        group = case['group']
        result = groups.setdefault(group, {**checked['groups'][group], 'baseline_outcomes': Counter(),
                                           'reload_outcomes': Counter(), 'improved_ids': [], 'regressed_ids': []})
        result['baseline_outcomes'][outcome(case['answer'], baseline)] += 1
        result['reload_outcomes'][outcome(case['answer'], reload)] += 1
        cid = case['id']
        before, after = (checked['scores'][phase][cid] for phase in ('baseline', 'reload'))
        if before != after:
            result['improved_ids' if after else 'regressed_ids'].append(cid)
    result = dict(assay=name, scope=checked['scope'], passed=checked['passed'], groups=groups,
                  failed_checks=[name for name, passed in checked['checks'].items() if not passed])
    codes = code_diagnostics(cases, reports)
    if codes['by_expected_verdict']:
        result['decision_codes'] = codes
    if retained_from is not None:
        if retained_from == name:
            raise ValueError('請指定不同的早期驗收。')
        prior = verify(assay.assay_folder(retained_from))
        result['retention'] = dict(previous_assay=retained_from, **retention(prior, verified))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--retained-from', help='Recompute retention of an earlier verified short assay.')
    args = parser.parse_args()
    try:
        result = review(args.name, args.retained_from)
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
