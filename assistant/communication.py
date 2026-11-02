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


def _length_request_text(text):
    """Hide quoted data, retaining quotes used as a response-style modifier.

    Code and Markdown blockquotes are always data. Other quotes count only
    in a local instruction frame: use a quoted style to answer, or give a
    quoted response. Removing delimiters in these frames preserves negation.
    This deliberately uses grammatical cues rather than guessing intent from
    the quoted words themselves.
    """
    def blank(value):
        return re.sub(r'[^\n]', ' ', value)

    lines = text.splitlines(keepends=True)
    visible = []
    fence = None
    for line in lines:
        if fence is not None:
            visible.append(blank(line))
            if re.match(r'^ {0,3}' + re.escape(fence[0]) +
                        r'{' + str(fence[1]) + r',}[ \t]*(?:\n)?$', line):
                fence = None
            continue
        if re.match(r'^ {0,3}>', line):
            visible.append(blank(line))
            continue
        opening = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            visible.append(blank(line))
        else:
            visible.append(line)
    text = ''.join(visible)
    text = re.sub(
        r'(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)',
        lambda match: blank(match.group()),
        text,
    )

    # Apostrophes within words (don't, user's) are not quotation marks.
    quotes = re.compile(
        r'「[^「」]*」|『[^『』]*』|“[^“”]*”|‘[^‘’]*’'
        r'|"[^"\n]*"|(?<!\w)\x27[^\x27\n]*\x27(?!\w)'
    )

    def quoted_text(match):
        before = text[:match.start()]
        after = text[match.end():]
        style_frame = (
            re.search(r'(?:用|以)\s*$', before)
            and re.match(
                r'\s*(?:的)?\s*(?:方式|形式|風格|风格|語氣|语气)?\s*'
                r'(?:來|来)?\s*(?:回答|回覆|回复|說明|说明|解釋|解释)',
                after,
            )
        )
        response_frame = (
            re.search(r'(?:給我|给我|提供|\b(?:give\s+me|provide))\s*$', before, re.I)
            and re.match(r'\s*(?:[。.!！?？,，;；]|$)', after)
        )
        english_style_frame = (
            re.search(r'\b(?:be|keep\s+it|answer|respond)\s*$', before, re.I)
            and re.match(r'\s*(?:[.!?,;]|$)', after)
        )
        if style_frame or response_frame or english_style_frame:
            return match.group()[1:-1]
        return blank(match.group())

    return quotes.sub(quoted_text, text)


def conversation_style(question, history):
    """Preserve the user's words and mark uncertain signals as hints, not facts."""
    text = question.strip()
    normalized_text = unicodedata.normalize('NFKC', text)
    request_text = _length_request_text(normalized_text)
    concise = _has_positive_cue(
        r'簡短|精簡|簡潔|直接給答案|不要長篇|\bbrief\b|\bconcise\b'
        r'|[一二兩两三四五六七八九十\d]+句(?:話回答|就好)'
        r'|用[一二兩两三四五六七八九十\d]+句(?:話)?說明'
        r'|\b(?:in|within)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+sentences?\b',
        request_text,
    )
    detailed = _has_positive_cue(
        r'詳細說明|逐步解釋|完整解釋|\bin detail\b|step.by.step',
        request_text,
    )
    correction = bool(re.search(r'不是這個意思|你誤會|我指的是|更正|\bi meant\b|misunderstood', text, re.I))
    return {
        'length_hint': 'mixed' if concise and detailed else 'concise' if concise else 'detailed' if detailed else 'unspecified',
        'possible_correction': correction,
        'has_prior_user_turn': any(row.get('role') == 'user' for row in history),
        'hints_only': True,
    }
