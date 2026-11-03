"""Record explicit criterion reviews without replacing original answers."""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_understanding import save_report
from scripts.evaluation_reports import (REVIEWERS, RUNTIME_FINGERPRINTS, MAX_JSON_BYTES,
    load_json, validate_report, validate_review, verify_suite, evidence_digest)


def apply_reviews(report, reviews, reviewer):
    rows = validate_report(report)
    if len(rows) != len(report['case_ids']):
        raise ValueError('請先完成本次案例收集，再寫入評讀，以免與執行中的評估互相覆寫。')
    if not isinstance(reviewer, str) or reviewer not in REVIEWERS:
        raise ValueError('需明確指定評讀者類型。')
    if not isinstance(reviews, list) or not reviews:
        raise ValueError('評讀清單不能為空。')
    seen, pending = set(), []
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get('id'), str):
            raise ValueError('評讀項目需有文字案例編號。')
        id = review['id']
        if id not in rows or id in seen:
            raise ValueError('評讀案例不存在或重複：' + str(id))
        seen.add(id)
        values = review.get('criteria_pass')
        if not isinstance(values, list) or len(values) != len(rows[id]['criteria']) or not all(type(value) is bool for value in values):
            raise ValueError('criteria_pass 必須逐一對應每項標準，使用 true 或 false。')
        if not isinstance(review.get('note'), str) or not review['note'].strip():
            raise ValueError('每題評讀需要具體理由。')
        pending.append((rows[id], {'reviewer': reviewer, 'criteria_pass': values,
                                  'result': 'pass' if all(values) else 'fail', 'note': review['note'],
                                  'reviewed_at': datetime.now(timezone.utc).isoformat(),
                                  'evidence_sha256': evidence_digest(report, rows[id])}))
    # Validate the whole batch before mutating any review.
    for row, review in pending:
        validate_review(review, row, report)
    for row, review in pending:
        row.setdefault('reviews', []).append(review)
    return report


def summarize(report, *, suite_bytes=None):
    validate_report(report)
    verified = verify_suite(report, suite_bytes)
    categories = defaultdict(lambda: {'pass': 0, 'fail': 0, 'pending': 0})
    reviewers = defaultdict(lambda: {'pass': 0, 'fail': 0})
    totals = {'pass': 0, 'fail': 0, 'pending': 0}
    for row in report['results']:
        reviews = row.get('reviews', [])
        if reviews:
            latest = reviews[-1]
            outcome = validate_review(latest, row, report)
            reviewers[latest['reviewer']][outcome] += 1
        else:
            outcome = 'pending'
        totals[outcome] += 1
        categories[row['category']][outcome] += 1
    collected = len(report['results'])
    answered = sum(bool(row['answer'].strip()) for row in report['results'])
    return {'collected': collected, 'not_collected': len(report['case_ids']) - collected,
            'answered': answered, 'not_answered': len(report['case_ids']) - answered,
            'empty_answers': collected - answered,
            'collection_errors': sum('error' in row for row in report['results']),
            'reviews': totals, 'latest_reviewers': sorted(reviewers),
            'by_reviewer': dict(reviewers), 'by_category': dict(categories),
            'suite_verified': verified,
            'bound_review_ids': [row['id'] for row in report['results'] if row.get('reviews') and 'evidence_sha256' in row['reviews'][-1]],
            'unbound_review_ids': [row['id'] for row in report['results'] if row.get('reviews') and 'evidence_sha256' not in row['reviews'][-1]],
            'missing_prompt_ids': [row['id'] for row in report['results'] if 'prompt' not in row],
            'mechanical_failures': sum(any(value is False for value in row.get('mechanical_checks', {}).values()) for row in report['results']),
            'note': 'Counts describe recorded rows/nonempty answer fields and latest explicit reviews, not task completion or IQ. Structural/suite consistency does not authenticate model calls, inputs or reviewer identity; no independent general intelligence score.'}


