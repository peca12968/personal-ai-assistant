"""User-saved search vocabulary; aliases select evidence, never create facts."""
import json
from pathlib import Path

from .acceptance import strict_json
from .retrieval import normalize

GUIDANCE = '''記憶若帶retrieval_aliases，代表這筆原文透過使用者儲存的搜尋稱呼匹配；groups列出該次匹配的用語組、revision是當時設定版本。
這是搜尋線索，不是模型翻譯、身分確認、新事實或任務授權；同組不保證在每個情境完全等義。根據原始content作答，不將稱呼設定當作額外事實，必要時說明採用該搜尋稱呼。
當前明確更正或區分名稱的要求優先，原文不足時不可因為匹配就猜答案。舊回答或工具紀錄中的稱呼匹配是歷史，不代表目前設定仍相同。'''


def validate(groups):
    if not isinstance(groups, list) or len(groups) > 8:
        raise ValueError('搜尋稱呼需為最多8組的清單。')
    result, seen = [], set()
    for group in groups:
        if not isinstance(group, list) or not 2 <= len(group) <= 6:
            raise ValueError('每組需有2至6個稱呼。')
        clean = []
        for value in group:
            if not isinstance(value, str) or not 2 <= len(value.strip()) <= 64 or any(
                ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff or c == '|' for c in value
            ):
                raise ValueError('每個稱呼需為2至64字，不含控制字元或分隔符號 |。')
            value = value.strip()
            key = normalize(value)
            if len(key) < 2 or not any(c.isalnum() for c in key) or key in seen:
                raise ValueError('稱呼須含文字或數字，且正規化後不能重複或跨組重疊。')
            seen.add(key)
            clean.append(value)
        result.append(clean)
    return result


def read(db):
    rows = db.execute('SELECT id,revision,groups_json,updated_at FROM memory_aliases').fetchall()
    if not rows:
        return {'source': 'default', 'revision': 0, 'groups': [], 'updated_at': None}
    if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or rows[0][1] < 1:
        raise ValueError('搜尋稱呼版本不完整。')
    row = rows[0]
    if not isinstance(row[2], str) or len(row[2]) > 12000:
        raise ValueError('搜尋稱呼資料過大或格式不符。')
    return {'source': 'user_saved', 'revision': row[1], 'groups': validate(strict_json(row[2])), 'updated_at': row[3]}


def get(store):
    with store.connect() as db:
        return read(db)


def save(store, groups, *, expected_revision):
    groups = validate(groups)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('請提供讀取時的版本編號。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = read(db)
        if current['revision'] != expected_revision:
            raise ValueError('搜尋稱呼已在其他視窗更新，請重新載入核對後再儲存。')
        db.execute('''INSERT INTO memory_aliases(id,revision,groups_json) VALUES(1,?,?)
            ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,groups_json=excluded.groups_json,
            updated_at=CURRENT_TIMESTAMP''', (expected_revision + 1, json.dumps(groups, ensure_ascii=False)))
        return read(db)


def read_file(path):
    with Path(path).open('rb') as stream:
        data = stream.read(12001)
    if len(data) > 12000:
        raise ValueError('搜尋稱呼檔案最多12000位元組。')
    return validate(strict_json(data.decode('utf-8-sig')))
