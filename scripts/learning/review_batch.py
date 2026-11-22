"""Inspect local batch results, separating cancelled progress from completed comparisons."""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from assistant import learning_batches as batches, local_models
from scripts.evaluation_reports import strict_json


def read_phase(path):
    limit = 2 * 1024 * 1024
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('階段報告超過讀取上限。')
    return strict_json(raw), hashlib.sha256(raw).hexdigest()


def cancelled_progress(path, manifest, receipt):
    phase = receipt.get('phase')
    if phase is not None and phase not in batches.PHASES:
        raise ValueError('取消紀錄包含未知階段。')
    completed = {}
    # A child can create partial files for the interrupted phase. Only grade
    # phases the controller had already advanced past, using their final rows.
    previous_phases = batches.PHASES[:batches.PHASES.index(phase)] if phase else ()
    for previous in previous_phases:
        report_path = path / (previous + '.json')
        report, digest = read_phase(report_path)
        scores = batches.grades(manifest, report, previous)
        groups = {}
        for case in batches.evaluation_cases(manifest):
            counts = groups.setdefault(case['group'], {'correct': 0, 'total': 0})
            counts['total'] += 1
            counts['correct'] += int(scores[case['id']])
        completed[previous] = {'groups': groups, 'observed_sha256': digest}
    steps = None
    progress_path = path / 'training.partial.json'
    if phase in ('train', 'reload') and progress_path.is_file():
        history = local_models.read_json(progress_path)
        if not isinstance(history, list) or len(history) > manifest['recipe']['steps']:
            raise ValueError('部分步驟紀錄長度不符。')
        for i, row in enumerate(history, 1):
            if (not isinstance(row, dict) or set(row) != {'step', 'loss'}
                    or type(row['step']) is not int or row['step'] != i
                    or type(row['loss']) not in (int, float) or not math.isfinite(row['loss'])):
                raise ValueError('部分步驟紀錄順序或內容不符。')
        steps = len(history)
    return {'batch': manifest['id'], 'scope': 'cancelled_batch_observations', 'status': 'cancelled',
            'phase_at_stop': phase, 'completed_phase_files': completed,
            'recorded_training_steps': steps, 'planned_training_steps': manifest['recipe']['steps']}


def score_groups(cases, scores):
    groups = {}
    for case in cases:
        cid = case['id']
        group = groups.setdefault(case['group'], {'total': 0, 'baseline': 0, 'train': 0, 'reload': 0,
                                                  'improved_ids': [], 'regressed_ids': [], 'reload_missed_ids': []})
        group['total'] += 1
        for phase in batches.PHASES:
            group[phase] += int(scores[phase][cid])
        if scores['reload'][cid] and not scores['baseline'][cid]:
            group['improved_ids'].append(cid)
        if not scores['reload'][cid]:
            group['reload_missed_ids'].append(cid)
            if scores['baseline'][cid]:
                group['regressed_ids'].append(cid)
    return groups


def heldout_by_origin(data_dir, manifest, cases, scores):
    parent = manifest['parent']
    inherited = []
    if parent is not None:
        path = batches.folder(data_dir, parent['id']) / 'manifest.json'
        try:
            saved = local_models.read_json(path, 8 * 1024 * 1024)
            digest = batches.queue.digest(saved)
        except FileNotFoundError:
            return {'status': 'parent_missing'}
        except (ValueError, OSError, RecursionError):
            return {'status': 'parent_unverified'}
        if (not isinstance(saved, dict) or saved.get('id') != parent['id']
                or digest != parent['manifest_sha256']):
            return {'status': 'parent_unverified'}
        inherited = saved.get('checks')
        if not isinstance(inherited, list) or manifest['checks'][:len(inherited)] != inherited:
            return {'status': 'parent_unverified'}
    heldout = [case for case in cases if case['group'] == 'heldout']
    by_origin = [{**case, 'group': 'inherited' if index < len(inherited) else 'added'}
                 for index, case in enumerate(heldout)]
    return {'status': 'verified', 'groups': score_groups(by_origin, scores)}


def review(data_dir, batch_id):
    path = batches.folder(data_dir, batch_id)
    manifest = batches.read_manifest(path, live=True)
    receipt = local_models.read_json(path / 'receipt.json')
    if (not isinstance(receipt, dict) or manifest['id'] != batch_id or receipt.get('schema') != 'confirmed_learning_receipt_v1'
            or receipt.get('manifest_sha256') != batches.queue.digest(manifest)):
        raise ValueError('批次編號或規約與保存紀錄不符。')
    if receipt.get('status') == 'cancelled':
        return cancelled_progress(path, manifest, receipt)
    if receipt.get('status') not in ('candidate', 'rejected'):
        raise ValueError('需要已完成三階段評分或已取消的批次。')

    reports = {}
    hashes = {}
    for phase in batches.PHASES:
        report_path = path / (phase + '.json')
        reports[phase], hashes[phase] = read_phase(report_path)
    if receipt.get('report_files') != hashes:
        raise ValueError('階段報告檔案與紀錄指紋不符。')
    assessment = batches.assess(manifest, reports)
    if (batches.queue.encoded(assessment) != batches.queue.encoded(receipt.get('assessment'))
            or assessment['passed'] != (receipt['status'] == 'candidate')):
        raise ValueError('重算結果與批次判定不符。')
    if batches.adapter_files(path / 'adapter') != reports['train'].get('adapter_files'):
        raise ValueError('保存的參數檔案與訓練報告不符。')

    cases = batches.evaluation_cases(manifest)
    scores = assessment['scores']
    groups = score_groups(cases, scores)
    origins = heldout_by_origin(data_dir, manifest, cases, scores)
    return {'batch': batch_id, 'scope': 'saved_short_prompt_phases', 'status': receipt['status'],
            'groups': groups, 'heldout_by_origin': origins, 'failed_checks': [key for key, passed in assessment['checks'].items() if not passed]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--batch', required=True)
    args = parser.parse_args()
    try:
        result = review(args.data_dir, args.batch)
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, '無法核對批次：' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'candidate' else 1


if __name__ == '__main__':
    raise SystemExit(main())
