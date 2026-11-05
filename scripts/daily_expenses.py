"""Exact totals for complete, explicitly scoped expense lists in the current message."""
import hashlib
import json
import re
import uuid
from decimal import Decimal

from scripts import daily_conditions, daily_clearance, daily_dialogue, daily_criteria, daily_identifiers, daily_meeting, daily_stock
from assistant import answer_runs, expression_math
from assistant.engine import Assistant, EngineError, make_context, render_prompt

NAME = r'[\u4e00-\u9fff]{1,12}'
AMOUNT = r'(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?'
ITEM = rf'(?P<name>{NAME})\s*(?P<amount>{AMOUNT})\s*(?P<currency>美元|元)'
STATUS = r'(?:已核准|待核准|待審|已駁回)'
OUTPUT = re.compile(r'(?:請)?只(?:回傳|輸出|回答|回覆)(?:合計的)?(?:阿拉伯)?(數字|整數)[。！]?$')
SCOPE = re.compile(r'(?:核銷規則[：:])?(?:請)?只(?:統計|加總|計入)(?:狀態為)?已核准的?(新台幣)?(?:支出|款項|項目|金額)')
TOTAL = re.compile(r'(?:已)?核准(?:的)?(?:金額|款項)(?:共|合計|總計)?(?:多少|是多少)[？?]?')
UNSAFE_LABEL = re.compile(r'核准|待審|駁回|更正|假設|如果|除非|只|其餘|各|每|扣|折|含稅|未稅|合計|小計|共計|另有|不是|原本|改為|尚未|不要|僅|排除|退費|退款|折讓|抵銷|預估|原價|是否|按|忽略|不|勿|別|預計|上限|至少|最多|幣|美金|日圓|英鎊|歐元|港元|負數')


def rows(text, status=None):
    result = []
    pattern = ITEM if status else ITEM + rf'\s*(?P<status>{STATUS})'
    for raw in re.split(r'[、，,；;與及和]' if status else r'[、，,；;]', text):
        match = re.fullmatch(pattern, raw.strip())
        if not match or UNSAFE_LABEL.search(match['name']):
            return None
        result.append(dict(source=raw.strip(), name=match['name'], amount=match['amount'],
                           currency=match['currency'], status=status or match['status']))
    return result


def parse(question):
    if len(question) > 2048:
        return None
    text = re.sub(r'[，,]\s*(?=(?:請)?只(?:回傳|輸出|回答|回覆))', '。', question.strip())
    fields = re.split(r'[。！？?\n]+', text.rstrip('。！'))
    # The complete message must comprise a list, a scope, optional corrections and a format.
    if not fields or not (output := OUTPUT.fullmatch(fields[-1].strip())):
        return None
    fields = [f.strip() for f in fields[:-1]]
    if len(fields) < 2:
        return None
    scope = SCOPE.fullmatch(fields[0])
    if scope:
        currency = '元' if scope[1] else None
        entries = rows(re.sub(r'^明細[：:]', '', fields[1]))
        edits = fields[2:]
    elif TOTAL.fullmatch(fields[-1]):
        currency = None
        body = fields[0]
        grouped = re.fullmatch(r'(?:(?:修繕|核銷|報支|請款|採購|支出|費用)單)?只核准(.+?)[，,]另有待核准(.+)', body)
        if grouped:
            approved, pending = rows(grouped[1], '已核准'), rows(grouped[2], '待核准')
            entries = approved + pending if approved and pending else None
        else:
            entries = rows(body)
        edits = fields[1:-1]
    else:
        return None
    if not entries or len(entries) > 24 or len(edits) > 8:
        return None
    if currency is None and len({r['currency'] for r in entries}) != 1:
        return None
    changes = []
    for text in edits:
        correction = re.fullmatch(rf'更正[：:]\s*({NAME})\s*改為({STATUS})', text)
        if not correction:
            return None
        matched = [r for r in entries if r['name'] == correction[1]]
        if len(matched) != 1:
            return None
        entry = matched[0]
        changes.append(dict(source=text, name=entry['name'], before=entry['status'], after=correction[2]))
        entry['status'] = correction[2]
    for entry in entries:
        entry['included'] = entry['status'] == '已核准' and (currency is None or currency == entry['currency'])
    return dict(schema='approved_expense_total_v1', currency=currency or entries[0]['currency'],
                rows=entries, changes=changes, output='integer' if output[1]=='整數' else 'decimal')


