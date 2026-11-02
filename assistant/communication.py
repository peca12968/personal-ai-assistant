"""Small, inspectable conversation cues; hints never override the actual request."""
import re
import unicodedata


def _has_positive_cue(pattern, text):
    """Ignore cues immediately preceded by a negation and optional modifiers."""
    negation = (
        r'(?:'
        r'(?:不要|不用|不需要|無需|无需|不必|別|别|不是|並非|并非|不)'
        r'\s*(?:(?:再|太|過於|过于|很|那麼|那么|這麼|这么)\s*)?'
        r'|\b(?:not|never|no|do\s+not|don[\x27\u2019]t)\s+'
        r'(?:(?:need\s+to|have\s+to)\s+)?'
        r'(?:(?:be|give|provide|explain|answer|respond|keep\s+it)\s+)?'
        r'(?:(?:very|too|so)\s+)?'
        r')$'
    )
    return any(
        not re.search(negation, text[:match.start()], re.I)
        for match in re.finditer(pattern, text, re.I)
    )


def conversation_style(question, history):
    """Preserve the user's words and mark uncertain signals as hints, not facts."""
    text = question.strip()
    normalized_text = unicodedata.normalize('NFKC', text)
    concise = _has_positive_cue(
        r'簡短|精簡|簡潔|直接給答案|不要長篇|\bbrief\b|\bconcise\b'
        r'|[一二兩两三四五六七八九十\d]+句(?:話回答|就好)'
        r'|用[一二兩两三四五六七八九十\d]+句(?:話)?說明'
        r'|\b(?:in|within)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+sentences?\b',
        normalized_text,
    )
    detailed = _has_positive_cue(
        r'詳細說明|逐步解釋|完整解釋|\bin detail\b|step.by.step',
        normalized_text,
    )
    correction = bool(re.search(r'不是這個意思|你誤會|我指的是|更正|\bi meant\b|misunderstood', text, re.I))
    return {
        'length_hint': 'mixed' if concise and detailed else 'concise' if concise else 'detailed' if detailed else 'unspecified',
        'possible_correction': correction,
        'has_prior_user_turn': any(row.get('role') == 'user' for row in history),
        'hints_only': True,
    }
