"""Bind clarification to public plan items and apply explicit scope reductions."""
import re
import unicodedata

from . import artifact_names, delivery_preference, goal_guidance_sources, goal_reply_sources, goal_sources, goal_stages, work_plan


GUIDANCE = '''有已保存工作清單時，若缺漏會影響特定未完成項目，提問使用 plan_questions，例如
{"question":"收貨窗口是誰？\n入場門號是多少？","plan_questions":[{"question":"收貨窗口是誰？","items":[2]},{"question":"入場門號是多少？","items":[3]}]}。
question 逐行等於各分問。items 填這個問題實際影響的所有未完成項目；一個獨立分問對應一個資料缺漏，無關的問題不綁進清單。
scope_cancelled 列出使用者已取消的項目。當前成果只交保留項目的實際結果。取消項目和原因已保存於工作清單，除非使用者要求一併交代，定稿省略已取消項目的名稱、經過及結果。
scope_reply 的 confirmed 分問有實際使用者原話及值，引用其 citation。revisions 保存使用者更正後的值、舊依據和須重核的項目。成果使用現行值並引用新回覆。declined_proposals 已明示未採用。pending_revisions 保存待選原話，選定前沿用最後確認值。completed_items 只包含已完成的確認工作。其他項目仍依清單完成。若交付核對已就緒，直接交付仍需的成果。
若清單的數量分問仍待答，原話中的多個候選、不同單位或暫估都還需要明確確認。沿用其他已核對的來源，只保留仍影響成果的分問。'''
_CANCEL = re.compile(r'(?:本輪|這次|本次)?\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做)\s*(.+)')
_TITLES = re.compile(r'「[^「」\r\n]+」(?:\s*(?:、|，|,|及|和|與)\s*「[^「」\r\n]+」)*')
_UNCHANGED = {'其他項目照舊', '其餘項目照舊', '其他項目維持原樣', '其餘照舊', '其他照舊'}
_OUTPUT_ONLY = re.compile(r'只(?:交付|交|提供|要)(?:已核對的|已確認的)?[\u4e00-\u9fff、]{1,45}')
_SCOPE_COMMA = re.compile(r'\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做|其餘|其他|只交|只提供|只要)')
_WORK_VERB = re.compile(r'^(?:安排|確認|核對|決定|記錄|填寫)')
_COLLECT_FIELD = re.compile(r'^(?:確認|記錄|登記|填寫)')
_ASK_FIELD = re.compile(r'(?:是誰|為誰|是哪位|是什麼|是多少|有多少|要用哪個|是哪一個|是哪個)[？?]$')
_NUMERIC_QUESTION = re.compile(r'(?:是|有)多少[？?]$')
_PERSON_QUESTION = re.compile(r'(?:是誰|為誰|是哪位)[？?]$')
_COLLOQUIAL_PERSON_QUESTION = re.compile(r'(?:是哪一位|是何人)[？?]$')
_TEXT_QUESTION = re.compile(r'(?:是什麼|是哪個|是哪一個|要用哪個|是哪種|要用哪種|用哪種|在哪裡|在哪兒|在何處)[？?]$')
_PLACE_QUESTION = re.compile(r'(?:在哪裡|在哪兒|在何處)[？?]$')
_PLACE_FIELD = re.compile(r'(?:地點|位置|集合點|會場|場地)$')
_PLACE_ASSIGN = re.compile(r'(?:已確認在|確定在|位於|在)\s*(.+)')
_TEXT_LITERAL = re.compile(r'[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff][A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff ._/\-·‧（）()＋+－]{0,39}')
_TEXT_STATUS = re.compile(r'^(?:(?:目前|這次|本次|我)\s*)?(?:不使用|不採用|不選|不在|未選定|'
                          r'未指定|未決定|尚不|仍不|不可|不能|不要|是否|請問|還需要|等待|'
                          r'待核對|待決定|沒決定|未定|先別|不知道|例如|原文寫|聽說|據說|改用|等一下|等候)')
_TEXT_STATUS_END = re.compile(r'(?:待定|未定|待核對|再確認|稍後決定|未決定|僅供參考|可能適用|沒採用|不採用)[）)]?$')
_TEXT_QUALIFIER = re.compile(r'(?:但|不過|只是)(?:尚|還|仍|要|須|另|得|未|不|待)')
_TEXT_PLACEHOLDER = frozenset('tbd unknown none null na pending 沒有 等等 尚待確認'.split())
_PERSON_NAME = re.compile(r'[\u3400-\u9fff]{2,6}(?:[·・‧][\u3400-\u9fff]{1,6})?')
_PERSON_MODAL = re.compile(r'^(?:可能|或許|大概|應該|也許|似乎|看來|暫定|暫未|尚未|還沒|還未|仍未|'
                          r'不確定|不知道|不清楚|未確認|未確定|未安排|未指派|待確認|待指派|還要|有待|'
                          r'不是|並非|並不是|暫不|先不|擬由|預計|提議|建議|我猜|我還|聽說|據說|仍需|還得|需要|須再)')
_PERSON_UNCONFIRMED_END = re.compile(r'(?:待確認|未確認|沒確認|還不確定|暫定|嗎|呢|吧)[）)]?$')
_PERSON_NONNAME = re.compile(r'確認|取消|改成|改為|如果|回覆|再說|晚點|等下|稍後|另外|同時|'
                            r'尚未|仍未|還沒|未定|暫不|可能|應該|也許|請問|例如|他叫|她叫|姓名|我猜|聽說')
_PERSON_PLACEHOLDER = frozenset('未知 不明 待定 未定 無人 有人 某人 不詳 無 另議 待補 無人員 沒有人 無人承接'.split())
_PERSON_QUOTE = re.compile(r'(?:例如|原文寫|文件記載|有人說|有人提議|有人建議|引文)?\s*「([^「」]+)」')
_FIELD_NAME = re.compile(r'[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 _-]{1,30}')
_REPLY_CONNECTOR = r'(?:(?:另外|然後)\s*)?'
_ASSIGN = re.compile(r'(?:已確認為|已確認是|確定為|確定是|是|為|：|:)\s*(.+)')
_DEFERRED = re.compile(r'(?:目前\s*)?(?:還沒確認|還未確認|尚未確認|未確認|待確認|還要確認|尚待確認|未知|還不知道|先不決定|稍後確認)')
_AMOUNT = r'(?:[0-9０-９]+(?:\.[0-9０-９]+)?|[零〇一二兩三四五六七八九十百千]+)'
_NUMBER = re.compile(rf'{_AMOUNT}\s*(?:[號号份件箱袋組張位人個座樓層班]|分鐘|小時)?')
_ADDITIONAL_COUNT_UNITS = '盤盒冊株瓶罐枚束片包套本杯卷'
_COUNT_UNITS = '份件箱袋組張位人個座樓層班' + _ADDITIONAL_COUNT_UNITS
_COLLOQUIAL_COUNT_QUESTION = re.compile(
    rf'(?:^(?:請問\s*)?|總共|還剩下?|剩下?|需要(?:準備)?|應準備|要(?:準備|用|發)?|需(?:準備)?|有|共|合計)'
    rf'(?:幾|多少)\s*(?P<unit>[{_COUNT_UNITS}])[？?]$')
_COUNT_NUMBER = re.compile(rf'(?P<amount>{_AMOUNT})\s*(?P<unit>[號号{_COUNT_UNITS}]|分鐘|小時)?')
_COUNT_FIELD_UNIT = re.compile(rf'([{_COUNT_UNITS}])數$')
_ADDITIONAL_COUNT = re.compile(rf'{_AMOUNT}\s*[{_ADDITIONAL_COUNT_UNITS}]')
_MIXED_COUNTS = re.compile(rf'{_AMOUNT}\s*[{_COUNT_UNITS}](?:\s*[，,、和與及]?\s*{_AMOUNT}\s*[{_COUNT_UNITS}]){{1,3}}')
_UNCERTAIN_COUNT = re.compile(rf'(?:約|大約|大概|可能(?:是|為)?|暫定(?:為)?|估計|先估|至少|至多)\s*{_COUNT_NUMBER.pattern}')
_ADJACENT_DIGITS = re.compile(r'[一二兩三四五六七八九]{2}')
_CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,23}')
_SHORT_WORD = re.compile(r'[\u4e00-\u9fff]{1,12}')
_UNCERTAIN = re.compile(r'待|未|還|不|暫|可能|大概|或|應該|也許|若|如果|請|要|確認|取消|改成|改為|另外|同時|再說|回覆|晚點|等下|稍後')
_POST_CANCEL = re.compile(r'(.{2,90}?)\s*(?:先不用了|不用了|先不做了|不做了|先取消|取消掉)')
_PLAIN_COMMA = re.compile(r'\s*(?!\s*「)|\s*「[^」\r\n]+」\s*(?:先不用了|不用了|先不做了|不做了|先取消|取消掉)')
_BEFORE_BARE_COUNT = rf'(?!\s*{_AMOUNT}\s*[{_COUNT_UNITS}])'
_COUNT_COMMA = re.compile(rf'{_BEFORE_BARE_COUNT}(?:{_PLAIN_COMMA.pattern})')
_QUOTED_TEXT_ASSIGN = r'\s*「[^「」\r\n]{1,31}」\s*(?:已確認為|已確認是|確定為|確定是|為|是|在|：|:)'
_TEXT_COMMA = re.compile(rf'{_COUNT_COMMA.pattern}|{_QUOTED_TEXT_ASSIGN}')
_KEEP_ALL = {'都保留', '都先保留', '兩項都保留', '都不取消', '不取消'}
_REVISION_VERB = r'(?:改成|改為|更正為|更正成|應改為)'
_REVISION = re.compile(r'(?:(?:欄位\s*)?「([^「」\r\n]{2,31})」|([\u4e00-\u9fffA-Za-z0-9 _-]{2,31}?))\s*'
                       r'(?:(?:剛才|先前|原先|上一則)\s*(?:說|填|寫|報)錯了?\s*[，,、]?\s*)?'
                       rf'{_REVISION_VERB}\s*(.+)')
_CONFIRMATION_FIELD_REVISION = re.compile(r'(?:(?:欄位\s*)?「([^「」\r\n]{2,31})」|([\u4e00-\u9fffA-Za-z0-9 _-]{2,31}?))'
                                          rf'\s*正式\s*{_REVISION_VERB}\s*(.+)')
_CONFIRMATION_QUESTION_REVISION = re.compile(r'更正\s*「([^「」\r\n]{1,400})」\s*(?:為|成)\s*(.+)')
_DIRECTIVE_REVISION = re.compile(
    r'(?:請\s*(?:(?:把|將)\s*)?|(?:把|將)\s*(?=「))?'
    r'(?:(?:欄位\s*)?「(?P<quoted>[^「」\r\n]{2,31})」|(?P<field>[\u4e00-\u9fffA-Za-z0-9 _-]{2,31}?))\s*'
    r'(?P<modifier>正式|確定|暫定|先不要|不要|先別|不必|可能|考慮|擬|建議)?\s*'
    r'(?P<verb>改用|改派(?:給|為)?|移到|移至)\s*(?P<literal>.+)')
_DIRECTIVE_EXTRA = re.compile(r'如果|視情況|不一定|僅供參考|然後|並且|並(?:把|將)|改用|改派|移到|移至')
_CONFIRMATION_WITHDRAWAL = re.compile(r'(?:請\s*)?撤回\s*(?:「([^「」\r\n]{1,400})」|([\u4e00-\u9fffA-Za-z0-9 _-]{2,31}?))'
                                      r'\s*(?:的)?(?:答覆|回答|確認)')
_REVISION_COMMA = re.compile(rf'(?!\s*{_REVISION_VERB})(?:{_PLAIN_COMMA.pattern})')
_DECLINE = r'(?:(?:目前|本輪|這次)\s*)?(?:未採用|尚未採用|沒有採用|不採用|先不採用|維持原值)'
_DECLINED_PROPOSAL = re.compile(r'(?:有人|同事|對方|主管)?\s*(?:曾)?(?:提議|建議)\s*'
                                r'(?:「[^」\r\n]{1,200}」|『[^』\r\n]{1,200}』)\s*[，,、]?\s*' + _DECLINE)
_QUALIFIED_REVISION_COMMA = re.compile(rf'{_REVISION_COMMA.pattern}|\s*「[^」\r\n]{{2,31}}」\s*(?:正式\s*)?{_REVISION_VERB}')
_COUNT_REVISION_COMMA = re.compile(rf'{_BEFORE_BARE_COUNT}(?:{_QUALIFIED_REVISION_COMMA.pattern})')
_TEXT_REVISION_COMMA = re.compile(rf'{_COUNT_REVISION_COMMA.pattern}|{_QUOTED_TEXT_ASSIGN}')
_PENDING_COMMA = re.compile(rf'(?!\s*{_DECLINE})(?:{_REVISION_COMMA.pattern})')
_FILENAME_REPLY_COMMA = re.compile(rf'{_PENDING_COMMA.pattern}|\s*「[^」\r\n]{{2,31}}」\s*{_REVISION_VERB}')
_FIELD_DELAY = r'(?:(?:晚點|稍後|之後)(?:再)?(?:決定|選擇|確認)|先不(?:決定|選擇|確認)|還沒決定)'
_FILENAME_FIRST_COMMA = re.compile(rf'{_FILENAME_REPLY_COMMA.pattern}|\s*「[^」\r\n]+」\s*{_FIELD_DELAY}'
                                   r'|\s*「[^」\r\n]+\.(?i:md|txt|json)」\s*(?=[。，,；;！？!?\r\n]|$)')
