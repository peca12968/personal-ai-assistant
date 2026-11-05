"""Frozen grammar for self-contained, explicitly requested condition checks."""
import re


PREFIX = re.compile(r'(?:請)?條件核對\s*[：:]')
MAX_CHARS = 4000
_SENTENCE = re.compile(r'[^。；;！？?!\r\n]+')
_AND = re.compile(r'「[^」]*」|(?:[，,]\s*)?(?:而且|並且|以及|且)')
_AND_FACT = re.compile(_AND.pattern + r'|及')
_ATOM = re.compile(r'[\u3400-\u9fffA-Za-z0-9_ -]{1,120}')
_AMBIGUOUS = re.compile(r'或|和|與|跟|同時|除非|才|若|如果|只要|只有|可能|不|未|無|沒|上次|前述|上述|附件|剛才|請|能否|是否|原因|所以')
_FACT = re.compile(r'(?:目前只知道|現在只知道|已觀察到|已確認|已知|觀察到)\s*(.+)')
_QUERY = re.compile(
    r'(?:(?:依這些資訊|依規則|依上述條件)[，,]\s*|這些資訊)?'
    r'(?:能否確定|是否能確定|可以確定|能確定|能否判定|能否確認|能判定)\s*(.+)'
)


def _conjunction(text, *, facts=False):
    start = 0
    parts = []
    for match in (_AND_FACT if facts else _AND).finditer(text):
        if match[0].startswith('「'):
            continue
        parts.append(text[start:match.start()].strip())
        start = match.end()
    parts.append(text[start:].strip())
    if len(parts) > 12 or not all(parts):
        raise ValueError('同一組合取需要一至十二個完整命題。')
    return parts


def _unquote(text):
    return text[1:-1] if text.startswith('「') and text.endswith('」') else text


def _declared(text):
    text = text.strip()
    negated = text.startswith('並非')
    if negated:
        text = text[2:].strip()
    quoted = text.startswith('「') and text.endswith('」')
    name = _unquote(text)
    if not name.strip() or name.strip() != name or not _ATOM.fullmatch(name) or (not quoted and _AMBIGUOUS.search(name)):
        raise ValueError('規則中的命題不明確，請分開條件並以「」標出完整命題。')
    return ('!' if negated else '') + name


def _terms(text):
    return [_declared(part) for part in _conjunction(text)]


def _modal(text, markers):
    # The connective belongs to the rule syntax, not the state name.
    if text.startswith(('「', '並非「')):
        return None
    matches = list(re.finditer(markers, text))
    if len(matches) != 1:
        return None
    match = matches[0]
    return (text[:match.start()] + text[match.end():]).strip()


def _rule(clause):
    match = re.fullmatch(r'只有(.+?)[，,]\s*(.+)', clause)
    if match:
        result = _modal(match[2], r'才(?:會|能|可)?')
        if not result:
            raise ValueError('必要條件請寫成「只有甲，乙才會成立」的形式。')
        return _terms(result), _terms(match[1]), 'required'
    match = re.fullmatch(r'(?:只要|如果|若)(.+?)[，,]\s*(.+)', clause)
    if match:
        if match[2].startswith('則'):
            result = match[2][1:].strip()
        else:
            result = _modal(match[2], r'就會|一定會?|必定會?')
        if not result:
            raise ValueError('充分條件請以「則」或「就會」接上結果。')
        return _terms(match[1]), _terms(result), 'required'
    match = re.fullmatch(r'(.+?)(?:也)?可能(?:使|讓|造成)(.+)', clause)
    if match:
        return _terms(match[1]), _terms(match[2]), 'possible'
    match = re.fullmatch(r'(.+?)(?:推出|就會讓|就會使)(.+)', clause)
    if match:
        return _terms(match[1]), _terms(match[2]), 'required'
    raise ValueError('請先逐句提供明確規則，再提供已知觀察。')


def _reference(text, labels):
    text = text.strip()
    negated = text.startswith('並非')
    if negated:
        text = text[2:].strip()
    quoted = text.startswith('「') and text.endswith('」')
    text = _unquote(text)
    if text in labels and (quoted or negated):
        return ('!' if negated else '') + text
    candidates = set()
    if text in labels:
        candidates.add(text)
    # Match one conventional surface marker to an existing full name. The
    # remaining text must match exactly, and nested modifiers stay ambiguous.
    if '的' not in text and not negated:
        for match in re.finditer(r'沒有|不會|會', text):
            name = text[:match.start()] + text[match.end():]
            if match.end() < len(text) and name in labels:
                candidates.add(('!' if match[0] in ('沒有', '不會') else '') + name)
    if len(candidates) != 1:
        raise ValueError(f'無法唯一對應命題「{text}」。請用與規則相同的名稱，否定時可寫「並非」加命題。')
    return candidates.pop()


def compile_request(question):
    if not isinstance(question, str) or not 1 <= len(question) <= MAX_CHARS:
        raise ValueError('本機條件核對限四千字。')
    prefix = PREFIX.match(question)
    if not prefix:
        raise ValueError('本機條件核對缺少指令開頭。')
    clauses = [match[0].strip() for match in _SENTENCE.finditer(question, prefix.end()) if match[0].strip()]
    if not 2 <= len(clauses) <= 49:
        raise ValueError('請提供規則及一個待判命題。')
    query = clauses[-1]
    if match := _QUERY.fullmatch(query):
        target = match[1].removesuffix('嗎').strip()
    elif match := re.fullmatch(r'問(.+)這個命題能否成立', query):
        target = match[1]
    else:
        raise ValueError('最後請單獨寫一個「能否確定……？」的問題。')
    rules, facts = [], []
    labels = set()
    for clause in clauses[:-1]:
        if len(clause) > 500:
            raise ValueError('單一規則或觀察限五百字。')
        if match := _FACT.fullmatch(clause):
            if not rules:
                raise ValueError('請先給規則，再給已知觀察。')
            text = match[1]
            try:
                values = [_reference(text, labels)]
            except ValueError:
                values = [_reference(part, labels) for part in _conjunction(text, facts=True)]
            facts.extend({'literal': value, 'source': clause} for value in values)
            continue
        if facts:
            raise ValueError('規則需寫在觀察之前，每句只寫一項規則或觀察。')
        when, results, kind = _rule(clause)
        labels.update(literal.removeprefix('!') for literal in when + results)
        rules.extend({'when': when, 'then': result, 'kind': kind, 'source': clause} for result in results)
        if len(labels) > 12 or len(rules) > 24:
            raise ValueError('本機條件核對最多十二個命題、二十四條規則。')
    if not rules or len(facts) > 24:
        raise ValueError('需提供至少一條規則，觀察最多二十四項。')
    return {'rules': rules, 'facts': facts, 'query': {'literal': _reference(target, labels), 'source': query}}
