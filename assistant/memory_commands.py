"""Explicit local memory commands. Model output is never an operation input."""
import hashlib
import json
import re
from datetime import date

from .context import make_context
from .store import allocate_memory_id, validate_memory

KINDS = {'偏好': 'preference', '事實': 'fact', '經驗': 'lesson'}
OPERATIONS = {'更正': 'edit', '查看': 'show', '停用': 'disable', '啟用': 'enable',
              '刪除': 'delete', '忘記': 'delete'}
HELP = ('請使用完整指令，例如：\n記住偏好：先給結論\n記住事實（到期 2099-12-31）：專案代號是青鳥\n'
        '列出記憶\n查看記憶 #12\n更正記憶 #12：新的內容\n停用記憶 #12\n啟用記憶 #12\n刪除記憶 #12\n'
        '一次處理一個指令；冒號後的全文是要保存的資料，不會當成其他指令執行。')


def parse_command(question):
    """Return None for normal chat; full-message matches only, never substring intent."""
    text = re.sub(r'^(?:請幫我|幫我|請)\s*', '', question.strip(), count=1)
    if text in {'列出記憶', '記憶列表'}:
        return {'op': 'list'}
    if text == '記憶指令':
        return {'op': 'help'}
    match = re.fullmatch(r'記住\s*(偏好|事實|經驗)?\s*(?:[（(]到期\s+([^()（）\n]+)[）)]\s*)?[：:]([\s\S]*)', text)
    if match:
        return {'op': 'save', 'kind': KINDS.get(match[1], 'fact'),
                'expires_on': match[2].strip() if match[2] else None, 'content': match[3].strip()}
    match = re.fullmatch(r'(更正|查看|停用|啟用|刪除|忘記)記憶\s*#([1-9][0-9]{0,18})(?:[：:]([\s\S]*))?', text)
    if match:
        op, mid, body = OPERATIONS[match[1]], int(match[2]), match[3]
        if mid > 9223372036854775807 or (op == 'edit') != (body is not None):
            return {'op': 'help'}
        return {'op': op, 'id': mid, 'content': body.strip() if body is not None else None}
    # A command-shaped typo gets local guidance, not an invented success from the model.
    if (re.match(r'^記住(?:偏好|事實|經驗)?\s*(?:[：:（(]|$)', text) or
            re.fullmatch(r'(?:更正|查看|停用|啟用|刪除|忘記)記憶\s*(?:#[0-9]*(?:[：:][\s\S]*)?|[：:])?', text)):
        return {'op': 'help'}
    return None


def metadata(row):
    if row is None:
        return None
    return {key: row[key] for key in ('id', 'kind', 'active', 'expires_on')} | {
        'content_sha256': hashlib.sha256(row['content'].encode('utf-8')).hexdigest()}


def describe(row, preview=False):
    status = '停用' if not row['active'] else '過期' if row['expires_on'] and row['expires_on'] < date.today().isoformat() else '啟用'
    content = row['content']
    if preview and len(content) > 200:
        content = content[:200] + '…（內容省略，可用查看指令）'
    kind = next(label for label, value in KINDS.items() if value == row['kind'])
    return f"#{row['id']} · {kind} · {status} · 到期：{row['expires_on'] or '不限'}\n{content}"


def operate(db, command):
    op = command['op']
    receipt = {'operation': op, 'status': 'read', 'before': None, 'after': None}
    if op == 'help':
        return '沒有變更記憶。\n' + HELP, receipt
    if op == 'list':
        total = db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        rows = db.execute('SELECT * FROM memories ORDER BY id DESC LIMIT 20').fetchall()
        receipt['listed_ids'] = [row['id'] for row in rows]
        receipt['total'] = total
        return (f'本機共有 {total} 筆記憶，以下列出最新 {len(rows)} 筆。\n\n' +
                '\n\n'.join(describe(row, preview=True) for row in rows) if rows else '目前沒有長期記憶。'), receipt
    if op == 'save':
        content = validate_memory(command['content'], command['kind'], command['expires_on'])
        values = (content, command['kind'], command['expires_on'] or None)
        row = db.execute('SELECT * FROM memories WHERE content=? AND kind=? AND expires_on IS ? AND active=1 ORDER BY id LIMIT 1', values).fetchone()
        if row:
            receipt.update(status='unchanged', before=metadata(row), after=metadata(row))
            return '這筆內容已存在，沒有重複新增。\n' + describe(row), receipt
        mid = allocate_memory_id(db)
        db.execute('INSERT INTO memories(id,content,kind,expires_on,active,updated_at) VALUES(?,?,?,?,1,CURRENT_TIMESTAMP)', (mid, *values))
        row = db.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone()
        receipt.update(status='created', after=metadata(row))
        return '已存入本機長期記憶。\n' + describe(row), receipt
    row = db.execute('SELECT * FROM memories WHERE id=?', (command['id'],)).fetchone()
    if row is None:
        receipt.update(status='not_found', requested_id=command['id'])
        return f"找不到記憶 #{command['id']}，沒有變更。可輸入「列出記憶」確認編號。", receipt
    receipt['before'] = metadata(row)
    if op == 'show':
        receipt['after'] = metadata(row)
        return describe(row), receipt
    if op == 'edit':
        content = validate_memory(command['content'], row['kind'], row['expires_on'])
        db.execute('UPDATE memories SET content=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (content, row['id']))
        prefix = '已更正本機記憶；類型、啟用狀態與到期日保留。'
    elif op in {'enable', 'disable'}:
        db.execute('UPDATE memories SET active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (int(op == 'enable'), row['id']))
        prefix = '已更新本機記憶狀態；過期記憶仍不會加入新回答。'
    elif op == 'delete':
        db.execute('DELETE FROM memories WHERE id=?', (row['id'],))
        receipt['status'] = 'deleted'
        return f"已刪除本機長期記憶 #{row['id']}。既有對話、先前匯出與備份仍保留原紀錄。", receipt
    else:
        raise ValueError('未知的記憶操作。')
    updated = db.execute('SELECT * FROM memories WHERE id=?', (row['id'],)).fetchone()
    receipt.update(status='updated', after=metadata(updated))
    return prefix + '\n' + describe(updated), receipt


def handle_command(store, sid, question, command, cancel=None):
    def check_cancel():
        if cancel and cancel.is_set():
            raise RuntimeError('已停止本次記憶操作，沒有保存變更。')
    check_cancel()
    # Mutation, receipts and local-only markers commit together, or all roll back.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        uid = db.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'user',?)", (sid, question)).lastrowid
        db.execute('SAVEPOINT memory_operation')
        try:
            answer, receipt = operate(db, command)
        except ValueError as exc:
            db.execute('ROLLBACK TO memory_operation')
            answer = '沒有變更記憶：' + str(exc)
            receipt = {'operation': command['op'], 'status': 'invalid', 'error': str(exc)}
        db.execute('RELEASE memory_operation')
        context = make_context(question, [], [])
        context.update(response_mode='local_memory', memory_action=receipt, documents={'catalog': [], 'excerpts': []})
        mid = db.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'assistant',?)", (sid, answer)).lastrowid
        db.execute('INSERT INTO answer_contexts(message_id,payload) VALUES(?,?)', (mid, json.dumps(context, ensure_ascii=False)))
        db.executemany('INSERT INTO local_memory_messages(message_id) VALUES(?)', [(uid,), (mid,)])
        db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新對話'", (question[:50], sid))
        check_cancel()
    return {'id': mid, 'content': answer, 'memory_ids': [], 'excerpt_ids': [], 'document_count': 0,
            'citation_checks': [], 'response_mode': 'local_memory', 'memory_action': receipt}