_FILENAME_DEFERRAL = re.compile(r'(?:定稿)?檔名(?:(?:還沒|還未|尚未)(?:想好|決定|確認)|'
                                r'(?:稍後|之後|晚點)(?:再)?(?:決定|確認)|先不(?:決定|確認)|待定)'
                                r'|先不(?:決定|確認)(?:定稿)?檔名')
_KEEP_VALUE = {'沿用原值', '維持原值', '用原值', '都不改', '不改', '保持原值'}
SUPPLEMENT_REPLY_VERSION = 12
PARTIAL_DOCUMENT_REPLY_VERSION = 13
COMBINED_EVIDENCE_REPLY_VERSION = 14
CARRY_CONFIRMATIONS_REPLY_VERSION = 15
CONFIRMATION_FILENAME_REPLY_VERSION = 16
RECONFIRMATION_REPLY_VERSION = 17
COMBINED_CORRECTION_REPLY_VERSION = 18
QUALIFIED_CORRECTION_REPLY_VERSION = 19
NUMERIC_CORRECTION_REPLY_VERSION = 20
COUNT_REPLY_VERSION = 21
COUNT_CORRECTION_REPLY_VERSION = 22
PERSON_REPLY_VERSION = 23
PERSON_CORRECTION_REPLY_VERSION = 24
TEXT_REPLY_VERSION = 25
TEXT_CORRECTION_REPLY_VERSION = 26
COLLOQUIAL_REPLY_VERSION = 27
COLLOQUIAL_CORRECTION_REPLY_VERSION = 28
CORRECTION_REPLY_VERSIONS = (COLLOQUIAL_CORRECTION_REPLY_VERSION, TEXT_CORRECTION_REPLY_VERSION, PERSON_CORRECTION_REPLY_VERSION,
                             COUNT_CORRECTION_REPLY_VERSION, NUMERIC_CORRECTION_REPLY_VERSION,
                             QUALIFIED_CORRECTION_REPLY_VERSION, COMBINED_CORRECTION_REPLY_VERSION)
CONFIRMATION_CORRECTION = 'confirmation_correction'
CONFIRMATION_CORRECTION_VERSION = 'confirmation_correction_version'
TYPED_GUIDANCE_VERSION = 2
DIRECTIVE_GUIDANCE_VERSION = 3


def validate_questions(args):
    if args.keys() - {'question', 'partial_result', 'plan_questions'}:
        raise ValueError('工作清單提問須使用獨立的工作分問。')
    parts = args['plan_questions']
    if not isinstance(parts, list) or not 1 <= len(parts) <= 4:
        raise ValueError('依工作清單提問需列一至四個分問。')
    questions = []
    for part in parts:
        if not isinstance(part, dict) or part.keys() != {'question', 'items'}:
            raise ValueError('每個工作分問須列問句和相關項目。')
        question, ids = part['question'], part['items']
        if (not isinstance(question, str) or not 1 <= len(question) <= 400 or question != question.strip()
                or any(ch in question for ch in '\r\n\0') or question in questions
                or not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(type(number) is not int or not 1 <= number <= 8 for number in ids) or len(set(ids)) != len(ids)):
            raise ValueError('工作分問及相關項目格式不符。')
        questions.append(question)
    if args['question'] != '\n'.join(questions):
        raise ValueError('顯示的問題須逐行對應工作分問。')


def bind_questions(args, steps):
    validate_questions(args)
    plan = work_plan.latest(steps)
    if plan is None:
        raise ValueError('須先保存工作清單，才能依工作項目提問。')
    active = {row['id'] for row in plan['items'] if row['state'] not in {'done', 'superseded'}}
    receipt = _scope_receipt(plan)
    language = receipt.get('language', {})
    choice = language.get('cancellation') or next(iter(language.get('pending_revisions', ())), None)
    if args == receipt.get('successor'):
        if choice:
            active.update(choice['items'])
        if target := receipt.get('confirmation_target_choice'):
            active.update(target['items'])
    parts = args['plan_questions']
    if any(not set(part['items']) <= active for part in parts):
        raise ValueError('問題只能連到仍需處理的工作項目。')
    requirements = {row['id']: row['confirmation_question'] for row in plan.get('evidence_requirements', ())
                    if 'confirmation_question' in row}
    for part in parts:
        if part['question'] in requirements.values() and any(
                requirements.get(number) != part['question'] for number in part['items']):
            raise ValueError('指定確認問題須綁定同一確認要求的工作項目。')
    anchor = steps[plan['sequence'] - 1]
    return {'plan_step': plan['sequence'], 'plan_sha256': goal_stages.digest(anchor), 'parts': parts}


def _target(task):
    return _WORK_VERB.sub('', task, count=1)


def _legacy_parts(question, plan):
    if '\n' in question or question.count('？') + question.count('?') != 1:
        return []
    matching = []
    for item in plan['items']:
        target = _target(item['task'])
        if len(target) < 2:
            continue
        suffix = rf'{re.escape(target)}(?:是誰|為誰|是哪位|是什麼|是多少|有多少|要用哪個|是哪一個)[？?]'
        if re.search(rf'(?:^|的\s*|：\s*|請問\s*)(?:正式)?{suffix}$', question):
            matching.append(item)
    if len(matching) != 1 or matching[0]['state'] in {'done', 'superseded'}:
        return []
    return [{'question': question, 'items': [matching[0]['id']]}]


def cancellations(source, plan):
    clauses = goal_guidance_sources.standalone_clauses(source['content'], comma_before=_SCOPE_COMMA)
    return _cancellations(clauses, plan)


def _cancellations(clauses, plan):
    ids_by_task = {}
    for item in plan['items']:
        ids_by_task.setdefault(item['task'], []).append(item['id'])
    active = {item['id'] for item in plan['items'] if item['state'] != 'superseded'}
    changes, used, outputs = [], set(), []
    for start, clause, question in clauses:
        if question:
            return []
        if clause in _UNCHANGED:
            continue
        if _OUTPUT_ONLY.fullmatch(clause) and not re.search(r'重算|重新|改成|改為|新增|另做', clause):
            outputs.append(clause)
            continue
        match = _CANCEL.fullmatch(clause)
        if not match:
            return []
        titles = match[1].strip()
        names = re.findall(r'「([^「」]+)」', titles) if _TITLES.fullmatch(titles) else [titles]
        ids = []
        for name in names:
            matches = ids_by_task.get(name, [])
            if len(matches) != 1 or matches[0] in used:
                return []
            number = matches[0]
            used.add(number)
            if number in active:
                ids.append(number)
        if ids:
            changes.append({'items': ids, 'start': start, 'end': start + len(clause), 'quote': clause})
    for item in plan['items']:
        if item['id'] in used and any(_target(item['task']) in clause for clause in outputs):
            return []
    return changes


def _waiting_parts(steps, plan):
    ask = steps[-1]
    action, observation = ask['action'], ask['observation']
    args = action['args']
    if action['tool'] != 'ask' or args.keys() - {'question', 'partial_result', 'plan_questions'}:
        raise ValueError('這個待答問題不屬於一般工作清單。')
    question = goal_reply_sources.checked_question(action, observation)
    if 'plan_questions' in args:
        binding = bind_questions(args, steps[:-1])
        if observation.get('work_plan_question') != binding or binding['plan_step'] != plan['sequence']:
            raise ValueError('待答問題與目前工作清單不一致。')
        parts = binding['parts']
    else:
        parts = _legacy_parts(question, plan)
    return question, parts


def _next_question(parts):
    if parts:
        return {'question': '\n'.join(part['question'] for part in parts), 'plan_questions': parts}
    return None


def _scope_receipt(plan):
    return plan.get('scope_reply') or plan.get('scope_change') or {}


def language_followup(steps):
    plan = work_plan.latest(steps)
    if plan and plan['sequence'] == len(steps) - 1:
        receipt = _scope_receipt(plan)
        if receipt.get('successor') == steps[-1]['action']['args']:
            return receipt.get('language')
    return None


def is_supplement_followup(steps):
    if len(steps) < 2:
        return False
    supplement, ask = steps[-2:]
    return (supplement['action']['tool'] == goal_sources.PLAN_SUPPLEMENT and ask['action']['tool'] == 'ask'
            and supplement['observation'].get('successor') == ask['action']['args']
            and supplement['observation'].get('goal_conditions_sha256') == ask['observation'].get('goal_conditions_sha256'))


def _successor(steps, plan, cancelled):
    question, parts = _waiting_parts(steps, plan)
    if not parts:
        return {'question': question}
    remaining = []
    for part in parts:
        ids = [number for number in part['items'] if number not in cancelled]
        if ids:
            remaining.append({'question': part['question'], 'items': ids})
    return _next_question(remaining)


def _without_delivery(text, spans):
    for row in spans:
        start, end = row['start'], row['end']
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def observe(steps, source, scope, prior_scope, *, waiting, delivery=None, filename_declaration=None, filename_guidance=None):
    plan = work_plan.latest(steps)
    if plan is None or source['steps_seen'] != len(steps):
        return None
    filename_ask = waiting and steps[-1]['observation'].get(artifact_names.ASK_MARK)
    if filename_guidance:
        previous = language_followup(steps) if waiting and not filename_ask else None
        if (not previous or previous.get('cancellation') or not previous.get('pending_revisions')
                or prior_scope != plan['goal_conditions_sha256'] or not _waiting_parts(steps, plan)[1]):
            return None
        changes = []
    elif filename_declaration:
        if prior_scope != plan['goal_conditions_sha256'] or waiting and not filename_ask:
            return None
        if filename_ask:
            name = filename_declaration['filename']
            draft = filename_ask['draft']['name']
            if name.casefold() in artifact_names.writes(steps) or name.rsplit('.', 1)[1].casefold() != draft.rsplit('.', 1)[1].casefold():
                raise ValueError('請提供同格式且尚未使用的定稿檔名。')
        changes = []
    elif delivery:
        if (prior_scope != plan['goal_conditions_sha256'] or (filename_ask and delivery['file_delivery'])
                or (waiting and not filename_ask and not _waiting_parts(steps, plan)[1])):
            return None
        text = _without_delivery(source['content'], [delivery])
        clauses = goal_guidance_sources.standalone_clauses(text, comma_before=_SCOPE_COMMA)
        if clauses and not waiting:
            return None
        changes = _cancellations(clauses, plan)
        if clauses and not changes:
            return None
    else:
        changes = cancellations(source, plan)
        if not changes:
            return None
    cancelled = {number for change in changes for number in change['items']}
    result, proof = _scope_update(steps, plan, source, changes, scope, prior_scope)
    if delivery:
        proof['delivery_mode'] = delivery
    if filename_declaration:
        proof['filename_declaration'] = filename_declaration
    if filename_guidance:
        choice, revision = filename_guidance
        proof['filename_choice'] = choice
        if revision:
            proof['filename_revision'] = revision
    if waiting:
        successor = None if filename_ask else _successor(steps, plan, cancelled)
        if previous := language_followup(steps):
            language = _retain_language(previous, cancelled)
            tasks = {item['id']: item['task'] for item in plan['items']}
            successor = _next_question(_show_waiting(language, tasks))
            proof['language'] = language
        proof.update(ask_sequence=steps[-1]['sequence'], ask_sha256=goal_stages.digest(steps[-1]), successor=successor)
    result['scope_change'] = proof
    return result


def _scope_update(steps, plan, source, changes, scope, prior_scope):
    cancelled = {number for change in changes for number in change['items']}
    current = prior_scope == plan['goal_conditions_sha256']
    items = []
    retained = set()
    for item in plan['items']:
        if item['id'] in cancelled:
            item = {**item, 'state': 'superseded', 'note': f'依 {source["citation"]} 取消此項。'}
        elif item['state'] == 'done':
            if current:
                retained.add(item['id'])
            else:
                item = {**item, 'state': 'pending', 'evidence_steps': [], 'note': '先前補充已變更條件，需重新核對。'}
        items.append(item)
    proof = {'citation': source['citation'], 'source_sha256': source['sha256'], 'changes': changes,
             'plan_step': plan['sequence'], 'plan_sha256': goal_stages.digest(steps[plan['sequence'] - 1]),
             'prior_scope': prior_scope, 'retained_items': [item['id'] for item in items if item['id'] in retained]}
    result = {'status': 'plan_recorded', 'items': items, 'goal_conditions_sha256': scope,
              'scope_cancelled': sorted(cancelled | set(plan.get('scope_cancelled', ()))),
              'review_note': '依實際更正更新工作清單，先前步驟及答覆仍可核對。'}
    if plan.get('verify_delivery'):
        result['verify_delivery'] = True
    if plan.get('evidence_requirements'):
        result['evidence_requirements'] = [
            {**item, 'defined_scope': scope} if item['id'] in retained else item
            for item in plan['evidence_requirements']]
        result['evidence_matches'] = [item for item in plan['evidence_matches'] if item['id'] in retained]
    return result, proof


