"""Bounded, attributable conversation context; no generated facts or hidden summaries."""
import re
import unicodedata

from .retrieval import terms

TOPIC_CHANGE = re.compile(r"換個話題|換一個話題|另外一件事|新問題|另起話題|\b(?:new topic|unrelated question|change the subject)\b", re.I)
FOLLOWUP = re.compile(
    r"^(?:那(?:接|它|這|要|怎|第)|接下來|接著|繼續|第二步|第三步|它|這個|這樣|上述|剛才|之前|改成|改用|換成|再給|再說|"
    r"what about\b|how about\b|then\b|continue\b|next\b|and\b|it\b|that\b|make it\b|change it\b|same\b)", re.I)
IMPORTANT = re.compile(r"目標|限制|必須|不能|不要|更正|改為|改用|改成|決定|取消|預算|期限|\b(?:goal|must|never|instead|decision|budget|deadline)\b", re.I)

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


def is_followup(question):
    text = unicodedata.normalize("NFKC", question).strip()
    text = POLITE.sub("", text, count=1)
    return bool(FOLLOWUP.search(text) or SHORT_FOLLOWUP.fullmatch(text))


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
    if TOPIC_CHANGE.search(question) or not is_followup(question):
        return None
    earlier = []
    for row in reversed(history):
        if row["role"] != "user":
            continue
        text = row["content"]
        earlier.append(clip(text, 1000)[0])
        if TOPIC_CHANGE.search(text) or not is_followup(text) or len(earlier) == 4:
            break
    return "\n".join(reversed(earlier)) if earlier else None


def select_excerpts(question, older, limit=6):
    if not older or limit <= 0 or TOPIC_CHANGE.search(question):
        return []
    query = terms(question)
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


def make_context(question, history, memories, brief=None, excerpts=None):
    recent = history[-16:]
    per_message = min(6000, 24000 // max(1, len(recent)))
    history_context = []
    for row in recent:
        content, truncated = clip(row["content"], per_message)
        history_context.append({"message_id": row.get("id"), "role": row["role"],
                                "content": content, "truncated": truncated})
    memory_context = []
    for row in memories[:8]:
        content, truncated = clip(row["content"], 1500)
        memory_context.append({"id": row["id"], "kind": row["kind"], "content": content,
                               "updated_at": row.get("updated_at"), "expires_on": row.get("expires_on"),
                               "truncated": truncated})
    return {"session_brief": brief or {}, "earlier_user_excerpts": excerpts or [],
            "memories": memory_context, "recent_conversation": history_context,
            "current_request": question}