TARGET = rf'(?:第\s*(?P<position>[1-9][0-9]?)\s*項\s*)?(?P<name>{NAME})\s*改為(?P<status>{STATUS})'
PREVIOUS = re.compile(rf'(?:請)?(?:把|將)上一筆核銷的\s*{TARGET}[，,]\s*(?:請)?(?:重新加總|重算)[。！]?')
CORRECTION = re.compile(rf'更正[：:]\s*{TARGET}[，,]\s*(?:請)?(?:重新加總|重算)(?:上一筆核銷)?[。！]?')


ROW_REPLY = re.compile(r'第\s*([0-9０-９]{1,3})\s*項[。！]?')


def selection_prompt(ledger, pending, total):
    options = [f'第{i}項（{row["amount"]} {row["currency"]}，{row["status"]}）'
               for i, row in enumerate(ledger['rows'], 1) if row['name'] == pending['name']]
    return (f'目前核准總額為 {total}。你要把哪一筆「{pending["name"]}」改為{pending["status"]}？'
            + '、'.join(options) + '。請回覆列號，例如「' + options[0].split('（')[0] + '」。')


def previous_answer(store, sid, before=None):
    # Never skip unrelated messages to find an older calculation.
    with store.connect() as db:
        return db.execute(
            'SELECT a.id,a.content,u.id AS uid,u.content AS question,r.context_json,c.payload FROM messages a '
            'JOIN answer_runs r ON r.answer_message_id=a.id AND r.session_id=a.session_id '
            "JOIN messages u ON u.id=r.user_message_id AND u.session_id=a.session_id AND u.role='user' "
            'JOIN answer_contexts c ON c.message_id=a.id WHERE a.session_id=? '
            'AND a.id=(SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 1) '
            "AND a.role='assistant' AND r.status='succeeded'", (sid, sid, before, before)).fetchone()


def source_ref(row):
    return dict(answer_id=row['id'], sha256=hashlib.sha256(row['payload'].encode('utf8')).hexdigest())


def previous_meeting(store, sid, before):
    row = previous_answer(store, sid, before)
    if row:
        saved = json.loads(row['payload'])
        initial = json.loads(row['context_json'])
        plan = saved.get('meeting_plan')
        calc = saved.get('calculation', {})
        if (isinstance(plan, dict) and plan.get('schema') == 'daily_availability_v1'
                and plan == initial.get('meeting_plan') and saved.get('meeting_source') == initial.get('meeting_source')
                and row['question'] == initial.get('current_request') == saved.get('current_request')
                and saved.get('response_mode') == 'local_calculation' and isinstance(calc, dict)
                and isinstance(calc.get('result'), dict) and calc.get('model_called') is False
                and calc.get('schema') == 'daily_meeting_calculation_v1' and calc.get('status') == 'complete'
                and row['content'] == daily_meeting.visible_answer(plan, calc)):
            return plan, source_ref(row)
    raise ValueError('上一則回答沒有可接續的同日會議紀錄，請提供每人的空檔與會議長度。')


