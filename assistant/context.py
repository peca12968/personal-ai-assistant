"""Bounded, attributable conversation context; no generated facts or hidden summaries."""
import re
import unicodedata

from .retrieval import terms

TOPIC_CHANGE = re.compile(r"換個話題|換一個話題|另外一件事|新問題|另起話題|\b(?:new topic|unrelated question|change the subject)\b", re.I)
FOLLOWUP = re.compile(
    r"^(?:那(?:接|它|這|要|怎|第)|接下來|接著|繼續|第二步|第三步|它|這個|這樣|上述|剛才|之前|改成|改用|換成|再給|再說|"
    r"what about\b|how about\b|then\b|continue\b|next\b|and\b|it\b|that\b|make it\b|change it\b|same\b)", re.I)
IMPORTANT = re.compile(r"目標|限制|必須|不能|不要|更正|改為|改用|改成|決定|取消|預算|期限|\b(?:goal|must|never|instead|decision|budget|deadline)\b", re.I)

MAX_EARLIER_CORRECTIONS = 6
CORRECTION_PREFIX = re.compile(
    r"^(?:(?:請|我|讓我)?(?:更正|修正|補充更正)(?:一下)?\s*[:,。]|"
    r"(?:(?:a\s+)?(?:quick|small)\s+)?correction\s*[:,]|"
    r"(?:let\s+me\s+correct\s+(?:that|myself)|I\s+need\s+to\s+correct\s+that)\s*[:,]|"
    r"不是(?:這個|那個|這樣)\s*[,。]\s*(?:我是指|我要的是)|"
    r"我(?:剛才|剛剛|先前)說錯了\s*[,。:])", re.I)

# Explicit direction changes often lack a formal "correction:" label. Retain
# their raw wording, including qualifiers, without deciding which work is
# cancelled, accepted or authorized. Only short leading imperative frames
# qualify; quoted/reported examples and questions must not gain this priority.
DIRECTION_PREFIX = re.compile(
    r"^(?:(?:請|麻煩(?:你)?)\s*)?(?:"
    r"(?:先)?(?:不用|不必|不要|別)(?:再)?\s*"
    r"(?:做|製作|開發|處理|進行|執行|實作|開啟|啟動)\s*"
    r"|(?:改用|改成|改為|改回|換成|換用)\s*"
    r"|取消(?:原本|之前|先前|原先)的?\s*)"
    r"|^(?:please\s+)?(?:"
    r"instead\s*[,;:]?\s+(?:use|make|keep|switch\s+to|focus\s+on)\s+"
    r"|(?P<complete>(?:cancel|drop|skip|stop)\s+(?:the\s+)?(?:previous|original|earlier)\s+"
    r"(?:task|plan|request|work)\b)\s*|switch\s+to\s+)", re.I)
DIRECTION_MENTION = re.compile(
    r"這句|这句|這幾個字|这几个字|的意思|的說法|的说法|只是(?:範例|范例|例子|引文)|"
    r"\b(?:phrase|quotation|quoted|example|means?|meaning)\b", re.I)


def is_direction_change(prefix):
    match = DIRECTION_PREFIX.match(prefix)
    if not match or DIRECTION_MENTION.search(prefix):
        return False
    # Reject first-sentence questions (including those after an initial comma),
    # while retaining later qualifiers as data in the original excerpt.
    sentence = re.split(r'[。.!;\n]', prefix, maxsplit=1)[0]
    if '?' in sentence or re.search(r'(?:嗎|吗|呢)\s*$', sentence):
        return False
    return bool(match.group('complete') or prefix[match.end():].strip(' ,;:。.!'))


def is_explicit_correction(text):
    """A bounded prefix hint for retaining raw user text, not a semantic verdict."""
    prefix = unicodedata.normalize('NFKC', text[:160]).lstrip()
    return bool(CORRECTION_PREFIX.search(prefix)) or is_direction_change(prefix)


