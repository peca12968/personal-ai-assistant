"""Recognize a delivered prose comparison of named options."""

import re

from . import goal_chat_fields, goal_guidance_sources


_BARE = r'[A-Za-z0-9\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff _·-]{1,47}?'
_QUOTED_OPTION = r'(?:「[^「」\r\n]{2,48}」|『[^『』\r\n]{2,48}』|“[^“”\r\n]{2,48}”)'
_OPTION = rf'(?:{_QUOTED_OPTION}|{_BARE})'
_JOIN = r'\s*(?:以及|和|與|跟|及|、)\s*'
_PAIR = re.compile(
    rf'(?P<left>{_OPTION}){_JOIN}(?P<right>{_OPTION})'
    r'(?:(?:各自|分別)?的?(?:差異|優缺點|利弊|取捨|用途|適用情況|適用條件))?'
)
_QUOTED_PAIR = re.compile(
    rf'(?P<left>{_QUOTED_OPTION}){_JOIN}(?P<right>{_QUOTED_OPTION})'
    r'(?:\s*(?:的|在)[^「」『』“”\r\n]{1,48})?'
)
_QUOTES = {'「': '」', '『': '』', '“': '”'}
_FENCE = re.compile(r'^([~\x60])\1{2,}')
_ANY_CLAUSE = re.compile('')
_PREDICATE = re.compile(r'適合|適用|可用|可在|可省|(?<!比)較|更(?!新|正|改)|需要|只在|僅在|支援|受限|不必|'
                        r'優點[是為]|缺點[是為]|能(?:在|用|省|處理|減少|提高|容納)|(?:需|要)用')
_META = re.compile(r'^(?:(?:抱歉|很抱歉|目前|暫時)[，,:： ]*)?(?:無法比較|不能比較|資料不足|'
                   r'還無法|請(?:先|再)?提供|請補充|範例|示例|引用|請比較|尚無法|尚缺|缺少)')
_MISSING_EVIDENCE = re.compile(
    r'(?:需(?:要)?|等(?:待)?)(?:你|您|使用者|對方)(?:先|再)?(?:提供|補充|確認)'
    r'|需(?:要)?(?:更(?:多|完整)(?:的)?|補充)(?:資料|資訊|條件)'
    r'|(?:資料|資訊|條件)(?:不足|缺漏|未提供|尚未提供)|(?:仍|尚|還)(?:待|須)確認'
)
_GENERIC = {'甲方', '乙方', '兩者', '兩案', '兩個選項', '兩種方案', '不同方式', '不同方案'}


def quoted_pair_names(subject):
    match = _QUOTED_PAIR.fullmatch(subject)
    if not match:
        return set()
    names = _labels(match['left']) | _labels(match['right'])
    return names if len(names) == 2 else set()


def _labels(text):
    label = text.strip()
    if label[0] in _QUOTES:
        candidates = (goal_chat_fields.label_key(label[1:-1]),)
    else:
        label = goal_chat_fields.label_key(label)
        candidates = (label, label.rsplit('的', 1)[-1])
    return {name for name in candidates if len(name) >= 2 and name not in _GENERIC}


def _visible_lines(text):
    fence = None
    for line in text.splitlines():
        line = line.strip()
        marker = _FENCE.match(line)
        if fence is None and marker:
            fence = marker[0]
        elif marker and marker[0].startswith(fence) and line == marker[0]:
            fence = None
        elif fence is None and not line.startswith(('>', '|')):
            yield line


def _direct_relation(text, options):
    # Match the two named options around a directional comparison. The
    # comparison can state one relation instead of a separate fact per option.
    for left, right in (options, options[::-1]):
        names = '|'.join(re.escape(name) for name in sorted(left, key=len, reverse=True))
        others = '|'.join(re.escape(name) for name in sorted(right, key=len, reverse=True))
        pattern = (rf'(?:{names}).{{0,24}}?(?<!對)比(?:起)?(?!較|如|方)'
                   rf'[「『“ ]*(?:{others})[」』” ]*'
                   r'(?:更(?!新|正|改)|較|多|少|快|慢|短|長|高|低|省|重|輕)')
        if re.search(pattern, text):
            return True
    return False


def matches(subjects, answer, *, required_names=()):
    # The direct request must name two options. A mention alone does not
    # establish that the visible reply actually compared either option.
    request = goal_guidance_sources.standalone_clauses(subjects, comma_before=_ANY_CLAUSE)
    if not request:
        return False
    subject = request[0][1]
    if not (pair := _QUOTED_PAIR.fullmatch(subject) or _PAIR.fullmatch(subject)):
        return False
    choices = pair['left'], pair['right']
    declared = {goal_chat_fields.label_key(name.strip().strip('「」『』“”')) for name in choices}
    if required_names and declared != required_names:
        return False
    options = tuple(_labels(name) for name in choices)
    if not all(options) or options[0] & options[1]:
        return False
    prose = '\n'.join(_visible_lines(answer))
    addressed = set()
    for _, clause, inquiry in goal_guidance_sources.standalone_clauses(prose, comma_before=_ANY_CLAUSE):
        text = goal_chat_fields.label_key(clause.lstrip('*- \t'))
        if (inquiry or text.endswith(('嗎', '呢')) or _META.match(text) or _MISSING_EVIDENCE.search(text)):
            continue
        present = [index for index, labels in enumerate(options)
                   if any(label in text for label in labels)]
        if len(present) == 2 and _direct_relation(text, options):
            return True
        if len(present) == 1 and _PREDICATE.search(text):
            addressed.add(present[0])
    return len(addressed) == len(options)
