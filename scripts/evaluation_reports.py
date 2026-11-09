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
LOCAL_TRACE_MODE = 'local_understanding_trace_v1'
GOAL_TRACE_MODE = 'conversation_goal_trace_v1'
DRAFT_TRACE_MODE = 'goal_draft_trace_v1'
TRACED_MODES = (TRACE_MODE, LOCAL_TRACE_MODE, GOAL_TRACE_MODE, DRAFT_TRACE_MODE)
DRAFT_CONTEXT_MARKER = '\n\n整理來源：\n'
GOAL_CONTEXT_MARKER = '\n\n目標與已觀察資料：\n'
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
    if 'local_selection_sha256' in report.get('fingerprint', {}) and report.get('mode') != LOCAL_TRACE_MODE:
        raise ValueError('本機引擎指紋與指定的評估模式不符。')
    traced = report.get('mode') in TRACED_MODES
    if traced and not all(digest(report.get('fingerprint', {}).get(key)) for key in
                          ('runner_sha256', 'report_validation_sha256', 'suite_sha256')):
        raise ValueError('追蹤報告缺少測試器、驗證器或案例集指紋。')
    if report.get('mode') in (GOAL_TRACE_MODE, DRAFT_TRACE_MODE) and not digest(report.get('fingerprint', {}).get('fixture_builder_sha256')):
        raise ValueError('自主追蹤報告缺少來源建構器指紋。')
    if report.get('mode') == LOCAL_TRACE_MODE and not digest(report['fingerprint'].get('local_selection_sha256')):
        raise ValueError('本機理解評估缺少引擎選擇指紋。')
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
            if report['mode'] == DRAFT_TRACE_MODE:
                validate_draft_trace(row)
            elif report['mode'] == GOAL_TRACE_MODE:
                validate_goal_trace(row)
            else:
                validate_trace(row)
                if report['mode'] == LOCAL_TRACE_MODE:
                    selection = row['context'].get('engine_selection')
                    if not isinstance(selection, dict) or selection.get('selection_fingerprint') != signature['local_selection_sha256']:
                        raise ValueError('本機評估的作答引擎與報告指紋不符。')
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
        if report.get('mode') in TRACED_MODES and canonical_digest(case) != row.get('fixture_sha256'):
            raise ValueError('案例歷史、記憶或其他完整輸入定義與原案例集不符。')
    return True


def _named_draft_catalog(source):
    """Frozen evidence projection for explicitly named drafts."""
    context = source['context']
    topic = context['named_topic']
    if (set(context) != {'session_id', 'captured_at', 'named_topic'} or source['available_documents'] != []
            or not isinstance(topic, dict)
            or set(topic) != {'title', 'status', 'sources', 'scanned_user_messages', 'history_limited'}
            or not text(topic['title']) or not 2 <= len(topic['title']) <= 48
            or topic['status'] not in ('found', 'not_found', 'ambiguous', 'source_limit')
            or type(topic['history_limited']) is not bool
            or type(topic['scanned_user_messages']) is not int or not 0 <= topic['scanned_user_messages'] <= 512):
        raise ValueError('具名草稿來源範圍不符。')
    rows = topic['sources']
    if (not isinstance(rows, list) or len(rows) > min(8, topic['scanned_user_messages'])
            or bool(rows) != (topic['status'] == 'found')):
        raise ValueError('具名草稿來源數量不符。')
    catalog = {}
    previous = total = 0
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'message_id', 'content'}
                or type(row['message_id']) is not int or not previous < row['message_id'] < 2**63
                or not text(row['content'])):
            raise ValueError('具名草稿原話或順序不符。')
        previous = row['message_id']
        total += len(row['content'])
        catalog['M' + str(previous)] = row['content']
    if total > 3600:
        raise ValueError('具名草稿原話超出字數。')
    catalog['R'] = source['current_request']
    return catalog