def correction_context(question, rows):
    # Keep this separate from relevance-ranked excerpts so unrelated old rows
    # cannot displace an explicit correction. Preserve uncertainty and ordering.
    if context_reset(question):
        return {'excerpts': [], 'selection_limited': False}
    chosen = sorted(rows, key=lambda row: row['id'])[-MAX_EARLIER_CORRECTIONS:]
    excerpts = []
    for row in chosen:
        content, truncated = clip(row['content'], 1000)
        excerpts.append({'message_id': row['id'], 'role': 'user',
                         'content': content, 'truncated': truncated})
    return {'excerpts': excerpts, 'selection_limited': len(rows) > MAX_EARLIER_CORRECTIONS}

# Only complete, bounded short requests qualify here. An ordinal inside a new
# subject (e.g. 第二個質數) is not evidence that the old topic should be retrieved.
POLITE = re.compile(r"^(?:請問|請|麻煩(?:你)?|please\b)\s*", re.I)
SHORT_FOLLOWUP = re.compile(
    r"(?:"
    r"(?:選(?:擇)?\s*)?第\s*(?:[一二三四五六七八九十百]+|[0-9]{1,3})\s*(?:個|項|種)"
    r"(?:呢|怎麼做|如何做|怎麼選)?"
    r"|(?:把|將)(?:上面|上述|剛才|前面|剛剛)(?:的)?(?:回答|內容|說明)?"
    r"(?:改成|整理成|轉成)(?:表格|清單|條列)"
    r"|(?:用|以)(?:表格|條列|清單)(?:整理|呈現|回答|說明)"
    r")[。.!！?？\s]*", re.I)

# Modal wording alone does not imply continuity. Require an explicit reference
# and a complete transformation request, so quoted phrases and new objects do
# not become retrieval anchors merely because they contain formatting verbs.
REFERENCE_FOLLOWUP = re.compile(
    r"(?:"
    r"(?:可以|能不能|可不可以|能否|是否可以|能)?\s*"
    r"(?:把|將)(?:上面|上述|剛才|前面|剛剛)(?:的)?(?:回答|內容|說明)?"
    r"(?:改成|整理成|轉成)(?:表格|清單|條列)(?:嗎|呢)?"
    r"|(?:(?:can|could|would)\s+you\s+)?"
    r"(?:put|turn|convert|format)\s+(?:the\s+)?"
    r"(?:previous|preceding|above|last)\s+(?:answer|response|explanation|content)\s+"
    r"(?:in(?:to)?|as)\s+(?:a\s+)?(?:table|list|bullet\s+points)"
    r")[。.!！?？\s]*", re.I)

