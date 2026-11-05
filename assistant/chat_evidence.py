"""Explicit local condition analysis with a reproducible answer receipt."""
import json

from . import condition_request_v1 as grammar
from . import evidence_logic, local_answer_receipts


MODE = 'local_evidence'
FIELD = 'evidence'
STATUS_LABELS = {
    'supported': '成立',
    'refuted': '不成立',
    'undetermined': '資料不足',
    'inconsistent': '條件互相衝突',
}


def matches_request(question):
    return bool(isinstance(question, str) and grammar.PREFIX.match(question))


def parse_request(question):
    if matches_request(question):
        return {'schema': 'local_evidence_plan_v1', 'request': question,
                'request_sha256': evidence_logic.digest(question)}
    return None


def _cancel(cancel):
    if cancel and cancel.is_set():
        from .engine import EngineError
        raise EngineError('已停止本機條件核對。', 'cancelled')


def format_answer(result):
    query = result['extraction']['query']['literal']
    title = '並非「' + query[1:] + '」' if query.startswith('!') else '「' + query + '」'
    lines = [f'{title}：{STATUS_LABELS[result["status"]]}。',
             f'符合規則與觀察的情況共 {result["compatible_worlds"]} 種。']
    if result['status'] == 'undetermined':
        name = query.removeprefix('!')
        names = [f'「{check["claim"]}」' for check in result['observation_checks']
                 if check['distinguishes'] and check['claim'] != name]
        lines.append(f'可直接核對「{name}」的狀態。')
        if names:
            lines.append('也可以核對以下任一項：' + '、'.join(names) + '。')
    return '\n'.join(lines)


def evaluate(plan, cancel=None):
    _cancel(cancel)
    if not isinstance(plan, dict) or parse_request(plan.get('request')) != plan:
        raise ValueError('本機條件核對計畫與原問不符。')
    question = plan['request']
    constraints = grammar.compile_request(question)
    result = evidence_logic.analyse_named(question, json.dumps(constraints, ensure_ascii=False))
    _cancel(cancel)
    answer = format_answer(result)
    return answer, {'schema': 'local_evidence_receipt_v1', 'plan': plan, 'status': 'complete',
                    'model_called': False, 'analysis': result, 'output': answer,
                    'output_sha256': evidence_logic.digest(answer)}


def validate(receipt, delivered, question=None):
    if not isinstance(receipt, dict) or not isinstance(receipt.get('plan'), dict):
        raise ValueError('本機條件核對紀錄格式不符。')
    answer, expected = evaluate(receipt['plan'])
    if (answer != delivered or (question is not None and question != receipt['plan']['request'])
            or json.dumps(receipt, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True)):
        raise ValueError('本機條件核對紀錄與原文或推論不符。')
    return receipt


def read(store, mid):
    return local_answer_receipts.read(store, mid, MODE, FIELD, validate)


def describe(receipt):
    return (receipt['output'] + '\n\n依本輪明示條件列出命題、原文、相容情況與觀察分支。\n\n'
            + json.dumps(receipt['analysis'], ensure_ascii=False, indent=2))
