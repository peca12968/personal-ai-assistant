"""Exact adaptive check order for independent, equally weighted binary scenarios."""

from fractions import Fraction
from functools import cache


def plan_order(groups, minutes, status, sufficient):
    names = [name for group in groups for name in group]
    bits = {name: 1 << i for i, name in enumerate(names)}
    remaining = tuple(sum(bits[name] for name in group) for group in groups)
    passed = 'supported' if sufficient else 'undetermined'

    def stop(outcome):
        return Fraction(0), 0, {'stop': outcome}

    @cache
    def solve(pending):
        if not pending:
            return stop(passed)
        candidates = []
        for i, group in enumerate(pending):
            rest = pending[:i] + pending[i + 1:]
            for name in names:
                bit = bits[name]
                if not group & bit:
                    continue
                yes_average, yes_max, yes = solve(rest)
                if group == bit:
                    no_average, no_max, no = stop('refuted')
                else:
                    no_average, no_max, no = solve(pending[:i] + (group ^ bit,) + pending[i + 1:])
                duration = minutes[name]
                average = duration + (yes_average + no_average) / 2
                worst = duration + max(yes_max, no_max)
                tree = dict(check=name, minutes=duration, if_true=yes, if_false=no)
                candidates.append((average, worst, bit, tree))
        average, worst, _, tree = min(candidates, key=lambda candidate: candidate[:3])
        return average, worst, tree

    average, worst, tree = solve(remaining) if status == 'undetermined' else stop(status)
    return dict(tree=tree, average_minutes=[average.numerator, average.denominator], max_minutes=worst)


def step_text(node, labels):
    if 'check' in node:
        return f'查「{node["check"]}」（{node["minutes"]} 分鐘）'
    outcome = node['stop']
    if outcome == 'undetermined':
        return f'停止這份檢查，仍是「{labels[outcome]}」，需要充分條件或直接證據'
    return f'停止檢查，判定「{labels[outcome]}」'


def answer_text(order, labels):
    tree = order['tree']
    if 'check' not in tree:
        return step_text(tree, labels) + '。'
    numerator, denominator = order['average_minutes']
    # At most eight binary checks, so eight decimal places represent every possible mean exactly.
    average = f'{numerator / denominator:.8f}'.rstrip('0').rstrip('.')
    return '\n'.join([
        '先' + step_text(tree, labels) + '。',
        '若符合要求，就' + step_text(tree['if_true'], labels) + '。',
        '若不符合要求，就' + step_text(tree['if_false'], labels) + '。',
        f'以待查條件的各種結果組合等權估算，平均需 {average} 分鐘，最長需 {order["max_minutes"]} 分鐘。',
    ])


def tree_lines(node, labels, indent=''):
    yield indent + step_text(node, labels)
    if 'check' in node:
        for branch, lead in (('if_true', '符合'), ('if_false', '不符合')):
            yield indent + '  ' + lead
            yield from tree_lines(node[branch], labels, indent + '    ')
