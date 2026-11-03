"""Read-only, explicitly selected evaluation snapshots for the desktop viewer."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from scripts.evaluation_reports import MAX_JSON_BYTES, strict_json
from scripts.review_understanding import summarize, compare

REVIEWERS = {'user':'使用者', 'development_agent':'開發代理', 'independent_reviewer':'獨立評讀者'}
OUTCOMES = {'pass':'通過', 'fail':'未通過', 'pending':'待評讀'}
DETAIL_LIMIT = 24000
PAGE_SIZE = 100


class LoadingCancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise LoadingCancelled('已停止載入。')


def read_selected(path, cancel):
    check_cancel(cancel)
    selected = Path(path).resolve()
    with selected.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    check_cancel(cancel)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('選取檔案超過32 MiB，未載入部分結果。')
    return raw, {'path':str(selected), 'sha256':hashlib.sha256(raw).hexdigest(), 'bytes':len(raw)}


def load_snapshot(paths, cancel=None):
    if not isinstance(paths, (tuple, list)) or len(paths) != 3 or not paths[0]:
        raise ValueError('請選取報告A，可另選報告B及原案例集。')
    reports, files = [], []
    for path in paths[:2]:
        if path:
            raw, metadata = read_selected(path, cancel)
            reports.append(strict_json(raw)); files.append(metadata)
            check_cancel(cancel)
    suite_bytes = None; suite_file = None
    if paths[2]:
        suite_bytes, suite_file = read_selected(paths[2], cancel)
    summaries = []
    for report in reports:
        check_cancel(cancel)
        summaries.append(summarize(report, suite_bytes=suite_bytes))
    paired = compare(*reports, suite_bytes=suite_bytes) if len(reports) == 2 else None
    check_cancel(cancel)
    return {'reports':reports, 'summaries':summaries, 'comparison':paired, 'files':files,
            'suite_file':suite_file, 'loaded_at':datetime.now(timezone.utc).isoformat(),
            'case_ids':list(reports[0]['case_ids']),
            'indexed':[{row['id']:row for row in report['results']} for report in reports]}


def clip(value, limit):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + '\n[…顯示省略，完整內容請查看原報告…]'


def summary_text(snapshot):
    lines = ['這是當次載入的本機副本；不是即時監控，也不是IQ或獨立能力認證。']
    for i, summary in enumerate(snapshot['summaries']):
        report = snapshot['reports'][i]; counts = summary['reviews']
        lines += [f'報告{"AB"[i]}：當時記錄的程式版本 {clip(report.get("git_head", "未記錄"), 64)}',
                  f'已收集 {summary["collected"]}／{len(report["case_ids"])}；尚未收集 {summary["not_collected"]}；非空答案 {summary["answered"]}；空答案 {summary["empty_answers"]}；錯誤 {summary["collection_errors"]}。',
                  f'最近評讀：通過 {counts["pass"]}、未通過 {counts["fail"]}、待評讀 {counts["pending"]}；已綁定 {len(summary["bound_review_ids"])}、舊評讀未綁定 {len(summary["unbound_review_ids"])}。']
        for reviewer, counts in summary['by_reviewer'].items():
            lines.append(f'　{REVIEWERS[reviewer]}：通過 {counts["pass"]}、未通過 {counts["fail"]}。')
        lines.append('原案例集：' + ('已核對選取檔案的位元組及已記錄欄位。' if summary['suite_verified'] else '未提供，尚未驗證。'))
        lines.append(f'未記錄原問題：{len(summary["missing_prompt_ids"])} 題；機械格式檢查失敗：{summary["mechanical_failures"]} 題。')
    paired = snapshot['comparison']
    if paired:
        changed = sum(row['before']=='fail' and row['after']=='pass' for row in paired['transitions'])
        regressed = sum(row['before']=='pass' and row['after']=='fail' for row in paired['transitions'])
        lines += [f'前後評讀對照 {paired["compared"]} 題：未通過→通過 {changed}，通過→未通過 {regressed}，維持 {paired["compared"]-changed-regressed}；待補結果／評讀 {len(paired["pending_ids"])}。',
                  f'評讀者變動 {len(paired["reviewer_changed_ids"])} 題；程式／提示指紋差異 {len(paired["changed_runtime_fingerprints"])} 項；來源指紋未記錄 {len(paired["missing_source_fingerprint_ids"])} 題。']
    lines.append('非空答案可能是提問或停止說明；評讀結果不證明已完成工作。記錄一致性核對不認證模型呼叫、資料真實或評讀者身份。')
    return '\n'.join(lines)


def row_state(row):
    if row is None:
        return '尚未收集', '—'
    if not row.get('reviews'):
        return '待評讀', '—'
    latest = row['reviews'][-1]
    return OUTCOMES[latest['result']], REVIEWERS[latest['reviewer']]


def case_detail(snapshot, side, id):
    if side >= len(snapshot['reports']):
        return '未選報告B；目前為單次查看。'
    row = snapshot['indexed'][side].get(id)
    if row is None:
        return '此題尚未收集，沒有答案或評讀可顯示。'
    lines = [f'案例：{clip(id, 300)}', f'分類：{clip(row["category"], 200)}',
             '原問題：\n' + clip(row.get('prompt', '未記錄原問題；不從案例檔補造當時輸入。'), 4000),
             '原答案／提問／停止說明：\n' + clip(row['answer'] or '（空答案）', 12000)]
    if 'error' in row:
        lines.append('收集錯誤：\n' + clip(row['error'], 2000))
    latest = row.get('reviews', [])[-1] if row.get('reviews') else None
    lines.append('原評讀標準（顯示最近一次評讀）：')
    for i, criterion in enumerate(row['criteria'][:100]):
        mark = ('通過' if latest['criteria_pass'][i] else '未通過') if latest else '待評讀'
        lines.append(f'{i+1}. [{mark}] {clip(criterion, 1000)}')
    if len(row['criteria']) > 100:
        lines.append(f'其餘 {len(row["criteria"])-100} 項標準顯示省略，請查看原報告。')
    if latest:
        lines += [f'評讀者：{REVIEWERS[latest["reviewer"]]}（報告所記錄）',
                  f'評讀時間：{latest["reviewed_at"]}；歷史評讀共 {len(row["reviews"])} 筆。',
                  '評讀理由：\n' + clip(latest['note'], 4000),
                  '證據綁定：' + ('已核對當時記錄的指紋。' if 'evidence_sha256' in latest else '舊評讀未綁定；沒有追溯補造。')]
    else:
        lines.append('尚無逐項評讀；有答案不等於通過。')
    lines.append('來源指紋：' + row.get('source_sha256', '未記錄，未獨立核對輸入來源。'))
    return clip('\n\n'.join(lines), DETAIL_LIMIT)
