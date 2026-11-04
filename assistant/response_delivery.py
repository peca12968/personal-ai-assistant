"""Local format-delivery evidence, separate from the model's input and answer score."""
import hashlib
import json

from .acceptance import strict_json

SCHEMA = 'answer_delivery_v1'
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = MAX_TEXT_BYTES * 6 + 1024


def digest(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_BYTES:
        raise ValueError('答案整理紀錄缺少有效文字。')
    raw = text.encode('utf8')
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError('答案整理紀錄超過文字上限。')
    return hashlib.sha256(raw).hexdigest()


def make_receipt(raw, delivered):
    if raw == delivered:
        return None
    receipt = dict(schema=SCHEMA, method='daily_format_v1', raw_answer=raw,
                   raw_sha256=digest(raw), delivered_sha256=digest(delivered))
    validate(receipt, delivered)
    return receipt


def validate(receipt, delivered):
    if (not isinstance(receipt, dict) or set(receipt) != {
            'schema', 'method', 'raw_answer', 'raw_sha256', 'delivered_sha256'}
            or receipt['schema'] != SCHEMA or receipt['method'] != 'daily_format_v1'
            or receipt['raw_sha256'] != digest(receipt['raw_answer'])
            or receipt['delivered_sha256'] != digest(delivered)
            or receipt['raw_answer'] == delivered):
        raise ValueError('答案整理紀錄與原答或交付內容不符。')
    return receipt


def save(db, message_id, delivered, receipt):
    validate(receipt, delivered)
    db.execute('INSERT INTO answer_deliveries(message_id,payload) VALUES(?,?)',
               (message_id, json.dumps(receipt, ensure_ascii=False)))


def read_db(db, message_id):
    row = db.execute('''SELECT CASE WHEN length(CAST(d.payload AS BLOB))<=? THEN d.payload END,
        m.content,m.role,r.status FROM answer_deliveries d
        JOIN messages m ON m.id=d.message_id
        LEFT JOIN answer_runs r ON r.answer_message_id=m.id WHERE d.message_id=?''',
        (MAX_RECEIPT_BYTES, message_id)).fetchone()
    if row is None:
        return None
    if (row[2] != 'assistant' or row[3] != 'succeeded' or not isinstance(row[0], str)
            or len(row[0]) > MAX_RECEIPT_BYTES or len(row[0].encode('utf8')) > MAX_RECEIPT_BYTES):
        raise ValueError('答案整理紀錄不屬於已完成回答或超過上限。')
    return validate(strict_json(row[0]), row[1])


def read(store, message_id):
    with store.connect() as db:
        return read_db(db, message_id)


def describe(store, message_id):
    with store.connect() as db:
        receipt = read_db(db, message_id)
        if receipt is None:
            return None
        delivered = db.execute('SELECT content FROM messages WHERE id=?', (message_id,)).fetchone()[0]
        return ('這是本機格式整理，沒有重新呼叫模型，不代表模型權重或正確率改善。\n\n'
                '模型原答：\n' + receipt['raw_answer'] + '\n\n交付答案：\n' + delivered)


def validate_saved(db):
    for message_id, size in db.execute('SELECT message_id,length(CAST(payload AS BLOB)) FROM answer_deliveries'):
        if type(size) is not int or not 0 < size <= MAX_RECEIPT_BYTES or read_db(db, message_id) is None:
            raise ValueError('答案整理紀錄超過上限或關聯不完整。')
