"""Help the model distinguish an unresolved prerequisite from a settled outcome."""
import re

CHOICE = re.compile(r'(?P<body>.+)[？?]\s*只(?:回|回答|輸出)「能」、「不能」或「資料不足」[。！]?$')
COUNT = re.compile(r'[\u4e00-\u9fff]{0,6}(?:沒查到|不知道|尚未查到)'
                   r'(?:(?P<owner>[\u4e00-\u9fff]{0,6})還剩幾(?:個|張)(?P<after>名額|位子|座位|票)|(?P<before>(?:預約)?名額|位子|座位|票)還剩幾(?:個|張))')


def capacity_trial(context):
    question = context['current_request']
    if len(question) > 1000 or context.get('documents', {}).get('catalog') or '\n' in question:
        return None
    match = CHOICE.fullmatch(question)
    if not match or re.search(r'[「」『』“”"`]|翻譯|改寫|舉例|假設|不是不知|不是沒查|不能說|並非|聽說', match['body']):
        return None
    parts = [part.strip() for part in re.split(r'[。；;]', match['body'])]
    if not 3 <= len(parts) <= 10 or any(not part for part in parts):
        return None
    facts, ask = parts[:-1], parts[-1]
    matches = [(i, m) for i, fact in enumerate(facts) if (m := COUNT.fullmatch(fact))]
    if len(matches) != 1 or not re.search(r'能|可以', ask):
        return None
    index, count = matches[0]
    known = facts[:index] + facts[index+1:]
    if not known or any(re.search(r'不知|未查|沒查|沒確認|未確認|是否|可能|不確定', fact) for fact in known):
        return None
    if re.search(r'照舊|剛才|上次|前文|上文|前述|之前提到|根據附件', question):
        return None
    if (context.get('recent_conversation') or context.get('conversation_tail')) and '只根據這些資料' not in question:
        return None
    if (context.get('memories') or context.get('session_brief') or context.get('earlier_user_excerpts')
            or context.get('task_runs') or context.get('autonomous_goals', {}).get('goals')
            or context.get('answer_feedback', {}).get('items') or context.get('earlier_user_corrections', {}).get('excerpts')):
        return None
    noun = count['after'] or count['before']
    owner = count['owner'] or ''
    classifier = '張' if noun == '票' else '個'
    alternatives = [f'{owner}至少還有一{classifier}{noun}', f'{owner}沒有剩餘{noun}']
    return dict(待查原文=facts[index], 對照假設=alternatives, 兩種情況都保留的原文=known, 本輪問句=ask)



def trial_prompt(focus):
    x, y = focus['對照假設']
    known = '。'.join(focus['兩種情況都保留的原文']) + '。'
    return ('請做兩次獨立判斷，僅依各情況列出的事實，判定下列行動能否執行。\n'
            '行動問題：' + focus['本輪問句'] + '？\n'
            '情況一：' + x + '。' + known + '\n'
            '情況二：' + y + '。' + known + '\n'
            '請逐行作答，每行只填一個「能」、「不能」或「資料不足」。格式為「一：答案」和「二：答案」。')


def branch_labels(raw):
    match = re.fullmatch(r'\s*一[：:]\s*(能|不能|資料不足)\s*[。]?[^\S\n]*\r?\n\s*二[：:]\s*(能|不能|資料不足)[。]?\s*', raw)
    return list(match.groups()) if match else None

def ordinary_context(context):
    return {k: v for k, v in context.items() if k not in ('response_mode', 'capacity_trial')}
