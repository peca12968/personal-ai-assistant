"""Explicit one-stage handoffs of reports or bounded, historical work checkpoints."""
import hashlib
import json
from . import work_plan


STOPPED = {'paused', 'failed', 'needs_input'}


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


def activity(row):
    action, observation = json.loads(row['action_json']), json.loads(row['observation_json'])
    tool, args = action['tool'], action['args']
    # Never copy document/memory contents, model reports, file bodies or CSV specs.
    allowed = {'query', 'document_id', 'start_line', 'end_line', 'line_number', 'start_char', 'operation', 'values',
               'date', 'start', 'end', 'days', 'months', 'day_policy', 'calendar', 'excluded_dates',
               'old_document_id', 'new_document_id', 'offset', 'pointer', 'plan_id', 'filename', 'filenames'}
    requested = {k: v for k, v in args.items() if k in allowed}
    fields = {'status', 'error', 'error_phase', 'requested_ranges', 'exact', 'decimal', 'rounded',
              'total_changes', 'changed_line_counts', 'next_offset', 'offset',
              'all_changes_in_page', 'page_content_complete', 'line_text_equal',
              'decoded_text_equal', 'byte_hash_equal', 'plan_id', 'report_sha256',
              'page_group_count', 'page_is_partial', 'line_number', 'start_char', 'end_char',
              'total_chars', 'next_start_char', 'omitted_before', 'omitted_after', 'line_complete'}
    if tool == 'read_json':
        fields |= {'pointer', 'kind', 'syntax_checked', 'page_unit', 'total_children',
                   'all_children_in_page', 'value_complete', 'returned_chars'}
    if tool == 'date_math':
        fields |= {'operation', 'date', 'weekday', 'weekday_iso', 'local_datetime',
                   'observed_at_utc', 'timezone_source', 'utc_offset_seconds', 'start', 'end',
                   'counted_days', 'calendar_days', 'calendar', 'holiday_source',
                   'months', 'day_policy', 'original_day', 'target_month_days', 'day_adjusted'}
    outcome = {k: v for k, v in observation.items() if k in fields}
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
