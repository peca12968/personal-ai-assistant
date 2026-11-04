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