# Compose complete utterances from finite grammatical slots. These fragments
# are only used with fullmatch; they do not broaden the legacy prefix hints.
# Selection punctuation excludes questions, and the only permitted trailing
# clause requests continuation. New subjects, reports and uncertainty therefore
# cannot be consumed as arbitrary suffixes.
_ZH_NUMBER = r"(?:[一二三四五六七八九十百]{1,6}|[0-9]{1,3})"
_ZH_CHOICE = (
    r"第\s*" + _ZH_NUMBER + r"\s*"
    r"(?:(?:個|項|種)\s*(?:選項|方案)?|選項|方案)"
)
_ZH_SELECT_VERB = r"(?:選擇|選定|選|採用|使用|用)"
_ZH_SELECT_TENSE = r"(?:(?:已經|已)\s*)?(?:決定\s*)?"
_ZH_SELECT_SUBJECT = r"(?:我\s*(?:就\s*)?|那\s*就\s*(?:我\s*)?)"
_ZH_POLITE = r"(?:(?:請\s*(?:幫我\s*)?|麻煩(?:你)?\s*(?:幫我\s*)?|幫我\s*))?"
_ZH_CONTINUE = r"(?:繼續|接著)(?:說明|說|解釋|介紹)?"
_ZH_SELECT_END = (
    r"\s*(?:了)?\s*(?:吧)?\s*"
    r"(?:[,，。;；]\s*(?:請\s*|麻煩(?:你)?\s*)?" + _ZH_CONTINUE + r")?"
    r"[。.!！\s]*"
)
_EN_CARDINAL = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|[0-9]{1,3})"
_EN_ORDINAL = (
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"[0-9]{1,3}(?:st|nd|rd|th))"
)
_EN_CHOICE_NOUN = r"(?:option|choice|alternative|plan)"
_EN_CHOICE = (
    r"(?:the\s+)?(?:" + _EN_ORDINAL + r"\s+" + _EN_CHOICE_NOUN
    + r"|" + _EN_CHOICE_NOUN + r"\s+" + _EN_CARDINAL + r")"
)
_EN_SELECT_BASE = r"(?:choose|select|take|use|adopt|go\s+with)"
_EN_SELECT_PAST = r"(?:chose|selected|took|used|adopted|went\s+with)"
_EN_SELECT_PARTICIPLE = r"(?:chosen|selected|taken|used|adopted|gone\s+with)"
_EN_SELECT_SUBJECT_VERB = (
    r"(?:i\s+(?:" + _EN_SELECT_BASE + r"|" + _EN_SELECT_PAST
    + r"|(?:have\s+)?(?:already\s+)?decided\s+to\s+" + _EN_SELECT_BASE + r")"
    r"|i(?:\s+will|'ll)\s+" + _EN_SELECT_BASE
    + r"|i(?:\s+would|'d)\s+like(?:\s+to\s+" + _EN_SELECT_BASE + r")?"
    r"|i(?:\s+have|'ve)\s+(?:already\s+)?(?:" + _EN_SELECT_PARTICIPLE
    + r"|decided\s+to\s+" + _EN_SELECT_BASE + r"))\s+"
)
_EN_POLITE = r"(?:please\s+)?"
_EN_CONTINUE = r"(?:continue|go\s+on|proceed)(?:\s+with\s+the\s+explanation)?"
_EN_SELECT_END = (
    r"(?:\s*,\s*(?:please|(?:please\s+)?" + _EN_CONTINUE + r"))?"
    r"(?:\s+please)?[.!\s]*"
)
COMPLETE_SELECTION_FOLLOWUP = re.compile(
    r"(?:" + _ZH_POLITE + _ZH_SELECT_SUBJECT + _ZH_SELECT_TENSE
    + _ZH_SELECT_VERB + r"\s*(?:了\s*)?" + _ZH_CHOICE + _ZH_SELECT_END
    + r"|" + _EN_POLITE + _EN_SELECT_SUBJECT_VERB + _EN_CHOICE + _EN_SELECT_END + r")", re.I)

# Formatting needs an explicit preceding object even with courteous wording.
# Question punctuation is allowed here because the complete request itself is
# a transformation, unlike a question about whether to choose an option.
_ZH_REFERENCE_TRANSFORM = (
    r"(?:把|將)\s*(?:上面|上述|剛才|前面|剛剛)(?:的)?\s*"
    r"(?:回答|內容|說明)?\s*(?:改成|整理成|轉成)\s*(?:表格|清單|條列)"
)
_EN_REFERENCE_TRANSFORM = (
    r"(?:put|turn|convert|format)\s+(?:the\s+)?"
    r"(?:previous|preceding|above|last)\s+(?:answer|response|explanation|content)\s+"
    r"(?:in(?:to)?|as)\s+(?:a\s+)?(?:table|list|bullet\s+points)"
)
COMPLETE_FORMAT_FOLLOWUP = re.compile(
    r"(?:" + _ZH_POLITE
    + r"(?:(?:可以|能不能|可不可以|能否|是否可以|能)\s*(?:幫我\s*)?)?"
    + _ZH_REFERENCE_TRANSFORM + r"(?:嗎|呢)?[。.!！?？\s]*"
    r"|" + _EN_POLITE + r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?"
    + _EN_REFERENCE_TRANSFORM + r"(?:\s*,?\s+please)?[.!?\s]*)", re.I)

