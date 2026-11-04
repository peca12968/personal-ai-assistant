"""Exact totals for complete, explicitly scoped expense lists in the current message."""
import hashlib
import json
import re
import uuid

from assistant import answer_runs, expression_math
from assistant.engine import Assistant, EngineError, make_context

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


TARGET = rf'(?:第(?P<position>[1-9][0-9]?)項)?(?P<name>{NAME})\s*改為(?P<status>{STATUS})'
PREVIOUS = re.compile(rf'(?:請)?(?:把|將)上一筆核銷的\s*{TARGET}[，,]\s*(?:請)?(?:重新加總|重算)[。！]?')
CORRECTION = re.compile(rf'更正[：:]\s*{TARGET}[，,]\s*(?:請)?(?:重新加總|重算)(?:上一筆核銷)?[。！]?')


def preceding_receipt(store, sid, before=None):
    # Do not skip unrelated messages to find an older expense calculation.
    with store.connect() as db:
        row = db.execute(
            'SELECT a.id,a.content,u.content AS question,r.context_json,c.payload FROM messages a '
            'JOIN answer_runs r ON r.answer_message_id=a.id AND r.session_id=a.session_id '
            "JOIN messages u ON u.id=r.user_message_id AND u.session_id=a.session_id AND u.role='user' "
            'JOIN answer_contexts c ON c.message_id=a.id WHERE a.session_id=? '
            'AND a.id=(SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 1) '
            "AND a.role='assistant' AND r.status='succeeded'", (sid, sid, before, before)).fetchone()
    if row:
        context = json.loads(row['payload'])
        ledger = context.get('expense_ledger')
        receipt = context.get('calculation', {})
        initial = json.loads(row['context_json'])
        if (isinstance(ledger, dict) and ledger.get('schema') == 'approved_expense_total_v1'
                and context.get('response_mode') == 'local_calculation'
                and isinstance(receipt, dict) and receipt.get('schema') == 'approved_expense_calculation_v1'
                and receipt.get('model_called') is False and receipt.get('status') == 'complete'
                and row['content'] in (receipt['result']['exact'], receipt['result']['decimal'])
                and row['question'] == context['current_request'] == initial['current_request']
                and ledger == initial.get('expense_ledger') and context.get('expense_source') == initial.get('expense_source')):
            return ledger, dict(answer_id=row['id'], sha256=hashlib.sha256(row['payload'].encode('utf8')).hexdigest())
    raise ValueError('請提供本次要更正的核銷明細；這段對話上一則回答沒有可接續的核銷紀錄。')


def apply_followup(ledger, target, question):
    name, position = target['name'], target['position']
    named = [(i, row) for i, row in enumerate(ledger['rows'], 1) if row['name'] == name]
    if not named:
        raise ValueError('上一筆核銷沒有「' + name + '」。請確認項目原名或重新提供明細。')
    matches = [(i, row) for i, row in named if position is None or int(position) == i]
    if len(matches) != 1:
        options = [f'第{i}項{name}（{r["amount"]} {r["currency"]}，{r["status"]}）' for i, r in named]
        raise ValueError('請指定要更正的列：' + '、'.join(options) + '。例如「把上一筆核銷的第'
                         + str(named[0][0]) + '項' + name + '改為' + target['status'] + '，重新加總」。')
    if len(ledger['changes']) >= 32:
        raise ValueError('這份明細已有 32 次更正；請重新提供完整明細。')
    i, row = matches[0]
    ledger['changes'].append(dict(source=question, name=name, position=i, before=row['status'], after=target['status']))
    row['status'] = target['status']
    row['included'] = row['status'] == '已核准' and row['currency'] == ledger['currency']


class DailyAssistant(Assistant):
    def _ask(self, sid, question, cancel=None, retry=None, draft=None, image_ids=()):
        question = question.strip()
        followup = PREVIOUS.fullmatch(question) or CORRECTION.fullmatch(question)
        local = self.allow_local_calculation and not image_ids
        ledger = parse(question) if local and not followup else None
        source = None
        if self.allow_local_calculation and followup and (image_ids or self.store.documents(sid, include_content=False)):
            raise ValueError('這個更正指令只能接續純文字核銷紀錄。若要核對附件，請說明要採用的明細。')
        if not local or (ledger is None and not followup) or (not followup and self.store.documents(sid, include_content=False)):
            return super()._ask(sid, question, cancel, retry=retry, draft=draft, image_ids=image_ids)
        if cancel and cancel.is_set():
            raise EngineError('已停止本機核銷計算。', 'cancelled')
        if retry:
            with self.store.connect() as db:
                previous = db.execute('SELECT context_json FROM answer_runs WHERE id=? AND session_id=?',
                                      (retry['id'], sid)).fetchone()
            prior = json.loads(previous[0]) if previous else {}
            if 'expense_ledger' not in prior:
                return super()._ask(sid, question, cancel, retry=retry, draft=draft, image_ids=image_ids)
        if followup:
            ledger, source = preceding_receipt(self.store, sid, retry['user_message_id'] if retry else None)
            apply_followup(ledger, followup, question)
        if retry and (prior['expense_ledger'] != ledger or prior.get('expense_source') != source):
            raise ValueError('原先選取的核銷明細已變更，請重新提交更正。')
        answer_runs.recover(self.store, sid)
        values = [r['amount'] for r in ledger['rows'] if r['included']]
        command = dict(expression=' + '.join(values) or '0', output=ledger['output'])
        context = make_context(question, [], [])
        context.update(response_mode='local_calculation', expense_ledger=ledger, calculation=command,
                       documents={'catalog': [], 'excerpts': []})
        if source:
            context['expense_source'] = source
        if retry:
            context['retry_of'] = dict(run_id=retry['id'], previous_status=retry['status'], uses_current_references=True)
        run_id = uuid.uuid4().hex
        with answer_runs.lease(self.store, run_id):
            uid = answer_runs.begin(self.store, sid, run_id, question, context, retry, draft=draft)
            try:
                if source and preceding_receipt(self.store, sid, uid)[1] != source:
                    raise ValueError('送出更正前對話或核銷明細已變更，請確認後再提交。')
                tree = dict(operation='sum', values=values or ['0'])
                result = expression_math.calculate({'expression': tree}, cancel)
                if ledger['output'] == 'integer' and '/' in result['exact']:
                    raise EngineError('精確核准金額不是整數，請改為只輸出數字。', 'calculation_invalid')
                answer = result['decimal'] if '/' in result['exact'] else result['exact']
                receipt = dict(**command, schema='approved_expense_calculation_v1', status='complete',
                               tree=tree, result=result, model_called=False)
                context['calculation'] = receipt
                if cancel and cancel.is_set():
                    raise EngineError('已停止本機核銷計算。', 'cancelled')
                mid = answer_runs.finish(self.store, run_id, answer, context, [])
            except Exception as exc:
                answer_runs.fail(self.store, run_id, getattr(exc, 'code', 'unexpected_error'))
                raise
        return dict(id=mid, content=answer, memory_ids=[], excerpt_ids=[], document_count=0,
                    citation_checks=[], run_id=run_id, user_message_id=uid, response_mode='local_calculation',
                    calculation=receipt)
