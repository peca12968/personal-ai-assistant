"""Conservative formatting of explicit, text-only local chat conclusions."""
from collections import Counter
from fractions import Fraction
import json
import re

from assistant.context import CURRENT_MESSAGE_ONLY, _instruction_text, current_message_only
from assistant.engine import render_prompt
from scripts import daily_dialogue, daily_stock
from scripts.daily_identifiers import missing_literal, omit_unrelated_identifier, supplied_code

SEP = r'(?:^|[。！？；，,;？?\n])\s*'
ONLY = r'(?:請)?只(?:需|要)?(?:回傳|回覆|回答|輸出|給|填入|填|回)'
END = r'\s*[。.!！]?\s*$'
CONTRACTS = {
    'missing': r'(?:若|如果)(?:無法得知|無法確定|資料不足|沒有資料)[，,]\s*' + ONLY + r'\s*[「『]?(資料不足|資訊不足|無法確定)[」』]?',
    'number': ONLY + r'\s*(?:一個|合計的|最後的|最終的)?(?:阿拉伯數字|整數|數字)',
    'time': ONLY + r'(?:最早可行的開始及結束時間|開始及結束時間|時段|時間區間)[，,]\s*格式\s*HH:MM-HH:MM',
    'code': ONLY + r'(?:單一|一個)?(?:代碼|編號)',
    'choice': ONLY + r'(?:一個)?(?:選項字母|選項代號|大寫英文字母)',
}
NUMBER = r'[+-]?(?:[1-9][0-9]{0,2}(?:,[0-9]{3})+|0|[1-9][0-9]*)(?:\.[0-9]+)?'
CLOCK = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'
RANGE = rf'({CLOCK})\s*[-–—至到]\s*({CLOCK})'
LABEL = r'(?:合計|總計|總額|總數|答案|結果|總金額|最終金額|金額|數量)'
IS = r'\s*(?:為|是|等於|[:：=])\s*[:：]?\s*'
CODE = r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*'
IDENTIFIER_FIELD = r'(?:代碼|編號|序號|型號)'
IDENTIFIER_TARGET = r'[\u4e00-\u9fffA-Za-z0-9]{1,24}?'
QUOTES = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|"[^"]*"|\x60[^\x60]*\x60', re.S)
_SCOPE_SENTENCE = r'([^。.!！．\r\n]{1,110})'
_LEADING_SCOPE = re.compile(_SCOPE_SENTENCE + r'[。.!！．\r\n]\s*')
_TRAILING_SCOPE = re.compile(r'(?<=[。.!！．\n])\s*' + _SCOPE_SENTENCE + r'[。.!！．]?$')
_SCOPED_QUESTION = re.compile(r'[^。.!！？?；;\x00-\x1f]{2,600}[？?]\s*([^\x00-\x1f]{1,180})')
_LABEL_QUOTES = {'「': '」', '『': '』', '"': '"', '“': '”', "'": "'", '‘': '’'}
_QUOTED_FALLBACK = r"""[「『"'“‘][^「」『』"'“”‘’\[\]【】\x00-\x1f\x60\x7f]{1,64}[」』"'”’]"""
_ENGLISH_FALLBACK = rf'(?:{_QUOTED_FALLBACK}|[A-Za-z]+(?: +[A-Za-z]+){{0,5}})'
_ENGLISH_MISSING = r'the [A-Za-z]+(?: +[A-Za-z]+){0,5} is (?:missing|not provided)'
_SCOPED_FALLBACKS = (
    re.compile(r'(?:資料不足時|沒有資料時|(?:若|如果)(?:無法確定|無法得知|資料不足|沒有資料))'
               rf'[，,]?\s*{ONLY}\s*(?P<label>{_QUOTED_FALLBACK})[。.]?'),
    re.compile(rf'If {_ENGLISH_MISSING}, +reply with exactly +(?P<label>{_ENGLISH_FALLBACK})\.?', re.I),
    re.compile(rf'Reply with exactly +(?P<label>{_ENGLISH_FALLBACK}) +if {_ENGLISH_MISSING}\.?', re.I),
)