def preceding_receipt(store, sid, before=None, *, pending_only=False, allow_pending=True):
    row = previous_answer(store, sid, before)
    if row:
        context = json.loads(row['payload'])
        ledger = context.get('expense_ledger')
        receipt = context.get('calculation', {})
        initial = json.loads(row['context_json'])
        pending = context.get('expense_selection')
        if pending_only and pending is None:
            return None
        if (isinstance(ledger, dict) and ledger.get('schema') == 'approved_expense_total_v1'
                and context.get('response_mode') == 'local_calculation'
                and isinstance(receipt, dict) and receipt.get('schema') == 'approved_expense_calculation_v1'
                and receipt.get('model_called') is False and receipt.get('status') == 'complete'
                and pending == initial.get('expense_selection')
                and row['question'] == context['current_request'] == initial['current_request']
                and ledger == initial.get('expense_ledger') and context.get('expense_source') == initial.get('expense_source')):
            total = receipt['result']['exact']
            if '/' in total:
                total = receipt['result']['decimal']
            expected = total
            if pending is not None:
                if not allow_pending:
                    raise ValueError('核銷選項來源不符，請重新提供明細。')
                origin, source, _ = preceding_receipt(store, sid, row['uid'], allow_pending=False)
                request = PREVIOUS.fullmatch(row['question']) or CORRECTION.fullmatch(row['question'])
                if (source != context.get('expense_source') or origin != ledger or not request
                        or pending != dict(name=request['name'], status=request['status']) or request['position']
                        or sum(r['name'] == pending['name'] for r in ledger['rows']) < 2):
                    raise ValueError('核銷選項或原始明細已變更。')
                expected = selection_prompt(ledger, pending, total)
            if row['content'] == expected:
                return ledger, source_ref(row), pending
    elif pending_only:
        return None
    raise ValueError('請提供本次要更正的核銷明細；這段對話上一則回答沒有可接續的核銷紀錄。')


def apply_followup(ledger, target, question, *, can_prompt=False):
    name, position = target['name'], target['position']
    named = [(i, row) for i, row in enumerate(ledger['rows'], 1) if row['name'] == name]
    if not named:
        raise ValueError('上一筆核銷沒有「' + name + '」。請確認項目原名或重新提供明細。')
    matches = [(i, row) for i, row in named if position is None or int(position) == i]
    if len(matches) != 1:
        if can_prompt and not position:
            return dict(name=name, status=target['status'])
        options = [f'第{i}項{name}（{r["amount"]} {r["currency"]}，{r["status"]}）' for i, r in named]
        raise ValueError('請指定要更正的列：' + '、'.join(options) + '。例如「把上一筆核銷的第'
                         + str(named[0][0]) + '項' + name + '改為' + target['status'] + '，重新加總」。')
    if len(ledger['changes']) >= 32:
        raise ValueError('這份明細已有 32 次更正；請重新提供完整明細。')
    i, row = matches[0]
    ledger['changes'].append(dict(source=question, name=name, position=i, before=row['status'], after=target['status']))
    row['status'] = target['status']
    row['included'] = row['status'] == '已核准' and row['currency'] == ledger['currency']


def option_source_valid(row, saved):
    initial = json.loads(row['context_json'])
    calc = saved.get('calculation', {})
    return (isinstance(saved.get('option_plan'), dict) and saved['option_plan'].get('schema') == 'daily_option_table_v1'
            and saved['option_plan'] == initial.get('option_plan') and saved.get('option_source') == initial.get('option_source')
            and saved.get('option_question') == initial.get('option_question') and saved.get('response_mode') == 'local_calculation'
            and row['question'] == initial.get('current_request') == saved.get('current_request')
            and isinstance(calc, dict) and isinstance(calc.get('result'), dict)
            and calc.get('schema') == 'daily_option_selection_v1' and calc.get('model_called') is False
            and calc.get('status') == 'complete' and row['content'] == calc.get('result', {}).get('exact'))


