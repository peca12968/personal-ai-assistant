"""Bounded text artifacts in a goal-owned directory; never overwrite existing bytes.

Persist the intended content before I/O. Retrying verifies matching bytes or
creates a missing file, including after backup restoration. Receipts describe
verification at a point in time, not permanent filesystem state.
"""
import hashlib
import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path

from . import delivery_preference
from .acceptance import strict_json
from .answer_runs import lease

MAX_FILES = 4
MAX_CHARS = 12000
MAX_BYTES = MAX_CHARS * 4


def filename(value):
    if not isinstance(value, str) or not re.fullmatch(r'\w[\w .-]{0,63}', value) or value[-1] in '. ':
        raise ValueError('檔名需為1–64字，只能含文字、數字、底線、空格、點與連字號，不可含路徑。')
    if value != unicodedata.normalize('NFC', value):
        raise ValueError('檔名的Unicode形式不一致，請使用一般文字檔名。')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', value.split('.')[0].upper()):
        raise ValueError('不能使用系統保留檔名。')
    if Path(value).suffix.lower() not in {'.md', '.txt', '.json'}:
        raise ValueError('目前只支援Markdown、TXT與JSON文件。')
    return value


def payload(name, content):
    filename(name)
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_CHARS or '\x00' in content:
        raise ValueError('文件內容需為1–12000字，且不含空字元。')
    if Path(name).suffix.lower() == '.json':
        strict_json(content)
    try:
        return content.encode('utf-8')
    except UnicodeError:
        raise ValueError('文件含無效文字。') from None


def enabled(db, gid):
    return delivery_preference.enabled(db, gid)


def inventory(db, gid):
    return [dict(row) for row in db.execute('''SELECT name,sha256,size_bytes,status AS recorded_status,error,verified_at
        FROM autonomous_artifacts WHERE goal_id=? ORDER BY rowid''', (gid,))]


def _ordinary(path, *, directory):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
        raise ValueError('成果位置含連結或重新解析點，未繼續讀寫。')
    if directory and not stat.S_ISDIR(info.st_mode) or not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError('成果位置的檔案類型不符。')


def directory(store, gid, *, create=False):
    if not isinstance(gid, str) or not re.fullmatch('[0-9a-f]{32}', gid):
        raise ValueError('目標編號格式不符。')
    anchor = store.path.resolve(strict=True).parent
    current = anchor
    for name in ('autonomy-artifacts', gid):
        current = current / name
        if create:
            try:
                current.mkdir()
            except FileExistsError:
                pass
        _ordinary(current, directory=True)
        if current.resolve(strict=True) != current:
            raise ValueError('成果位置超出此目標資料夾。')
    return current


def _record(store, gid, name):
    with store.connect() as db:
        if not delivery_preference.initially_enabled(db, gid):
            raise ValueError('此目標未啟用交付文件，請另建已勾選交付文件的目標。')
        row = db.execute('SELECT * FROM autonomous_artifacts WHERE goal_id=? AND file_key=?', (gid, filename(name).casefold())).fetchone()
    if row is None:
        raise ValueError('此目標沒有保存指定文件。')
    return dict(row)


def _verify_file(path, expected):
    _ordinary(path, directory=False)
    with path.open('rb') as stream:
        actual = stream.read(MAX_BYTES + 1)
    if actual != expected:
        raise ValueError('同名文件內容不同，未覆寫；請保留原檔並使用新檔名。')


def materialize(store, gid, name):
    """Caller holds the goal lease. No deletion or replacement on any path."""
    row = _record(store, gid, name)
    expected = payload(row['name'], row['content'])
    digest = hashlib.sha256(expected).hexdigest()
    if digest != row['sha256'] or len(expected) != row['size_bytes']:
        raise ValueError('已保存文件的內容與指紋不一致，未建立文件。')
    try:
        folder = directory(store, gid, create=True)
        target = folder / row['name']
        try:
            with target.open('xb') as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        directory(store, gid)
        _verify_file(target, expected)
    except (OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else '本機文件讀寫未完成；已保存內容保留，可核對或重建。'
        with store.connect() as db:
            db.execute("UPDATE autonomous_artifacts SET status='uncertain',error=? WHERE id=?", (message, row['id']))
        raise ValueError(message) from None
    with store.connect() as db:
        db.execute("UPDATE autonomous_artifacts SET status='verified',error='',verified_at=CURRENT_TIMESTAMP WHERE id=?", (row['id'],))
        verified_at = db.execute('SELECT verified_at FROM autonomous_artifacts WHERE id=?', (row['id'],)).fetchone()[0]
    return {'name': row['name'], 'sha256': digest, 'size_bytes': len(expected), 'verified_at': verified_at,
            'verification': 'UTF-8 bytes read back and matched saved content'}


def write(store, gid, name, content):
    """Reserve an immutable file intent, then materialize it idempotently."""
    data = payload(name, content)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not enabled(db, gid):
            raise ValueError('此目標未啟用交付文件。')
        old = db.execute('SELECT content FROM autonomous_artifacts WHERE goal_id=? AND file_key=?', (gid, name.casefold())).fetchone()
        if old:
            if old['content'] != content:
                raise ValueError('同名文件已保存不同內容，請使用新檔名。')
        else:
            if db.execute('SELECT COUNT(*) FROM autonomous_artifacts WHERE goal_id=?', (gid,)).fetchone()[0] >= MAX_FILES:
                raise ValueError('每個目標最多保存4份文件，請建立下一階段目標。')
            db.execute('''INSERT INTO autonomous_artifacts(id,goal_id,name,file_key,content,sha256,size_bytes,status)
                VALUES(?,?,?,?,?,?,?,'prepared')''',
                (uuid.uuid4().hex, gid, name, name.casefold(), content, hashlib.sha256(data).hexdigest(), len(data)))
    return materialize(store, gid, name)


def validate_names(names):
    if not isinstance(names, list) or not 1 <= len(names) <= MAX_FILES:
        raise ValueError('交付需列出1到4份已保存的文件。')
    keys = [filename(name).casefold() for name in names]
    if len(set(keys)) != len(keys):
        raise ValueError('交付檔名不可重複。')
    return names


def deliver(store, gid, names, *, expected=None):
    names = validate_names(names)
    with store.connect() as db:
        if not enabled(db, gid):
            raise ValueError('此目標目前採用文字交付，請在視窗交付文字。')
    # Check the whole selection before any reconstruction writes.
    for name in names:
        row = _record(store, gid, name)
        if expected is not None and row['content'] != expected.get(name.casefold()):
            raise ValueError('檔案登記的內容與目前已核對的成果不一致。')
    return [materialize(store, gid, name) for name in names]


def rebuild(store, sid, gid, cancel=None):
    with lease(store, gid):
        with store.connect() as db:
            if db.execute('SELECT 1 FROM autonomous_goals WHERE id=? AND session_id=?', (gid, sid)).fetchone() is None:
                raise ValueError('找不到此對話的目標。')
            names = [row['name'] for row in inventory(db, gid)]
        validate_names(names)
        receipts = []
        for name in names:
            if cancel is not None and cancel.is_set():
                raise ValueError('已停止核對／重建；先前已完成的文件保留。')
            receipts.append(materialize(store, gid, name))
    return {'directory': str(directory(store, gid)), 'artifacts': receipts,
            'note': '核對或重建已保存的歷史內容，未重新驗證內容是否符合目前資料。'}