def _draft_catalog(source):
    """Frozen v1 evidence projection; never import today's runtime draft code."""
    try:
        context = source['context']
        if 'named_topic' in context:
            return _named_draft_catalog(source)
        catalog = {}
        opening = context.get('opening_user_message')
        rows = ([opening] if opening is not None else []) + context['messages'] + context['earlier_corrections']
        for item in rows:
            if item['role'] == 'user':
                catalog['M' + str(item['message_id'])] = item['content']
        for item in context.get('answer_feedback', {}).get('items', []):
            catalog['F' + str(item['answer_message_id'])] = item['correction']
        for key in ('objective', 'constraints_text', 'decisions'):
            value = context['brief'].get(key, '')
            if value.strip():
                catalog['B_' + key] = value
        if source['current_request'].strip():
            catalog['R'] = source['current_request']
        if any(not isinstance(value, str) for value in catalog.values()):
            raise ValueError('草稿來源內容須為文字。')
        return catalog
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('草稿來源缺少可核對的使用者背景。') from exc


def _draft_objective(proposal, catalog):
    def bounded(value, maximum):
        return text(value) and len(value) <= maximum and '\0' not in value
    if not isinstance(proposal, dict) or set(proposal) != {'items', 'questions'}:
        raise ValueError('草稿回覆欄位不符。')
    items, questions = proposal['items'], proposal['questions']
    if (not isinstance(items, list) or len(items) > 10 or not isinstance(questions, list)
            or len(questions) > 4 or any(not bounded(q, 300) for q in questions)):
        raise ValueError('草稿項目或問題格式不符。')
    labels = {'goal': '目標', 'constraint': '限制', 'deliverable': '交付'}
    kinds, lines = [], []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'kind', 'text', 'sources'}:
            raise ValueError('草稿項目欄位不符。')
        kind = item['kind']
        if (not isinstance(kind, str) or kind not in labels
                or not bounded(item['text'], 1200 if kind == 'goal' else 400)):
            raise ValueError('草稿項目內容不符。')
        refs = item['sources']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 3:
            raise ValueError('草稿項目缺少來源。')
        used = set()
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {'key', 'quote'}
                    or not isinstance(ref['key'], str) or ref['key'] not in catalog
                    or ref['key'] in used or not bounded(ref['quote'], 300)
                    or ref['quote'] not in catalog[ref['key']]):
                raise ValueError('草稿引文與保存的使用者原話不符。')
            used.add(ref['key'])
        kinds.append(kind)
        lines.append(labels[kind] + '：' + item['text'])
    if (items and (kinds[0] != 'goal' or kinds.count('goal') != 1)) or (not items and not questions):
        raise ValueError('草稿需有一個目標或待釐清問題。')
    if not items:
        return ''
    result = '\n'.join(lines + ['待釐清（不可自行假設）：' + q for q in questions])
    if len(result) > 4000:
        raise ValueError('草稿目標超過4000字。')
    return result


