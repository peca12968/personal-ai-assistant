"""Recognize a saved question that explicitly asks for an earlier changed field."""
import re


def _quoted(text):
    return ('「' + text + '」', '『' + text + '』', '"' + text + '"')


def _alternatives(values):
    ordered = sorted(set(values), key=lambda value: (-len(value), value))
    return '(?:' + '|'.join(map(re.escape, ordered)) + ')'


def choices(question, fields, value, quote, citation):
    if (not isinstance(question, str) or not 2 <= len(question) <= 800
            or any(char in question for char in '\r\n\x00') or len(fields) < 2):
        return None
    tokens = {}
    for field in fields:
        for token in (field, *_quoted(field)):
            if token in tokens and tokens[token] != field:
                return None
            tokens[token] = field
    label = _alternatives(tokens)
    captures = [f'({label})' for _ in fields]
    separator = r' *(?:還是|或是?|、|，|,) *'
    last_separator = r' *(?:還是|或是?) *'
    options = separator.join(captures[:-1]) + last_separator + captures[-1]
    scope = r'(?:為了(?:依(?:原)?值班表)?(?:確認|判斷)(?:指定收件人|收件人|通知對象|本次去向|去向)[，,] *)?'
    lead = scope + r'(?:請問|請確認|想確認)? *'
    tail = (r'[？?](?: *(?:值班表依改動欄位指定收件人|收件人由改動欄位決定|去向依改動欄位而定)'
            r'[。.]?)?')
    time = r'(?:(?:這次|本次|第一筆|原先) *)?'
    change = r'(?:正式 *)?(?:改成|改為|調整為) *' + _alternatives((value, *_quoted(value)))
    literal = _alternatives((*_quoted(quote), *_quoted(quote + '。')))
    cited = rf'(?:你(?:先前|剛才)?(?:說|回覆)[：: ]*)?{literal} *(?:{re.escape(citation)})?[，, ]*'
    identifies = r'(?:的?是|是指|指的是|指的|指|說的是) *'
    forms = (
        rf'{lead}(?:{time}{change}|{cited}){identifies}{options}{tail}',
        rf'{lead}{time}是 *{options} *{change}{tail}',
        rf'{lead}(?:{time}{change}|{cited})(?:指的是|是指|指|的是|是)? *哪(?:一個|個|一)?欄(?:位)?[？?] *'
        rf'請(?:回覆|回答|確認) *{options}[。.]?',
    )
    for pattern in forms:
        match = re.fullmatch(pattern, question)
        if match:
            names = [tokens[token] for token in match.groups()]
            if set(names) == set(fields):
                return names
    return None