def _carry_independent_replies(proof, plan, steps, prior_scope):
    if retained := work_plan._retained_reply_rows(plan, steps, prior_scope):
        rows = [row for row in retained if row['id'] in proof['retained_items']]
        if rows:
            proof['retained_reply_items'] = rows


def confirmation_targets(steps, scope):
    plan = work_plan.latest(steps)
    if not plan or not plan.get('verify_delivery') or _scope_receipt(plan).get('language', {}).get('pending_revisions'):
        return {}
    tasks = {row['id']: row['task'] for row in plan['items'] if row['state'] != 'superseded'}
    requirements = {row['id']: row for row in plan.get('evidence_requirements', ()) if row['id'] in tasks}
    current = work_plan._current_confirmations(requirements, steps, scope)
    targets = {}
    for number, spec in requirements.items():
        if question := spec.get('confirmation_question'):
            target = targets.setdefault(question, {'items': [], 'confirmed': []})
            target['items'].append(number)
            if number in current:
                target['confirmed'].append(current[number])
    for question, target in targets.items():
        target['field'] = _asked_field({'question': question, 'items': target['items']}, tasks)
    return targets


def _confirmation_field_matches(field, targets, *, qualified, text_literals=False, colloquial=False):
    return [question for question, target in targets.items() if field == target['field'] or (
        qualified and _FIELD_NAME.fullmatch(field)
        and field in _bound_field_names(question, target['field'], text_literals=text_literals, colloquial=colloquial))]


def _confirmation_literal(literal, questions, number, *, person_names, text_literals, colloquial):
    if literal is None:
        return None
    literal = literal.strip()
    if text_literals and all(_TEXT_QUESTION.search(question) for question in questions):
        return _text_literal(literal)
    if person_names and all(_person_question(question, colloquial=colloquial) for question in questions):
        return _person_literal(literal)
    return _value(literal, number=number)


def confirmation_guidance_version(content):
    if any(not question and _DIRECTIVE_REVISION.fullmatch(clause)
           for _, clause, question in goal_guidance_sources.standalone_clauses(content)):
        return DIRECTIVE_GUIDANCE_VERSION
    return TYPED_GUIDANCE_VERSION


def _directive_revision(clause, targets):
    match = _DIRECTIVE_REVISION.fullmatch(clause)
    if not match:
        return None
    field = (match['quoted'] or match['field']).strip()
    matches = _confirmation_field_matches(field, targets, qualified=True, text_literals=True, colloquial=True)
    if not matches:
        return None
    if match['modifier'] not in (None, '正式', '確定'):
        raise ValueError('尚未收到明確的新值。原確認及進度保留。')
    verb = match['verb']
    for question in matches:
        if verb.startswith('改派'):
            supported = _person_question(question, colloquial=True)
        else:
            supported = _TEXT_QUESTION.search(question) and (verb == '改用' or _PLACE_QUESTION.search(question)
                or any(_PLACE_FIELD.search(label) for label in _bound_field_names(
                    question, targets[question]['field'], text_literals=True, colloquial=True)))
        if not supported:
            raise ValueError('這個更正用語與原欄位不符，請逐字指定原問句及新值。')
    literal = match['literal'].strip()
    if not re.fullmatch(r'「[^「」]+」', literal) and _DIRECTIVE_EXTRA.search(literal):
        raise ValueError('請為每個原問句提供一個明確的新值，並將其他指示分開保存。')
    return field, matches, literal


def _confirmation_requests(clauses, targets, *, allow_withdrawal, qualified=False, number=_NUMBER,
                           person_names=False, text_literals=False, colloquial=False, directive_verbs=False):
    requested = {}
    remaining = []
    choice = None
    for start, clause, is_question in clauses:
        if is_question:
            remaining.append((start, clause))
            continue
        withdrawal = allow_withdrawal and _CONFIRMATION_WITHDRAWAL.fullmatch(clause)
        if withdrawal:
            if withdrawal[1] in targets:
                matches = [withdrawal[1]]
            elif qualified:
                matches = _confirmation_field_matches((withdrawal[1] or withdrawal[2]).strip(), targets,
                                                       qualified=True, text_literals=text_literals, colloquial=colloquial)
            else:
                matches = [question for question, target in targets.items() if target['field'] in withdrawal.groups()]
            literal = None
        elif match := _CONFIRMATION_QUESTION_REVISION.fullmatch(clause):
            matches, literal = ([match[1]] if match[1] in targets else []), match[2]
        elif directive_verbs and (directive := _directive_revision(clause, targets)):
            field, matches, literal = directive
        elif match := _CONFIRMATION_FIELD_REVISION.fullmatch(clause) or _REVISION.fullmatch(clause):
            field = (match[1] or match[2]).strip()
            matches = _confirmation_field_matches(field, targets, qualified=qualified,
                                                  text_literals=text_literals, colloquial=colloquial)
            literal = match[3]
        else:
            remaining.append((start, clause))
            continue
        if not matches:
            remaining.append((start, clause))
            continue
        value = _confirmation_literal(literal, matches, number, person_names=person_names,
                                      text_literals=text_literals, colloquial=colloquial)
        if len(matches) != 1:
            if (not qualified or withdrawal or choice or value is None
                    or not all(targets[question]['confirmed'] for question in matches)
                    or any(_numeric_question(question, colloquial=colloquial)
                           and not _number_matches(value, question, number, colloquial=colloquial)
                           for question in matches)):
                raise ValueError('有多個工作分問使用這個欄名，請逐字指定要更正的原問句。')
            choice = {'field': field, 'questions': matches, 'value': value, 'evidence': _literal_span(start, clause),
                      'items': sorted({number for question in matches for number in targets[question]['items']})}
            continue
        question = matches[0]
        if (question in requested or (not withdrawal and (
                value is None or (_numeric_question(question, colloquial=colloquial)
                                  and not _number_matches(value, question, number, colloquial=colloquial))))):
            raise ValueError('請為每個原問句提供一個明確的新值。')
        requested[question] = (value, _literal_span(start, clause))
    return requested, remaining, choice


def _confirmation_revisions(steps, plan, targets, requested, *, qualified=False, bind_numeric=False,
                            number=_NUMBER, person_names=False, text_literals=False, colloquial=False):
    active = {row['id']: row for row in plan['items'] if row['state'] != 'superseded'}
    result = []
    for question, (value, evidence) in requested.items():
        target = targets[question]
        by_step = {}
        for row in target['confirmed']:
            by_step.setdefault(row['reply_step'], []).append(row)
        for sequence, rows in by_step.items():
            step = steps[sequence - 1]
            proof = _scope_receipt(step['observation'])
            parts = [part for part in (*proof.get('revisions', ()), *proof.get('parts', ()))
                     if part['question'] == question and part.get('value') == rows[0]['value']]
            if not {row['id'] for row in rows} <= {number for part in parts for number in part['items']}:
                raise ValueError('更正缺少相符的原始確認證據。')
            labels = _bound_field_names(question, target['field'], text_literals=text_literals, colloquial=colloquial)
            field = target['field'] or (next(iter(labels), '') if text_literals or colloquial else '')
            prior = {'field': field, 'items': [row['id'] for row in rows], 'collected_items': [],
                     'value': rows[0]['value'], 'evidence': parts[0]['evidence'], 'step': sequence,
                     'step_sha256': goal_stages.digest(step), 'citation': rows[0]['citation'], 'source_sha256': rows[0]['sha256']}
            if bind_numeric and (previous_value := parts[0].get('value_evidence')):
                prior['value_evidence'] = previous_value
            fields = None
            if qualified:
                fields = {label for label in labels if label and _confirmation_field_matches(
                    label, targets, qualified=True, text_literals=text_literals, colloquial=colloquial) == [question]}
            revision = _revision_entry(plan, active, field, question, prior, value, evidence,
                                       dependency_fields=fields)
            revision['recheck_items'] = sorted(set(revision['recheck_items']) | set(prior['items']))
            if value is not None and ((bind_numeric and number.fullmatch(value))
                                      or _language_field(question, person_names=person_names, text_literals=text_literals,
                                                         colloquial=colloquial)):
                revision['value_evidence'] = _accepted_value_span(value, evidence)
            result.append(revision)
    return result


def _updated_confirmations(targets, revisions, source, sequence):
    current = {row['id']: row for target in targets.values() for row in target['confirmed']}
    for revision in revisions:
        for number in revision['items']:
            value = revision['value']
            if value is None:
                del current[number]
            else:
                current[number] = {**current[number], 'value': value, 'reply_step': sequence,
                                   'citation': source['citation'], 'sha256': source['sha256']}
    return current


def observe_confirmation_correction(steps, source, scope, prior_scope, *, waiting, version=1):
    if source['steps_seen'] != len(steps):
        return None
    targets = confirmation_targets(steps, prior_scope)
    if not any(target['confirmed'] for target in targets.values()):
        return None
    clauses = goal_guidance_sources.standalone_clauses(source['content'])
    typed = version in (TYPED_GUIDANCE_VERSION, DIRECTIVE_GUIDANCE_VERSION)
    requested, remaining, choice = _confirmation_requests(clauses, targets, allow_withdrawal=True, qualified=typed,
                                                           number=_COUNT_NUMBER if typed else _NUMBER,
                                                           person_names=typed, text_literals=typed, colloquial=typed,
                                                           directive_verbs=version == DIRECTIVE_GUIDANCE_VERSION)
    if choice:
        raise ValueError('有多個工作分問使用這個欄名，請逐字指定要更正的原問句。')
    if not requested:
        return None
    if remaining or any(not targets[question]['confirmed'] for question in requested):
        raise ValueError('這筆更正須指向已核對的答覆。請將其他指示或尚待回答的問題分開保存。')
    plan = work_plan.latest(steps)
    reopened = {question: [row['id'] for row in targets[question]['confirmed']]
                for question, (value, _) in requested.items() if value is None}
    parts = []
    if waiting:
        ask = steps[-1]
        filename_ask = ask['observation'].get(artifact_names.ASK_MARK)
        if filename_ask and not reopened:
            return None
        if ((not filename_ask and 'plan_questions' not in ask['action']['args'])
                or ask['observation'].get('goal_conditions_sha256') != prior_scope
                or (language_followup(steps) or {}).get('cancellation')):
            raise ValueError('目前待答問題有專用的確認流程，請先處理目前問題。')
        if not filename_ask:
            _, parts = _waiting_parts(steps, plan)
            if any(part['question'] in requested and part['question'] not in reopened for part in parts):
                raise ValueError('這個分問仍在等待答覆，請從目前提問保存新值。')
    has_withdrawals = bool(reopened)
    parts = [{**part, 'items': list(dict.fromkeys(part['items'] + reopened.pop(part['question'], [])))} for part in parts]
    successor = _next_question(parts + [{'question': question, 'items': numbers} for question, numbers in reopened.items()])
    if has_withdrawals:
        if len(successor['question']) > 800:
            raise ValueError('待答問題過長，請分開撤回及重新確認。')
        validate_questions(successor)
    changes = _confirmation_revisions(steps, plan, targets, requested, qualified=typed, bind_numeric=typed,
                                      number=_COUNT_NUMBER if typed else _NUMBER, person_names=typed,
                                      text_literals=typed, colloquial=typed)
    revisions = [row for row in changes if row['value'] is not None]
    withdrawals = [row for row in changes if row['value'] is None]
    current = _updated_confirmations(targets, changes, source, len(steps) + 1)
    result, proof = _scope_update(steps, plan, source, [], scope, prior_scope)
    _apply_revisions(result, proof, {}, plan, revisions + withdrawals, source)
    if withdrawals:
        proof['revisions'] = revisions
        proof['withdrawn_confirmations'] = withdrawals
    if waiting:
        proof.update(ask_sequence=len(steps), ask_sha256=goal_stages.digest(steps[-1]))
    proof.update(verified_confirmations=list(current.values()), successor=successor)
    _carry_independent_replies(proof, plan, steps, prior_scope)
    result['scope_change'] = proof
    return result


def _question_field_name(question):
    match = _ASK_FIELD.search(question)
    return question[:match.start()].removeprefix('請問').strip() if match else ''


def _bound_field_names(question, legacy_name, *, text_literals=False, colloquial=False):
    if colloquial and (match := _COLLOQUIAL_COUNT_QUESTION.search(question)):
        label = f'{match["unit"]}數'
        prefix = question[:match.start()].removeprefix('請問').strip()
        complete = prefix + ('數' if prefix.endswith(match['unit']) else label)
        return dict.fromkeys((complete, legacy_name, label))
    match = (_COLLOQUIAL_PERSON_QUESTION.search(question) if colloquial else None) or (
        _TEXT_QUESTION.search(question) if text_literals else None)
    if match:
        complete = question[:match.start()].removeprefix('請問').strip()
        if re.fullmatch(r'「[^「」]+」', complete):
            complete = complete[1:-1]
        return dict.fromkeys((complete, legacy_name, complete.rsplit('的', 1)[-1]))
    return dict.fromkeys((_question_field_name(question), legacy_name))


def _numeric_question(question, *, colloquial=False):
    return _NUMERIC_QUESTION.search(question) or (colloquial and _COLLOQUIAL_COUNT_QUESTION.search(question))


def _person_question(question, *, colloquial=False):
    return _PERSON_QUESTION.search(question) or (colloquial and _COLLOQUIAL_PERSON_QUESTION.search(question))