def _scoped_missing_label(question, draft):
    """Remove one extra full stop from an already selected literal."""
    if not draft.endswith(('.', '。')) or len(draft) > 65:
        return None
    if (leading := _LEADING_SCOPE.match(question)) and current_message_only(leading[1]):
        body = question[leading.end():]
    elif (trailing := _TRAILING_SCOPE.search(question)) and current_message_only(trailing[1]):
        body = question[:trailing.start()].rstrip()
    else:
        return None
    single = _SCOPED_QUESTION.fullmatch(body)
    if not single or _instruction_text(body, mask_unclosed=True).count('?') != 1:
        return None
    for pattern in _SCOPED_FALLBACKS:
        match = pattern.fullmatch(single[1])
        if not match:
            continue
        label = match['label']
        if closing := _LABEL_QUOTES.get(label[0]):
            if label[-1] != closing:
                return None
            label = label[1:-1]
        if (label == label.strip() and draft[:-1] == label and any(char.isalnum() for char in label)
                and not re.search(r'https?://', label, re.I)):
            return label
    return None


NUMERIC_CONCLUSION = re.compile(
    rf'(?:(?:(?:因此|所以)[，,]?\s*)?(?P<label>{LABEL}(?:新台幣金額|金額|數量)?){IS})?'
    rf'(?:(?P<left>(?:{NUMBER}\s*[-+*/×÷]\s*)+{NUMBER})\s*=\s*)?'
    rf'(?P<result>{NUMBER})\s*(?:元|片|個|件|顆)?')
TIME_ASSERTION = (r'(?:最早可行|最早|可行)(?:的)?(?:連續\s*[0-9]+\s*分鐘)?'
                  r'(?:開始及結束時間|時間區間|時間|時段)')
TIME_CONCLUSION = re.compile(rf'(?:(?:因此|所以)[，,]\s*)?(?:{TIME_ASSERTION}{IS})?{RANGE}')
PRIOR_TIME_CONCLUSION = re.compile(rf'(?:(?:因此|所以)[，,]\s*)?(?:{TIME_ASSERTION}|答案|結果){IS}{RANGE}')
MISSING_FOLLOWUP = re.compile(r'請(?:提供|補充)[^，,。；;！？：:\n]{1,50}(?:資訊|資料|文件|查詢|確認)')


def equation_agrees(match):
    """Check a bounded numeric claim as data. None means keep the original workings."""
    left = match['left']
    if len(left) > 256 or len(match['result']) > 64:
        return None
    first = re.match(NUMBER, left)
    value = Fraction(first[0].replace(',', ''))
    claimed = Fraction(match['result'].replace(',', ''))
    terms = re.findall(rf'([-+*/×÷])\s*({NUMBER})', left[first.end():])
    if len(terms) > 24:
        return None
    for op, literal in terms:
        operand = Fraction(literal.replace(',', ''))
        if op == '+':
            value += operand
        elif op == '-':
            value -= operand
        elif len(terms) != 1:
            return None
        elif op in '×*':
            return value * operand == claimed
        else:
            return operand != 0 and value == claimed * operand
    return value == claimed


def contract(question):
    # Match an imperative at a clause boundary, never a quoted example.
    for kind, pattern in CONTRACTS.items():
        match = re.search(SEP + pattern + END, question)
        if not match:
            continue
        prefix = question[:match.start()]
        if ('\x60\x60\x60' in prefix or re.search(r'[「『“"\x60]', QUOTES.sub('', prefix))
                or re.search(r'(?:請|並|並且|還要|需要)(?:再)?(?:解釋|說明|列出步驟)|(?:舉例|格式範例|範例指令)[：:]\s*$', QUOTES.sub('', prefix))):
            continue
        return kind, (match.group(1) if kind == 'missing' else None)
    return None


