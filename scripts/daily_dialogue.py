"""Daily text-only dialogue support for explicitly revised constraints and short labels."""
import re


TOPIC_RETURN = re.compile(r'^(?:回到(?:剛才|之前|先前)?)(?:的)?([\u4e00-\u9fff]{2,12}?)'
                          r'(?:的事情|的話題|的問題|的事)[。，,]\s*(.+)$')
SWITCH = re.compile(r'^(?:換個話題|換個問題|換個題目|說別的事|另外一件事)[。，,：:]')


def resumed_topic(context):
    question = context['current_request']
    request = TOPIC_RETURN.fullmatch(question)
    if not request or len(question) > 800 or context.get('documents', {}).get('catalog'):
        return None
    topic, current = request.groups()
    history = context.get('recent_conversation', [])
    if not history or history[-1]['role'] != 'assistant':
        return None
    if (context.get('conversation_tail') or {}).get('message_id') != history[-1]['message_id']:
        return None
    # A named thread must match one completed stretch of user turns. Never retrieve model guesses.
    groups = [[]]
    for row in history:
        if row['role'] != 'user':
            continue
        if row['truncated']:
            return None
        content = row['content']
        if SWITCH.match(content) or TOPIC_RETURN.match(content):
            groups.append([])
        groups[-1].append(row)
    matches = [group for group in groups if any(topic in r['content'] for r in group)]
    if len(matches) != 1 or matches[0] is groups[-1]:
        return None
    source = matches[0]
    if sum(len(r['content']) for r in source) > 1600:
        return None
    return dict(話題=topic, 先前使用者原話=[dict(訊息=r['message_id'], 原文=r['content']) for r in source],
                本輪接續=current, 核對方式='依時間順序套用更正，再以本輪描述的人物、持有物和限制逐一核對。只有明說共用的物件或仍有效的環境條件才沿用。只回答本輪的問題。')


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


def feasibility_evidence(context):
    q = context['current_request']
    if (context.get('documents', {}).get('catalog') or len(q) > 1200
            or re.search(r'假設|舉例|翻譯|改寫|聽說|據說|如果|[「『“].+[」』”].*[？?]', q)
            or not re.search(r'能否[^。？?]{0,30}(?:運|送|搬|通過)', q)):
        return None
    # Keep the whole statement, including qualifications about operating status.
    statements = re.split(r'[。！？?\n]', q)
    ability, unknown = [], []
    for fact in statements[:-1]:
        if '能否' in fact or '只回' in fact:
            continue
        clauses = re.split(r'[，,；;]', fact)
        if any(re.search(r'(?:可|能)(?:讓|供|容納)|能載|可通過|能直接|能在.{1,12}運行', clause)
               and not re.search(r'是否|未必|不一定|不確定|可能|不能|不可|未能|無法|沒有說|尚未確認', clause)
               for clause in clauses):
            ability.append(fact)
        if re.search(r'是否.{1,12}(?:未查到|不明)|(?:淨寬|寬度|高度|尺寸).{0,4}(?:沒有紀錄|尚未量測|不詳)', fact):
            unknown.append(fact)
    if not ability and not unknown:
        return None
    focus = {}
    if unknown:
        focus['尚未核實的原話'] = unknown
        if any(re.search(r'(?:淨寬|寬度).{0,4}(?:沒有紀錄|尚未量測|不詳)', fact) for fact in unknown):
            focus['情況核對'] = '情況一：實際通道比物件寬。情況二：實際通道比物件窄。分別核對其他已知條件；不要自行挑選其中一種寬度。'
        elif any('是否開放' in fact for fact in unknown):
            focus['情況核對'] = '分開檢查今天有開放和今天未開放的情況，兩者都要考慮其他已知通道。不能把未查到開放狀態當成已關閉。'
        else:
            focus['情況核對'] = '將未知條件視為兩種可能，分別核對是否會改變最後結果。'
    elif ability:
        focus['通行能力的原話'] = ability
        focus['能力核對'] = '原文明說可供這件物品通過時，按該條件判斷。另核對有無明說的其他阻礙。'
    return focus


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
    choices = re.search(r'[。？！?]\s*只(?:回|給|回答|輸出)(「[^「」]{1,16}」(?:、「[^「」]{1,16}」){0,4}或「[^「」]{1,16}」)[。！]?$', question)
    if choices and not conclusion and label in re.findall(r'「([^「」]+)」', choices[1]):
        return label
    if not re.search(r'[。？！?]\s*只(?:回|給|回答|輸出)[\u4e00-\u9fff]{0,12}名稱[。！]?$', question):
        return None
    if label in question or any(r['role'] == 'user' and not r['truncated'] and label in r['content']
                                for r in context.get('recent_conversation', [])):
        return label
    return None