NO_PREVIOUS = re.compile(
    r"(?:不要|不用|不必|別)(?:再)?\s*(?:沿用|使用|參考|接續|延續)\s*"
    r"(?:前文|上文|之前|先前|剛才|上述|前面)"
    r"|\b(?:do not|don't|without)\s+(?:use|using|reuse|reusing|refer to|referring to)\s+"
    r"(?:the\s+)?(?:previous|prior|earlier|above)\s+(?:context|conversation|messages?|answers?)\b", re.I)


def _instruction_text(question):
    """Mask quoted/code spans with spaces, keeping offsets and line breaks."""
    text = unicodedata.normalize('NFKC', question)
    chars = list(text)

    def hide(start, end):
        for index in range(start, end):
            if chars[index] not in '\r\n':
                chars[index] = ' '

    # Fences and blockquotes are recognized before inline markup and quotes.
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if fence:
            hide(offset, offset + len(line))
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) +
                            r'{' + str(fence[1]) + r',}[ \t]*(?:\r?\n)?', line):
                fence = None
        elif re.match(r' {0,3}>', line):
            hide(offset, offset + len(line))
        else:
            opening = re.match(r' {0,3}(`{3,}|~{3,})', line)
            if opening:
                run = opening.group(1)
                fence = (run[0], len(run))
                hide(offset, offset + len(line))
        offset += len(line)

    visible = ''.join(chars)
    index = 0
    while index < len(visible):
        if visible[index] != '`':
            index += 1
            continue
        opening = re.match(r'`+', visible[index:]).group()
        end = index + len(opening)
        closing = re.search(r'(?<!`)`{' + str(len(opening)) + r'}(?!`)', visible[end:])
        if closing:
            end += closing.end()
            hide(index, end)
        index = end

    # Directional openers are unambiguous even beside Chinese text. A right
    # single quote can also be an English apostrophe; classify that use by
    # Latin word boundaries, never by the broad Unicode isalnum predicate.
    pairs = {'「': '」', '『': '』', '“': '”', '‘': '’', '"': '"', "'": "'"}
    stack = []
    visible = ''.join(chars)

    def latin_letter(char):
        return char.isalpha() and 'LATIN' in unicodedata.name(char, '')

    def apostrophe(index, closing):
        if not index or not latin_letter(visible[index - 1]):
            return False
        following = index + 1
        if following < len(visible) and latin_letter(visible[following]):
            # Without a matching opener, retain legacy word punctuation.
            if not closing:
                return True
            # Inspect complete adjacent Latin components, with a fixed bound.
            # A capitalized following word alone is not evidence of a name:
            # a quotation may close immediately before a new sentence.
            start = index - 1
            while start > 0 and index - start < 64 and latin_letter(visible[start - 1]):
                start -= 1
            end = following
            while end < len(visible) and end - following < 64 and latin_letter(visible[end]):
                end += 1
            if ((start > 0 and latin_letter(visible[start - 1]))
                    or (end < len(visible) and latin_letter(visible[end]))):
                return False
            stem = visible[start:index]
            suffix = visible[following:end]
            # Contractions and singular possessives have short grammatical
            # suffixes. Name elisions require a short prefix as well as a
            # capitalized, otherwise lowercase second component (O'Connor,
            # d'Artagnan); arbitrary capitalized continuation words do not.
            if suffix.lower() in ('s', 't', 're', 've', 'll', 'd', 'm'):
                return True
            return (1 <= len(stem) <= 2 and len(suffix) >= 2
                    and suffix[0].isupper() and suffix[1:].islower())
        # A trailing possessive apostrophe must not open a quotation. When a
        # matching quotation is active, its delimiter takes precedence.
        return not closing

    for index, char in enumerate(visible):
        closing = bool(stack and char == stack[-1][0])
        if char in "'’" and apostrophe(index, closing):
            continue
        if closing:
            _, start = stack.pop()
            hide(start, index + 1)
        elif char in pairs:
            stack.append((pairs[char], index))
    return ''.join(chars)


