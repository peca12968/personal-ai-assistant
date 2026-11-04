"""Conservative formatting of explicit, text-only local chat conclusions."""
import json
import re

from assistant.engine import render_prompt

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
QUOTES = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|"[^"]*"|\x60[^\x60]*\x60', re.S)


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
        # Bind the missing field to the subject actually asked about. A generic
        # caveat, an unrelated unknown, or a later positive conclusion is insufficient.
        if re.fullmatch(r'請(?:提供|補充)[^。！？]{1,50}(?:資訊|資料|文件|查詢|確認)', last) and len(sentences) > 1:
            last = sentences[-2]
        m = re.fullmatch(r'(?:根據[^，。]{1,28}[，,]\s*)?(?:目前)?(?:無法得知|無法確定|無從得知)([^，。；！？]{2,70})', last)
        if m:
            field = re.sub(r'^(?:報價單|訂單|資料表)\s+(?=[A-Za-z]+[0-9])', '', m[1].strip())
            request = re.split(r'(?:問[：:]|[。！？])', QUOTES.sub('', question))
            if any(field in clause and re.search(r'多少|為何|是什麼|為多少', clause) for clause in request):
                return literal
        return draft
    if re.search(r'取決於|假設|可能|估算|不一定|尚未確認|無法確認|候選|暫定|僅供參考|不保證|[?？]', draft):
        return draft
    if re.search(r'(?:不確定|不足|無法|可能|假設|如果|或者|若|假如|例如|排除|非|取消|[?？])', last):
        return draft
    if kind == 'number':
        # Optional workings must be a numeric expression, not a list of amounts.
        match = re.fullmatch(
            rf'(?:(?:(?:因此|所以)[，,]?\s*)?(?:{LABEL}(?:新台幣金額|金額|數量)?){IS})?'
            rf'(?:(?:{NUMBER}\s*[-+*/×÷]\s*)+{NUMBER}\s*=\s*)?({NUMBER})\s*(?:元|片|個|件|顆)?', last)
        if len(sentences) > 1 and not re.search(LABEL + '|=', last):
            return draft
        if match and ('整數' not in question[question.rfind('只'):] or '.' not in match[1]):
            return match[1].replace(',', '')
    if kind == 'time':
        m = re.fullmatch(rf'(?:(?:因此|所以)[，,]\s*)?(?:(?:最早可行|最早|可行)(?:的)?(?:連續\s*[0-9]+\s*分鐘)?(?:開始及結束時間|時間區間|時間|時段){IS})?{RANGE}', last)
        if m and (len(sentences) == 1 or '可行' in last or '最早' in last):
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
        m = re.fullmatch(rf'([^。！？；，,]{{1,36}}?)(?:上)?的(?:代碼|編號){IS}({CODE})', last)
        clauses = re.split(r'[。！？；，,\n]', QUOTES.sub('', question))
        targets = [c for c in clauses if re.search(r'請問|是什麼|為何|哪個|多少', c)]
        if m and targets and m[1].strip() in targets[-1]:
            return m[2]
    return draft


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

    def answer(self, prompt, cancel=None):
        draft = self.engine.answer(prompt, cancel)
        marker = '\n\n以下 JSON 是使用者資料與當前問題：\n'
        _, found, payload = prompt.partition(marker)
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
        return extract(ctx['current_request'], explanation_body(ctx, draft))
