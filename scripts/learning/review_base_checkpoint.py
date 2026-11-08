"""Recompute a completed short checkpoint assay, including a rejected candidate."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evaluation_reports import strict_json
from scripts.learning import assess_base_checkpoint as assay
from scripts.learning.review_batch import read_phase


STATES = {'已確認成立', '已確認不成立', '未知'}


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


def review(name):
    cases, reports, checked = verify(assay.assay_folder(name))
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
    return dict(assay=name, scope=checked['scope'], passed=checked['passed'], groups=groups,
                failed_checks=[name for name, passed in checked['checks'].items() if not passed])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    try:
        result = review(args.name)
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