def _focus_question(question, *, person_names, text_literals, colloquial):
    if colloquial:
        return _COLLOQUIAL_COUNT_QUESTION.search(question) or _COLLOQUIAL_PERSON_QUESTION.search(question)
    if text_literals:
        return _TEXT_QUESTION.search(question)
    return _PERSON_QUESTION.search(question) if person_names else _NUMERIC_QUESTION.search(question)


def _language_field(question, *, person_names, text_literals, colloquial=False):
    return ((person_names and _person_question(question, colloquial=colloquial))
            or (text_literals and _TEXT_QUESTION.search(question)))


def _asked_field(part, tasks):
    if not (name := _question_field_name(part['question'])):
        return ''
    candidates = {_target(tasks[number]).removeprefix('登記') for number in part['items']}
    candidates = {item for item in candidates if name == item or name.endswith(('的' + item, '的正式' + item))}
    if len(candidates) == 1:
        return next(iter(candidates))
    # A shared question can ask a field which is not itself the title of any task.
    name = name.rsplit('的', 1)[-1].removeprefix('正式')
    return name if _FIELD_NAME.fullmatch(name) else ''


def _value(literal, *, number=_NUMBER):
    if re.fullmatch(r'「[^「」\r\n]{1,40}」', literal):
        literal = literal[1:-1].strip()
    if literal.casefold() in {'tbd', 'unknown', 'none', 'null', 'na', '有', '無', '沒有', '某人', '有人', '等等'}:
        return None
    if (number.fullmatch(literal) or _CODE.fullmatch(literal)
            or (_SHORT_WORD.fullmatch(literal) and not _UNCERTAIN.search(literal))):
        return literal
    return None


def _reply_value(text):
    if _DEFERRED.fullmatch(text):
        return 'deferred', None
    if match := _ASSIGN.fullmatch(text):
        literal = match[1]
        if _DEFERRED.fullmatch(literal):
            return 'deferred', None
        choices = re.split(r'\s*(?:或者|或是|或)\s*', literal)
        if len(choices) > 1 and all(_value(item) is not None for item in choices):
            return 'ambiguous', None
        if value := _value(literal):
            return 'confirmed', value
    return None


def _reply_parts(clauses, parts, fields, cancelled, *, single_followup):
    parsed = {}
    active_parts = [index for index, part in enumerate(parts) if set(part['items']) - cancelled]
    for start, raw, _ in clauses:
        clause = re.sub('^' + _REPLY_CONNECTOR, '', raw, count=1)
        matches = [(index, result) for index in active_parts if (name := fields[index]) and clause.startswith(name)
                   and (result := _reply_value(clause[len(name):].strip()))]
        if not matches and single_followup and len(active_parts) == 1 and len(clauses) == 1:
            index = active_parts[0]
            if (_NUMBER.fullmatch(clause) and _NUMERIC_QUESTION.search(parts[index]['question'])) or clause.startswith('「'):
                if value := _value(clause):
                    matches = [(index, ('confirmed', value))]
            elif _DEFERRED.fullmatch(clause):
                matches = [(index, ('deferred', None))]
        if len(matches) != 1 or matches[0][0] in parsed:
            return None
        index, (state, value) = matches[0]
        if value and _NUMERIC_QUESTION.search(parts[index]['question']) and not _NUMBER.fullmatch(value):
            return None
        evidence = {'start': start, 'end': start + len(raw), 'quote': raw}
        parsed[index] = {'state': state, 'evidence': evidence}
        if value is not None:
            parsed[index]['value'] = value
    return parsed


def observe_reply(steps, source, scope, prior_scope, *, version=1, delivery=None, filename_requirement=None):
    plan = work_plan.latest(steps)
    if plan is None or source['ask_sequence'] != len(steps):
        return None
    if version == artifact_names.MIXED_REPLY_VERSION:
        return _observe_filename_revision(steps, plan, source, scope, prior_scope, delivery)
    if version == CONFIRMATION_FILENAME_REPLY_VERSION:
        requirements = {row['id']: row for row in plan.get('evidence_requirements', ())}
        confirmations = work_plan._current_confirmations(requirements, steps, prior_scope)
        if not any(work_plan.confirmation_changed(steps, row) for row in confirmations.values()):
            return None
        result = _observe_filename_revision(steps, plan, source, scope, prior_scope, delivery)
        if result is None:
            return None
        proof = result['scope_reply']
        if proof.get('revisions') or proof.get('language', {}).get('pending_revisions'):
            return None
        proof['verified_confirmations'] = list(confirmations.values())
        _carry_independent_replies(proof, plan, steps, prior_scope)
        return result
    if version == artifact_names.DEFERRED_REPLY_VERSION:
        return _observe_filename_revision(steps, plan, source, scope, prior_scope, delivery, defer_filename=True)
    if version == artifact_names.REPLY_VERSION:
        if plan['goal_conditions_sha256'] != prior_scope:
            raise ValueError('定稿檔名問題的條件已改變，請重新核對工作清單。')
        choice = artifact_names.selection(steps, source, prior_scope)
        result, proof = _scope_update(steps, plan, source, [], scope, prior_scope)
        proof.update(filename_choice=choice, completed_items=[], successor=None)
        result['scope_reply'] = proof
        result['review_note'] = '依實際答覆確認定稿檔名並保留仍適用的核對證據。'
        return result
    question, parts = _waiting_parts(steps, plan)
    if not parts or source['question'] != question:
        return None
    tasks = {item['id']: item['task'] for item in plan['items']}
    if version in CORRECTION_REPLY_VERSIONS:
        return _observe_combined_correction(steps, plan, source, scope, prior_scope, parts, tasks,
                                            qualified=version != COMBINED_CORRECTION_REPLY_VERSION,
                                            bind_numeric=version >= NUMERIC_CORRECTION_REPLY_VERSION,
                                            count_units=version >= COUNT_CORRECTION_REPLY_VERSION,
                                            person_names=version >= PERSON_CORRECTION_REPLY_VERSION,
                                            text_literals=version >= TEXT_CORRECTION_REPLY_VERSION,
                                            colloquial=version == COLLOQUIAL_CORRECTION_REPLY_VERSION)
    if version in (COUNT_REPLY_VERSION, PERSON_REPLY_VERSION, TEXT_REPLY_VERSION, COLLOQUIAL_REPLY_VERSION):
        return _observe_typed_reply(steps, plan, source, scope, prior_scope, parts, tasks,
                                    person_names=version >= PERSON_REPLY_VERSION, text_literals=version >= TEXT_REPLY_VERSION,
                                    colloquial=version == COLLOQUIAL_REPLY_VERSION)
    if version == RECONFIRMATION_REPLY_VERSION:
        targets = _pending_confirmation_targets(steps, parts)
        if not targets:
            return None
        result = _observe_combined_evidence_reply(steps, plan, source, scope, prior_scope, parts, tasks,
                                                 carry=True, reconfirming=True)
        if result is not None:
            _record_reconfirmation(result['scope_reply'], targets, len(steps) + 1)
        return result
    if version in (COMBINED_EVIDENCE_REPLY_VERSION, CARRY_CONFIRMATIONS_REPLY_VERSION):
        return _observe_combined_evidence_reply(steps, plan, source, scope, prior_scope, parts, tasks,
                                                carry=version == CARRY_CONFIRMATIONS_REPLY_VERSION)
    if version == SUPPLEMENT_REPLY_VERSION:
        return _observe_supplement_reply(steps, plan, source, scope, prior_scope, parts, tasks)
    if version == PARTIAL_DOCUMENT_REPLY_VERSION:
        if prior_scope != plan['goal_conditions_sha256'] or not plan.get('verify_delivery'):
            return None
        result = _observe_direct_plan_reply(steps, plan, source, scope, prior_scope, parts, tasks)
        if not result:
            return None
        proof = result['scope_reply']
        pending = (proof['successor'] or {}).get('plan_questions', ())
        completed = set(proof['completed_items'])
        if (not completed or not pending
                or any(row['state'] not in ('confirmed', 'unanswered') for row in proof['parts'])
                or any(not set(row['items']) <= completed for row in proof['parts'] if row['state'] == 'confirmed')
                or not work_plan.are_missing_document_questions(steps, pending)):
            return None
        return result
    if version in (artifact_names.FIELD_CHOICE_REPLY_VERSION, artifact_names.FILENAME_FIRST_REPLY_VERSION,
                   artifact_names.REPLACE_REPLY_VERSION):
        return _observe_choice_with_filename(steps, plan, source, scope, prior_scope, parts, tasks, delivery,
                                             filename_requirement, version=version)
    if version in (2, 3, 4, 5):
        return _observe_natural_reply(steps, plan, source, scope, prior_scope, parts, tasks, version=version, delivery=delivery)
    fields = [_asked_field(part, tasks) for part in parts]
    prefixes = '|'.join(re.escape(name) for name in fields if name)
    reply_prefix = rf'|\s*{_REPLY_CONNECTOR}(?:{prefixes})' if prefixes else ''
    comma_before = re.compile(rf'{_SCOPE_COMMA.pattern}|\s*(?:本輪|這次|本次){_SCOPE_COMMA.pattern}{reply_prefix}')
    clauses = goal_guidance_sources.standalone_clauses(source['answer'], comma_before=comma_before)
    if not clauses or any(question_mark for _, _, question_mark in clauses):
        return None
    scope_clauses = [clause for clause in clauses if _CANCEL.fullmatch(clause[1]) or clause[1] in _UNCHANGED]
    changes = _cancellations(scope_clauses, plan)
    single_followup = (plan['sequence'] == len(steps) - 1
                       and (plan.get('scope_reply') or {}).get('successor') == steps[-1]['action']['args'])
    if not changes and (scope_clauses or not single_followup):
        return None
    cancelled = {number for change in changes for number in change['items']}
    parsed = _reply_parts([clause for clause in clauses if clause not in scope_clauses], parts, fields, cancelled,
                          single_followup=single_followup)
    if parsed is None:
        return None

    return _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed)


def _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed, *, language=None, revisions=()):
    cancelled = {number for change in changes for number in change['items']}

    result, proof = _scope_update(steps, plan, source, changes, scope, prior_scope)
    remaining, dispositions, complete = [], [], {}
    requirements = {item['id'] for item in plan.get('evidence_requirements', ())}
    for index, part in enumerate(parts):
        ids = [number for number in part['items'] if number not in cancelled]
        outcome = parsed.get(index, {'state': 'unanswered' if ids else 'cancelled'})
        dispositions.append({'question': part['question'], 'items': part['items'], 'active_items': ids, **outcome})
        if outcome['state'] == 'confirmed':
            for number in ids:
                task = tasks[number]
                if (number not in requirements and _COLLECT_FIELD.match(task)
                        and _COLLECT_FIELD.sub('', task, count=1) == fields[index]
                        and sum(number in other['items'] for other in parts) == 1):
                    complete[number] = outcome['value']
        elif ids:
            remaining.append({'question': part['question'], 'items': ids})
    if revisions:
        _apply_revisions(result, proof, complete, plan, revisions, source)
    for index, item in enumerate(result['items']):
        if item['id'] in complete:
            note = f'依 {source["citation"]} 確認：{complete[item["id"]]}。'
            result['items'][index] = {**item, 'state': 'done', 'evidence_steps': [len(steps) + 1], 'note': note}
    proof.update(ask_sequence=len(steps), ask_sha256=goal_stages.digest(steps[-1]), parts=dispositions,
                 completed_items=sorted(complete), successor=_next_question(remaining))
    if language is not None:
        waiting = _language_successor(language, remaining, dispositions, fields, tasks)
        successor = proof['successor'] = _next_question(waiting)
        if successor and len(successor['question']) > 800:
            return None
        if successor:
            try:
                validate_questions(successor)
            except ValueError:
                return None
        proof['language'] = language
    result['scope_reply'] = proof
    result['review_note'] = '分開核對原回覆內的範圍變更、確認值與待答分問。'
    return result


def _literal_span(start, clause):
    return {'start': start, 'end': start + len(clause), 'quote': clause}


def _accepted_value_span(value, evidence):
    # The parser accepted the trailing literal, after any field label or qualifier.
    offset = evidence['quote'].rindex(value)
    return _literal_span(evidence['start'] + offset, value)


def _cancellation_names(clause):
    match = _CANCEL.fullmatch(clause) or _POST_CANCEL.fullmatch(clause)
    if match is None:
        return []
    names = match[1].strip()
    return re.findall(r'「([^「」]+)」', names) if _TITLES.fullmatch(names) else [names]


def _candidate_items(name, tasks):
    exact = [number for number, task in tasks.items() if task == name]
    if exact or not _FIELD_NAME.fullmatch(name):
        return exact
    return [number for number, task in tasks.items() if _target(task).endswith(name)]