def compare(first, second, *, suite_bytes=None):
    summarize(first, suite_bytes=suite_bytes)
    summarize(second, suite_bytes=suite_bytes)
    fa, fb = first.get('fingerprint', {}), second.get('fingerprint', {})
    if not fa.get('suite_sha256') or fa.get('suite_sha256') != fb.get('suite_sha256') or first['case_ids'] != second['case_ids']:
        raise ValueError('對照需要相同案例集指紋與相同案例選取。')
    if first.get('mode') != second.get('mode'):
        raise ValueError('對照的評估模式不同。')
    changed_runtime = {}
    for key in fa.keys() | fb.keys():
        if fa.get(key) != fb.get(key):
            if key not in RUNTIME_FINGERPRINTS:
                raise ValueError('測試腳本、來源產生器或其他固定條件指紋不同，不能直接比較。')
            changed_runtime[key] = {'before': fa.get(key), 'after': fb.get(key)}
    a = {row['id']: row for row in first['results']}
    b = {row['id']: row for row in second['results']}
    transitions, pending, changed_reviewers, changed_requests = [], [], [], []
    for id in first['case_ids']:
        if id in a and id in b:
            for field in ('category', 'criteria', 'prompt', 'source_sha256', 'fixture_sha256'):
                if a[id].get(field) != b[id].get(field) or (field in a[id]) != (field in b[id]):
                    raise ValueError('同題問題、分類、標準或輸入來源不一致，不能直接比較。')
            if a[id].get('request_sha256') != b[id].get('request_sha256'):
                changed_requests.append(id)
        if id not in a or id not in b or not a[id].get('reviews') or not b[id].get('reviews'):
            pending.append(id)
            continue
        before, after = a[id]['reviews'][-1], b[id]['reviews'][-1]
        if before['reviewer'] != after['reviewer']:
            changed_reviewers.append(id)
        transitions.append({'id': id, 'before': before['result'], 'after': after['result'],
                            'before_reviewer': before['reviewer'], 'after_reviewer': after['reviewer'],
                            'before_evidence_bound': 'evidence_sha256' in before,
                            'after_evidence_bound': 'evidence_sha256' in after})
    return {'compared': len(transitions), 'pending_ids': pending, 'transitions': transitions,
            'reviewer_changed_ids': changed_reviewers, 'changed_runtime_fingerprints': changed_runtime,
            'changed_request_ids': changed_requests, 'suite_verified': suite_bytes is not None,
            'runner_fingerprint_recorded': 'runner_sha256' in fa,
            'matching_recorded_source_ids': [id for id in first['case_ids'] if id in a and id in b and 'source_sha256' in a[id]],
            'missing_source_fingerprint_ids': [id for id in first['case_ids'] if id in a and id in b and 'source_sha256' not in a[id]],
            'missing_prompt_ids': [id for id in first['case_ids'] if id in a and id in b and 'prompt' not in a[id]],
            'note': 'Single-run paired recorded reviews, not authenticated evidence or an intelligence score. Model service variability and reviewer differences are not controlled; absent input/model fingerprints remain unverified.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, help='逐題評讀 JSON 清單，附 criteria_pass 與 note')
    parser.add_argument('--reviewer', choices=['user', 'development_agent', 'independent_reviewer'])
    parser.add_argument('--suite', type=Path, help='以指定案例集原始位元組核對指紋、問題與評讀標準')
    parser.add_argument('--compare', type=Path, help='與另一份相同案例報告比較')
    args = parser.parse_args()
    try:
        report = load_json(args.report)
        suite_bytes = None
        if args.suite:
            with args.suite.open('rb') as stream:
                suite_bytes = stream.read(MAX_JSON_BYTES + 1)
        if args.reviews:
            if not args.reviewer:
                raise ValueError('寫入評讀需提供 --reviewer。')
            apply_reviews(report, load_json(args.reviews), args.reviewer)
        result = compare(report, load_json(args.compare), suite_bytes=suite_bytes) if args.compare else summarize(report, suite_bytes=suite_bytes)
        # Do not append reviews if suite or paired comparison validation failed.
        if args.reviews:
            save_report(args.report, report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
