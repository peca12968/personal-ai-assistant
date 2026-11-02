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


def apply_reviews(report, reviews, reviewer):
    if len(report['results']) != len(report['case_ids']):
        raise ValueError('請先完成本次案例收集，再寫入評讀，以免與執行中的評估互相覆寫。')
    if reviewer not in {'user', 'development_agent', 'independent_reviewer'}:
        raise ValueError('需明確指定評讀者類型。')
    if not isinstance(reviews, list) or not reviews:
        raise ValueError('評讀清單不能為空。')
    rows = {row['id']: row for row in report['results']}
    seen, pending = set(), []
    for review in reviews:
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
                                  'reviewed_at': datetime.now(timezone.utc).isoformat()}))
    # Validate the whole batch before mutating any review.
    for row, review in pending:
        row.setdefault('reviews', []).append(review)
    return report


def summarize(report):
    categories = defaultdict(lambda: {'pass': 0, 'fail': 0, 'pending': 0})
    totals = {'pass': 0, 'fail': 0, 'pending': 0}
    reviewers = set()
    for row in report['results']:
        reviews = row.get('reviews', [])
        if reviews:
            latest = reviews[-1]
            valid = (len(latest['criteria_pass']) == len(row['criteria']) and
                     all(type(value) is bool for value in latest['criteria_pass']))
            if not valid:
                raise ValueError('評讀內容格式錯誤。')
            outcome = 'pass' if all(latest['criteria_pass']) else 'fail'
            reviewers.add(latest['reviewer'])
        else:
            outcome = 'pending'
        totals[outcome] += 1
        categories[row['category']][outcome] += 1
    missing = len(report['case_ids']) - len(report['results'])
    return {'answered': len(report['results']), 'not_answered': missing, 'reviews': totals,
            'latest_reviewers': sorted(reviewers), 'by_category': dict(categories),
            'mechanical_failures': sum(any(value is False for value in row.get('mechanical_checks', {}).values()) for row in report['results']),
            'note': 'Review results are limited to this run and reviewer; not an independent general intelligence score.'}


def compare(first, second):
    summarize(first)
    summarize(second)
    if first['fingerprint']['suite_sha256'] != second['fingerprint']['suite_sha256'] or first['case_ids'] != second['case_ids']:
        raise ValueError('對照需要相同案例集與相同案例選取。')
    a = {row['id']: row for row in first['results']}
    b = {row['id']: row for row in second['results']}
    transitions, pending = [], []
    for id in first['case_ids']:
        if id not in a or id not in b or not a[id].get('reviews') or not b[id].get('reviews'):
            pending.append(id)
            continue
        before = all(a[id]['reviews'][-1]['criteria_pass'])
        after = all(b[id]['reviews'][-1]['criteria_pass'])
        transitions.append({'id': id, 'before': 'pass' if before else 'fail', 'after': 'pass' if after else 'fail'})
    return {'compared': len(transitions), 'pending_ids': pending, 'transitions': transitions,
            'note': 'Single-run paired results; model service variability and reviewer differences are not controlled.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, help='逐題評讀 JSON 清單，附 criteria_pass 與 note')
    parser.add_argument('--reviewer', choices=['user', 'development_agent', 'independent_reviewer'])
    parser.add_argument('--compare', type=Path, help='與另一份相同案例報告比較')
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding='utf-8'))
        if args.reviews:
            if not args.reviewer:
                raise ValueError('寫入評讀需提供 --reviewer。')
            apply_reviews(report, json.loads(args.reviews.read_text(encoding='utf-8')), args.reviewer)
            save_report(args.report, report)
        result = compare(report, json.loads(args.compare.read_text(encoding='utf-8'))) if args.compare else summarize(report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
