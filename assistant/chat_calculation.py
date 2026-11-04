"""Explicit whole-message arithmetic, parsed as data and evaluated locally."""
import re
from . import expression_math

MAX_CHARS = 4096
MAX_TOKENS = 128
TRANSLATION = str.maketrans('０１２３４５６７８９＋－＊／．（）−×÷', '0123456789+-*/.()-*/')
NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)')
OPERATIONS = {'+':'sum', '-':'difference', '*':'product', '/':'quotient'}
PRECEDENCE = {'+':1, '-':1, '*':2, '/':2, 'u+':3, 'u-':3}
PREFIX = re.compile(r'(?:(?:請(?:幫我)?|幫我)?\s*(?:精確計算|計算|算出)|(?:please\s+)?calculate\b)', re.I)
SEPARATOR = re.compile(r'[，,；;]')
FORMAT = re.compile(r'只(?:輸出|回答|答)(?:整數答案|整數|答案|結果|數字|精確值)|answer only|integer only|only (?:the )?(?:answer|result)', re.I)
ARITHMETIC = re.compile(r'[0-9.+*/()\-\s]+')


def parse_request(question):
    """No intent inference, substring execution, quoted text or history parsing."""
    text = question.strip().translate(TRANSLATION)
    head = PREFIX.match(text)
    if not head:
        return None
    # Consume each boundary once: adjacent whitespace quantifiers in a single
    # whole-message regex can backtrack for minutes on long non-math prose.
    body = text[head.end():].lstrip()
    if body.startswith((':', '：')): body = body[1:].lstrip()
    if len(body) > 1 and body.endswith(('。', '.', '!', '！')): body = body[:-1]
    fields = SEPARATOR.split(body, maxsplit=1)
    expression = fields[0].strip()
    requested = fields[1].strip().casefold() if len(fields) == 2 else ''
    if not ARITHMETIC.fullmatch(expression) or (len(fields) == 2 and not FORMAT.fullmatch(requested)):
        return None
    mode = 'integer' if '整數' in requested or requested == 'integer only' else 'exact' if requested else 'explain'
    return {'schema':'chat_calculation_v1', 'expression':expression, 'output':mode}


def parse_expression(text, cancel=None):
    if len(text) > MAX_CHARS:
        raise ValueError('算式最多4096字。')
    values, operators = [], []
    position = count = depth = 0
    expect_number = True

    def reduce_one():
        op = operators.pop()
        if op in ('u+', 'u-'):
            if not values: raise ValueError('正負號後缺少數值。')
            if op == 'u-': values.append({'operation':'difference', 'values':['0', values.pop()]})
        else:
            if len(values) < 2: raise ValueError('運算子缺少數值。')
            right, left = values.pop(), values.pop()
            values.append({'operation':OPERATIONS[op], 'values':[left, right]})

    while position < len(text):
        expression_math.check_cancel(cancel)
        if text[position].isspace(): position += 1; continue
        count += 1
        if count > MAX_TOKENS: raise ValueError('算式最多128個數字、括號與運算子。')
        token = text[position]
        number = NUMBER.match(text, position)
        if number:
            if not expect_number: raise ValueError('數值之間缺少運算子；不支援隱含乘法。')
            value = number[0]
            if value.startswith('.'): value = '0' + value
            if value.endswith('.'): value += '0'
            values.append(value); position = number.end(); expect_number = False
            continue
        position += 1
        if token == '(':
            if not expect_number: raise ValueError('括號前缺少運算子；不支援隱含乘法。')
            depth += 1
            if depth > 8: raise ValueError('括號最多8層。')
            operators.append(token)
        elif token == ')':
            if expect_number or not depth: raise ValueError('括號不成對或括號內缺少數值。')
            while operators[-1] != '(': reduce_one()
            operators.pop(); depth -= 1; expect_number = False
        elif token in OPERATIONS:
            if expect_number:
                if token not in '+-': raise ValueError('運算子前缺少數值。')
                operators.append('u' + token)
            else:
                while operators and operators[-1] != '(' and PRECEDENCE[operators[-1]] >= PRECEDENCE[token]: reduce_one()
                operators.append(token); expect_number = True
        else:
            raise ValueError('只支援十進位數字、四則運算與括號。')
    if expect_number or depth: raise ValueError('算式未完成或括號不成對。')
    while operators: reduce_one()
    if len(values) != 1: raise ValueError('算式格式不符。')
    expression_math.validate({'expression':values[0]})
    return values[0]


def evaluate(command, cancel=None):
    tree = parse_expression(command['expression'], cancel)
    result = expression_math.calculate({'expression':tree}, cancel)
    if command['output'] == 'integer' and '/' in result['exact']:
        raise ValueError('精確結果不是整數；未自行捨入。請改為「只輸出精確值」或另說明捨入要求。')
    answer = result['exact']
    if command['output'] == 'explain':
        answer = command['expression'] + ' = ' + answer + '\n（本機精確計算，未呼叫模型）'
    receipt = {**command, 'status':'complete', 'tree':tree, 'result':result,
               'model_called':False, 'evaluation':'left_to_right_postorder', 'precedence':'unary, product/quotient, sum/difference'}
    return answer, receipt


def describe(receipt):
    lines = ['本機精確計算；沒有呼叫模型。', '算式：' + receipt['expression'],
             '精確結果：' + receipt['result']['exact'],
             '先括號與正負號，再乘除，後加減；同級由左至右。分數不自動捨入。', '\n已完成的運算（先子項、後父項）：']
    for step in receipt['result']['calculations']:
        lines.append((step['path'] or '根算式') + ' · ' + step['operation'] + ' → ' + step['exact'])
    return '\n'.join(lines)