def _natural_scope(clauses, plan, tasks, pending, source):
    active = {item['id'] for item in plan['items'] if item['state'] != 'superseded'}
    fields, changes, used = [], [], set()
    resolution = None
    awaiting_choice = pending
    for start, clause, _ in clauses:
        evidence = _literal_span(start, clause)
        names = _cancellation_names(clause)
        if awaiting_choice and resolution is None:
            selected = clause.removeprefix('「').removesuffix('」')
            keep_all = clause in _KEEP_ALL
            choices = [] if keep_all else [number for number in awaiting_choice['items'] if tasks[number] == selected
                                           or names == [tasks[number]]]
            if len(choices) == 1 or keep_all:
                resolution = {'items': choices, 'evidence': evidence, 'from': awaiting_choice['citation']}
                if choices:
                    changes.append({'items': choices, **evidence})
                    used.update(choices)
                continue
        if clause in _UNCHANGED:
            continue
        if not names:
            fields.append((start, clause))
            continue
        ids = []
        for name in names:
            matches = _candidate_items(name, tasks)
            if len(matches) > 1:
                if pending or len(names) != 1 or not set(matches) <= active or len({tasks[n] for n in matches}) != len(matches):
                    return None
                pending = {'items': matches, 'evidence': evidence, 'citation': source['citation']}
            elif len(matches) != 1 or matches[0] in used or matches[0] not in active:
                return None
            else:
                ids.append(matches[0])
                used.add(matches[0])
        if ids:
            changes.append({'items': ids, **evidence})
    if resolution:
        if not resolution['items'] and set(awaiting_choice['items']) & used:
            return None
        pending = None
    if pending and set(pending['items']) & used:
        return None
    return changes, fields, pending, resolution


def _bare_value(clause, *, numeric):
    literal = clause
    if numeric:
        if match := re.fullmatch(r'(?:就用|用|以)\s*(.+?)(?:為準)?', literal):
            literal = match[1].strip()
        return _value(literal) if _NUMBER.fullmatch(literal) else None
    return _value(literal) if clause.startswith('「') else None


def _expected_count_unit(question, *, colloquial):
    if colloquial:
        if asked := _COLLOQUIAL_COUNT_QUESTION.search(question):
            return asked['unit']
        if _question_field_name(question).endswith('號'):
            return '號'
    expected = _COUNT_FIELD_UNIT.search(_question_field_name(question))
    return expected[1] if expected else None


def _number_matches(value, question, number, *, colloquial=False):
    match = number.fullmatch(value)
    if match is None or number is _NUMBER:
        return match is not None
    unit = match['unit']
    expected = _expected_count_unit(question, colloquial=colloquial)
    if ((expected and expected in _COUNT_UNITS) or (unit and unit in _COUNT_UNITS)) and _ADJACENT_DIGITS.search(match['amount']):
        return False
    return not unit or not expected or unit == expected or {unit, expected} in ({'人', '位'}, {'號', '号'})


def _count_reply_value(text, question, *, bare=False, colloquial=False):
    if match := _ASSIGN.fullmatch(text):
        literal = match[1].strip()
    elif bare and (match := re.fullmatch(r'(?:就用|用|以)\s*(.+?)(?:為準)?', text)):
        literal = match[1].strip()
    else:
        literal = text
    if _DEFERRED.fullmatch(literal):
        return 'deferred', None
    if value := _value(literal, number=_COUNT_NUMBER):
        if _COUNT_NUMBER.fullmatch(value):
            return ('confirmed', value) if _number_matches(
                value, question, _COUNT_NUMBER, colloquial=colloquial) else ('ambiguous', None)
    choices = re.split(r'\s*(?:或者|或是|或)\s*', literal)
    numeric_choices = len(choices) > 1 and all(
        _COUNT_NUMBER.fullmatch(_value(item, number=_COUNT_NUMBER) or '') for item in choices)
    if numeric_choices or _MIXED_COUNTS.fullmatch(literal) or _UNCERTAIN_COUNT.fullmatch(literal):
        return 'ambiguous', None
    return None


def _person_pending(text):
    return (text in _PERSON_PLACEHOLDER or _DEFERRED.fullmatch(text) is not None
            or _PERSON_MODAL.search(text) is not None or _PERSON_UNCONFIRMED_END.search(text) is not None)


def _person_literal(text):
    name = text[1:-1].strip() if re.fullmatch(r'「[^「」]+」', text) else text
    if _person_pending(name) or '或' in name or _PERSON_NONNAME.search(name):
        return None
    return _value(text) or (name if _PERSON_NAME.fullmatch(name) else None)


def _person_reply_value(text, *, bare=False, question=False):
    assignment = _ASSIGN.fullmatch(text)
    literal = assignment[1] if assignment else text
    plain = literal[1:-1].strip() if re.fullmatch(r'「[^「」]+」', literal) else literal
    if question or _person_pending(plain):
        return 'deferred', None
    choices = re.split(r'\s*(?:或者|或是|或|、)\s*', plain)
    if len(choices) > 1 and all(_person_literal(choice) for choice in choices):
        return 'ambiguous', None
    bare_name = bare and (_PERSON_NAME.fullmatch(literal) or re.fullmatch(r'「[^「」]+」', literal))
    if (assignment or bare_name) and (name := _person_literal(literal)):
        return 'confirmed', name
    return None


def _text_pending(value):
    return (value.casefold() in _TEXT_PLACEHOLDER or _person_pending(value)
            or _TEXT_STATUS.search(value) is not None or _TEXT_STATUS_END.search(value) is not None
            or _TEXT_QUALIFIER.search(value) is not None)


def _text_literal(text):
    value = text[1:-1].strip() if re.fullmatch(r'「[^「」]+」', text) else text
    if _text_pending(value) or '或' in value or '、' in value:
        return None
    return value if _TEXT_LITERAL.fullmatch(value) else None


def _text_reply_value(text, question, label, *, bare=False, inquiry=False):
    assignment = _ASSIGN.fullmatch(text)
    if not assignment and (_PLACE_QUESTION.search(question) or _PLACE_FIELD.search(label)):
        assignment = _PLACE_ASSIGN.fullmatch(text)
    literal = assignment[1] if assignment else text
    quoted = re.fullmatch(r'「([^「」]+)」', literal)
    value = quoted[1].strip() if quoted else literal
    if inquiry or _text_pending(value):
        return 'deferred', None
    choices = re.split(r'\s*(?:或者|或是|或|、)\s*', value)
    if len(choices) > 1 and all(_text_literal(choice) for choice in choices):
        return 'ambiguous', None
    if (assignment or (bare and quoted)) and (accepted := _text_literal(literal)):
        return 'confirmed', accepted
    return None


def _bound_field_answer(clause, question, name, quoted, *, inquiry, person, literal, colloquial):
    for label in filter(None, _bound_field_names(question, name, text_literals=literal, colloquial=colloquial)):
        if (person or literal) and quoted and quoted[1].startswith(label):
            rest = quoted[1][len(label):].strip()
            if (_person_reply_value(rest) if person else _text_reply_value(rest, question, label)):
                return 'deferred', None
        quoted_label = f'「{label}」'
        prefix = quoted_label if (person or literal) and clause.startswith(quoted_label) else label
        if not clause.startswith(prefix):
            continue
        rest = clause[len(prefix):].strip()
        if person:
            outcome = _person_reply_value(rest, question=inquiry)
        elif literal:
            outcome = _text_reply_value(rest, question, label, inquiry=inquiry)
        else:
            outcome = _count_reply_value(rest, question, colloquial=colloquial)
        if outcome:
            return outcome
    return None


def _natural_fields(clauses, parts, fields, cancelled, *, allow_bare=True, count_units=False,
                    person_names=False, text_literals=False, colloquial=False, question_offsets=()):
    statements, unassigned = {}, []
    active = [index for index, part in enumerate(parts) if set(part['items']) - cancelled]
    for start, raw in clauses:
        clause = re.sub('^' + _REPLY_CONNECTOR, '', raw, count=1)
        evidence = _literal_span(start, raw)
        matches = []
        quoted = _PERSON_QUOTE.fullmatch(clause) if person_names or text_literals else None
        for index in active:
            question = parts[index]['question']
            numeric = bool(_numeric_question(question, colloquial=colloquial))
            person = person_names and _person_question(question, colloquial=colloquial)
            literal = text_literals and _TEXT_QUESTION.search(question)
            name = fields[index]
            if person or literal or (count_units and numeric and start not in question_offsets):
                outcome = _bound_field_answer(clause, question, name, quoted, inquiry=start in question_offsets,
                                              person=person, literal=literal, colloquial=colloquial)
                if outcome:
                    matches.append((index, outcome))
            elif start not in question_offsets and name and clause.startswith(name):
                rest = clause[len(name):].strip()
                outcome = _reply_value(rest)
                if outcome is None and numeric and _NUMBER.fullmatch(rest):
                    outcome = ('confirmed', rest)
                if outcome and (not numeric or outcome[1] is None or _NUMBER.fullmatch(outcome[1])):
                    matches.append((index, outcome))
        if not matches and allow_bare:
            mismatched = set()
            bare_literal = _value(clause, number=_COUNT_NUMBER) if colloquial else None
            supplied_unit = _COUNT_NUMBER.fullmatch(bare_literal or '')
            for index in active:
                question = parts[index]['question']
                numeric = bool(_numeric_question(question, colloquial=colloquial))
                if person_names and _person_question(question, colloquial=colloquial):
                    if len(active) == 1 and (outcome := _person_reply_value(
                            clause, bare=True, question=start in question_offsets)):
                        matches.append((index, outcome))
                elif text_literals and _TEXT_QUESTION.search(question):
                    if len(active) == 1 and (outcome := _text_reply_value(
                            clause, question, fields[index], bare=True, inquiry=start in question_offsets)):
                        matches.append((index, outcome))
                elif start in question_offsets:
                    continue
                elif count_units and numeric:
                    if (outcome := _count_reply_value(clause, question, bare=True, colloquial=colloquial)) and outcome[0] != 'deferred':
                        matches.append((index, outcome))
                        if supplied_unit and supplied_unit['unit'] and outcome[0] == 'ambiguous':
                            mismatched.add(index)
                elif value := _bare_value(clause, numeric=numeric):
                    matches.append((index, ('confirmed', value)))
            if mismatched and any(
                    outcome[0] == 'confirmed' and _expected_count_unit(parts[index]['question'], colloquial=True)
                    for index, outcome in matches):
                matches = [(index, outcome) for index, outcome in matches if index not in mismatched]
            if len(matches) > 1:
                unassigned.append({'evidence': evidence, 'questions': [parts[index]['question'] for index, _ in matches]})
                continue
            if not matches and len(active) == 1 and _DEFERRED.fullmatch(clause):
                matches = [(active[0], ('deferred', None))]
        if count_units and len(matches) > 1 and all(
                _numeric_question(parts[index]['question'], colloquial=colloquial)
                or _language_field(parts[index]['question'], person_names=person_names, text_literals=text_literals,
                                   colloquial=colloquial)
                for index, _ in matches):
            unassigned.append({'evidence': evidence, 'questions': [parts[index]['question'] for index, _ in matches]})
            continue
        if len(matches) != 1:
            return None
        index, (state, value) = matches[0]
        statement = {'state': state, 'evidence': evidence}
        if value is not None:
            statement['value'] = value
        statements.setdefault(index, []).append(statement)
    parsed = {}
    for index, rows in statements.items():
        number = _COUNT_NUMBER if count_units else _NUMBER
        identities = {(row['state'], _value_identity(row.get('value'), number=number)) for row in rows}
        if len(identities) == 1:
            parsed[index] = {**rows[0], **({'statements': rows} if len(rows) > 1 else {})}
        else:
            parsed[index] = {'state': 'conflicting', 'statements': rows}
    return parsed, unassigned


def _observe_supplement_reply(steps, plan, source, scope, prior_scope, parts, tasks):
    if (not is_supplement_followup(steps) or prior_scope != steps[-1]['observation']['goal_conditions_sha256']):
        return None
    result = _observe_direct_plan_reply(steps, plan, source, scope, prior_scope, parts, tasks)
    if result is not None:
        result['scope_reply']['supplement_step'] = steps[-2]['sequence']
    return result


def _observe_direct_plan_reply(steps, plan, source, scope, prior_scope, parts, tasks):
    fields = [_asked_field(part, tasks) for part in parts]
    clauses = goal_guidance_sources.standalone_clauses(source['answer'], comma_before=_PLAIN_COMMA)
    if not clauses or any(question for _, _, question in clauses):
        return None
    parsed = _direct_reply_fields([(start, clause) for start, clause, _ in clauses], parts, fields)
    if parsed is None:
        return None
    return _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, [], parsed)


def _direct_reply_fields(clauses, parts, fields):
    statements = _natural_fields(clauses, parts, fields, set())
    if statements is None:
        return None
    parsed, unassigned = statements
    if unassigned or not any(row['state'] == 'confirmed' for row in parsed.values()):
        return None
    return parsed


def _pending_confirmation_targets(steps, parts):
    pending = work_plan.confirmation_withdrawals(steps)
    return {number: pending[number] for part in parts for number in part['items']
            if number in pending and pending[number]['question'] == part['question'] and 'reply_step' not in pending[number]}


def _record_reconfirmation(proof, targets, sequence):
    proof['reconfirmed_withdrawals'] = [
        {'id': row['id'], 'question': row['question'], 'withdrawal_step': targets[row['id']]['withdrawal_step']}
        for row in proof['verified_confirmations'] if row['id'] in targets and row['reply_step'] == sequence]


