"""Recognize explicit colloquial steering without treating reports as instructions."""
import re


_LINE = r'\[D[1-9][0-9]{0,11}:L[1-9][0-9]{0,11}\]'
_REPORTED_RULE = re.compile(
    rf'(?:有人說「(?:先)?改採 *(?P<quoted>{_LINE})」'
    rf'|聽說要改採 *(?P<reported>{_LINE})[，,] *(?:是)?確定了嗎'
    rf'|(?:要)?改採 *(?P<question>{_LINE}) *嗎)')
_DEFERRED_RULE = re.compile(rf'改採 *(?:{_LINE} *(?:還沒確定|先等等)|那筆先等等)')


def field_names(domains):
    aliases = {}
    for name in domains:
        short = name.removesuffix('班別')
        if short and short != name and short not in domains:
            aliases.setdefault(short, []).append(name)
    return {**{name: name for name in domains}, **{short: names[0] for short, names in aliases.items() if len(names) == 1}}


def clause_boundary(domains):
    fields = '|'.join(re.escape(name) for name in sorted(field_names(domains), key=len, reverse=True))
    return re.compile(
        rf'[「」『』]|[。；;？！\r\n]+|[，,][ \t]*(?:'
        rf'(?:並且?|同時|另外|而且|也|再|另)(?:把|將)?[ \t]*|(?={fields}|改採|撤回))')


def _values(text, field_values):
    options = [value.strip().removeprefix('「').removesuffix('」') for value in re.split(r'還是|或', text)]
    if len(options) == 2 and len(set(options)) == 2 and set(options) <= set(field_values):
        return options
    return None


def clause(clause_text, domains):
    if match := _REPORTED_RULE.fullmatch(clause_text):
        citation = next(value for value in match.groupdict().values() if value is not None)
        return 'clarify_rule', {'citation': citation, 'operation': 'replace'}
    if _DEFERRED_RULE.fullmatch(clause_text):
        return 'deferred_rule', None
    names = field_names(domains)
    for label in sorted(names, key=len, reverse=True):
        if not clause_text.startswith(label):
            continue
        field = names[label]
        remainder = clause_text[len(label):].strip()
        if remainder in {'照舊', '維持原值'}:
            return 'unchanged', None
        if remainder in {'還在等回覆', '還沒確定', '我還要問'}:
            return 'deferred_condition', {'field': field}
        if remainder.endswith('我還要問') and (values := _values(remainder.removesuffix('我還要問'), domains[field])):
            return 'deferred_condition', {'field': field, 'values': values}
        if '還是' in remainder and (values := _values(remainder, domains[field])):
            return 'clarify_condition', {'field': field, 'values': values}
        explicit = re.fullmatch(r'(?:就用|就|這邊[，,] *)[ \t]*(?P<value>.+?)(?:就好|好了)?', remainder)
        if explicit is None and label == field:
            explicit = re.fullmatch(r'(?P<value>.+)', remainder)
        if explicit:
            value = explicit['value'].strip().removeprefix('「').removesuffix('」')
            if value in domains[field]:
                return 'condition', (field, value)
        return None
    return None


def question(pending):
    if pending['kind'] == 'clarify_rule':
        return f'「{pending["target"]["citation"]}」要正式改採嗎？請回答「改採」或「維持原規則」。'
    target = pending['target']
    left, right = target['values']
    return f'「{target["field"]}」要用哪個值？請回答「{left}」或「{right}」。'


def answer(pending, text):
    if text == '取消這則補充':
        return 'cancel', None
    if pending['kind'] == 'clarify_rule':
        if text in {'改採', '正式改採', '是，改採'}:
            return 'choice', pending['target']
        if text in {'維持原規則', '不改採', '不用改採'}:
            return 'keep', None
    else:
        target = pending['target']
        if text in target['values']:
            return 'condition', (target['field'], text)
        if text == '維持原值':
            return 'keep', None
    raise ValueError(question(pending) + ' 可回答「取消這則補充」。')


def named_answer(pending, text):
    target = pending['target']
    if pending['kind'] == 'clarify_rule':
        citation = re.escape(target['citation'])
        if re.fullmatch(rf'(?:正式)?改採 *{citation}', text):
            return 'choice', target
        if re.fullmatch(rf'(?:不改採|不用改採|維持原規則) *{citation}', text):
            return 'keep', None
        if re.fullmatch(rf'改採 *{citation} *(?:還沒確定|先等等)', text):
            return 'defer', None
        return None
    field = target['field']
    if not text.startswith(field):
        return None
    remainder = text[len(field):].strip()
    if remainder in {'先不決定', '還沒確定', '還在等回覆'}:
        return 'defer', None
    match = re.fullmatch(r'(?:正式改為|正式確認為|改為|改成|就用|[：:])? *(.+)', remainder)
    if match:
        value = match[1].removeprefix('「').removesuffix('」')
        if value in target['values']:
            return 'condition', (field, value)
    return None
