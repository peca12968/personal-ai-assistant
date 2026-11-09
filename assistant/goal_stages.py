"""Explicit one-stage handoffs of reports or bounded, historical work checkpoints."""
import hashlib
import json
import re
from . import work_plan
from .documents import MAX_DOCUMENTS


STOPPED = {'paused', 'failed', 'needs_input'}
SOURCE_LABELS = {'unchanged': '版本相同', 'different': '同編號版本不同',
                 'removed': '本階段未收錄', 'added': '本階段新增'}


def eligible(status, step_count):
    return status == 'report_ready' or status in STOPPED and 0 < step_count <= 32


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def bounded(value, limit=1200):
    """Keep short typed metadata intact; mark long previews rather than silently cut."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(encoded) <= limit:
        return {'value': value, 'truncated': False}
    return {'json_preview': encoded[:limit], 'truncated': True, 'original_chars': len(encoded)}


def _source_index(sources):
    if not isinstance(sources, list) or len(sources) > MAX_DOCUMENTS:
        return None
    result = {}
    for row in sources:
        if (not isinstance(row, dict) or set(row) != {'document_id', 'name', 'sha256'}
                or type(row['document_id']) is not int or row['document_id'] < 1 or row['document_id'] in result
                or not isinstance(row['name'], str) or not isinstance(row['sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
            return None
        result[row['document_id']] = row
    return result


def _prior_source_steps(previous):
    checkpoint = previous.get('checkpoint')
    activity = checkpoint.get('activity') if isinstance(checkpoint, dict) else None
    excerpts, incomplete = {}, {}
    if not isinstance(activity, list) or len(activity) > 32:
        return excerpts, incomplete
    for step in activity:
        if not isinstance(step, dict) or type(step.get('sequence')) is not int or not 1 <= step['sequence'] <= 32:
            continue
        bounded_metadata = step.get('observation_metadata')
        if not isinstance(bounded_metadata, dict) or bounded_metadata.get('truncated') is not False:
            continue
        metadata = bounded_metadata.get('value')
        if not isinstance(metadata, dict):
            continue
        for field, target in (('provided_ranges', excerpts), ('requested_ranges', incomplete)):
            rows = metadata.get(field)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or type(row.get('document_id')) is not int:
                    continue
                if field == 'requested_ranges' and row.get('status') in (None, 'provided'):
                    continue
                target.setdefault(row['document_id'], set()).add(step['sequence'])
    return excerpts, incomplete


def source_comparison(previous, sources):
    """Compare captured catalogs, without reopening files or importing old evidence."""
    old = _source_index(previous.get('sources'))
    new = _source_index(sources)
    if old is None or new is None or old == new:
        return None
    excerpts, incomplete = _prior_source_steps(previous)
    result = []
    for did in sorted(old.keys() | new.keys()):
        earlier, current = old.get(did), new.get(did)
        if earlier is None:
            status = 'added'
        elif current is None:
            status = 'removed'
        else:
            status = 'unchanged' if earlier == current else 'different'
        row = {'document_id': did, 'status': status, 'previous': earlier, 'current': current}
        if earlier is not None and did in excerpts:
            row['prior_excerpt_steps'] = sorted(excerpts[did])
        if earlier is not None and did in incomplete:
            row['prior_incomplete_steps'] = sorted(incomplete[did])
        result.append(row)
    return result


def describe_source_comparison(rows):
    lines = ['兩階段保存的來源比對']
    for row in rows:
        versions = []
        earlier, current = row['previous'], row['current']
        if earlier is not None:
            versions.append(f"原階段 {earlier['name']} {earlier['sha256'][:12]}")
        if current is not None and current != earlier:
            versions.append(f"本階段 {current['name']} {current['sha256'][:12]}")
        line = f"D{row['document_id']} · {SOURCE_LABELS[row['status']]} · " + ' → '.join(versions)
        for field, label in (('prior_excerpt_steps', '舊片段步驟'), ('prior_incomplete_steps', '舊未完整查讀步驟')):
            if field in row:
                line += ' · ' + label + ' ' + '、'.join(map(str, row[field]))
        lines.append(line)
    return '\n'.join(lines)


def activity(row):
    action, observation = json.loads(row['action_json']), json.loads(row['observation_json'])
    tool, args = action['tool'], action['args']
    # Never copy document/memory contents, model reports, file bodies or CSV specs.
    allowed = {'query', 'document_id', 'start_line', 'end_line', 'line_number', 'start_char', 'operation', 'values',
               'date', 'start', 'end', 'days', 'months', 'offsets', 'day_policy', 'calendar', 'excluded_dates',
               'old_document_id', 'new_document_id', 'offset', 'pointer', 'value_pointer', 'group_by', 'plan_id', 'filename', 'filenames',
               'message_id', 'before', 'after', 'value', 'places', 'mode'}
    requested = {k: v for k, v in args.items() if k in allowed}
    if tool == 'read_ranges':
        requested['ranges'] = args['ranges']
    if tool == 'summarize_json':
        requested.update({k:args[k] for k in ('order_by','top_n','include_median') if k in args})
    fields = {'status', 'error', 'error_phase', 'calculation_failure', 'requested_ranges', 'exact', 'decimal', 'rounded',
              'decimal_precision', 'decimal_rounding',
              'total_changes', 'changed_line_counts', 'next_offset', 'offset',
              'all_changes_in_page', 'page_content_complete', 'line_text_equal',
              'decoded_text_equal', 'byte_hash_equal', 'source_fingerprint_equal', 'plan_id', 'report_sha256',
              'page_group_count', 'page_is_partial', 'line_number', 'start_char', 'end_char',
              'total_chars', 'next_start_char', 'omitted_before', 'omitted_after', 'line_complete'}
    if tool == 'read_json':
        fields |= {'pointer', 'kind', 'syntax_checked', 'page_unit', 'total_children',
                   'all_children_in_page', 'value_complete', 'returned_chars'}
    if tool == 'summarize_json':
        fields |= {'pointer','value_pointer','syntax_checked','complete','count','raw_values_provided',
                   'source_count','excluded_count','filtered','filter_count','filter_logic',
                   'grouped','group_count','group_order','returned_group_count','all_groups_in_page',
                   'order_by','tie_breaker','top_n','selected_group_count','unselected_group_count',
                   'all_selected_groups_in_page','median_included','median_method'}
    if tool == 'month_schedule':
        fields |= {'anchor_date', 'offsets', 'day_policy', 'count', 'all_items_provided'}
    if tool == 'round_number':
        fields |= {'input_exact', 'places', 'mode', 'fixed_decimal', 'value_changed', 'is_tie', 'difference_exact'}
    if tool == 'date_math':
        fields |= {'operation', 'date', 'weekday', 'weekday_iso', 'local_datetime',
                   'observed_at_utc', 'timezone_source', 'utc_offset_seconds', 'start', 'end',
                   'counted_days', 'calendar_days', 'calendar', 'holiday_source',
                   'months', 'day_policy', 'original_day', 'target_month_days', 'day_adjusted'}
    outcome = {k: v for k, v in observation.items() if k in fields}
    if tool == 'month_schedule':
        outcome['schedule_details_omitted'] = True
    if tool == 'calculate_expression':
        outcome.update({k:observation[k] for k in ('node_count','number_count','operation_count','max_depth',
                       'evaluation_order','all_calculations_provided') if k in observation})
        outcome['expression_details_omitted'] = True
        outcome['calculation_details_omitted'] = True
    if tool == 'ask' and 'partial_result' in observation:
        outcome['partial_result_omitted'] = True
    if tool in ('check_decision', 'check_source_decision'):
        decision = observation['decision'] if tool == 'check_source_decision' else observation
        results = decision.get('possible_results', decision.get('recorded_results', []))
        outcome.update(status=decision['status'], matching_row_count=len(decision['matching_rows']),
                       decision_rows_omitted=True)
        result_count = 'recorded_result_count' if 'recorded_results' in decision else 'possible_result_count'
        outcome[result_count] = len(results)
        if 'coverage' in decision:
            outcome['coverage_counts'] = {key: len(value) for key, value in decision['coverage'].items()}
        if 'max_questions' in decision:
            outcome['max_questions'] = decision['max_questions']
        if tool == 'check_source_decision':
            outcome['source_rows_omitted'] = True
    if tool in ('search_conversation', 'read_conversation'):
        outcome.update({k:observation[k] for k in ('total_matches','returned_count') if k in observation})
        outcome['history_details_omitted'] = True
    if tool == 'read_conversation':
        outcome.update({k:observation[k] for k in ('message_id','requested_before','requested_after',
            'provided_before','provided_after','window_limited','order') if k in observation})
    if tool == 'summarize_json':
        outcome['filter_conditions_omitted'] = bool(args.get('filters'))
        outcome['group_details_omitted'] = bool(args.get('group_by'))
    if 'excerpts' in observation:
        outcome['provided_ranges'] = [{k: v for k, v in excerpt.items()
                                      if k in {'document_id', 'start_line', 'end_line', 'partial_line',
                                               'start_char', 'end_char', 'line_chars'}}
                                     for excerpt in observation['excerpts']]
    if 'memories' in observation:
        outcome['selected_memory_count'] = len(observation['memories'])
    result = {'sequence': row['sequence'], 'tool': tool,
            'purpose': bounded(action['note'], 240), 'request': bounded(requested),
            'observation_metadata': bounded(outcome)}
    failed = observation.get('failed_action')
    if tool == 'invalid' and isinstance(failed, dict):
        # Keep failed attempts explicitly separate from executed results. In
        # particular, never hand off rejected file bodies/reports/CSV specs.
        result['failed_attempt'] = {'tool': failed['tool'],
            'purpose': bounded(failed['note'], 240),
            'request': bounded({k:v for k,v in failed['args'].items() if k in allowed})}
        if failed['tool']=='calculate_expression':
            result['failed_attempt']['expression_details_omitted'] = True
            result['failed_attempt']['calculation_details_omitted'] = True
    return result


def checkpoint(db, row):
    rows = db.execute('SELECT * FROM autonomous_steps WHERE goal_id=? ORDER BY sequence LIMIT 33',
                      (row['id'],)).fetchall()
    if not eligible(row['status'], len(rows)):
        raise ValueError('請選取已交付成果，或已有步驟且已停止的目標；未開始或執行中不能帶入。')
    value = {'step_count': len(rows), 'activity': [activity(step) for step in rows],
            'question': row['question'], 'notice': row['notice'],
            'saved_files': [dict(item) for item in db.execute(
                'SELECT name,sha256,status AS recorded_status FROM autonomous_artifacts WHERE goal_id=? ORDER BY rowid', (row['id'],))],
            'saved_csv_plans': [dict(item) for item in db.execute(
                'SELECT id AS plan_id,status,report_sha256 FROM autonomous_csv_plans WHERE goal_id=? ORDER BY rowid', (row['id'],))],
            'omissions': '僅含直接上一階段的動作目的、有限參數與結果中繼資料；不含原文、記憶內容、文件內文、CSV規格與數值表、聊天或祖先背景。不是完整工作內容，也不授予本階段引用範圍或舊計畫／檔案操作權限。'}
    plan = work_plan.latest([{'sequence': r['sequence'], 'action': json.loads(r['action_json']),
                             'observation': json.loads(r['observation_json'])} for r in rows])
    if plan is not None:
        value['work_plan'] = plan
    return value


def capture(db, sid, parent_id):
    row = db.execute('SELECT * FROM autonomous_goals WHERE session_id=? AND id=?', (sid, parent_id)).fetchone()
    if row is None:
        raise ValueError('找不到此對話的上一階段目標。')
    progress = None
    if row['status'] != 'report_ready':
        progress = checkpoint(db, row)
    elif not row['result'].strip():
        raise ValueError('上一階段成果是空的，請查看原目標。')
    if len(row['result']) > 12000 or len(row['notes']) > 12000 or len(row['objective']) > 4000:
        raise ValueError('上一階段資料超過支援上限，請整理為新目標。')
    value = {'parent_goal_id': row['id'], 'objective': row['objective'],
            'report': row['result'], 'report_sha256': hashlib.sha256(row['result'].encode('utf-8')).hexdigest(),
            'user_notes': row['notes'], 'sources': json.loads(row['sources_json']),
            'recorded_status': row['status'], 'parent_updated_at': row['updated_at']}
    if progress is not None:
        value.update(schema_version=2, checkpoint=progress)
        value['snapshot_sha256'] = digest(value)
    return value


def read(db, gid):
    row = db.execute('SELECT parent_goal_id,context_json FROM autonomous_handoffs WHERE goal_id=?', (gid,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[1])
        if value['parent_goal_id'] != row[0] or hashlib.sha256(value['report'].encode('utf-8')).hexdigest() != value['report_sha256']:
            raise ValueError()
        if 'schema_version' in value or 'checkpoint' in value or 'snapshot_sha256' in value:
            if value['schema_version'] != 2 or 'checkpoint' not in value or value['snapshot_sha256'] != digest(
                    {k: v for k, v in value.items() if k != 'snapshot_sha256'}):
                raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('上一階段快照不一致，請查看原目標並另建階段。') from None
    return value
