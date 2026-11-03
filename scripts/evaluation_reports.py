"""Validate recorded evaluation structure; this does not authenticate evidence."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

MAX_JSON_BYTES = 32 * 1024 * 1024
REVIEWERS = {'user', 'development_agent', 'independent_reviewer'}
RUNTIME_FINGERPRINTS = {'source_sha256', 'prompt_sha256', 'communication_prompt_sha256'}
TRACE_MODE = 'understanding_trace_v1'
TRACE_CONTEXT_MARKER = '\n\n以下 JSON 是使用者資料與當前問題：\n'
MAX_TRACE_BYTES = 2 * 1024 * 1024


def canonical_digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('評估輸入不能建立穩定JSON指紋。') from exc
    return hashlib.sha256(raw).hexdigest()


def validate_trace(row):
    fixture = row.get('fixture')
    if not isinstance(fixture, dict) or canonical_digest(fixture) != row.get('fixture_sha256'):
        raise ValueError('原始案例定義與指紋不符。')
    if not text(row.get('prompt')):
        raise ValueError('實際請求追蹤缺少有效案例問題。')
    for field in ('id', 'category', 'prompt', 'criteria'):
        if field not in fixture or fixture[field] != row.get(field):
            raise ValueError('案例定義與報告問題／標準不一致。')
    request, context, offset = row.get('request'), row.get('context'), row.get('request_context_offset')
    if (not isinstance(request, str) or len(request) > MAX_TRACE_BYTES
            or not isinstance(context, dict) or type(offset) is not int
            or not 0 <= offset < len(request)):
        raise ValueError('實際請求、脈絡或位置紀錄無效。')
    try:
        raw = request.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('實際請求不是有效UTF-8。') from exc
    if len(raw) > MAX_TRACE_BYTES or hashlib.sha256(raw).hexdigest() != row.get('request_sha256'):
        raise ValueError('實際請求與指紋不符或超過2 MiB。')
    # Use the recorded boundary, never today's prompts or a regenerated input.
    # Quoted copies of the marker inside user JSON cannot redirect this check.
    if not request[:offset].endswith(TRACE_CONTEXT_MARKER):
        raise ValueError('實際請求的脈絡邊界無效。')
    if canonical_digest(strict_json(request[offset:])) != canonical_digest(context):
        raise ValueError('實際請求與保存的作答脈絡不同。')
    if context.get('current_request') != row['prompt'].strip():
        raise ValueError('實際作答問題與案例不一致。')
    if type(row.get('engine_call_count')) is not int or row['engine_call_count'] != 1:
        raise ValueError('理解回答追蹤需有且只有一次引擎呼叫。')
    if not isinstance(row.get('engine_answer'), str):
        raise ValueError('缺少引擎回傳文字；不能以處理後答案補造。')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON含重複欄位，拒絕使用不明確的評估資料。')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('評估JSON不接受NaN或Infinity。')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('評估JSON編碼或巢狀結構無效。') from exc


def load_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('評估JSON單檔上限32 MiB，未讀取部分結果。')
    return strict_json(raw)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def criteria(value):
    return isinstance(value, list) and bool(value) and all(text(item) for item in value)


def digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def index_report(report):
    if not isinstance(report, dict):
        raise ValueError('評估報告須為JSON物件。')
    if 'format_version' in report and (type(report['format_version']) is not int or report['format_version'] != 1):
        raise ValueError('不支援此評估報告版本。')
    ids, rows = report.get('case_ids'), report.get('results')
    if not isinstance(ids, list) or not ids or not all(text(id) for id in ids) or len(set(ids)) != len(ids):
        raise ValueError('案例選取須為非空且不重複的文字編號清單。')
    if not isinstance(rows, list) or len(rows) > len(ids):
        raise ValueError('報告結果清單或數量無效。')
    selected = set(ids)
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not text(row.get('id')) or row['id'] not in selected or row['id'] in indexed:
            raise ValueError('報告含未知、重複或無效案例編號。')
        indexed[row['id']] = row
    return indexed


def evidence_digest(report, row):
    run_fields = ('format_version', 'git_head', 'created_at', 'fingerprint', 'case_ids', 'mode')
    payload = {'run': {key: report[key] for key in run_fields if key in report},
               'case': {key: value for key, value in row.items() if key not in {'reviews', 'human_grade'}}}
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('評讀證據不能建立穩定JSON指紋。') from exc
    return hashlib.sha256(raw).hexdigest()


def validate_review(review, row, report):
    if not isinstance(review, dict) or not isinstance(review.get('reviewer'), str) or review['reviewer'] not in REVIEWERS:
        raise ValueError('評讀者類型無效。')
    values = review.get('criteria_pass')
    if not isinstance(values, list) or len(values) != len(row['criteria']) or not all(type(value) is bool for value in values):
        raise ValueError('評讀需逐項提供真正布林，對應所有非空標準。')
    outcome = 'pass' if all(values) else 'fail'
    if review.get('result') != outcome or not text(review.get('note')):
        raise ValueError('評讀結果與逐項標準不符，或缺少理由。')
    try:
        timestamp = datetime.fromisoformat(review['reviewed_at'])
        if timestamp.utcoffset() is None:
            raise ValueError('missing timezone')
    except (KeyError, TypeError, ValueError):
        raise ValueError('評讀時間需為含時區的ISO時間。') from None
    if outcome == 'pass' and not row['answer'].strip():
        raise ValueError('空答案不能記為全部通過。')
    if 'evidence_sha256' in review:
        if not digest(review['evidence_sha256']) or review['evidence_sha256'] != evidence_digest(report, row):
            raise ValueError('評讀對應的答案、標準或執行證據已改變，不能沿用舊評分。')
    return outcome


def validate_report(report):
    rows = index_report(report)
    if 'fingerprint' in report:
        signature = report['fingerprint']
        if not isinstance(signature, dict) or any(not text(k) or not digest(v) for k, v in signature.items()):
            raise ValueError('評估指紋須為具名SHA-256文字。')
    if 'mode' in report and not text(report['mode']):
        raise ValueError('評估模式無效。')
    traced = report.get('mode') == TRACE_MODE
    if traced and not all(digest(report.get('fingerprint', {}).get(key)) for key in
                          ('runner_sha256', 'report_validation_sha256', 'suite_sha256')):
        raise ValueError('追蹤報告缺少測試器、驗證器或案例集指紋。')
    for row in rows.values():
        if not text(row.get('category')) or not criteria(row.get('criteria')) or not isinstance(row.get('answer'), str):
            raise ValueError('案例需有分類、非空標準與答案文字；空答案另列統計。')
        if 'prompt' in row and not text(row['prompt']):
            raise ValueError('案例問題無效。')
        if 'error' in row and not isinstance(row['error'], str):
            raise ValueError('案例錯誤需為文字。')
        for field in ('source_sha256', 'fixture_sha256', 'request_sha256'):
            if field in row and not digest(row[field]):
                raise ValueError('案例來源或請求指紋無效。')
        if traced:
            validate_trace(row)
        mechanical = row.get('mechanical_checks', {})
        if not isinstance(mechanical, dict) or any(not text(k) or type(v) is not bool for k, v in mechanical.items()):
            raise ValueError('機械檢查需為具名布林；不能代替語意評讀。')
        reviews = row.get('reviews', [])
        if not isinstance(reviews, list):
            raise ValueError('評讀歷史須為清單。')
        for review in reviews:
            validate_review(review, row, report)
    return rows


def verify_suite(report, raw):
    if raw is None:
        return False
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
        raise ValueError('案例集須為最多32 MiB的原始位元組。')
    if report.get('fingerprint', {}).get('suite_sha256') != hashlib.sha256(raw).hexdigest():
        raise ValueError('指定案例集的原始位元組與報告指紋不符。')
    cases = strict_json(raw)
    if not isinstance(cases, list) or not cases:
        raise ValueError('案例集須為非空清單。')
    indexed = {}
    for case in cases:
        if not isinstance(case, dict) or not text(case.get('id')) or case['id'] in indexed:
            raise ValueError('案例集編號無效或重複。')
        if not text(case.get('category')) or not text(case.get('prompt')) or not criteria(case.get('criteria')):
            raise ValueError('案例集缺少分類、問題或非空標準。')
        indexed[case['id']] = case
    if any(id not in indexed for id in report['case_ids']):
        raise ValueError('報告選取不在指定案例集中。')
    for row in report['results']:
        case = indexed[row['id']]
        for field in ('category', 'criteria', 'prompt'):
            if field in row and row[field] != case[field]:
                raise ValueError('報告的問題、分類或標準與指定案例集不符。')
        if report.get('mode') == TRACE_MODE and canonical_digest(case) != row.get('fixture_sha256'):
            raise ValueError('案例歷史、記憶或其他完整輸入定義與原案例集不符。')
    return True
