"""Small, inspectable conversation cues; hints never override the actual request."""
import re
import unicodedata


def _has_positive_cue(pattern, text):
    """Ignore locally negated cues using bounded verb and object phrases.

    Only grammatical connectors may bridge a negation and a cue. Sentence
    boundaries and arbitrary topic words cannot extend the negation's scope.
    """
    english_object = (
        r'(?:me|it|this|that|them|these|those|us|'
        r'(?:(?:the|your|my|this|that|an?)\s+)?'
        r'(?:answer|response|explanation|summary))'
    )
    negation = (
        r'(?:'
        r'(?:不要|不用|不需要|無需|无需|不必|別|别|不是|並非|并非|不)'
        r'\s*(?:要\s*)?'
        r'(?:(?:用|以|給我|给我|提供|保持|回答|回覆|回复|說明|说明|解釋|解释)\s*)?'
        r'(?:(?:再|太|過於|过于|很|那麼|那么|這麼|这么)\s*)?'
        r'|\b(?:not|never|no|do\s+not|don[\x27\u2019]t)\s+'
        r'(?:(?:need\s+to|have\s+to|want(?:\s+to)?)\s+)?'
        r'(?:(?:be|give|provide|explain|answer|respond|keep)\s+)?'
        r'(?:' + english_object + r'\s+)?'
        r'(?:(?:an?|the)\s+)?'
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
    quoted response. Nested quotes remain data even within a style frame.
    Delimiters become spaces so negation and word boundaries are preserved.
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

    def latin_letter(char):
        return char.isalpha() and 'LATIN' in unicodedata.name(char, '')

    def is_apostrophe(index, char, stack):
        """Resolve word punctuation without swallowing paired quote ends.

        A left curly mark opens a quote even when it touches letters. A
        right mark inside a Latin word is normally an apostrophe, but an
        active single quote also makes it a possible closing delimiter.
        In that case, possessive/contraction suffixes and elided names
        supply local grammatical evidence for keeping it in the word.
        """
        if char not in ("'", '’') or index == 0:
            return False
        before = text[index - 1]
        after = text[index + 1] if index + 1 < len(text) else ''
        if char == "'":
            return bool(after and (before.isalnum() or before == '_')
                        and (after.isalnum() or after == '_'))
        if not latin_letter(before):
            return False
        closing_single = bool(stack and stack[-1][1] == '‘')
        if not closing_single:
            # This also preserves trailing plural possessives inside
            # double quotes, where this mark cannot close the quote.
            return True
        if not after or not latin_letter(after):
            return False
        suffix = re.match(r'[^\W\d_]+', text[index + 1:])
        word = suffix.group() if suffix else ''
        if word.lower() in ('s', 't', 're', 've', 'll', 'd', 'm'):
            return True
        # Names such as O’Connor and elisions such as d’Artagnan.
        prefix = re.search(r'[^\W\d_]+$', text[:index])
        return bool(prefix and len(prefix.group()) == 1 and after.isupper())

    # Track paired delimiters, including repeated asymmetric opening marks.
    pairs = {'「': '」', '『': '』', '“': '”', '‘': '’', '"': '"', "'": "'"}
    stack = []
    spans = []
    for index, char in enumerate(text):
        if is_apostrophe(index, char, stack):
            continue
        if char == '\n':
            # Straight quotes, unlike paired typographic quotes, are local
            # to a line, matching the existing treatment of quoted strings.
            stack = [entry for entry in stack if entry[1] not in ('"', "'")]
        if stack and char == pairs[stack[-1][1]]:
            start, _ = stack.pop()
            spans.append((start, index + 1))
        elif char in pairs:
            stack.append((index, char))

    def is_style_frame(start, end):
        before = text[:start]
        after = text[end:]
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
        return bool(style_frame or response_frame or english_style_frame)

    result = list(text)
    outer_end = -1
    for start, end in sorted(spans, key=lambda span: (span[0], -span[1])):
        nested = start < outer_end
        if not nested:
            outer_end = end
        if not nested and is_style_frame(start, end):
            result[start] = ' '
            result[end - 1] = ' '
        else:
            result[start:end] = list(blank(text[start:end]))
    return ''.join(result)


def _current_length_request(text):
    """Discard withdrawn instructions only for a direct style correction.

    Match the start of the correction, never a later length word in a topic
    correction. English objects are limited to pronouns and response nouns;
    arbitrary topic phrases cannot bridge a verb and a style modifier.
    Keep the entire replacement so combined short/long requests stay mixed.
    """
    markers = re.compile(
        r'(?:最後\s*)?(?:更正|改為|改成)\s*[:：,，]?\s*'
        r'|算了\s*[,，:：]?\s*'
        r'|\b(?:actually|i\s+meant|instead)\b\s*[:,]?\s*',
        re.I,
    )
    chinese_style = (
        r'(?:請\s*)?(?:先\s*)?'
        r'(?:(?:不要|不用|不需要|無需|无需|不必|別|别|不是|並非|并非|不)\s*(?:要\s*)?)?'
        r'(?:改為\s*|改成\s*)?'
        r'(?:簡短|精簡|簡潔|直接給答案|不要長篇|詳細說明|逐步解釋|完整解釋'
        r'|(?:用)?[一二兩两三四五六七八九十\d]+句(?:話回答|就好|話?說明))'
    )
    english_object = (
        r'(?:(?:it|this|that|them|these|those)'
        r'|(?:(?:the|your|this|that)\s+)?(?:answer|response|explanation|summary))'
    )
    english_style = (
        r'(?:please\s+)?'
        r'(?:(?:do\s+not|don[\x27\u2019]t|never)\s+)?'
        r'(?:'
        r'be\s+(?:brief|concise)\b'
        r'|keep\s+' + english_object + r'\s+(?:brief|concise)\b'
        r'|(?:explain|answer|respond)(?:\s+' + english_object + r')?\s+'
        r'(?:in\s+detail\b|step.by.step\b)'
        r'|(?:give|provide)\s+(?:me\s+)?(?:a\s+)?(?:brief|concise)\b'
        r')'
    )
    direct_style = re.compile(r'(?:' + chinese_style + r'|' + english_style + r')', re.I)
    start = 0
    for marker in markers.finditer(text):
        if direct_style.match(text, marker.end()):
            start = marker.end()
    return text[start:]


def conversation_style(question, history):
    """Preserve the user's words and mark uncertain signals as hints, not facts."""
    text = question.strip()
    normalized_text = unicodedata.normalize('NFKC', text)
    request_text = _current_length_request(_length_request_text(normalized_text))
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