def validate_draft_trace(row):
    fixture = row.get('fixture')
    if not isinstance(fixture, dict) or canonical_digest(fixture) != row.get('fixture_sha256'):
        raise ValueError('草稿原始案例與指紋不符。')
    for key in ('id', 'category', 'prompt', 'criteria'):
        if key not in fixture or fixture[key] != row.get(key):
            raise ValueError('草稿案例與問題／標準不符。')
    source = row.get('source')
    if (not isinstance(source, dict) or not text(row.get('prompt'))
            or source.get('current_request') != row['prompt']
            or canonical_digest(source) != row.get('captured_source_sha256')):
        raise ValueError('草稿來源或本次問題不符。')
    catalog = _draft_catalog(source)
    if source['context'].get('session_id') != row.get('session_id') or not text(row.get('session_id')):
        raise ValueError('草稿來源對話不符。')
    calls = row.get('calls')
    if (not isinstance(calls, list) or len(calls) > 1
            or type(row.get('engine_call_count')) is not int or row['engine_call_count'] != len(calls)):
        raise ValueError('草稿只允許一次實際引擎呼叫。')
    topic = source['context'].get('named_topic')
    if topic is not None and topic['status'] != 'found' and (calls or 'error' not in row):
        raise ValueError('具名草稿來源未確定時不得呼叫引擎或交付草稿。')
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError('草稿引擎追蹤格式不符。')
        request, offset = call.get('request'), call.get('request_context_offset')
        if (not isinstance(request, str) or type(offset) is not int or not 0 <= offset < len(request)
                or not request[:offset].endswith(DRAFT_CONTEXT_MARKER)):
            raise ValueError('草稿實際請求邊界不符。')
        try:
            raw = request.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('草稿實際請求不是UTF-8。') from exc
        if len(raw) > MAX_TRACE_BYTES or hashlib.sha256(raw).hexdigest() != call.get('request_sha256'):
            raise ValueError('草稿實際請求指紋或大小不符。')
        if canonical_digest(strict_json(request[offset:])) != canonical_digest({**source, 'source_catalog': catalog}):
            raise ValueError('草稿實際請求與保存來源不同。')
        if call.get('status') == 'error':
            if ('engine_answer' in call or 'engine_answer_sha256' in call
                    or not text(call.get('error')) or row.get('error') != call['error']):
                raise ValueError('引擎失敗不能補造回覆或隱藏錯誤。')
        elif call.get('status') == 'returned':
            answer = call.get('engine_answer')
            try:
                raw = answer.encode('utf-8') if isinstance(answer, str) else None
            except UnicodeError as exc:
                raise ValueError('草稿回覆不是UTF-8。') from exc
            if (raw is None or len(raw) > MAX_TRACE_BYTES or 'error' in call
                    or hashlib.sha256(raw).hexdigest() != call.get('engine_answer_sha256')):
                raise ValueError('草稿原始回覆指紋或大小不符。')
        else:
            raise ValueError('草稿引擎追蹤尚未結束。')
    if 'error' in row:
        if not text(row['error']) or row['answer'] != '' or 'draft' in row:
            raise ValueError('草稿失敗不能宣稱已有可用成果。')
        return
    if not calls or calls[0]['status'] != 'returned':
        raise ValueError('草稿成功缺少實際回覆。')
    draft = row.get('draft')
    if not isinstance(draft, dict) or set(draft) != {'source', 'source_fingerprint', 'proposal', 'objective_text'}:
        raise ValueError('草稿成果欄位不符。')
    stable_source = {**source, 'context': dict(source['context'])}
    stable_source['context'].pop('captured_at', None)
    if draft['source'] != source or draft['source_fingerprint'] != canonical_digest(stable_source):
        raise ValueError('草稿成果來源指紋不符。')
    raw_answer = calls[0]['engine_answer']
    if len(raw_answer) > 20000 or strict_json(raw_answer) != draft['proposal']:
        raise ValueError('草稿成果與原始引擎JSON不同。')
    objective = _draft_objective(draft['proposal'], catalog)
    if (draft['objective_text'] != objective
            or row['answer'] != (objective or '\n'.join(draft['proposal']['questions']))):
        raise ValueError('草稿顯示答案與核對成果不符。')


