"""Experimental three-valued reasoning for two explicitly alternative devices."""
import re

CHOICE = re.compile(r'(?P<body>.+)[？?]只回「能」、「不能」或「資料不足」[。！]?$')
HEADER = re.compile(r'([\u4e00-\u9fff]{1,8})?(?:只有|有)([\u4e00-\u9fff]{1,3})、([\u4e00-\u9fff]{1,3})兩台[\u4e00-\u9fff]{1,8}[，,]任選一台即可')
LABEL = r'(能|不能|資料不足)'
REPLY = re.compile(r'\s*共同[：:]\s*'+LABEL+r'[。]?\s*\n\s*一[：:]\s*'+LABEL+r'[。]?\s*\n\s*二[：:]\s*'+LABEL+r'[。]?\s*')


def plan(question):
    match = CHOICE.fullmatch(question) if isinstance(question,str) and len(question)<=1000 else None
    if not match or re.search(r'[「」『』“”"\x60\n]|翻譯|改寫|假設|舉例', match['body']):
        return None
    parts=match['body'].split('。')
    header=HEADER.fullmatch(parts[0])
    if not header or header[2]==header[3] or len(parts)<4 or not all(parts):
        return None
    names=[header[2]+'機',header[3]+'機']
    devices=[]
    shared=[]
    for fact in parts[1:-1]:
        if any(fact.startswith(name) for name in names):
            if sum(name in fact for name in names)!=1:
                return None
            devices.append(fact)
        else:
            shared.append(fact)
    if len(devices)!=2 or not shared or not all(sum(f.startswith(n) for f in devices)==1 for n in names):
        return None
    return dict(header=parts[0], devices=devices, shared=shared, question=parts[-1])


def prompt(plan):
    a,b=plan['devices']
    return ('判定以下三項，各自只依該項原文判斷。使用能、不能或資料不足。'
            '\n任務：'+plan['question']+'？\n選項關係：'+plan['header']+'。'
            '\n共同前提是否滿足：'+'。'.join(plan['shared'])+'。'
            '\n第一台自身是否可操作：'+a+'。\n第二台自身是否可操作：'+b+
            '。\n這三項請各答一行，格式「共同：答案」、「一：答案」、「二：答案」。'
            '\n設備狀態只判設備自身，共同前提另列，不要混合。')


def decode(raw):
    match=REPLY.fullmatch(raw)
    return list(match.groups()) if match else None


def combine(labels):
    if not isinstance(labels,list) or len(labels)!=3 or any(v not in ('能','不能','資料不足') for v in labels):
        raise ValueError('需要共同前提及兩個設備的三個有效判斷。')
    common,first,second=labels
    available='能' if '能' in (first,second) else '不能' if first==second=='不能' else '資料不足'
    if '不能' in (common,available):
        return '不能'
    return '能' if common==available=='能' else '資料不足'


STATUS_LABELS = {'已確認成立':'能', '已確認不成立':'不能', '未知':'資料不足'}
STATUS = '(' + '|'.join(STATUS_LABELS) + ')'
STATUS_REPLY = re.compile(r'\s*共同[：:]\s*'+STATUS+r'[。]?\s*\n\s*一[：:]\s*'+STATUS+r'[。]?\s*\n\s*二[：:]\s*'+STATUS+r'[。]?\s*')


def status_prompt(plan):
    a, b = plan['devices']
    return ('只判斷原文能確認什麼，不猜測設備狀態。每一項使用同一組標籤。'
            '\n已確認成立：原文明確支持共同前提已滿足，或該台本身可操作。'
            '\n已確認不成立：原文明確支持共同前提受阻，或該台本身不可操作。'
            '\n未知：這一項的狀態尚未確認。尚未確認不是已確認不成立。條件句只說明規則，不代表條件已成立。'
            '\n任務：' + plan['question'] + '？'
            '\n共同前提原文：' + '。'.join(plan['shared']) + '。'
            '\n第一台自身原文：' + a + '。'
            '\n第二台自身原文：' + b + '。'
            '\n各台自身的狀態依各台原文判斷，共同前提只在共同一項判斷。'
            '\n請填三行「共同：標籤」、「一：標籤」、「二：標籤」。')


def decode_status(raw):
    match = STATUS_REPLY.fullmatch(raw)
    return [STATUS_LABELS[label] for label in match.groups()] if match else None


def isolated_prompts(plan):
    instructions = ('只根據本段文字判斷指定項目。使用三個標籤之一。'
                    '「已確認成立」表示原文明說該項目已滿足。'
                    '「已確認不成立」表示原文明說該項目受阻。'
                    '「未知」表示未提供足夠資料確認。'
                    '未查證不能當作已確認不成立。只回標籤，不加其他文字。')
    common = instructions + '\n指定項目：任務所需的共同前提是否已滿足。' + '\n任務：' + plan['question'] + '？\n原文：' + '。'.join(plan['shared']) + '。'
    devices = [instructions + '\n指定項目：這台設備自身是否已確認可操作。\n原文：' + fact + '。'
               for fact in plan['devices']]
    return [common, *devices]


def decode_isolated(raw):
    label = raw.strip().removesuffix('。')
    return STATUS_LABELS.get(label)
