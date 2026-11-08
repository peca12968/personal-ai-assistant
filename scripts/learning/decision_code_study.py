"""Compile a synthetic prerequisite study with balanced decisions and reply codes."""
from collections import Counter
from itertools import product
import re


SCHEMA = 'synthetic_prerequisite_decision_codes_v1'
PATTERNS = {
    'all': ('TT', 'TT', 'FT', 'FU', 'TU', 'UT'),
    'either': ('TF', 'TU', 'FF', 'FF', 'FU', 'UF'),
    'shared_backup': ('TTF', 'TTU', 'FTF', 'FTU', 'TFU', 'UTF'),
    'necessary_only': ('TT', 'TT', 'FT', 'FU', 'TT', 'TU'),
    'required_marker': ('T', 'T', 'F', 'F', 'T', 'U'),
}
NECESSARY_RULES = ('necessary_only', 'required_marker')
VERDICTS = ('supported', 'refuted', 'undetermined')
OBSERVATIONS = {'T': (True,), 'F': (False,), 'U': (False, True)}


def decide(rule, pattern, witnessed=False):
    worlds = product(*(OBSERVATIONS[state] for state in pattern))
    if rule == 'either':
        outcomes = {any(world) for world in worlds}
    elif rule == 'shared_backup':
        outcomes = {common and (first or second) for common, first, second in worlds}
    else:
        outcomes = {all(world) for world in worlds}
        if rule in NECESSARY_RULES:
            if witnessed:
                if outcomes != {True}:
                    raise ValueError('A witnessed outcome must agree with the necessary facts.')
            else:
                outcomes.add(False)
    if outcomes == {True}:
        return 'supported'
    if outcomes == {False}:
        return 'refuted'
    return 'undetermined'


def rule_text(rule, action, conditions):
    if rule == 'required_marker':
        condition, = conditions
        return f'凡是能{action}的情況，「{condition}」必定成立。即使不能{action}，該標記也可能存在。'
    first, second, *remaining = (f'「{condition}」' for condition in conditions)
    if rule == 'either':
        return f'{first}和{second}至少一項成立即可{action}，兩項都不成立就不能{action}。'
    if rule == 'shared_backup':
        return f'{action}須先有{first}，且{second}或{remaining[0]}至少一項成立。這兩類要求都滿足即可{action}。'
    if rule == 'necessary_only':
        return f'規則只說明{action}必須同時滿足{first}和{second}，沒有說這兩項已涵蓋所有要求。'
    return f'{action}需要{first}和{second}同時成立，這兩項就是完整要求。'


def validate_group(group, seen):
    if not isinstance(group, dict) or set(group) != {'id', 'rule', 'action', 'conditions', 'codes'}:
        raise ValueError('Invalid decision-code scenario fields.')
    name, rule = group['id'], group['rule']
    action, conditions, codes = group['action'], group['conditions'], group['codes']
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_]{1,32}', name) or name in seen
            or not isinstance(rule, str) or rule not in PATTERNS
            or not isinstance(conditions, list) or len(conditions) != len(PATTERNS[rule][0])
            or any(not isinstance(x, str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,30}', x)
                   for x in [action, *conditions])
            or len(set(conditions)) != len(conditions)
            or not isinstance(codes, list) or len(codes) != 3
            or any(not isinstance(x, str) or not re.fullmatch(r'[A-Z0-9\u4e00-\u9fff]{1,4}', x) for x in codes)
            or len(set(codes)) != 3):
        raise ValueError('Invalid or duplicate decision-code scenario.')
    seen.add(name)


def compile_group(group, heldout):
    rule, action, conditions, codes = (group[key] for key in ('rule', 'action', 'conditions', 'codes'))
    rule_clause = rule_text(rule, action, conditions)
    rows = []
    wording = {
        'T': '已確認「{}」成立',
        'F': '已確認「{}」不成立',
        'U': '「{}」尚未查證',
    }
    for index, pattern in enumerate(PATTERNS[rule]):
        witnessed = rule in NECESSARY_RULES and index < 2
        verdict = decide(rule, pattern, witnessed)
        offset = index % 2
        assigned = codes[offset:] + codes[:offset]
        available, blocked, unknown = assigned
        facts = '；'.join(wording[state].format(condition) for state, condition in zip(pattern, conditions, strict=True))
        if witnessed:
            facts += f'；另有實測證實可以{action}'
        question = f'已知紀錄為：{facts}。現在能否{action}？'
        if heldout:
            instruction = f'回覆代碼「{unknown}」表示待確認，「{blocked}」表示確定不能，「{available}」表示確定能。整個回答只留代碼。'
        else:
            instruction = f'確定可行回「{available}」，確定不可行回「{blocked}」，現有資料不足回「{unknown}」。只輸出指定代碼。'
        instruction_first = (index // 2) % 2 == int(heldout)
        prompt = instruction + rule_clause + question if instruction_first else rule_clause + question + instruction
        rows.append(dict(id=f"{group['id']}_{index + 1}", contrast=group['id'], prompt=prompt,
                         answer=assigned[VERDICTS.index(verdict)], verdict=verdict,
                         code_map=dict(zip(VERDICTS, assigned, strict=True))))
    if (Counter(row['verdict'] for row in rows) != Counter(dict.fromkeys(VERDICTS, 2))
            or Counter(row['answer'] for row in rows) != Counter(dict.fromkeys(codes, 2))):
        raise ValueError('Each scenario must balance decisions and output codes.')
    return rows


def compile_study(spec):
    if not isinstance(spec, dict) or set(spec) != {'schema', 'training', 'heldout'} or spec['schema'] != SCHEMA:
        raise ValueError('Decision-code study schema mismatch.')
    seen = set()
    result = {}
    for split, limit in (('training', 7), ('heldout', 2)):
        groups = spec[split]
        if not isinstance(groups, list) or not 1 <= len(groups) <= limit:
            raise ValueError('Invalid number of decision-code scenarios.')
        rows = []
        for group in groups:
            validate_group(group, seen)
            rows.extend(compile_group(group, heldout=split == 'heldout'))
        result[split] = rows
    return result