def _observe_combined_evidence_reply(steps, plan, source, scope, prior_scope, parts, tasks, *, carry=False, reconfirming=False):
    if not plan.get('verify_delivery') or (
            not reconfirming and prior_scope != steps[-1]['observation'].get('goal_conditions_sha256')):
        return None
    requirements = {row['id']: row for row in plan.get('evidence_requirements', ())}
    required = {number: row['confirmation_question'] for number, row in requirements.items()
                if 'confirmation_question' in row}
    prior = work_plan._current_confirmations(requirements, steps, prior_scope) if carry else {}
    if carry and not prior and not reconfirming:
        return None
    if not prior and not any(required.get(number) == part['question'] for part in parts for number in part['items']):
        return None
    result = _observe_direct_plan_reply(steps, plan, source, scope, prior_scope, parts, tasks)
    if result is None:
        return None
    proof = result['scope_reply']
    confirmations = {number: row for number, row in prior.items() if any(
        item['id'] == number and item['state'] != 'superseded' for item in plan['items'])}
    if not _verify_reply_parts(proof, required, confirmations, source, len(steps) + 1, carry=carry):
        return None
    if not confirmations and not reconfirming:
        return None
    proof['verified_confirmations'] = list(confirmations.values())
    if carry:
        _carry_independent_replies(proof, plan, steps, prior_scope)
    return result


def _observe_typed_reply(steps, plan, source, scope, prior_scope, parts, tasks, *,
                         person_names=False, text_literals=False, colloquial=False):
    if (not plan.get('verify_delivery') or 'plan_questions' not in steps[-1]['action']['args']
            or prior_scope != steps[-1]['observation'].get('goal_conditions_sha256')
            or language_followup(steps) or pending_confirmation_target(steps)):
        return None
    requirements = {row['id']: row for row in plan.get('evidence_requirements', ())}
    required = {number: row['confirmation_question'] for number, row in requirements.items()
                if 'confirmation_question' in row}
    selected_questions = {
        part['question'] for part in parts if _focus_question(
            part['question'], person_names=person_names, text_literals=text_literals, colloquial=colloquial)
        and all(required.get(number) == part['question'] for number in part['items'])}
    if not selected_questions:
        return None
    fields = [_asked_field(part, tasks) for part in parts]
    comma_before = _TEXT_COMMA if text_literals else _COUNT_COMMA
    clauses = goal_guidance_sources.standalone_clauses(source['answer'], comma_before=comma_before)
    question_offsets = {start for start, _, question in clauses if question}
    if not clauses or (question_offsets and not (person_names or text_literals)):
        return None
    statements = _natural_fields([(start, clause) for start, clause, _ in clauses], parts, fields, set(),
                                 count_units=True, person_names=person_names, text_literals=text_literals,
                                 colloquial=colloquial, question_offsets=question_offsets)
    if statements is None:
        return None
    parsed, unassigned = statements
    allowed_questions = selected_questions
    if person_names:
        allowed_questions = selected_questions | {
            part['question'] for part in parts if (
                _numeric_question(part['question'], colloquial=colloquial)
                or (text_literals and _person_question(part['question'], colloquial=colloquial))
                or (colloquial and _TEXT_QUESTION.search(part['question'])))
            and all(required.get(number) == part['question'] for number in part['items'])}
    if any(not set(row['questions']) <= allowed_questions for row in unassigned):
        return None
    if not any(selected_questions.intersection(row['questions']) for row in unassigned) and not any(
            parts[index]['question'] in selected_questions and (
                text_literals or row['state'] != 'confirmed' or
                (not _value(row['value']) if person_names else not _NUMBER.fullmatch(row['value']))
                or (_question_field_name(parts[index]['question']) != fields[index]
                    and _question_field_name(parts[index]['question']) in row['evidence']['quote']))
            for index, row in parsed.items()):
        # A routine scalar answer can keep the earlier receipt.
        return None
    withdrawals = _pending_confirmation_targets(steps, parts)
    if person_names and withdrawals and not any(row['state'] == 'confirmed' for row in parsed.values()):
        return None  # Keep the existing save gate for a withdrawn answer with no new confirmation.
    current = work_plan._current_confirmations(requirements, steps, prior_scope)
    active = {row['id'] for row in plan['items'] if row['state'] != 'superseded'}
    confirmations = {number: row for number, row in current.items() if number in active}
    result = _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, [], parsed)
    proof = result['scope_reply']
    sequence = len(steps) + 1
    if not _verify_reply_parts(proof, required, confirmations, source, sequence,
                               carry=True, keep_counts_pending=True, keep_people_pending=person_names,
                               keep_text_pending=text_literals, colloquial=colloquial):
        return None
    proof['verified_confirmations'] = list(confirmations.values())
    if unassigned:
        numeric_unassigned = [row for row in unassigned if all(
            _numeric_question(question, colloquial=colloquial) for question in row['questions'])]
        if numeric_unassigned:
            proof['unassigned_counts'] = numeric_unassigned
        if text_literals:
            text_unassigned = [row for row in unassigned if any(_TEXT_QUESTION.search(question) for question in row['questions'])]
            if text_unassigned:
                proof['unassigned_text'] = text_unassigned
            unassigned = [row for row in unassigned if row not in text_unassigned]
        if len(numeric_unassigned) != len(unassigned):
            proof['unassigned_people'] = [row for row in unassigned if row not in numeric_unassigned]
    if withdrawals:
        _record_reconfirmation(proof, withdrawals, sequence)
    _carry_independent_replies(proof, plan, steps, prior_scope)
    return result


def _verify_reply_parts(proof, required, confirmations, source, sequence, *, carry,
                        keep_counts_pending=False, keep_people_pending=False, keep_text_pending=False, colloquial=False):
    for part in proof['parts']:
        if part['state'] == 'unanswered':
            continue
        typed = all(required.get(number) == part['question'] for number in part['items'])
        recognized = typed and ((keep_counts_pending and _numeric_question(part['question'], colloquial=colloquial))
                                or _language_field(part['question'], person_names=keep_people_pending,
                                                   text_literals=keep_text_pending, colloquial=colloquial))
        if part['state'] != 'confirmed':
            if recognized:
                continue
            return False
        if recognized:
            part['value_evidence'] = _accepted_value_span(part['value'], part['evidence'])
        for number in part['items']:
            if required.get(number) == part['question']:
                confirmations[number] = {'id': number, 'question': part['question'], 'value': part['value'],
                                         'reply_step': sequence, 'citation': source['citation'], 'sha256': source['sha256']}
            elif not carry or number not in proof['completed_items']:
                return False
    return True


def pending_confirmation_target(steps):
    plan = work_plan.latest(steps)
    if plan and plan['sequence'] == len(steps) - 1:
        receipt = _scope_receipt(plan)
        ask = steps[-1]['action']
        if ask['tool'] == 'ask' and receipt.get('successor') == ask['args']:
            return receipt.get('confirmation_target_choice')
    return None


def _target_choice_part(choice):
    names = '、'.join(f'「{question}」' for question in choice['questions'])
    return {'question': (f'「{choice["field"]}」指的是哪一欄？可選 {names}。'
                         f'請用完整欄名加上「改為{choice["value"]}」回覆，或回答「都不改」。'),
            'items': choice['items']}


def _resolve_confirmation_target(pending, requested, remaining):
    keep = [(start, clause) for start, clause in remaining if clause in _KEEP_VALUE]
    if len(keep) == 1 and not requested:
        start, clause = keep[0]
        remaining.remove(keep[0])
        return {'step': pending['step'], 'decision': 'keep', 'evidence': _literal_span(start, clause)}
    if not keep and len(requested) == 1 and (question := next(iter(requested))) in pending['questions']:
        return {'step': pending['step'], 'decision': 'correct', 'question': question}
    if keep or requested:
        raise ValueError('請指定目前列出的一個完整欄名及明確新值，或回答「都不改」。')
    return None


def _observe_combined_correction(steps, plan, source, scope, prior_scope, parts, tasks, *,
                                 qualified=False, bind_numeric=False, count_units=False,
                                 person_names=False, text_literals=False, colloquial=False):
    if (prior_scope != steps[-1]['observation'].get('goal_conditions_sha256')
            or 'plan_questions' not in steps[-1]['action']['args']
            or (language_followup(steps) or {}).get('cancellation')):
        return None
    targets = confirmation_targets(steps, prior_scope)
    if not any(target['confirmed'] for target in targets.values()):
        return None
    pending = pending_confirmation_target(steps) if qualified else None
    if pending:
        parts = parts[1:]  # The generated target question is separate from the original open fields.
    def focused(question):
        return _focus_question(question, person_names=person_names, text_literals=text_literals, colloquial=colloquial)

    needs_focus = person_names or text_literals or colloquial
    focus_in_current_parts = colloquial and any(focused(part['question']) for part in parts)
    if needs_focus and not focus_in_current_parts and not any(
            focused(question) and (
                question in (pending or {}).get('questions', ())
                or any(label and label in source['answer'] for label in
                       _bound_field_names(question, target['field'], text_literals=text_literals, colloquial=colloquial)))
            for question, target in targets.items()):
        return None
    if count_units and not person_names and not _ADDITIONAL_COUNT.search(source['answer']):
        if not any((unit := _COUNT_FIELD_UNIT.search(_question_field_name(question)))
                   and unit[1] in _ADDITIONAL_COUNT_UNITS for question in targets):
            return None
    if qualified and not count_units and not pending and all(
            _question_field_name(question) == target['field']
            and _confirmation_field_matches(target['field'], targets, qualified=True) == [question]
            for question, target in targets.items()):
        return None
    comma_before = _QUALIFIED_REVISION_COMMA if qualified else _REVISION_COMMA
    if text_literals:
        comma_before = _TEXT_REVISION_COMMA
    elif count_units:
        comma_before = _COUNT_REVISION_COMMA
    clauses = goal_guidance_sources.standalone_clauses(source['answer'], comma_before=comma_before)
    if not clauses or any(question for _, _, question in clauses):
        return None
    number = _COUNT_NUMBER if count_units else _NUMBER
    try:
        requested, remaining, choice = _confirmation_requests(clauses, targets, allow_withdrawal=False,
                                                               qualified=qualified, number=number, person_names=person_names,
                                                               text_literals=text_literals, colloquial=colloquial)
    except ValueError:
        if qualified:
            raise
        return None
    if not (requested or choice or pending):
        return None
    if bind_numeric and not count_units and not any(number.fullmatch(value) for value, _ in requested.values()):
        return None
    if qualified and not count_units and not (choice or pending or any(
            _question_field_name(question) != targets[question]['field']
            or len(_confirmation_field_matches(targets[question]['field'], targets, qualified=True)) > 1
            for question in requested)):
        return None

    open_questions = {part['question'] for part in parts}
    if any(not targets[question]['confirmed'] or question in open_questions for question in requested):
        return None
    if choice and (pending or set(choice['questions']) & (requested.keys() | open_questions)):
        raise ValueError('有多個工作分問使用這個欄名，請逐字指定要更正的原問句。')
    resolution = _resolve_confirmation_target(pending, requested, remaining) if pending else None
    if pending and not resolution:
        choice = pending
    fields = [_asked_field(part, tasks) for part in parts]
    if count_units:
        statements = _natural_fields(remaining, parts, fields, set(), count_units=True, person_names=person_names,
                                     text_literals=text_literals, colloquial=colloquial)
        parsed = statements[0] if statements is not None and not statements[1] else None
    else:
        parsed = _direct_reply_fields(remaining, parts, fields) if remaining else {}
    if parsed is None or (pending and not (resolution or parsed)):
        if pending or choice:
            raise ValueError('請分開寫明要回答的欄位。待選更正可指定完整欄名及新值，或回答「都不改」。')
        return None
    if needs_focus and not any(focused(question) for question in (
            *requested, *(choice or {}).get('questions', ()), *(pending or {}).get('questions', ()),
            *(parts[index]['question'] for index in parsed))):
        return None
    revisions = _confirmation_revisions(steps, plan, targets, requested,
                                         qualified=qualified, bind_numeric=bind_numeric, number=number,
                                         person_names=person_names, text_literals=text_literals, colloquial=colloquial)
    result = _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, [], parsed, revisions=revisions)
    proof = result['scope_reply']
    sequence = len(steps) + 1
    confirmations = _updated_confirmations(targets, revisions, source, sequence)
    required = {number: question for question, target in targets.items() for number in target['items']}
    if not _verify_reply_parts(proof, required, confirmations, source, sequence,
                                carry=True, keep_counts_pending=count_units, keep_people_pending=person_names,
                                keep_text_pending=text_literals, colloquial=colloquial):
        return None
    proof['verified_confirmations'] = list(confirmations.values())
    if choice:
        choice = {**choice, 'step': choice.get('step', sequence)}
        next_parts = (proof['successor'] or {}).get('plan_questions', [])
        successor = _next_question([_target_choice_part(choice), *next_parts])
        if len(successor['question']) > 800:
            raise ValueError('待答問題過長，請用完整欄名及新值直接更正。')
        validate_questions(successor)
        proof.update(confirmation_target_choice=choice, successor=successor)
    if resolution:
        proof['confirmation_target_resolution'] = resolution
    if withdrawals := _pending_confirmation_targets(steps, parts):
        _record_reconfirmation(proof, withdrawals, sequence)
    _carry_independent_replies(proof, plan, steps, prior_scope)
    return result


