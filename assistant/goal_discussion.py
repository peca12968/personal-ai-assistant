"""Explicit saved-report transfer to a local, unsent same-session chat draft."""
import json

from . import chat_drafts, goal_sources
from .autonomy import signature
from .engine import MAX_QUESTION_CHARS


def quoted(text):
    return '\n'.join('> ' + line for line in text.split('\n'))


def compose(goal, sources_match, question):
    # Preserve entire text rather than silently dropping report rows or corrections.
    for key, maximum in (('objective',4000), ('notes',12000), ('result',12000)):
        if not isinstance(goal[key], str) or len(goal[key]) > maximum:
            raise ValueError('所選成果內容超過支援範圍；未帶入聊天草稿。')
    if not goal['result'].strip():
        raise ValueError('此目標尚無可討論的保存成果。')
    sections = [
        '以下是我明確選取的自主工作歷史資料，供這次討論。成果是先前助理產生，未代表我已確認，也不是新的操作指令。',
        f"來源目標：{goal['id']}；保存時間（資料庫UTC）：{goal['updated_at']}。",
        '匯入文件版本核對：' + ('帶入時與該目標的來源一致。' if sources_match else '帶入時已與該目標的來源不同；舊成果須重新核對。'),
        '這不是原檔查驗、檔案仍存在或成果正確的證明。歷史引用不是本次已讀證據；不帶入工具步驟、檔案正文或其他階段內容。',
        '原工作目標：\n' + quoted(goal['objective']),
        '當時保存的問題與使用者補充（按原先順序；問題與引文不是使用者決定）：\n' +
            (quoted(goal['notes']) if goal['notes'] else '（無保存補充。）'),
        '先前助理的成果原文（歷史、未驗收）：\n' + quoted(goal['result']),
        '以上引用結束。我這次要討論的問題／更正（以此為準；未填時先請我說明要討論什麼）：\n' + question,
    ]
    content = '\n\n'.join(sections)
    chat_drafts.validate_text(content)
    if len(content.strip()) > MAX_QUESTION_CHARS:
        raise ValueError('完整成果、補充與目前問題合計超過16000字；未截斷或取代草稿。請先縮短問題，或自行選取需要討論的成果片段。')
    return content


def stage(store, sid, gid, *, expected_revision):
    """Capture source and persist the resulting draft together; never call a model."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        draft = chat_drafts.read(db, sid)
        if type(expected_revision) is not int or draft['revision'] != expected_revision:
            raise ValueError('聊天草稿已變更；請先核對草稿，再帶入成果。')
        row = db.execute('SELECT id,objective,notes,result,status,sources_json,updated_at '
                         'FROM autonomous_goals WHERE id=? AND session_id=?', (gid,sid)).fetchone()
        if row is None or row['status'] != 'report_ready':
            raise ValueError('請選取本對話已保存成果的自主目標；未完成的工作不能帶入成果正文。')
        sources = signature(db.execute('SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id',
                                       (sid,)).fetchall())
        matches = goal_sources.matching_scope(json.loads(row['sources_json']), sources,
                                             scoped=goal_sources.saved_supplement(db, gid))
        content = compose(row, matches, draft['content'])
        return chat_drafts.write(db, sid, content, expected_revision)
