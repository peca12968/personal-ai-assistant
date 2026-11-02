"""Small, inspectable conversation cues; hints never override the actual request."""
import re


def conversation_style(question, history):
    """Preserve the user's words and mark uncertain signals as hints, not facts."""
    text = question.strip()
    concise = bool(re.search(r'簡短|精簡|簡潔|直接給答案|不要長篇|\bbrief\b|\bconcise\b', text, re.I))
    detailed = bool(re.search(r'詳細說明|逐步解釋|完整解釋|\bin detail\b|step.by.step', text, re.I))
    correction = bool(re.search(r'不是這個意思|你誤會|我指的是|更正|\bi meant\b|misunderstood', text, re.I))
    return {
        'length_hint': 'mixed' if concise and detailed else 'concise' if concise else 'detailed' if detailed else 'unspecified',
        'possible_correction': correction,
        'has_prior_user_turn': any(row.get('role') == 'user' for row in history),
        'hints_only': True,
    }
