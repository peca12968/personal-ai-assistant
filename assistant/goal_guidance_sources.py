"""Attribute explicitly appended goal guidance to its original user text."""
import hashlib
import json
import re

from .goal_reply_sources import saved_report


CITATIONS = re.compile(r'\[更正#[^\]\r\n]{0,32}\]')
GUIDANCE = ('本目標的user_notes依時間順序列出。history是先前問題與補充的原有文字；'
            '帶citation的content是以「補充／更正此目標」保存的主動更正原話。'
            '在問題、暫表或定稿採用、比較這些更正時，在對應欄位旁引用提供的更正編號，例如[更正#1]。'
            '原值仍引用文件，對提問的回覆仍引用補充編號。區分原值、被取代值、實際生效值與未採用提案。'
            '依每則原話的對象、條件與適用範圍判斷，只重算受影響的結論。'
            '暫表與定稿保留已讀且仍屬本次交付的欄位，例如相同批次的窗口、查驗人或期限，除非使用者縮小範圍或欄位不再適用。'
            '編號只指原話，不能當成所有引文或假設都已採用。不可自行編號或沿用其他目標的編號。')
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00')
_CLAUSE_TOKENS = re.compile(r"""```|~~~|`|[「」『』“”‘’"'。；;！？?!，,\r\n]""")
_QUOTES = {'「': '」', '『': '』', '“': '”', '‘': '’', '"': '"', "'": "'", '`': '`', '```': '```', '~~~': '~~~'}
_LITERAL_QUOTE = {'"', "'", '`', '```', '~~~'}


def standalone_clauses(text, *, comma_before=None):
    """Return exact clause offsets and whether the delimiter was a question mark."""
    clauses, stack = [], []
    start = 0
    for token in [*_CLAUSE_TOKENS.finditer(text), None]:
        value = token[0] if token else ''
        if value == "'" and 0 < token.start() < len(text) - 1:
            left, right = text[token.start() - 1], text[token.end()]
            if left.isascii() and left.isalnum() and right.isascii() and right.isalnum():
                continue
        if stack and value == stack[-1]:
            stack.pop()
        elif stack and stack[-1] in _LITERAL_QUOTE:
            continue
        elif value in _QUOTES:
            stack.append(_QUOTES[value])
        elif value in _QUOTES.values():
            return []
        elif not stack:
            if value in {'，', ','} and (comma_before is None or not comma_before.match(text, token.end())):
                continue
            end = token.start() if token else len(text)
            raw = text[start:end]
            if clause := raw.strip():
                clauses.append((start + len(raw) - len(raw.lstrip()), clause, value in {'？', '?'}))
            start = token.end() if token else end
    return [] if stack else clauses


def note(content, steps_seen, stamp):
    return f"\n使用者主動更正／補充（已記錄{steps_seen}步；{stamp}）：\n{content}\n"


def read(db, gid, notes, step_count):
    sources = []
    previous_end, previous_step = 0, 0
    for number, start, steps_seen, stamp, content in db.execute(
            'SELECT number,notes_start,steps_seen,stamp,content FROM autonomous_guidance_sources '
            'WHERE goal_id=? ORDER BY number', (gid,)):
        if (type(number) is not int or number != len(sources) + 1
                or type(start) is not int or not previous_end <= start <= len(notes)
                or type(steps_seen) is not int or not previous_step <= steps_seen <= min(31, step_count)
                or not isinstance(stamp, str) or not STAMP.fullmatch(stamp)
                or not isinstance(content, str) or not content.strip() or len(content) > 4000 or '\0' in content):
            raise ValueError('主動更正的保存位置或格式不符。')
        part = note(content, steps_seen, stamp)
        previous_end, previous_step = start + len(part), steps_seen
        if notes[start:previous_end] != part:
            raise ValueError('主動更正來源與保存的補充原文不一致。')
        raw = json.dumps([gid, number, start, steps_seen, stamp, content], ensure_ascii=False, separators=(',', ':'))
        sources.append({'citation': f'[更正#{number}]', 'number': number, 'steps_seen': steps_seen, 'notes_start': start,
                        'recorded_at': stamp, 'content': content,
                        'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()})
    return sources


def prompt_notes(notes, sources):
    if not sources:
        return notes
    segments = []
    cursor = 0
    for source in sources:
        start = source['notes_start']
        if start > cursor:
            segments.append({'history': notes[cursor:start]})
        segments.append({key: source[key] for key in ('citation', 'steps_seen', 'content')})
        cursor = start + len(note(source['content'], source['steps_seen'], source['recorded_at']))
    if cursor < len(notes):
        segments.append({'history': notes[cursor:]})
    return segments


def citations(text, sources, *, label='成果'):
    by_token = {item['citation']: item for item in sources}
    checks = []
    for match in CITATIONS.finditer(text):
        source = by_token.get(match.group())
        if source is None:
            raise ValueError(f'{label}引用了本目標尚未保存的主動更正，請核對更正編號。')
        checks.append({key: source[key] for key in ('citation', 'number', 'sha256')})
    return checks


def prior_sources(sources, sequence):
    return [source for source in sources if source['steps_seen'] < sequence]


def validate_saved(db, *, table_present):
    candidates = "SELECT goal_id FROM autonomous_steps WHERE observation_json LIKE '%\"guidance_citations\"%'"
    if table_present:
        candidates += ' UNION SELECT goal_id FROM autonomous_guidance_sources'
    for (gid,) in db.execute(f'SELECT DISTINCT goal_id FROM ({candidates})'):
        (notes,) = db.execute('SELECT notes FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
        steps = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                           'WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        sources = read(db, gid, notes, len(steps)) if table_present else []
        for sequence, action_json, observation_json in steps:
            action, observation = json.loads(action_json), json.loads(observation_json)
            report = saved_report(action, observation)
            prior = prior_sources(sources, sequence)
            if 'guidance_citations' not in observation and not prior:
                continue
            expected = citations(report, prior) if isinstance(report, str) else []
            if (observation.get('guidance_citations') != (expected or None)
                    or (not expected and 'guidance_citations' in observation)):
                raise ValueError('成果的主動更正引用與保存來源不一致。')