# These are deliberately bounded textual hints, not general semantic parsing.
# A local negation or reported/mentioned phrase is not a reset instruction.
_RESET_NEGATION = re.compile(
    r"(?:不要|不用|不必|別|不是|並非|并非|沒有|没有|不想|無須|无需|避免)"
    r"[^。.!！?？;；\n]{0,16}$|"
    r"\b(?:do\s+not|don't|don’t|not|never|no\s+need\s+to|avoid)"
    r"(?:\s+[\w’'-]+){0,5}\s*$", re.I)
_RESET_REPORT = re.compile(
    r"(?:他說|她說|你說|我說|提到|提及|寫著|写着|寫了|標題|标题|字樣|字样|"
    r"詞語|词语|這句|这句|意思是|例如|比如|所謂|所谓)"
    r"[^。.!！?？;；\n]{0,24}$|"
    r"\b(?:said|says|wrote|written|quoted|mention(?:ed|s)?|phrase|word|"
    r"heading|title|label|example|means?)\b[^.!?;\n]{0,48}$", re.I)
_RESET_MENTION_END = re.compile(
    r"^\s*(?:這(?:句|個詞)|这(?:句|个词)|這幾個字|这几个字|的(?:意思|字樣|字样|說法|说法)|"
    r"(?:is|was)\s+(?:a\s+|the\s+)?(?:phrase|heading|title|label|example)\b)", re.I)
_NOMINAL_RESET_PREFIX = re.compile(
    r"\s*(?:(?:好|好的|那|現在|现在|接下來|接下来|請|请|我有|問個|问个|"
    r"另外問個|另外问个)\s*|"
    r"(?:ok(?:ay)?|then|now|please|a|an|start|begin|let's|let’s|"
    r"i\s+have|i\s+have\s+an?)\s+)*", re.I)


def context_reset(question):
    """Recognize explicit unquoted reset requests using bounded local hints.

    This intentionally favors retaining context for negated or reported wording.
    It does not attempt to infer arbitrary intent or rewrite the user's text.
    """
    text = _instruction_text(question)
    # Normalize contraction punctuation only after directional quote masking.
    text = text.replace('’', "'")
    for pattern in (NO_PREVIOUS, TOPIC_CHANGE):
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 100):match.start()]
            after = text[match.end():match.end() + 64]
            if (_RESET_NEGATION.search(before) or _RESET_REPORT.search(before)
                    or _RESET_MENTION_END.search(after)):
                continue
            if pattern is NO_PREVIOUS:
                # Explicit prohibitions can be embedded in an answer request,
                # e.g. 'Then answer without using the previous context.'
                return True
            phrase = match.group().lower()
            if phrase in ('新問題', '另外一件事', 'new topic', 'unrelated question'):
                clause = re.split(r'[。.!！?？,，;；:：\n]', before)[-1]
                if not _NOMINAL_RESET_PREFIX.fullmatch(clause):
                    continue
                # Nominal labels require a delimiter, an instruction, or end.
                if after.strip() and not re.match(r'^[\s]*[,，:：。.!！?？;；\n]', after):
                    continue
            return True
    return False


def is_followup(question):
    text = unicodedata.normalize("NFKC", question).strip()
    if context_reset(question):
        return False
    # Match the whole original utterance, including any quotation/code marks.
    # Masking first could silently discard a contradictory or reported clause.
    # Curly apostrophes are equivalent only within these new complete rules.
    # Collapse whitespace for matching only. Adjacent optional slots must not
    # backtrack over thousands of equivalent whitespace partitions. Quotes,
    # code marks and qualifiers stay present; stored/current text stays raw.
    complete_text = re.sub(r'\s+', ' ', text.replace('’', "'"))
    if (COMPLETE_SELECTION_FOLLOWUP.fullmatch(complete_text)
            or COMPLETE_FORMAT_FOLLOWUP.fullmatch(complete_text)):
        return True
    text = POLITE.sub("", text, count=1)
    return bool(FOLLOWUP.search(text) or SHORT_FOLLOWUP.fullmatch(text)
                or REFERENCE_FOLLOWUP.fullmatch(text))


