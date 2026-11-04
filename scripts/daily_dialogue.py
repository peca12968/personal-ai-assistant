"""Daily text-only dialogue support for explicitly revised constraints and short labels."""
import re


def revision_focus(context):
    question = context['current_request']
    history = context.get('recent_conversation', [])
    if (not any(r['role'] == 'user' for r in history)
            or context.get('documents', {}).get('catalog') or len(question) > 800
            or re.search(r'[「『“"`]|翻譯|改寫|舉例', question)):
        return None
    statements = re.split(r'[。！？?\n]', question)
    first = statements[0]
    if not (re.match(r'^(?:更正|修正|更新)[，,：:]', first)
            or re.search(r'^(?:[\u4e00-\u9fff]{1,12})(?:要求|限制|條件)(?:已)?取消[，,]', first)):
        return None
    # Include only complete, user-authored statements before the question or output instruction.
    facts = []
    for statement in statements[:-1]:
        if not statement or re.search(r'哪|嗎|幾|怎麼|如何|只回|只給|請問', statement):
            break
        facts.append(statement)
    if not facts:
        return None
    return dict(本輪使用者更新=facts, 處理方式='先依本輪更新替換相衝突的舊條件，保留仍有效的限制。逐一排除違反現行條件的選項，再按使用者指定的格式回答。')


def short_label(context, draft):
    question = context['current_request']
    if (not isinstance(draft, str) or len(draft) > 2500 or len(question) > 1200
            or re.search(r'解釋|說明理由|列出步驟|翻譯|改寫|重述|複述|照抄|舉例|[\n"`]', question)):
        return None
    body = draft.strip()
    echoed, boundary, last = body.rpartition('\n')
    if boundary and len(echoed.strip()) >= 10 and question.endswith(echoed.strip()):
        body = last.strip()
    token = r'([\u4e00-\u9fffA-Za-z0-9]{1,16})'
    value = re.fullmatch(token + r'[。.!！]?', body)
    conclusion = False
    if not value and not re.search(r'如果|假如|也許|可能|不確定|取決於|僅供參考|[【\[?？]', body):
        last = re.split(r'[。\n]', body.rstrip('。！'))[-1].strip()
        value = re.fullmatch(r'(?:因此|所以)[，,]\s*(?:目前|現在)?(?:應走|應選擇|應選|應使用|應前往|答案是)' + token, last)
        conclusion = True
    if not value:
        return None
    label = value[1]
    if conclusion and re.search(r'或者|或是|也可|以及|[或與及和]', label):
        return None
    binary = re.search(r'[。？！?]\s*只(?:回|給|回答|輸出)「([^「」]{1,16})」或「([^「」]{1,16})」[。！]?$', question)
    if binary and not conclusion and label in binary.groups():
        return label
    if not re.search(r'[。？！?]\s*只(?:回|給|回答|輸出)[\u4e00-\u9fff]{0,12}名稱[。！]?$', question):
        return None
    if label in question or any(r['role'] == 'user' and not r['truncated'] and label in r['content']
                                for r in context.get('recent_conversation', [])):
        return label
    return None