def questionable_charge(question, last):
    # Only compare explicitly listed addends, never every number in the model's prose.
    last = ' '.join(last.split())
    if (len(last) > 512 or not re.search(r'(?:^|[，。；！？])(?:已)?核准(?:的)?(?:金額|款項)(?:共|合計|總計)?多少'
                      r'|(?:^|[：:，。；！？])\s*(?:請)?只(?:加總|統計|計入)(?:狀態為)?已核准', question)
            or re.search(r'更正|改為|改成|匯率|換算|折抵|折扣|待核准(?:的)?(?:款項|金額).*多少|[「『“"`]', question)):
        return None
    money = r'(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?'
    term = rf'({money})\s*(?:元\s*)?(?:（[\u4e00-\u9fff]{{1,12}}）\s*)?'
    equation = re.fullmatch(rf'(?:[\u4e00-\u9fff]{{1,24}}[為是：:]\s*[:：]?\s*)?'
                            rf'({term}(?:\s*\+\s*{term}){{1,23}})\s*=\s*{money}\s*(?:元)?', last)
    if not equation:
        return None
    addends = {Fraction(re.fullmatch(term, t.strip())[1]) for t in equation[1].split('+')}
    amounts = list(re.finditer(rf'(?<![\d.,+-])({money})\s*(美元|元)', question))
    occurrences = Counter(Fraction(entry[1]) for entry in amounts)
    for entry in amounts:
        value = Fraction(entry[1])
        if value not in addends or occurrences[value] != 1:
            continue
        prefix = question[:entry.start()]
        suffix = question[entry.end():]
        pending = re.search(r'待(?:核准|審)(?:[\u4e00-\u9fff]{0,8})\s*$', prefix) or re.match(r'\s*待(?:核准|審)', suffix)
        foreign = entry[2] == '美元' and re.search(r'只(?:加總|統計|計入)(?:狀態為)?已核准的?新台幣', question)
        if pending or foreign:
            reason = '待核准' if pending else '美元'
            return '篩選核對：加總中有一筆 ' + entry[1] + '，與原題的' + reason + '項目金額相同。請確認該加項的來源，再採用總額。'
    return None


def numeric_delivery(question, draft, sentences):
    last = sentences[-1]
    charge_warning = questionable_charge(question, last)
    if charge_warning:
        return draft + '\n\n' + charge_warning
    # A direct "X 多少？" question may be answered with "X 48 元" instead
    # of a generic "答案是". Require the same field in the final assertion.
    field = re.search(SEP + r'(?:請問)?([\u4e00-\u9fff]{2,16}?)多少(?:錢)?[？?]\s*' +
                      CONTRACTS['number'] + END, question)
    assertion = last
    if field and last.startswith(field[1]):
        remainder = last[len(field[1]):].strip()
        if re.fullmatch(rf'{NUMBER}\s*(?:元|片|個|件|顆)?', remainder):
            assertion = '答案是' + remainder
    match = NUMERIC_CONCLUSION.fullmatch(assertion)
    if not match or (len(sentences) > 1 and not (match['label'] or match['left'])):
        return draft
    result = match['result'].replace(',', '')
    if match['left']:
        agrees = equation_agrees(match)
        if agrees is False:
            return draft + '\n\n數字核對：原答的等式「' + match['left'] + ' = ' + match['result'] + '」不成立，最後數字仍待確認。'
        if agrees is None:
            return draft
    # Keep an unsupported earlier equation or a different explicit conclusion
    # visible, even if a later explanation separates it from the last answer.
    for sentence in sentences[:-1]:
        prior = NUMERIC_CONCLUSION.fullmatch(sentence)
        if '=' in sentence or '＝' in sentence:
            if not prior or (prior['left'] and equation_agrees(prior) is not True):
                return draft
        if (prior and (prior['label'] or prior['left'])
                and prior['result'].replace(',', '') != result):
            return draft
    if '整數' in question[question.rfind('只'):] and '.' in result:
        return draft
    return result


def missing_field_matches(question, field):
    """Bind one complete asked-about field, rather than a name substring."""
    requests = []
    for clause in re.split(r'問[：:]|[。！？?；\n]', QUOTES.sub('■', question)):
        request = re.fullmatch(r'(?:請問)?([^，,：:]{2,70}?)(?:是多少|為多少|是什麼|多少|為何)', clause.strip())
        if request:
            requests.append(request[1].strip())
    if len(requests) != 1:
        return False
    target = requests[0]
    field = field.strip()
    if field == target:
        return True
    # An unqualified document ID can be answered with its category. Never
    # strip a category explicitly requested by the user (a table is not a quote).
    if re.match(r'[A-Za-z]+[0-9]', target):
        return re.sub(r'^(?:報價單|訂單|資料表)\s+(?=[A-Za-z]+[0-9])', '', field) == target
    return False


