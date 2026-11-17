"""Recover scoped user decisions when a pinned comparison is used for current work."""

import re

from . import goal_chat_fields, goal_comparison_handoff as handoff, goal_comparison_prose
from . import goal_conversation, goal_guidance_sources


KEY = 'comparison_current_fields'
GUIDANCE = (
    'comparison_current_fields 是依目標指定的回答與欄位，查到建立工作時的現行比較資料。'
    'rows 中的 original 是原比較仍適用的欄值，來源為指定回答；confirmed 是使用者後來確認的欄值，'
    'source 的 message_id 與 quote 是實際使用者原文。withdrawn 表示舊值已撤回，須等待重新確認。'
    '針對仍缺的項目提問，保留其他有效欄。比較原文中的舊值只供回顧。'
    '本工作開始後使用者已正式補答或更正時，依本工作的較新資料更新相應欄位。'
    '來源引用使用實際訊息編號與所核對的欄位。不要把對話來源當成文件來源。'
)
_RAW_QUOTES = '*\x60「」『』“” '
_CHANGE = r'(?:重新|現已|現在)?(?:正式確認|已確認|正式更正)'
_BE = r'(?:為|是|[:：])'
_GROUP_HEADER = re.compile(r'^正式更正\s*(?P<field>.+?)(?:的)?(?:舊比較|比較(?:結果|表)?)$')
_UNCONFIRMED = re.compile(r'^(?:建議|提議|暫定|假設|如果|可能)|(?:的話|尚待確認|待確認|才採用)$'
                          r'|[（(]\s*(?:若|如果|除非|暫定|尚待)')


def _initial_rows(row, names, dimension):
    columns = {}
    for header, data in handoff._table_orientations(row['content']):
        if not handoff._covers_cells(header, data, names, {dimension}):
            continue
        index = [goal_comparison_prose.dimension_key(cell) for cell in header].index(dimension, 1)
        current = {handoff._table_name(cells[0]): (cells[0].strip(_RAW_QUOTES), cells[index].strip())
                   for cells in data if handoff._table_name(cells[0]) in names}
        if columns and columns != current:
            raise ValueError('指定比較對同一欄位提供不同值，請指定要查核的比較。')
        columns = current
    return columns


def _field_patterns(names, dimension):
    patterns = {}
    for name in names:
        subject = handoff._scope_label(name)
        field = handoff._scope_label(dimension)
        scoped = rf'{subject}\s*的\s*{field}'
        patterns[name] = (
            handoff._withdrawal_pattern(name, dimension),
            re.compile(rf'^(?:{_CHANGE}\s*{scoped}\s*{_BE}|{scoped}\s*{_CHANGE}\s*{_BE}|'
                       rf'正式改(?:為|成)\s*{scoped}\s*{_BE})\s*(?P<value>[^，,]+)$', re.I),
            re.compile(rf'^{subject}\s*(?:的)?(?:原本|原先|先前)的(?P<previous>[^，,]+?)(?:取消|撤回)'
                       rf'\s*[，,]\s*正式改(?:為|成)\s*(?P<value>[^，,]+)$', re.I),
        )
    return patterns


def _user_changes(db, sid, after, through):
    return db.execute("""
        SELECT id, content FROM messages
        WHERE session_id=? AND role='user' AND id>? AND id<=? AND length(content)<=?
            AND (instr(content,'撤回') OR instr(content,'確認') OR instr(content,'正式更正')
                 OR instr(content,'正式改為') OR instr(content,'正式改成'))
            AND id NOT IN (SELECT message_id FROM local_memory_messages)
        ORDER BY id
        """, (sid, after, through, goal_conversation.PER_MESSAGE))


def _apply_message(rows, message_id, text, patterns, dimension):
    clauses = goal_guidance_sources.standalone_clauses(text)
    if any(not inquiry and handoff._TENTATIVE.match(goal_chat_fields.label_key(clause))
           for _, clause, inquiry in clauses):
        return
    scope_quote = None
    for _, clause, inquiry in clauses:
        if inquiry or clause.endswith(('嗎', '呢')):
            continue
        normalized = goal_chat_fields.label_key(clause)
        if heading := _GROUP_HEADER.fullmatch(normalized):
            scope_quote = clause if goal_comparison_prose.dimension_key(heading['field']) == dimension else None
            continue
        for name, (withdrawal, confirmation, group_change) in patterns.items():
            record = rows[name]
            source = {'kind': 'user', 'message_id': message_id, 'quote': clause}
            if withdrawal.match(normalized):
                record.update(status='withdrawn', value=None, source=source)
                continue
            match = confirmation.fullmatch(clause)
            if not match and scope_quote and (grouped := group_change.fullmatch(clause)):
                if record['value'] is not None and handoff._table_name(grouped['previous']) == handoff._table_name(record['value']):
                    match = grouped
                    source['scope_quote'] = scope_quote
            if match:
                value = match['value'].strip()
                if handoff._UNFILLED.match(handoff._table_name(value)) or _UNCONFIRMED.search(value):
                    continue
                record.update(status='confirmed', value=value, source=source)


def capture(db, sid, objective, messages):
    pin = handoff._pinned_answer(objective)
    if not pin or not messages or not any(handoff._CURRENT.match(clause) for clause in handoff._authority_clauses(objective)):
        return None
    names, dimensions = handoff._named_scope(objective, handoff._PINNED, 5)
    if len(names) != 2 or len(dimensions) != 1:
        return None
    completed = handoff._completed(db, sid, pin)
    if not completed:
        return None
    row, context = completed
    through = messages[-1]['message_id']
    if (row['id'] > through or not handoff._reference(row, 5)
            or not handoff._comparison_subjects(row['request']) or handoff._work_fields(db, sid, row, context)):
        return None
    if any(handoff._TENTATIVE.match(clause) for clause in handoff._authority_clauses(row['request'])):
        return None
    dimension, = dimensions
    initial = _initial_rows(row, names, dimension)
    if not initial:
        return None
    rows = {name: {'option': label, 'field': dimension, 'status': 'original', 'value': value,
                   'source': {'kind': 'answer', 'message_id': pin}}
            for name, (label, value) in initial.items()}
    patterns = _field_patterns(names, dimension)
    for message_id, content in _user_changes(db, sid, row['user_id'], through):
        _apply_message(rows, message_id, content, patterns, dimension)
    return {'version': 1, 'answer_id': pin, 'through_message_id': through, 'rows': list(rows.values())}


def validate(db, sid, objective, messages, saved):
    verified = capture(db, sid, objective, messages)
    if verified is None or goal_conversation.digest(saved) != goal_conversation.digest(verified):
        raise ValueError('比較現行欄值快照與指定回答或使用者來源不符。')


def describe(saved):
    return '\n'.join([f'現行比較 · 以回答 #{saved["answer_id"]} 的欄位為範圍。'] + [
        f'{row["option"]} · {row["field"]} · {row["value"] if row["value"] is not None else "已撤回，待確認"}'
        f'（來源 #{row["source"]["message_id"]}）'
        for row in saved['rows']
    ])