def clip(text, limit):
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    marker = "\n[…中段省略…]\n"
    if limit <= len(marker):
        return text[:limit], True
    available = max(0, limit - len(marker))
    first = available // 2
    return text[:first] + marker + text[-(available - first):], True


def followup_query(question, history):
    """Use only preceding user turns, and only for recognizable continuations."""
    if context_reset(question) or not is_followup(question):
        return None
    earlier = []
    for row in reversed(history):
        if row["role"] != "user":
            continue
        text = row["content"]
        earlier.append(clip(text, 1000)[0])
        if context_reset(text) or not is_followup(text) or len(earlier) == 4:
            break
    return "\n".join(reversed(earlier)) if earlier else None


def select_excerpts(question, older, limit=6, *, followup=None):
    if not older or limit <= 0 or context_reset(question):
        return []
    query = terms(question)
    # A pronoun may name the field while its preceding user turn names the object.
    # Use those words for retrieval only. Keep the source rows and request verbatim.
    if followup and not context_reset(followup):
        query.update(terms(followup))
    ranked = []
    for index, row in enumerate(older):
        score = len(query & terms(row["content"])) + (3 if IMPORTANT.search(row["content"]) else 0)
        if score or index == 0:
            ranked.append((score, row["id"], row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked = [item[2] for item in ranked[:limit]]
    # Keep the opening user request as an anchor for the original task.
    if older[0] not in picked:
        picked = picked[:max(0, limit - 1)] + [older[0]]
    excerpts = []
    for row in sorted(picked, key=lambda row: row["id"]):
        content, truncated = clip(row["content"], 1000)
        excerpts.append({"message_id": row["id"], "role": "user", "content": content,
                         "truncated": truncated})
    return excerpts


def _history_budgets(recent):
    """Share spare character capacity without changing message selection.

    Short messages consume only their actual length. Redistribute the rest
    equally among unsatisfied messages, capped at 6000 each and 24000 total.
    Any indivisible remainder goes to the newer messages, in stable order.
    Budgets include clip()'s omission marker; these are code points, not tokens.
    """
    wanted = [min(6000, len(row['content'])) for row in recent]
    budgets = [0] * len(recent)
    pending = list(range(len(recent)))
    remaining = 24000
    while pending:
        share, extra = divmod(remaining, len(pending))
        satisfied = [index for index in pending if wanted[index] <= share]
        if satisfied:
            for index in satisfied:
                budgets[index] = wanted[index]
                remaining -= wanted[index]
            pending = [index for index in pending if index not in satisfied]
        else:
            for offset, index in enumerate(reversed(pending)):
                budgets[index] = share + (offset < extra)
            break
    return budgets


def make_context(question, history, memories, brief=None, excerpts=None):
    recent = history[-16:]
    budgets = _history_budgets(recent)
    history_context = []
    for row, budget in zip(recent, budgets):
        content, truncated = clip(row["content"], budget)
        history_context.append({"message_id": row.get("id"), "role": row["role"],
                                "content": content, "truncated": truncated})
    memory_context = []
    for row in memories[:8]:
        content, truncated = clip(row["content"], 1500)
        memory_context.append({"id": row["id"], "kind": row["kind"], "content": content,
                               "updated_at": row.get("updated_at"), "expires_on": row.get("expires_on"),
                               "truncated": truncated})
        if 'retrieval_aliases' in row:
            memory_context[-1]['retrieval_aliases'] = row['retrieval_aliases']
    return {"session_brief": brief or {}, "earlier_user_excerpts": excerpts or [],
            "memories": memory_context, "recent_conversation": history_context,
            "current_request": question}
