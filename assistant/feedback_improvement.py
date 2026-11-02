"""Local, user-authored acceptance drafts from saved answer feedback."""
import hashlib
import json

from .acceptance import validate_suite
from .context import clip


def feedback_source(store, message_id):
    with store.connect() as db:
        row = db.execute('''SELECT f.message_id,f.rating,f.correction,f.updated_at,m.content AS answer,c.payload
            FROM feedback f JOIN messages m ON m.id=f.message_id
            LEFT JOIN answer_contexts c ON c.message_id=m.id WHERE f.message_id=?''', (message_id,)).fetchone()
    if row is None:
        raise ValueError('這筆回饋已不存在，請重新整理。')
    source = dict(row)
    payload = source.pop('payload')
    source['context'] = json.loads(payload) if payload else None
    source['fingerprint'] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return source


def check_source(store, message_id, fingerprint):
    source = feedback_source(store, message_id)
    if source['fingerprint'] != fingerprint:
        raise ValueError('回饋或原回答快照已變更，請重新選取並核對改善內容。')
    return source


def example_from_source(source):
    if source['rating'] != -1 or not source['correction'].strip():
        raise ValueError('請先為需要改進的回答填寫具體更正。')
    context = source['context']
    if not context or context.get('response_mode') == 'local_memory':
        raise ValueError('此入口需要模型回答的原始快照；本機記憶操作或舊版無快照回答不適用。')
    history = context.get('recent_conversation', [])
    if any(row.get('truncated') for row in history):
        raise ValueError('原回答的近期前文有截斷，無法直接轉成完整驗收例子；請在自我改進另填可重現的例子。')
    return {'question': context['current_request'],
            'history': [{'role': row['role'], 'content': row['content']} for row in history]}


def make_draft(source, question, history, problem, expected):
    example_from_source(source)  # Reject unsuitable source, even when caller provides edited input.
    if not isinstance(problem, str) or not problem.strip() or len(problem) > 2500:
        raise ValueError('改善描述需為 1–2500 字。')
    suite = validate_suite({'version': 1, 'cases': [{'id': 'feedback_' + str(source['message_id']),
        'kind': 'communication', 'question': question, 'history': history,
        'expect': {'length_hint': expected}}]}, ['assistant/communication.py'])
    return {'goal': '改善溝通提示的回答長度辨識，遵守使用者指定的驗收期望。提示仍須服從完整原文。\n'
                    '使用者描述的問題：\n' + problem.strip() + '\n不要修改測試或把整句範例硬編碼成規則。',
            'selected': ['assistant/communication.py'], 'acceptance': suite,
            'source_message_id': source['message_id'], 'source_fingerprint': source['fingerprint']}


def make_followup_draft(source, question, history, problem, selected_turns):
    """Build an exact retrieval-context expectation from explicitly selected turns.

    None is an explicit request to retrieve no previous topic; an empty selection
    is not treated as consent to that choice. Indices refer to the edited history.
    """
    example_from_source(source)
    if not isinstance(problem, str) or not problem.strip() or len(problem) > 2500:
        raise ValueError('改善描述需為 1–2500 字。')
    case = {'id': 'feedback_' + str(source['message_id']), 'kind': 'followup',
            'question': question, 'history': history, 'expect': None}
    validate_suite({'version': 1, 'cases': [case]}, ['assistant/context.py'])
    if selected_turns is not None:
        if (not isinstance(selected_turns, list) or not 1 <= len(selected_turns) <= 4 or
                any(type(i) is not int or not 0 <= i < len(history) for i in selected_turns) or
                selected_turns != sorted(set(selected_turns)) or
                any(history[i]['role'] != 'user' for i in selected_turns)):
            raise ValueError('請依前文順序選取一到四則使用者訊息，不能重複或選取助理回答。')
        case['expect'] = '\n'.join(clip(history[i]['content'], 1000)[0] for i in selected_turns)
    suite = validate_suite({'version': 1, 'cases': [case]}, ['assistant/context.py'])
    return {'goal': '改善接續問題的前文檢索選取，依使用者指定的前文或不沿用要求驗收。'
                    '保留換話題、隔離與長度上限，不能將助理猜測當成使用者事實。\n'
                    '使用者描述的問題：\n' + problem.strip() + '\n不要修改測試或把整句範例硬編碼成規則。',
            'selected': ['assistant/context.py'], 'acceptance': suite,
            'source_message_id': source['message_id'], 'source_fingerprint': source['fingerprint']}
