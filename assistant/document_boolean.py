"""Parse bounded table conditions and evaluate them with three truth values."""
import re


_SYMBOLS = {'且': '且', '或': '或', '(': '(', ')': ')', '（': '(', '）': ')'}
_PRECEDENCE = {'或': 1, '且': 2}
_OPERATORS = {'或': 'or', '且': 'and'}
# A numeric comparison's 或 and all symbols inside a quoted value are data.
_SPLIT = re.compile(r'(「[^」]*」|大於或等於|小於或等於|[且或()（）])')


def _tokens(text):
    tokens, parts = [], []
    for part in _SPLIT.split(text):
        if part not in _SYMBOLS:
            parts.append(part)
            continue
        if atom := ''.join(parts).strip():
            tokens.append(atom)
        parts.clear()
        tokens.append(_SYMBOLS[part])
    if atom := ''.join(parts).strip():
        tokens.append(atom)
    return tokens


def parse(text, parse_atom):
    """Return the original-order filters and a precedence-aware expression tree."""
    tokens = _tokens(text)
    if len(tokens) > 80 or not any(token in ('或', '(') for token in tokens):
        return None
    filters = []
    position = 0

    def take():
        nonlocal position
        if position >= len(tokens):
            raise ValueError('條件式不完整。')
        token = tokens[position]
        position += 1
        return token

    def expression(minimum, depth):
        token = take()
        if token == '(':
            if depth >= 4:
                raise ValueError('括號層數超出上限。')
            left = expression(1, depth + 1)
            if take() != ')':
                raise ValueError('括號不完整。')
        else:
            condition = parse_atom(token)
            if condition is None or len(filters) >= 8:
                raise ValueError('條件無效或超出上限。')
            left = len(filters)
            filters.append(condition)
        while position < len(tokens) and _PRECEDENCE.get(tokens[position], 0) >= minimum:
            operator = take()
            right = expression(_PRECEDENCE[operator] + 1, depth)
            left = dict(operator=_OPERATORS[operator], left=left, right=right)
        return left

    try:
        tree = expression(1, 0)
    except ValueError:
        return None
    return (filters, tree) if position == len(tokens) else None


def evaluate(tree, outcomes):
    """Return the result and the unresolved leaf indexes that still affect it."""
    if isinstance(tree, int):
        status = outcomes[tree]
        return status, [tree] if status == 'unknown' else []
    left, left_pending = evaluate(tree['left'], outcomes)
    right, right_pending = evaluate(tree['right'], outcomes)
    decisive = 'false' if tree['operator'] == 'and' else 'true'
    if decisive in (left, right):
        return decisive, []
    if 'unknown' in (left, right):
        return 'unknown', left_pending + right_pending
    return left, []


def describe(tree):
    if isinstance(tree, int):
        return f'條件 {tree + 1}'
    symbol = '且' if tree['operator'] == 'and' else '或'
    return f'（{describe(tree["left"])} {symbol} {describe(tree["right"])}）'