def extract(question, draft):
    wanted = contract(question)
    if not wanted or len(draft) > 16000 or re.search(r'\x60|\[D\d|【|https?://', draft):
        return draft
    kind, literal = wanted
    # Only the final declarative sentence may supply a value. Never pick the
    # last matching token from a paragraph of alternatives or from the question.
    sentences = [part.strip() for part in re.split(r'[。\n]', draft.strip()) if part.strip()]
    if not sentences:
        return draft
    last = sentences[-1]
    if kind == 'missing':
        if _instruction_text(question, mask_unclosed=True).count('?') > 1:
            return draft
        if re.fullmatch(re.escape(literal) + r'[。！]?', draft.strip()):
            return literal
        if len(sentences) == 2 and sentences[0] == literal:
            absence = re.fullmatch(r'([^，,。；！？]{2,70}?)(?:目前)?'
                                   r'(?:尚未提供|未提供|未登記|不詳|沒有資料|沒有紀錄)'
                                   r'(?:[，,]\s*(?:因此)?(?:無法得知|無法確定))?', last)
            if absence and missing_field_matches(question, absence[1]):
                return literal
        # Bind the missing field to the subject actually asked about. A generic
        # caveat, an unrelated unknown, or a later positive conclusion is insufficient.
        if len(sentences) == 2 and MISSING_FOLLOWUP.fullmatch(last):
            last = sentences[0]
        elif len(sentences) != 1:
            return draft
        m = re.fullmatch(r'(?:根據[^，。]{1,28}[，,]\s*)?(?:目前)?(?:無法得知|無法確定|無從得知)([^，。；！？]{2,70})', last)
        if m and missing_field_matches(question, m[1]):
            return literal
        return draft
    if re.search(r'取決於|假設|可能|估算|不一定|尚未確認|無法確認|候選|暫定|僅供參考|不保證|[?？]', draft):
        return draft
    if re.search(r'(?:不確定|不足|無法|可能|假設|如果|或者|若|假如|例如|排除|非|取消|[?？])', last):
        return draft
    if kind == 'number':
        return numeric_delivery(question, draft, sentences)
    if kind == 'time':
        m = TIME_CONCLUSION.fullmatch(last)
        if m and (len(sentences) == 1 or '可行' in last or '最早' in last):
            for sentence in sentences[:-1]:
                prior = PRIOR_TIME_CONCLUSION.fullmatch(sentence)
                if prior and prior.groups() != m.groups():
                    return draft
            return m[1] + '-' + m[2]
    if kind == 'choice':
        m = re.fullmatch(r'(?:答案[為是：:]\s*)?([A-Z])(?:[、.：:]?\s+([^\s，。！？]{1,24}))?', last)
        if m and re.search(r'(?<![A-Za-z])' + m[1] + r'(?![A-Za-z])', question):
            if m[2] is None or re.search(m[1] + r'[、.：:]?\s*' + re.escape(m[2]), question):
                return m[1]
    if kind == 'code':
        try:
            boxed = json.loads(draft)
        except ValueError:
            boxed = None
        if isinstance(boxed, list) and len(boxed) == 1:
            boxed = boxed[0]
        if isinstance(boxed, str) and re.fullmatch(CODE, boxed):
            return boxed
        return identifier_conclusion(question, sentences) or draft
    return draft


def identifier_conclusion(question, sentences):
    # Parse a complete short question, not a substring of a different object's name.
    requests = []
    for clause in re.split(r'[。！？?；，,\n]', QUOTES.sub('■', question)):
        clause = clause.strip()
        if re.match(ONLY, clause):
            continue
        match = re.fullmatch(rf'(?:請問|請查|我想查)?({IDENTIFIER_TARGET})(?:的)?'
                             rf'({IDENTIFIER_FIELD})(?:是多少|是什麼|為何|是哪個)?', clause)
        if match:
            requests.append(match.groups())
    if len(requests) != 1:
        return None
    target, field = requests[0]
    output_field = re.search(ONLY + r'(?:單一|一個)?(代碼|編號)' + END, question)
    if not output_field or output_field[1] != field:
        return None
    if re.search(r'或|和|與|跟|上述|這個|那個|不要|不是|更正|改查|改問', target):
        return None
    assertion = re.compile(rf'({IDENTIFIER_TARGET})(?:上的|的)?({IDENTIFIER_FIELD}){IS}({CODE})')
    last = assertion.fullmatch(sentences[-1])
    if not last or last.groups()[:2] != (target, field) or len(last[3]) > 128:
        return None
    # Do not conceal contradictory assertions about the same object and field.
    for sentence in sentences[:-1]:
        prior = assertion.fullmatch(sentence)
        if prior and prior.groups()[:2] == (target, field) and prior[3] != last[3]:
            return None
    return last[3]