def _value_identity(value, *, number=_NUMBER):
    if value is not None and number.fullmatch(value):
        return ''.join(unicodedata.normalize('NFKC', value).split())
    return value


def _language_successor(language, remaining, dispositions, fields, tasks):
    by_question = {part['question']: (part, fields[index]) for index, part in enumerate(dispositions)}
    previous = {entry['part']['question']: entry['display'] for entry in language.pop('previous_waiting')}
    waiting = []
    for part in remaining:
        outcome, field = by_question[part['question']]
        display = previous.get(part['question'], part['question'])
        if outcome['state'] == 'conflicting':
            values = list(dict.fromkeys(row['value'] for row in outcome['statements'] if 'value' in row))
            if field and len(values) == 2:
                display = f'{field}要以「{values[0]}」還是「{values[1]}」為準？'
        waiting.append({'part': part, 'display': display})
    language['waiting'] = waiting
    return _show_waiting(language, tasks)


def _retain_language(previous, cancelled):
    waiting = []
    for entry in previous['waiting']:
        part = entry['part']
        ids = [number for number in part['items'] if number not in cancelled]
        if ids:
            waiting.append({**entry, 'part': {**part, 'items': ids}})
    pending = previous['cancellation']
    if pending and set(pending['items']) & cancelled:
        pending = None
    retained = {'waiting': waiting, 'cancellation': pending}
    if 'pending_revisions' in previous:
        retained['pending_revisions'] = _trim_revision_choices(previous['pending_revisions'], cancelled)
    return retained


def _show_waiting(language, tasks):
    if cancellation := language['cancellation']:
        ids = cancellation['items']
        choices = '或'.join(f'「{tasks[number]}」' for number in ids)
        return [{'question': f'要取消哪一項？請選{choices}，也可回答「都保留」。', 'items': ids}]
    if pending := language.get('pending_revisions'):
        choice = pending[0]
        old = choice['previous']['value']
        values = '或'.join(f'「{row["value"]}」' for row in choice['options'])
        return [{'question': f'{choice["field"]}要改為{values}，還是沿用「{old}」？', 'items': choice['items']}]
    return [{**entry['part'], 'question': entry['display']} for entry in language['waiting']]


def _observe_natural_reply(steps, plan, source, scope, prior_scope, parts, tasks, *, version, delivery, filename=None, deferral=None):
    receipt = _scope_receipt(plan)
    follows = plan['sequence'] == len(steps) - 1 and receipt.get('successor') == steps[-1]['action']['args']
    previous = receipt.get('language', {}) if follows else {}
    waiting = previous.get('waiting', [])
    if previous:
        parts = [entry['part'] for entry in waiting]
    text = _without_delivery(source['answer'], [span for span in (filename, deferral) if span]) if filename else source['answer']
    if version == 5:
        if (not delivery or prior_scope != plan['goal_conditions_sha256']
                or not (follows or 'plan_questions' in steps[-1]['action']['args'])):
            return None
        text = _without_delivery(text, delivery['receipt']['plan_parts'])
    comma = _FILENAME_REPLY_COMMA if filename else {3: _REVISION_COMMA, 4: _PENDING_COMMA, 5: _PENDING_COMMA}.get(version, _PLAIN_COMMA)
    clauses = goal_guidance_sources.standalone_clauses(text, comma_before=comma)
    if (not clauses and version != 5 and not filename) or any(question_mark for _, _, question_mark in clauses):
        return None
    declined = []
    if version in (4, 5):
        declined = [_literal_span(start, clause) for start, clause, _ in clauses if _DECLINED_PROPOSAL.fullmatch(clause)]
        clauses = [entry for entry in clauses if not _DECLINED_PROPOSAL.fullmatch(entry[1])]
    scope_parts = _natural_scope(clauses, plan, tasks, previous.get('cancellation'), source)
    if scope_parts is None:
        return None
    changes, field_clauses, pending, resolution = scope_parts
    if not (changes or pending or resolution or follows or version == 5):
        return None
    cancelled = {number for change in changes for number in change['items']}
    language = {'cancellation': pending, 'resolution': resolution, 'unassigned': [], 'previous_waiting': waiting}
    revisions = ()
    if version in (3, 4, 5):
        if (version != 5 and not follows) or prior_scope != plan['goal_conditions_sha256']:
            return None
        if version in (4, 5):
            revised = _parse_revision_choices(field_clauses, steps, plan, tasks, cancelled, previous, source, declined,
                                             delivery_only_ok=version == 5)
        else:
            revised = _parse_revisions(field_clauses, steps, plan, tasks, cancelled)
        if revised is None:
            return None
        field_clauses, revisions = revised[:2]
        if version in (4, 5):
            language.update(revised[2])
    fields = [_asked_field(part, tasks) for part in parts]
    explicit = version in (4, 5) and bool(previous.get('pending_revisions') or previous.get('cancellation'))
    replies = _natural_fields(field_clauses, parts, fields, cancelled, allow_bare=not explicit)
    if replies is None:
        return None
    parsed, unassigned = replies
    language['unassigned'] = unassigned
    result = _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed,
                           language=language, revisions=revisions)
    if result and version == 5:
        result['scope_reply']['delivery_mode'] = delivery
    return result


def _field_deferral(clause, field):
    name = re.escape(field)
    subject = rf'(?:{name}|「{name}」|這欄|這個欄位|目前欄位)'
    return re.fullmatch(rf'{subject}\s*{_FIELD_DELAY}|先不(?:決定|選擇|確認)\s*{subject}', clause)


def _observe_choice_with_filename(steps, plan, source, scope, prior_scope, parts, tasks, delivery, requirement, *, version):
    language = language_followup(steps) or {}
    if (prior_scope != plan['goal_conditions_sha256'] or not language.get('pending_revisions')
            or language.get('cancellation') or not requirement):
        return None
    replace = version == artifact_names.REPLACE_REPLY_VERSION
    defer_field = version == artifact_names.FILENAME_FIRST_REPLY_VERSION
    text = source['answer']
    if (defer_field or replace) and delivery:
        text = _without_delivery(text, delivery['receipt']['plan_parts'])
    comma = _FILENAME_FIRST_COMMA if defer_field or replace else _FILENAME_REPLY_COMMA
    clauses = goal_guidance_sources.standalone_clauses(text, comma_before=comma)
    names, deferral = [], None
    field = language['pending_revisions'][0]['field']
    for start, clause, question in clauses:
        if not question:
            name = (artifact_names.replacement_declaration(clause, start) if replace
                    else artifact_names.followup_declaration(clause, start))
            if not name and defer_field:
                name = artifact_names.bare_filename(clause, start)
            if name:
                names.append(name)
                continue
            if (defer_field or replace) and deferral is None and _field_deferral(clause, field):
                deferral = _literal_span(start, clause)
                continue
        if replace and (question or artifact_names.followup_declaration(clause, start) or artifact_names.bare_filename(clause, start)):
            return None
        if defer_field:
            return None
    if len(names) != 1:
        return None
    bound = artifact_names.confirmed_choice(steps, requirement) if replace else artifact_names.deferred_question(steps, requirement)
    if bound is None:
        return None
    name = names[0]
    result = _observe_natural_reply(steps, plan, source, scope, prior_scope, parts, tasks,
                                    version=5 if delivery else 4, delivery=delivery, filename=name, deferral=deferral)
    if not result:
        return None
    reply = result['scope_reply']
    resolved = reply['language']['revision_resolution']
    if defer_field or (replace and not resolved):
        if (reply['successor'] != steps[-1]['action']['args']
                or reply.get('revisions') or reply['changes'] or reply['language']['waiting'] != language['waiting']
                or reply['language']['pending_revisions'] != language['pending_revisions']):
            return None
        if deferral:
            reply['field_deferral'] = {'field': field, 'evidence': deferral}
    elif not resolved or deferral:
        return None
    ask, proof = bound[:2]
    reply['filename_choice'] = artifact_names.confirm_filename(steps, ask, proof, name)
    if replace:
        reply['filename_revision'] = bound[2]
    return result


def _confirmed_fields(steps, plan, tasks):
    chain = []
    sequence = plan['sequence']
    while proof := _scope_receipt(steps[sequence - 1]['observation']):
        chain.append((steps[sequence - 1], proof))
        prior = proof['plan_step']
        if not 1 <= prior < sequence or proof['prior_scope'] != steps[prior - 1]['observation'].get('goal_conditions_sha256'):
            break
        sequence = prior
    confirmed = {}
    for step, proof in reversed(chain):
        origin = {'step': step['sequence'], 'step_sha256': goal_stages.digest(step), 'citation': proof['citation'],
                  'source_sha256': proof['source_sha256']}
        for part in proof.get('parts', ()):
            field = _asked_field(part, tasks)
            if field and part['state'] == 'confirmed':
                ids = part['active_items']
                collected = sorted(set(ids) & set(proof['completed_items']))
                confirmed[part['question']] = {'field': field, 'items': ids, 'collected_items': collected,
                                               'value': part['value'], 'evidence': part['evidence'], **origin}
        for revision in proof.get('revisions', ()):
            confirmed[revision['question']] = {**{key: revision[key] for key in ('field', 'items', 'collected_items',
                                                                                'value', 'evidence', 'operand_paths')}, **origin}
    return confirmed


def _dependent_operands(plan, previous, fields):
    paths = {row['id']: set(row['paths']) for row in previous.get('operand_paths', ())}
    for row in plan.get('evidence_requirements', ()):
        for operand in row.get('operands', ()):
            if operand.get('field') in fields:
                paths.setdefault(row['id'], set()).add(operand['path'])
    evidence = previous.get('value_evidence', previous['evidence'])
    for row in plan.get('evidence_matches', ()):
        for operand in row.get('operands', ()):
            if (operand.get('source_sha256') == previous['source_sha256'] and operand.get('citation') == previous['citation']
                    and evidence['start'] <= operand['number_start'] < operand['number_end'] <= evidence['end']):
                paths.setdefault(row['id'], set()).add(operand['path'])
    return paths


def _parse_revisions(clauses, steps, plan, tasks, cancelled):
    if not any(_REVISION.fullmatch(clause) for _, clause in clauses):
        return None
    confirmed = _confirmed_fields(steps, plan, tasks)
    active = {row['id']: row for row in plan['items'] if row['state'] != 'superseded' and row['id'] not in cancelled}
    remaining, revisions, seen = [], [], set()
    for start, clause in clauses:
        match = _REVISION.fullmatch(clause)
        if match is None:
            remaining.append((start, clause))
            continue
        field = (match[1] or match[2]).strip()
        options = [(question, row) for question, row in confirmed.items() if row['field'] == field
                   and set(row['items']) & active.keys()]
        value = _value(match[3].strip())
        if len(options) != 1 or field in seen or value is None:
            return None
        question, previous = options[0]
        if _NUMERIC_QUESTION.search(question) and not _NUMBER.fullmatch(value):
            return None
        seen.add(field)
        revisions.append(_revision_entry(plan, active, field, question, previous, value, _literal_span(start, clause)))
    return remaining, revisions


def _revision_entry(plan, active, field, question, previous, value, evidence, *, dependency_fields=None):
    required = {row['id'] for row in plan.get('evidence_requirements', ())}
    ids = set(previous['items']) & active.keys()
    collected = sorted(number for number in (ids & set(previous['collected_items'])) - required
                       if active[number]['state'] == 'done' and active[number]['evidence_steps'] == [previous['step']])
    dependent = _dependent_operands(plan, previous, {field} if dependency_fields is None else dependency_fields)
    changed = _value_identity(value) != _value_identity(previous['value'])
    recheck = sorted(((ids | dependent.keys()) & active.keys()) - set(collected)) if changed else []
    return {'field': field, 'question': question, 'items': sorted(ids), 'value': value, 'evidence': evidence,
            'previous': previous, 'collected_items': collected, 'recheck_items': recheck,
            'operand_paths': [{'id': number, 'paths': sorted(dependent[number])}
                              for number in sorted(dependent.keys() & active.keys())]}


def _trim_revision_choices(choices, cancelled):
    return [{**row, 'items': items} for row in choices
            if (items := [number for number in row['items'] if number not in cancelled])]


def _revision_selection(clause, match, choice):
    if clause in _KEEP_VALUE:
        return choice['previous']['value']
    if match:
        if (match[1] or match[2]).strip() != choice['field']:
            return None
        literal = match[3].strip()
    else:
        literal = re.sub(r'^(?:就用|改用|選用|採用|沿用|選|用)\s*', '', clause, count=1)
    value = _value(literal)
    if value is not None and any(_value_identity(value) == _value_identity(row)
                                 for row in [choice['previous']['value'], *(row['value'] for row in choice['options'])]):
        return value
    return None