def stock_clarification_ids(db, sid, context, before):
    """Identify saved clarification answers immediately preceding a quantity-only user turn."""
    history = context['recent_conversation']
    current = dict(role='user', content=context['current_request'], message_id=before)
    checked = []
    for previous, reply in zip(history, [*history[1:], current]):
        if (previous['role'] != 'assistant' or previous['truncated'] or reply['role'] != 'user'
                or not daily_stock.BRIEF_COUNT.fullmatch(reply['content'])):
            continue
        row = db.execute("""SELECT m.id FROM messages m JOIN answer_contexts c ON c.message_id=m.id
            JOIN answer_runs r ON r.answer_message_id=m.id AND r.session_id=m.session_id
            WHERE m.id=? AND m.session_id=? AND m.role='assistant' AND m.content=? AND r.status='succeeded'
            AND json_extract(c.payload,'$.response_mode')='verified_inventory'
            AND json_extract(c.payload,'$.inventory_check.schema') IN ('stock_opening_clarification_v1','stock_event_clarification_v1')
            AND json_extract(c.payload,'$.inventory_check.delivered')=m.content
            AND NOT EXISTS (SELECT 1 FROM messages n WHERE n.session_id=m.session_id AND n.id>m.id AND (? IS NULL OR n.id<?))""",
            (previous['message_id'], sid, previous['content'], reply['message_id'], reply['message_id'])).fetchone()
        if row:
            checked.append(row['id'])
    return checked


