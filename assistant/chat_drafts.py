"""Local unsent editors; consumption commits with the submitted user message."""
import uuid

MAX_CHARS = 64000


def read(db, sid):
    if not db.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
        raise ValueError('找不到草稿所屬對話。')
    row = db.execute('SELECT revision,content FROM chat_drafts WHERE session_id=?', (sid,)).fetchone()
    return {'revision': row[0], 'content': row[1]} if row else {'revision': 0, 'content': ''}


def get(store, sid):
    with store.connect() as db:
        return read(db, sid)


def validate_text(content):
    if not isinstance(content, str) or len(content) > MAX_CHARS:
        raise ValueError('聊天草稿最多64000字；未截斷，請先縮短或另存文字。')
    try:
        content.encode('utf-8')
    except UnicodeError:
        raise ValueError('草稿含無效Unicode字元，未保存。') from None


def write(db, sid, content, revision):
    validate_text(content)
    if type(revision) is not int or revision < 0 or read(db, sid)['revision'] != revision:
        raise ValueError('此對話的草稿已由另一個視窗變更；請按「核對草稿」，目前輸入保留。')
    if revision >= 9223372036854775807:
        raise ValueError('草稿版本已達上限，未保存。')
    db.execute('''INSERT INTO chat_drafts(session_id,revision,content) VALUES(?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,content=excluded.content''',
        (sid, revision+1, content))
    return {'revision': revision+1, 'content': content}


def save(store, sid, content, revision):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        return write(db, sid, content, revision)


def submission(snapshot):
    return {**snapshot, 'token': uuid.uuid4().hex}


def consume(db, sid, question, draft, message_id):
    if draft is None:
        return
    if (not isinstance(draft, dict) or set(draft) != {'revision','content','token'} or
            type(draft['revision']) is not int or draft['revision'] < 1 or
            not isinstance(draft['token'],str) or len(draft['token']) != 32 or
            any(c not in '0123456789abcdef' for c in draft['token'])):
        raise ValueError('送出的草稿版本無效。')
    validate_text(draft['content'])
    if read(db,sid) != {k:draft[k] for k in ('revision','content')} or draft['content'].strip() != question:
        raise ValueError('草稿或送出內容已變更，沒有送出；請核對草稿。')
    write(db,sid,'',draft['revision'])
    db.execute('INSERT INTO chat_draft_submissions(token,session_id,revision,message_id) VALUES(?,?,?,?)',
               (draft['token'],sid,draft['revision'],message_id))


def submitted(store, sid, draft):
    with store.connect() as db:
        return bool(db.execute('SELECT 1 FROM chat_draft_submissions WHERE token=? AND session_id=? AND revision=?',
                               (draft['token'],sid,draft['revision'])).fetchone())


def validate_saved(db):
    for sid, revision, content in db.execute('SELECT session_id,revision,content FROM chat_drafts'):
        validate_text(content)
        if type(revision) is not int or revision < 1:
            raise ValueError('聊天草稿版本無效。')
    for token, sid, revision, mid in db.execute('SELECT token,session_id,revision,message_id FROM chat_draft_submissions'):
        row = db.execute('SELECT session_id,role FROM messages WHERE id=?',(mid,)).fetchone()
        if (not isinstance(token,str) or len(token)!=32 or any(c not in '0123456789abcdef' for c in token) or
                type(revision) is not int or revision < 1 or read(db,sid)['revision'] <= revision or
                not row or row[0]!=sid or row[1]!='user'):
            raise ValueError('聊天草稿送出紀錄無效。')
