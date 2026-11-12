"""Bind clarification to public plan items and apply explicit scope reductions."""
import re

from . import goal_guidance_sources, goal_reply_sources, goal_stages, work_plan


GUIDANCE = '''有已保存工作清單時，若缺漏會影響特定未完成項目，提問使用 plan_questions，例如
{"question":"收貨窗口是誰？\n入場門號是多少？","plan_questions":[{"question":"收貨窗口是誰？","items":[2]},{"question":"入場門號是多少？","items":[3]}]}。
question 逐行等於各分問。items 填這個問題實際影響的所有未完成項目；一個獨立分問對應一個資料缺漏，無關的問題不綁進清單。
scope_cancelled 列出使用者已取消的項目。當前成果只交保留項目的實際結果。取消項目和原因已保存於工作清單，除非使用者要求一併交代，定稿省略已取消項目的名稱、經過及結果。'''
_CANCEL = re.compile(r'(?:本輪|這次|本次)?\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做)\s*(.+)')
_TITLES = re.compile(r'「[^「」\r\n]+」(?:\s*(?:、|，|,|及|和|與)\s*「[^「」\r\n]+」)*')
_UNCHANGED = {'其他項目照舊', '其餘項目照舊', '其他項目維持原樣', '其餘照舊', '其他照舊'}
_OUTPUT_ONLY = re.compile(r'只(?:交付|交|提供|要)(?:已核對的|已確認的)?[\u4e00-\u9fff、]{1,45}')
_SCOPE_COMMA = re.compile(r'\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做|其餘|其他|只交|只提供|只要)')
_WORK_VERB = re.compile(r'^(?:安排|確認|核對|決定|記錄|填寫)')


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
    parts = args['plan_questions']
    if any(not set(part['items']) <= active for part in parts):
        raise ValueError('問題只能連到仍需處理的工作項目。')
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
    ids_by_task = {}
    for item in plan['items']:
        ids_by_task.setdefault(item['task'], []).append(item['id'])
    active = {item['id'] for item in plan['items'] if item['state'] != 'superseded'}
    changes, used, outputs = [], set(), []
    for start, clause, question in goal_guidance_sources.standalone_clauses(source['content'], comma_before=_SCOPE_COMMA):
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


def _successor(steps, plan, cancelled):
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
    if not parts:
        return {'question': question}
    remaining = []
    for part in parts:
        ids = [number for number in part['items'] if number not in cancelled]
        if ids:
            remaining.append({'question': part['question'], 'items': ids})
    return {'question': '\n'.join(part['question'] for part in remaining), 'plan_questions': remaining} if remaining else None


def observe(steps, source, scope, prior_scope, *, waiting):
    plan = work_plan.latest(steps)
    if plan is None or source['steps_seen'] != len(steps):
        return None
    changes = cancellations(source, plan)
    if not changes:
        return None
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
    if waiting:
        proof.update(ask_sequence=steps[-1]['sequence'], ask_sha256=goal_stages.digest(steps[-1]),
                     successor=_successor(steps, plan, cancelled))
    result = {'status': 'plan_recorded', 'items': items, 'goal_conditions_sha256': scope, 'scope_change': proof,
              'scope_cancelled': sorted(cancelled | set(plan.get('scope_cancelled', ()))),
              'review_note': '依實際更正更新工作清單，先前步驟及答覆仍可核對。'}
    if plan.get('verify_delivery'):
        result['verify_delivery'] = True
    if plan.get('evidence_requirements'):
        result['evidence_requirements'] = [
            {**item, 'defined_scope': scope} if item['id'] in retained else item
            for item in plan['evidence_requirements']]
        result['evidence_matches'] = [item for item in plan['evidence_matches'] if item['id'] in retained]
    return result


def replay(db, args, prior, after, goal, guidance, *, withdrawals_present):
    if args.keys() not in ({'scope_correction'}, {'scope_correction', 'ask_sequence'}):
        raise ValueError('已保存的工作範圍更正格式不符。')
    source = next((item for item in guidance if item['citation'] == args['scope_correction']), None)
    if source is None:
        raise ValueError('工作範圍更正缺少使用者原話。')
    start = source['notes_start']
    end = start + len(goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at']))
    previous = work_plan.conditions({**goal, 'notes': goal['notes'][:start]})
    scope = work_plan.conditions({**goal, 'notes': goal['notes'][:end]})
    waiting = 'ask_sequence' in args
    expected = observe(prior, source, scope, previous, waiting=waiting)
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
        successor = proof['successor']
        if successor and (not after or after['action']['tool'] != 'ask' or after['action']['args'] != successor
                          or after['observation'].get('question') != successor['question']):
            raise ValueError('工作範圍更正缺少相符的保留問題。')
    return expected