def _parse_revision_choices(clauses, steps, plan, tasks, cancelled, previous, source, declined, *, delivery_only_ok=False):
    confirmed = _confirmed_fields(steps, plan, tasks)
    active = {row['id']: row for row in plan['items'] if row['state'] != 'superseded' and row['id'] not in cancelled}
    prior = previous.get('pending_revisions', [])
    pending = _trim_revision_choices(prior, cancelled)
    shown = prior[0] if prior and not previous.get('cancellation') and pending and pending[0]['field'] == prior[0]['field'] else None
    remaining, revisions, grouped = [], [], {}
    resolution = None
    for start, clause in clauses:
        evidence = _literal_span(start, clause)
        match = _REVISION.fullmatch(clause)
        if shown and (selected := _revision_selection(clause, match, shown)) is not None:
            if resolution:
                return None
            old = shown['previous']
            keep = _value_identity(selected) == _value_identity(old['value'])
            resolution = {'field': shown['field'], 'value': selected, 'kept': keep, 'evidence': evidence,
                          'from': shown['citation'], 'source_sha256': shown['source_sha256']}
            if not keep:
                revisions.append(_revision_entry(plan, active, shown['field'], shown['question'], old, selected, evidence))
            pending = pending[1:]
            continue
        if match is None:
            remaining.append((start, clause))
            continue
        field = (match[1] or match[2]).strip()
        if (resolution and field == resolution['field']) or any(row['field'] == field for row in pending):
            return None
        literals = re.split(r'\s*(?:或者|或是|或)\s*', match[3].strip())
        options = [{'value': _value(literal), 'evidence': evidence} for literal in literals]
        if any(row['value'] is None for row in options):
            return None
        grouped.setdefault(field, []).extend(options)

    out_of_scope = []
    for field, options in grouped.items():
        candidates = [(question, row) for question, row in confirmed.items() if row['field'] == field]
        if len(candidates) != 1:
            return None
        question, old = candidates[0]
        if _NUMERIC_QUESTION.search(question) and any(not _NUMBER.fullmatch(row['value']) for row in options):
            return None
        items = sorted((set(old['items']) | _dependent_operands(plan, old, {field}).keys()) & active.keys())
        if not items:
            if not set(old['items']) & cancelled:
                return None
            out_of_scope.append({'field': field, 'options': options})
            continue
        identities = {}
        for row in options:
            identities.setdefault(_value_identity(row['value']), row)
        if len(identities) == 1:
            selected = next(iter(identities.values()))
            revisions.append(_revision_entry(plan, active, field, question, old, selected['value'], selected['evidence']))
            continue
        alternatives = [row for identity, row in identities.items() if identity != _value_identity(old['value'])]
        if len(alternatives) > 4:
            return None
        pending.append({'field': field, 'question': question, 'previous': old, 'items': items, 'options': alternatives,
                        'statements': options, 'citation': source['citation'], 'source_sha256': source['sha256']})
    if not (declined or pending or resolution or prior or out_of_scope or delivery_only_ok):
        return None
    return remaining, revisions, {'declined_proposals': declined, 'pending_revisions': pending,
                                  'revision_resolution': resolution, 'out_of_scope_revisions': out_of_scope}


def _observe_filename_revision(steps, plan, source, scope, prior_scope, delivery, *, defer_filename=False):
    ask, question = artifact_names.bound_question(steps, source, prior_scope)
    if plan['goal_conditions_sha256'] != prior_scope:
        raise ValueError('定稿檔名問題的條件已改變，請重新核對工作清單。')
    text = source['answer']
    if delivery:
        text = _without_delivery(text, delivery['receipt']['plan_parts'])
    clauses = goal_guidance_sources.standalone_clauses(text, comma_before=_FILENAME_REPLY_COMMA)
    if any(question_mark for _, _, question_mark in clauses):
        return None
    fields, declined, names = [], [], []
    deferral = None
    for start, clause, _ in clauses:
        if defer_filename and deferral is None and _FILENAME_DEFERRAL.fullmatch(clause):
            deferral = _literal_span(start, clause)
            continue
        if _DECLINED_PROPOSAL.fullmatch(clause):
            declined.append(_literal_span(start, clause))
            continue
        if declared := artifact_names.declaration(clause, only=True):
            names.append({**declared, 'start': start + declared['start'], 'end': start + declared['end']})
        elif not _REVISION.fullmatch(clause) and (literal := artifact_names.reply_filename(clause, start=start)):
            names.append(literal)
        else:
            fields.append((start, clause))
    file_mode = not delivery or delivery['receipt']['file_delivery']
    if defer_filename and (names or delivery):
        return None
    if len(names) > 1 or (file_mode and not names and not defer_filename):
        return None
    choice = artifact_names.confirm_filename(steps, ask, question, names[0]) if names else None
    tasks = {item['id']: item['task'] for item in plan['items']}
    parsed = _parse_revision_choices(fields, steps, plan, tasks, set(), {}, source, declined, delivery_only_ok=True)
    if parsed is None or parsed[0]:
        return None
    _, revisions, language = parsed
    if defer_filename and not (revisions or language['pending_revisions']):
        return None
    language.update(cancellation=None, resolution=None, unassigned=[], previous_waiting=[])
    result = _reply_result(steps, plan, source, scope, prior_scope, [], [], tasks, [], {}, language=language,
                           revisions=revisions)
    if result:
        if choice:
            result['scope_reply']['filename_choice'] = choice
        if delivery:
            result['scope_reply']['delivery_mode'] = delivery
        if defer_filename:
            result['scope_reply']['filename_pending'] = question['requirement']
            if deferral:
                result['scope_reply']['filename_deferral'] = deferral
    return result


def _apply_revisions(result, proof, complete, plan, revisions, source):
    recheck = {number for row in revisions for number in row['recheck_items']}
    revised = {number: row['value'] for row in revisions for number in row['collected_items']}
    affected = recheck | revised.keys()
    for index, item in enumerate(result['items']):
        if item['id'] in recheck:
            result['items'][index] = {**item, 'state': 'pending', 'evidence_steps': [],
                                      'note': f'依 {source["citation"]} 的更正重新核對此項。'}
            complete.pop(item['id'], None)
    complete.update(revised)
    proof['retained_items'] = [number for number in proof['retained_items'] if number not in affected]
    if 'evidence_requirements' in result:
        result['evidence_requirements'] = [row if row['id'] in recheck else current for row, current in zip(
            plan['evidence_requirements'], result['evidence_requirements'], strict=True)]
        result['evidence_matches'] = [row for row in result['evidence_matches'] if row['id'] not in recheck]
    proof['revisions'] = revisions


def replay(db, args, prior, after, goal, guidance, replies, *, withdrawals_present, delivery_present=False, pending=None):
    confirmation = args.get(CONFIRMATION_CORRECTION)
    version = args.get(CONFIRMATION_CORRECTION_VERSION, 1)
    if CONFIRMATION_CORRECTION_VERSION in args and (confirmation is not True or type(version) is not int
                                                   or version not in (TYPED_GUIDANCE_VERSION, DIRECTIVE_GUIDANCE_VERSION)):
        raise ValueError('已保存的確認更正版本不符。')
    special = args.keys() & {'delivery_mode', 'filename_declaration', artifact_names.GUIDANCE_MARK}
    keys = args.keys() - special - {CONFIRMATION_CORRECTION, CONFIRMATION_CORRECTION_VERSION}
    if (keys not in ({'scope_correction'}, {'scope_correction', 'ask_sequence'})
            or any(args[key] is not True or not delivery_present for key in special)
            or len(special) > 1 or (CONFIRMATION_CORRECTION in args and (confirmation is not True or special))):
        raise ValueError('已保存的工作範圍更正格式不符。')
    source = next((item for item in guidance if item['citation'] == args['scope_correction']), None)
    if source is None:
        raise ValueError('工作範圍更正缺少使用者原話。')
    if version == DIRECTIVE_GUIDANCE_VERSION and confirmation_guidance_version(source['content']) != version:
        raise ValueError('已保存的確認更正版本不符。')
    start = source['notes_start']
    end = start + len(goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at']))
    previous = work_plan.conditions({**goal, 'notes': goal['notes'][:start]})
    scope = work_plan.conditions({**goal, 'notes': goal['notes'][:end]})
    waiting = 'ask_sequence' in args
    delivery = delivery_preference.verify_plan_guidance(db, goal['id'], source) if 'delivery_mode' in args else None
    named = None
    if 'filename_declaration' in args:
        named = artifact_names.declaration(source['content'], only=True)
        if not named or not delivery_preference.initially_enabled(db, goal['id']):
            raise ValueError('定稿檔名補充缺少可核對的來源。')
    filename_guidance = None
    if artifact_names.GUIDANCE_MARK in args:
        delivery = delivery_preference.verify_filename_guidance(db, goal['id'], source)
        requirement = artifact_names.requirement({**goal, 'steps': prior,
            'reply_sources': [row for row in replies if row['notes_start'] < start],
            'guidance_sources': [row for row in guidance if row['notes_start'] < start]})
        filename_guidance = artifact_names.guidance_choice(prior, source['content'], requirement, delivery=delivery)
        if not waiting or not filename_guidance or not delivery_preference.initially_enabled(db, goal['id']):
            raise ValueError('定稿檔名更正缺少原問題或已保存的來源。')
    expected = (observe_confirmation_correction(prior, source, scope, previous, waiting=waiting, version=version) if confirmation else
                observe(prior, source, scope, previous, waiting=waiting, delivery=delivery, filename_declaration=named,
                        filename_guidance=filename_guidance))
    if expected is None:
        raise ValueError('工作範圍更正與原清單或保存步驟不一致。')
    if waiting:
        proof = expected['scope_change']
        ask_sequence = args['ask_sequence']
        if type(ask_sequence) is not int or ask_sequence != proof['ask_sequence']:
            raise ValueError('工作範圍更正缺少對應的原問題。')
        record = db.execute('SELECT guidance_number,guidance_sha256 FROM autonomous_question_withdrawals '
                            'WHERE goal_id=? AND ask_sequence=?', (goal['id'], ask_sequence)).fetchone() if withdrawals_present else None
        if record != (source['number'], source['sha256']):
            raise ValueError('工作範圍更正缺少相符的問題撤下紀錄。')
    successor = expected['scope_change'].get('successor')
    if successor and (not after or after['action']['tool'] != 'ask' or after['action']['args'] != successor
                      or after['observation'].get('question') != successor['question']
                      or ((delivery or filename_guidance or confirmation)
                          and after['observation'].get('goal_conditions_sha256') != scope)
                      or (confirmation and pending is not None and pending != successor['question'])):
        raise ValueError('工作範圍更正缺少相符的保留問題。')
    return expected


def replay_reply(db, args, prior, after, goal, replies, guidance, *, delivery_present=False):
    version = args.get('scope_reply_version', 1)
    keys = {'scope_reply', 'scope_reply_version'} if version != 1 else {'scope_reply'}
    if args.keys() != keys or type(version) is not int or not 1 <= version <= COLLOQUIAL_CORRECTION_REPLY_VERSION:
        raise ValueError('已保存的工作清單答覆格式不符。')
    source = next((item for item in replies if item['citation'] == args['scope_reply']), None)
    if source is None:
        raise ValueError('工作清單答覆缺少使用者原話。')
    start = source['notes_start']
    end = start + len(goal_reply_sources.note(source['question'], source['answer']))
    previous = work_plan.conditions({**goal, 'notes': goal['notes'][:start]})
    scope = work_plan.conditions({**goal, 'notes': goal['notes'][:end]})
    delivery = None
    requirement = None
    if version == 5:
        if not delivery_present:
            raise ValueError('工作清單答覆缺少交付方式紀錄。')
        delivery = delivery_preference.verify_plan_reply(db, goal['id'], source)
    if version in (artifact_names.REPLY_VERSION, artifact_names.MIXED_REPLY_VERSION, artifact_names.DEFERRED_REPLY_VERSION,
                   CONFIRMATION_FILENAME_REPLY_VERSION):
        if not delivery_present or not delivery_preference.enabled_before(db, goal['id'], start):
            raise ValueError('定稿檔名答覆缺少當時的文件交付設定。')
        if version != artifact_names.REPLY_VERSION and delivery_preference.plan_reply(db, goal['id'], source):
            delivery = delivery_preference.verify_plan_reply(db, goal['id'], source)
    if version in (artifact_names.FIELD_CHOICE_REPLY_VERSION, artifact_names.FILENAME_FIRST_REPLY_VERSION,
                   artifact_names.REPLACE_REPLY_VERSION):
        if not delivery_present or not delivery_preference.initially_enabled(db, goal['id']):
            raise ValueError('定稿檔名答覆缺少原文件交付設定。')
        requirement = artifact_names.requirement({**goal, 'steps': prior,
            'reply_sources': [row for row in replies if row['notes_start'] < start],
            'guidance_sources': [row for row in guidance if row['notes_start'] < start]})
        if delivery_preference.plan_reply(db, goal['id'], source):
            delivery = delivery_preference.verify_plan_reply(db, goal['id'], source)
    expected = observe_reply(prior, source, scope, previous, version=version, delivery=delivery, filename_requirement=requirement)
    if expected is None:
        raise ValueError('工作清單答覆與原問題或保存來源不一致。')
    successor = expected['scope_reply']['successor']
    if successor and (not after or after['action']['tool'] != 'ask' or after['action']['args'] != successor
                      or after['observation'].get('question') != successor['question']
                      or after['observation'].get('goal_conditions_sha256') != scope):
        raise ValueError('工作清單答覆缺少相符的保留問題。')
    if artifact_names.needs_refreshed_question(expected) and (
            not after or not after['observation'].get(artifact_names.ASK_MARK)
            or after['observation'].get('goal_conditions_sha256') != scope
            or after['observation'].get('question') != source['question']):
        raise ValueError('已保存的欄位更正缺少接續的定稿檔名問題。')
    return expected