def validate_goal_trace(row):
    """Check recorded inputs, not the current product or service identity."""
    if not text(row.get('prompt')):
        raise ValueError('自主追蹤缺少有效案例問題。')
    fixture = row.get('fixture')
    if not isinstance(fixture, dict) or canonical_digest(fixture) != row.get('fixture_sha256'):
        raise ValueError('自主案例定義與指紋不符。')
    for field in ('id', 'category', 'prompt', 'criteria'):
        if field not in fixture or fixture[field] != row.get(field):
            raise ValueError('自主案例定義與問題／標準不符。')
    goal = row.get('goal')
    if not isinstance(goal, dict) or goal.get('objective') != row['prompt'].strip():
        raise ValueError('保存目標與案例問題不符。')
    if (goal.get('id') != row.get('goal_id') or goal.get('session_id') != row.get('session_id')
            or not text(row.get('goal_id')) or not text(row.get('session_id')) or not isinstance(goal.get('steps'), list)):
        raise ValueError('自主目標識別或步驟清單不符。')
    status=goal.get('status')
    if not isinstance(status,str) or status not in {'pending','paused','failed','needs_input','report_ready'}:
        raise ValueError('自主評估須保存已停止的已知目標狀態。')
    if any(not isinstance(goal.get(key),str) for key in ('result','question','notice')):
        raise ValueError('自主成果、問題與通知須為文字。')
    if row['answer']!=(goal['result'] or goal['question'] or goal['notice']):
        raise ValueError('評估答案與保存的成果／問題／通知不符。')
    if status in {'pending','paused','failed'} and (goal['result'] or goal['question']):
        raise ValueError('未交付的自主狀態不能附帶已完成成果或待答問題。')
    if status in {'pending','failed'} and not text(row.get('error')):
        raise ValueError('未啟動或失敗的自主收集需明示錯誤。')
    for i, step in enumerate(goal['steps']):
        if (not isinstance(step, dict) or type(step.get('sequence')) is not int or step['sequence'] != i+1
                or not isinstance(step.get('action'), dict) or not isinstance(step.get('observation'), dict)):
            raise ValueError('自主保存步驟格式無效。')
    calls = row.get('calls')
    if (not isinstance(calls, list) or len(calls) > 8
            or type(row.get('engine_call_count')) is not int or row['engine_call_count'] != len(calls)):
        raise ValueError('自主引擎呼叫數無效。')
    if len(goal['steps']) > len(calls):
        raise ValueError('保存步驟數超過實際呼叫數。')
    if status=='pending' and (calls or goal['steps']):
        raise ValueError('未啟動目標不能已有引擎呼叫或執行步驟。')
    if not calls and not text(row.get('error')):
        raise ValueError('未呼叫引擎的自主案例需明示錯誤，不能視為實際回答。')
    for i, call in enumerate(calls):
        if not isinstance(call, dict) or type(call.get('sequence')) is not int or call['sequence'] != i+1:
            raise ValueError('自主呼叫順序無效或重複。')
        request, offset = call.get('prompt'), call.get('request_context_offset')
        if (not isinstance(request, str) or len(request) > MAX_TRACE_BYTES or type(offset) is not int
                or not 0 <= offset < len(request) or not request[:offset].endswith(GOAL_CONTEXT_MARKER)):
            raise ValueError('自主請求或JSON邊界無效。')
        try:
            raw = request.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('自主請求不是有效UTF-8。') from exc
        if len(raw) > MAX_TRACE_BYTES or hashlib.sha256(raw).hexdigest() != call.get('request_sha256'):
            raise ValueError('自主請求指紋不符或超限。')
        context = strict_json(request[offset:])
        if (not isinstance(context, dict) or canonical_digest(context) != call.get('context_sha256')
                or context.get('objective') != goal['objective']):
            raise ValueError('自主請求脈絡指紋或目標不符。')
        steps = [{**step, 'observation': {k:v for k,v in step['observation'].items() if k != 'companion_profile'}}
                 for step in goal['steps'][:i]]
        if (context.get('steps') != steps or len(steps) != i
                or context.get('conversation_history') != goal.get('conversation_history')):
            raise ValueError('自主請求與當時已保存步驟／歷史副本不符。')
        if call.get('status') == 'returned':
            if not isinstance(call.get('raw'), str) or 'error' in call:
                raise ValueError('成功呼叫缺少原始回應或混入錯誤。')
            try:
                answer_bytes = call['raw'].encode('utf-8')
            except UnicodeError as exc:
                raise ValueError('自主回應不是有效UTF-8。') from exc
            if len(answer_bytes)>MAX_TRACE_BYTES or hashlib.sha256(answer_bytes).hexdigest()!=call.get('raw_sha256'):
                raise ValueError('自主原始回應超限或指紋不符。')
        elif call.get('status') == 'error':
            if (not text(call.get('error')) or 'raw' in call or 'raw_sha256' in call or i != len(calls)-1
                    or row.get('error')!=call['error'] or len(goal['steps'])!=i or status not in {'failed','paused'}):
                raise ValueError('失敗呼叫需保留錯誤、不得補造回答或自動重試。')
        else:
            raise ValueError('自主呼叫尚未結束或狀態無效。')
    if any(step['action'].get('tool') in ('ask','finish','deliver') for step in goal['steps'][:-1]):
        raise ValueError('本次新目標收集不能在詢問或交付後繼續執行。')
    if status in {'report_ready','needs_input'}:
        if not calls or len(goal['steps'])!=len(calls) or calls[-1]['status']!='returned':
            raise ValueError('交付或詢問必須有最後成功返回且已保存的步驟。')
        step=goal['steps'][-1];args=step['action'].get('args')
        field='report' if status=='report_ready' else 'question'
        value=goal['result' if status=='report_ready' else 'question']
        tools=('finish','deliver') if status=='report_ready' else ('ask',)
        if (not text(value) or goal['question' if status=='report_ready' else 'result']
                or step['action'].get('tool') not in tools or not isinstance(args,dict)
                or args.get(field)!=value or step['observation'].get(field)!=value):
            raise ValueError('結束狀態、工具動作、實際觀察與成果／問題不一致。')