# An echoed paragraph is sometimes part of the requested deliverable.
KEEP_QUESTION = re.compile(
    r'題目放在|問題放在|原題開頭|原題起頭|先讀題|說一遍|抄題|重述|複述|復述|覆述|重複|複誦|逐字|原文|照抄|引述|引用|回顯|回聲|翻譯|改寫|'
    r'(?:保留|列出|寫出|寫下|附上|貼出|包含|顯示|印出|抄寫|朗讀|先寫|先列|放上|先放|先印|唸出)'
    r'.{0,12}(?:題|問|原句|輸入)|repeat|restate|verbatim|echo|quote', re.I)


OMIT_QUESTION = re.compile(
    r'(?:^|(?<=[，,。；;！？\n]))\s*(?:請)?(?:不要|不用|不必|不需|無須|無需|別)(?:先|再)?'
    r'(?:重述|複述|復述|重複|抄寫)(?:我(?:的)?|本次|原)?(?:問題|題目|提問)\s*(?=[，,。；;！？\n]|$)')


def explanation_body(context, draft):
    question = context['current_request']
    if ('\n' in question or not re.search(r'請(?:解釋|說明)|為什麼|為何', question)
            or not draft.startswith(question)):
        return draft
    boundary = re.match(r'(?:\r?\n){2}', draft[len(question):])
    if not boundary:
        return draft
    body = draft[len(question) + boundary.end():]
    directives = QUOTES.sub('', question)
    remaining, omit = OMIT_QUESTION.subn('', directives)
    if not body.strip() or KEEP_QUESTION.search(remaining if omit else question):
        return draft
    if omit:
        return body
    for key in ('recent_conversation', 'earlier_user_excerpts', 'memories', 'session_brief',
                'answer_feedback', 'earlier_user_corrections', 'companion_profile'):
        if KEEP_QUESTION.search(json.dumps(context.get(key), ensure_ascii=False)):
            return draft
    return body


class DailyLocalEngine:
    """Wrap ordinary local chat; other model jobs and raw evaluations pass through."""
    def __init__(self, engine):
        self.engine = engine
        self.display_name = engine.display_name

    def describe(self):
        return self.engine.describe()

    def answer_raw(self, prompt, cancel=None):
        return self.engine.answer(prompt, cancel)

    def answer(self, prompt, cancel=None):
        return self.answer_with_delivery(prompt, cancel)[0]

    def answer_with_delivery(self, prompt, cancel=None):
        from assistant.response_delivery import make_receipt
        native_delivery = getattr(self.engine, 'answer_with_delivery', None)
        if callable(native_delivery):
            draft, receipt = native_delivery(prompt, cancel)
            if receipt is not None:
                return draft, receipt
        else:
            draft = self.engine.answer(prompt, cancel)
        delivered = self.deliver(prompt, draft)
        return delivered, make_receipt(draft, delivered)

    def deliver(self, prompt, draft):
        marker = '\n\n以下 JSON 是使用者資料與當前問題：\n'
        _, found, payload = prompt.rpartition(marker)
        if not found:
            return draft
        try:
            ctx = json.loads(payload)
        except ValueError:
            return draft
        if (not isinstance(ctx, dict) or not isinstance(ctx.get('current_request'), str)
                or ctx.get('calculation_mode') or not isinstance(ctx.get('documents'), dict) or ctx['documents'].get('excerpts')
                or ctx.get('engine_selection') != self.describe() or render_prompt(ctx) != prompt):
            return draft
        question = ctx['current_request']
        from assistant.prompt_scope import transcription_focus
        if transcription_focus(ctx):
            return draft
        if ctx.get('context_scope') == CURRENT_MESSAGE_ONLY and not ctx.get('image_inputs'):
            if (label := _scoped_missing_label(question, draft)) is not None:
                return label
        concise = missing_literal(question, draft) or supplied_code(ctx, draft) or daily_dialogue.short_label(ctx, draft)
        if concise is not None:
            return concise
        delivered = extract(question, omit_unrelated_identifier(ctx, explanation_body(ctx, draft)))
        return daily_stock.concise(ctx, delivered) or delivered
