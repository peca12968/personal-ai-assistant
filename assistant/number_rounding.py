"""Explicit decimal-place rounding of bounded exact rationals, without floats."""
from fractions import Fraction
from . import exact_math
from .engine import EngineError


MODES = ('half_even', 'half_up', 'floor', 'ceiling', 'truncate')
GUIDANCE = '''round_number只做明確指定規則的精確數值捨入，args精確為{"value":"1132887/200","places":2,"mode":"half_up"}。
value沿用calculate的精確數字字串語法與2000位限制，從前一步exact取值，不能用40位decimal預覽代替原值；places為0到12的嚴格整數，指小數位而非有效位數。mode必須明列：half_even最接近、恰半取偶；half_up最接近、恰半遠離零（負數-2.345到2位為-2.35）；floor向負無限大；ceiling向正無限大；truncate朝零截斷。
回傳input_exact原精確值、exact捨入後精確值、fixed_decimal固定小數位文字（保留尾零，零不帶負號）、places、mode、value_changed、is_tie及difference_exact（捨入後減原值）。is_tie只表示原數位於指定精度兩個相鄰值的正中間，不代表只有恰半才改值；value_changed是數值是否改變，不是格式改變。所有回傳分數約分後分子／分母也各限2000位，超限整次失敗。很長fixed_decimal可能因補零超過輸入字數上限，後續使用exact。
捨入會改變原精確值；依使用者指定時點（逐項或總計後）與規則執行，不能自行假定貨幣、稅則或改成不同時點。必要規則不明且影響結果時詢問；不得把工具可用當作已選定規則或永久偏好。僅試算，不進行付款、開票、記帳或文件產出，不增加文件引用或其他權限。'''


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise EngineError('數值捨入已停止，未產生成功結果。', 'cancelled')


def validate(args):
    if not isinstance(args, dict) or set(args) != {'value', 'places', 'mode'}:
        raise ValueError('數值捨入需明確提供value、places及mode。')
    if type(args['places']) is not int or not 0 <= args['places'] <= 12:
        raise ValueError('小數位places需為0到12的整數。')
    if not isinstance(args['mode'], str) or args['mode'] not in MODES:
        raise ValueError('捨入規則需為half_even、half_up、floor、ceiling或truncate。')
    return exact_math.parse(args['value'])


def calculate(args, cancel=None):
    check_cancel(cancel)
    number = validate(args)
    places, mode = args['places'], args['mode']
    scale = 10 ** places
    units, remainder = divmod(abs(number.numerator) * scale, number.denominator)
    sign = -1 if number < 0 else 1
    twice = remainder * 2
    is_tie = twice == number.denominator
    increment = False
    if mode in ('half_even', 'half_up'):
        increment = twice > number.denominator or (is_tie and (mode == 'half_up' or units % 2 == 1))
    elif mode == 'floor':
        increment = sign < 0 and remainder != 0
    elif mode == 'ceiling':
        increment = sign > 0 and remainder != 0
    units = (units + int(increment)) * sign
    result = exact_math.bounded(Fraction(units, scale))
    difference = exact_math.bounded(result - number)
    whole, fraction = divmod(abs(units), scale)
    fixed = ('-' if units < 0 else '') + str(whole)
    if places:
        fixed += '.' + str(fraction).zfill(places)
    check_cancel(cancel)
    return {'status': 'number_rounded', 'input_exact': str(number), 'places': places, 'mode': mode,
            'exact': str(result), 'fixed_decimal': fixed, 'value_changed': result != number,
            'is_tie': is_tie, 'difference_exact': str(difference)}
