"""Bounded rational arithmetic with a separate, deterministic decimal preview."""
import re
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction


MAX_DIGITS = 2000
LIMIT = 10 ** MAX_DIGITS
PRECISION = 40


def bounded(number):
    # Compare integers before converting them to text. Each operation is checked
    # after reduction, before another operand can grow an unbounded intermediate.
    if abs(number.numerator) >= LIMIT or number.denominator >= LIMIT:
        raise ValueError('約分後的中間結果分子／分母各不得超過2000位；未產生部分成功結果。')
    return number


def parse(value):
    if (not isinstance(value, str) or len(value) > MAX_DIGITS * 2 + 2
            or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+|/[0-9]+)?', value)):
        raise ValueError('數字須為整數、有限小數或整數分數字串，例如-2、0.125、-1/7；不接受算式、空白或科學記號。')
    unsigned = value.removeprefix('-')
    if '/' in unsigned:
        numerator, denominator = unsigned.split('/')
        if max(len(numerator), len(denominator)) > MAX_DIGITS:
            raise ValueError('分數的分子與分母各最多2000位數字。')
        if not int(denominator):
            raise ValueError('分數的分母不能為零。')
    elif len(unsigned.replace('.', '')) > MAX_DIGITS:
        raise ValueError('整數／有限小數合計最多2000位數字，前導零也計入。')
    return bounded(Fraction(value))


def calculate(args):
    op, values = args['operation'], args['values']
    if (op not in ('sum', 'difference', 'product', 'quotient')
            or not isinstance(values, list) or not 1 <= len(values) <= 50):
        raise ValueError('計算運算或數量不支援。')
    if op in ('difference', 'quotient') and len(values) != 2:
        raise ValueError('減法與除法恰好需要兩個數。')
    numbers = [parse(value) for value in values]
    result = numbers[0]
    for number in numbers[1:]:
        if op == 'sum':
            result += number
        elif op == 'difference':
            result -= number
        elif op == 'product':
            result *= number
        else:
            if not number:
                raise ValueError('不能除以零。')
            result /= number
        bounded(result)
    # Do not inherit the caller's rounding, exponent bounds, flags or traps.
    # The rational value remains authoritative, even for terminating decimals
    # that require more than PRECISION significant digits to display exactly.
    context = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN,
                      Emin=-999999, Emax=999999, capitals=1, clamp=0,
                      flags=[], traps=[])
    with localcontext(context):
        decimal = Decimal(result.numerator) / Decimal(result.denominator)
    return {'exact': str(result), 'decimal': str(decimal),
            'rounded': Fraction(decimal) != result,
            'decimal_precision': PRECISION, 'decimal_rounding': 'ROUND_HALF_EVEN'}
