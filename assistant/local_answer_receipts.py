"""Read and check local operation receipts against their saved chat rows."""
from .acceptance import strict_json


MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 16000 * 4


def load_context(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('回答快照不是有效文字或超過32 MiB。')
    try:
        return strict_json(raw)
    except RecursionError as exc:
        raise ValueError('回答快照巢狀結構過深。') from exc


def checked_row(db, mid, context, mode, field, validate):
    rows = db.execute('''SELECT CASE WHEN length(CAST(m.content AS BLOB))<=? THEN m.content END,
        m.role,m.session_id,r.status,
        CASE WHEN length(CAST(r.context_json AS BLOB))<=? THEN r.context_json END,
        CASE WHEN length(CAST(u.content AS BLOB))<=? THEN u.content END,
        u.role,u.session_id,r.session_id FROM messages m
        JOIN answer_runs r ON r.answer_message_id=m.id
        JOIN messages u ON u.id=r.user_message_id WHERE m.id=? LIMIT 2''',
        (MAX_TEXT_BYTES, MAX_JSON_BYTES, MAX_TEXT_BYTES, mid)).fetchall()
    if len(rows) != 1:
        raise ValueError('本機操作缺少唯一完成流程。')
    row = rows[0]
    receipt = validate(context.get(field), row[0], context.get('current_request'))
    plan = receipt['plan']
    initial = load_context(row[4])
    if (row[1] != 'assistant' or row[3] != 'succeeded' or row[6] != 'user'
            or not row[2] == row[7] == row[8] or row[5] != plan['request']
            or context.get('current_request') != plan['request']
            or context.get('engine_selection') is not None or context.get('image_inputs')
            or context.get('memories') != [] or context.get('recent_conversation') != []
            or context.get('earlier_user_excerpts') != []
            or context.get('documents') != {'catalog': [], 'excerpts': []}
            or not isinstance(initial, dict) or initial.get('response_mode') != mode
            or initial.get('current_request') != plan['request'] or initial.get(field) != plan):
        raise ValueError('本機操作快照與已完成流程不符。')
    check = db.execute('''SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END
                          FROM answer_checks WHERE message_id=?''', (MAX_JSON_BYTES, mid)).fetchone()
    if check is None or load_context(check[0]) != []:
        raise ValueError('本機操作不能冒稱文件驗證。')
    return receipt


def read(store, mid, mode, field, validate):
    with store.connect() as db:
        row = db.execute('''SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END
                            FROM answer_contexts WHERE message_id=?''', (MAX_JSON_BYTES, mid)).fetchone()
        if row is None:
            return None
        context = load_context(row[0])
        if not isinstance(context, dict) or context.get('response_mode') != mode:
            return None
        return checked_row(db, mid, context, mode, field, validate)


def validate_saved(db, specs):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'answer_contexts' not in tables:
        return
    for mid, raw in db.execute('''SELECT message_id,CASE WHEN length(CAST(payload AS BLOB))<=?
                                THEN payload END FROM answer_contexts''', (MAX_JSON_BYTES,)):
        context = load_context(raw)
        if isinstance(context, dict):
            mode = context.get('response_mode')
            if isinstance(mode, str) and mode in specs:
                field, validate = specs[mode]
                checked_row(db, mid, context, mode, field, validate)