class DailyAssistant(Assistant):
    def prepare_context(self, sid, question, exclude_message_id=None):
        context = super().prepare_context(sid, question, exclude_message_id)
        if self.allow_local_calculation:
            focus = daily_identifiers.context_focus(context)
            if focus:
                context['identifier_request'] = focus
            if daily_stock.candidate(question):
                with self.store.connect() as db:
                    checked = stock_clarification_ids(db, sid, context, exclude_message_id)
                if checked:
                    context['stock_clarification_ids'] = checked
            inventory = daily_stock.context_hint(context)
            if inventory:
                context['inventory_reasoning'] = inventory
            elif resumed := daily_dialogue.resumed_topic(context):
                context['resumed_topic'] = resumed
            elif update := daily_dialogue.revision_focus(context):
                context['current_revision'] = update
            clearance = daily_clearance.opening_reference(context)
            if clearance:
                context['door_measurements'] = clearance
            elif evidence := daily_dialogue.feasibility_evidence(context):
                context['feasibility_evidence'] = evidence
        return context

    def _ask(self, sid, question, cancel=None, retry=None, draft=None, image_ids=()):
        question = question.strip()
        if self.allow_local_transcription and not image_ids:
            from assistant.chat_transcription import parse_request
            if parse_request(question) is not None:
                return super()._ask(sid,question,cancel=cancel,retry=retry,draft=draft,image_ids=image_ids)
        followup = PREVIOUS.fullmatch(question) or CORRECTION.fullmatch(question)
        local = self.allow_local_calculation and not image_ids
        choice = ROW_REPLY.fullmatch(question)
        before = retry['user_message_id'] if retry else None
        selection = preceding_receipt(self.store, sid, before, pending_only=True) if self.allow_local_calculation and choice else None
        prior = {}
        if retry and local:
            with self.store.connect() as db:
                original = db.execute('SELECT context_json FROM answer_runs WHERE id=? AND session_id=?',
                                      (retry['id'], sid)).fetchone()
            prior = json.loads(original[0]) if original else {}
            if choice and not selection and prior.get('expense_source'):
                raise ValueError('原先的核銷選項已變更，請重新提供明細。')
        if (local and hasattr(self.engine, 'answer_raw') and daily_conditions.COUNT.search(question)
                and daily_conditions.CHOICE.fullmatch(question)
                and not prior.get('image_inputs') and (not retry or prior.get('response_mode') == 'capacity_comparison')):
            context = self.prepare_context(sid, question, exclude_message_id=before)
            if retry:
                # The retried question itself is the current DB tail.
                context['conversation_tail'] = prior.get('conversation_tail')
            focus = daily_conditions.capacity_trial(context)
            if retry and focus != prior.get('capacity_trial'):
                raise ValueError('可用名額的來源已變更，請重新提供條件。')
            if focus:
                context.update(response_mode='capacity_comparison', capacity_trial=focus)

                def compare_capacity(uid):
                    with self.store.connect() as db:
                        latest = db.execute('SELECT MAX(id) FROM messages WHERE session_id=? AND id<?', (sid, uid)).fetchone()[0]
                    if latest != (context.get('conversation_tail') or {}).get('message_id'):
                        raise ValueError('送出前對話已變更，請重新確認名額條件。')
                    trial = self.engine.answer_raw(daily_conditions.trial_prompt(focus), cancel)
                    labels = daily_conditions.branch_labels(trial)
                    receipt = dict(schema='capacity_comparison_v1', trial_answer=trial, branches=labels)
                    if labels:
                        answer = labels[0] if labels[0] == labels[1] else '資料不足'
                    else:
                        if cancel and cancel.is_set():
                            raise EngineError('已停止回答。', 'cancelled')
                        raw = self.engine.answer_raw(render_prompt(daily_conditions.ordinary_context(context)), cancel)
                        receipt['fallback_answer'] = raw
                        answer = daily_dialogue.short_label(context, raw) or raw
                    receipt['delivered'] = answer
                    return answer, receipt

                return self._run_recorded(sid, question, context, compare_capacity, cancel, retry, draft, 'capacity_check')
        measured = daily_clearance.measured_choice(question) if local else None
        if (local and hasattr(self.engine, 'answer_raw') and (daily_clearance.proof_format(question) or measured)
                and not prior.get('image_inputs') and (not retry or prior.get('response_mode') == 'verified_clearance')):
            context = self.prepare_context(sid, question, exclude_message_id=before)
            source = None
            if measured:
                row = previous_answer(self.store, sid, before)
                if row and daily_clearance.proof_format(row['question']):
                    saved = json.loads(row['payload'])
                    if retry:
                        context['conversation_tail'] = dict(message_id=row['id'], role='assistant')
                    ref = daily_clearance.opening_reference(context)
                    if (saved.get('response_mode') == 'verified_clearance' and saved.get('current_request') == row['question']
                            and saved.get('clearance_check', {}).get('delivered') == row['content'] and ref
                            and ref.get('來源使用者訊息') == row['uid']
                            and ('阻礙原因' in ref or Decimal(ref['本輪淨寬毫米']) != Decimal(ref['箱邊毫米']))):
                        source = source_ref(row)
                        context.update(door_measurements=ref, clearance_source=source)
                if retry and (not source or source != prior.get('clearance_source')):
                    raise ValueError('原先的門口條件已變更，請重新提供尺寸。')
            if context.get('door_measurements') and (not measured or source):
                context['response_mode'] = 'verified_clearance'

                def verify_clearance(uid):
                    if source:
                        current = previous_answer(self.store, sid, uid)
                        if (not current or source_ref(current) != source or current['uid'] != row['uid']
                                or current['content'] != row['content'] or current['question'] != row['question']):
                            raise ValueError('送出前門口的來源回答已變更，請重新確認。')
                    raw = self.engine.answer_raw(render_prompt(context), cancel)
                    return daily_clearance.verified_answer(context, raw)

                return self._run_recorded(sid, question, context, verify_clearance, cancel, retry, draft, 'clearance_check')
        if (local and daily_stock.candidate(question) and hasattr(self.engine, 'answer_raw')
                and not prior.get('image_inputs') and (not retry or prior.get('response_mode') == 'verified_inventory')):
            context = self.prepare_context(sid, question, exclude_message_id=before)
            if retry:
                with self.store.connect() as db:
                    tail = db.execute('SELECT id,role FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 1',
                                      (sid, before)).fetchone()
                context['conversation_tail'] = dict(message_id=tail['id'],role=tail['role']) if tail else None
                context.pop('inventory_reasoning', None)
                hint = daily_stock.context_hint(context)
                if hint:
                    context['inventory_reasoning'] = hint
                if prior.get('inventory_reasoning') != hint:
                    raise ValueError('庫存來源已變更，請重新提供異動明細。')
            if 'inventory_reasoning' in context:
                context['response_mode'] = 'verified_inventory'

                def verify_stock(uid):
                    ids = context['inventory_reasoning']['來源訊息編號']
                    if ids:
                        with self.store.connect() as db:
                            if context.get('stock_clarification_ids', []) != stock_clarification_ids(db, sid, context, uid):
                                raise ValueError('庫存追問已變更，請重新確認數量。')
                            latest = db.execute('SELECT id FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 1',
                                                (sid, uid)).fetchone()
                            if not latest or latest['id'] != context['conversation_tail']['message_id']:
                                raise ValueError('送出前庫存對話已變更，請重新確認異動明細。')
                            for entry in context['recent_conversation']:
                                if entry['message_id'] in ids:
                                    source = db.execute("SELECT content FROM messages WHERE session_id=? AND id=? AND role='user'",
                                                        (sid,entry['message_id'])).fetchone()
                                    if not source or source['content'] != entry['content']:
                                        raise ValueError('庫存異動來源已變更。')
                    raw = self.engine.answer_raw(render_prompt(context), cancel)
                    return daily_stock.verified_answer(context, raw)

                return self._run_recorded(sid, question, context, verify_stock, cancel, retry, draft, 'inventory_check')
        selection_change = daily_criteria.revision(question) if local else None
        option_plan = daily_criteria.parse(question) if local else None
        if (option_plan or selection_change) and not self.store.documents(sid, include_content=False):
            origin = None
            clarification = None
            if selection_change:
                previous = previous_answer(self.store, sid, before)
                saved = json.loads(previous['payload']) if previous else {}
                if 'option_plan' in saved:
                    if not option_source_valid(previous, saved):
                        raise ValueError('上一份方案紀錄已變動，請重新提供條件。')
                    origin = source_ref(previous)
                    option_plan = saved['option_plan']
                    clarification = daily_criteria.revise(option_plan, selection_change)
                elif prior.get('option_source'):
                    raise ValueError('已無法核對原先的方案紀錄，請重新提供條件。')
            if option_plan and (not retry or 'option_plan' in prior):
                if retry and (prior['option_plan'] != option_plan or prior.get('option_source') != origin
                              or prior.get('option_question') != clarification):
                    raise ValueError('重試所需的方案來源已變更。')
                context = make_context(question, [], [])
                context.update(response_mode='local_calculation', option_plan=option_plan,
                               documents={'catalog': [], 'excerpts': []})
                if origin:
                    context['option_source'] = origin
                if clarification:
                    context['option_question'] = clarification

                def resolve_options(uid):
                    if origin:
                        row = previous_answer(self.store, sid, uid)
                        if not row or source_ref(row) != origin or not option_source_valid(row, json.loads(row['payload'])):
                            raise ValueError('提交前方案來源已變更，請重新核對。')
                    return daily_criteria.resolve(option_plan, clarification)

                return self._run_recorded(sid, question, context, resolve_options, cancel, retry, draft)
        change = daily_meeting.FOLLOWUP.fullmatch(question) if self.allow_local_calculation else None
        meeting = daily_meeting.parse(question) if local and not (followup or choice or change) else None
        if change or meeting:
            attached = image_ids or self.store.documents(sid, include_content=False)
            if change and attached:
                raise ValueError('會議更正需要純文字的原始時段；若要參考附件，請重新提供完整條件。')
            if not attached and (not retry or prior.get('meeting_plan')):
                origin = None
                if change:
                    meeting, origin = previous_meeting(self.store, sid, before)
                    daily_meeting.revise(meeting, change[1])
                if retry and (prior.get('meeting_plan') != meeting or prior.get('meeting_source') != origin):
                    raise ValueError('重試所需的會議來源已變更，請重新提供空檔。')
                context = make_context(question, [], [])
                context.update(response_mode='local_calculation', meeting_plan=meeting,
                               documents={'catalog': [], 'excerpts': []})
                if origin:
                    context['meeting_source'] = origin

                def resolve_meeting(uid):
                    if origin and previous_meeting(self.store, sid, uid)[1] != origin:
                        raise ValueError('提交前會議來源已變更，請核對後再更正。')
                    return daily_meeting.resolve(meeting)

                return self._run_recorded(sid, question, context, resolve_meeting, cancel, retry, draft)
        ledger = parse(question) if local and not (followup or selection) else None
        pending = None
        source = None
        if self.allow_local_calculation and (followup or selection) and (image_ids or self.store.documents(sid, include_content=False)):
            raise ValueError('這個更正指令只能接續純文字核銷紀錄。若要核對附件，請說明要採用的明細。')
        if not local or (ledger is None and not (followup or selection)) or (ledger and self.store.documents(sid, include_content=False)):
            return super()._ask(sid, question, cancel, retry=retry, draft=draft, image_ids=image_ids)
        if cancel and cancel.is_set():
            raise EngineError('已停止本機核銷計算。', 'cancelled')
        if retry and 'expense_ledger' not in prior:
            return super()._ask(sid, question, cancel, retry=retry, draft=draft, image_ids=image_ids)
        if selection:
            ledger, source, offered = selection
            apply_followup(ledger, {**offered, 'position': choice[1]}, question)
        elif followup:
            ledger, source, offered = preceding_receipt(self.store, sid, before)
            pending = apply_followup(ledger, followup, question, can_prompt=offered is None)
        if retry and (prior['expense_ledger'] != ledger or prior.get('expense_source') != source
                      or prior.get('expense_selection') != pending):
            raise ValueError('原先選取的核銷明細已變更，請重新提交更正。')
        values = [r['amount'] for r in ledger['rows'] if r['included']]
        command = dict(expression=' + '.join(values) or '0', output=ledger['output'])
        context = make_context(question, [], [])
        context.update(response_mode='local_calculation', expense_ledger=ledger, calculation=command,
                       documents={'catalog': [], 'excerpts': []})
        if pending:
            context['expense_selection'] = pending
        if source:
            context['expense_source'] = source

        def calculate(uid):
            if source and preceding_receipt(self.store, sid, uid)[1] != source:
                raise ValueError('送出更正前對話或核銷明細已變更，請確認後再提交。')
            tree = dict(operation='sum', values=values or ['0'])
            result = expression_math.calculate({'expression': tree}, cancel)
            if ledger['output'] == 'integer' and '/' in result['exact']:
                raise EngineError('精確核准金額不是整數，請改為只輸出數字。', 'calculation_invalid')
            answer = result['decimal'] if '/' in result['exact'] else result['exact']
            receipt = dict(**command, schema='approved_expense_calculation_v1', status='complete',
                           tree=tree, result=result, model_called=False)
            if pending:
                answer = selection_prompt(ledger, pending, answer)
            return answer, receipt

        return self._run_recorded(sid, question, context, calculate, cancel, retry, draft)

    def _run_recorded(self, sid, question, context, calculate, cancel, retry, draft, receipt_key='calculation'):
        if cancel and cancel.is_set():
            raise EngineError('已停止本機計算。', 'cancelled')
        answer_runs.recover(self.store, sid)
        if retry:
            context['retry_of'] = dict(run_id=retry['id'], previous_status=retry['status'], uses_current_references=True)
        run_id = uuid.uuid4().hex
        with answer_runs.lease(self.store, run_id):
            uid = answer_runs.begin(self.store, sid, run_id, question, context, retry, draft=draft)
            try:
                answer, receipt = calculate(uid)
                context[receipt_key] = receipt
                if cancel and cancel.is_set():
                    raise EngineError('已停止本機計算。', 'cancelled')
                mid = answer_runs.finish(self.store, run_id, answer, context, [])
            except Exception as exc:
                answer_runs.fail(self.store, run_id, getattr(exc, 'code', 'unexpected_error'))
                raise
        return dict(id=mid, content=answer, memory_ids=[r['id'] for r in context['memories']],
                    excerpt_ids=[r['message_id'] for r in context['earlier_user_excerpts']], document_count=0,
                    citation_checks=[], run_id=run_id, user_message_id=uid, response_mode=context['response_mode'],
                    **{receipt_key:receipt})
