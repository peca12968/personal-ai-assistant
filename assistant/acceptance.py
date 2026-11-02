"""Declarative, host-graded acceptance cases for source self-improvement."""
import json
import re
from pathlib import Path

MODULES = {'communication': 'assistant/communication.py', 'followup': 'assistant/context.py',
           'retrieval': 'assistant/retrieval.py'}


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON 不可包含重複欄位。')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('不接受非有限數字。')))


def text_value(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('驗收文字空白、過長或無效。')


def validate_suite(suite, selected=None):
    if not isinstance(suite, dict) or set(suite) != {'version', 'cases'} or type(suite['version']) is not int or suite['version'] != 1:
        raise ValueError('驗收格式需為 version=1 與 cases。')
    cases = suite['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= 40:
        raise ValueError('驗收需有 1–40 個案例。')
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('驗收案例需為物件。')
        cid, kind = case.get('id'), case.get('kind')
        if not isinstance(cid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', cid) or cid in seen:
            raise ValueError('案例 ID 必須唯一，使用英數字、底線或連字號。')
        seen.add(cid)
        if not isinstance(kind, str) or kind not in MODULES or (selected is not None and MODULES[kind] not in selected):
            raise ValueError('案例類型必須對應本輪選定的溝通、脈絡或檢索模組。')
        if kind in {'communication', 'followup'}:
            if set(case) != {'id', 'kind', 'question', 'history', 'expect'}:
                raise ValueError('溝通／接續案例欄位不符。')
            text_value(case['question'])
            history = case['history']
            if not isinstance(history, list) or len(history) > 16:
                raise ValueError('案例前文最多 16 則。')
            for row in history:
                if not isinstance(row, dict) or set(row) != {'role', 'content'} or row['role'] not in ('user', 'assistant'):
                    raise ValueError('前文需包含 user/assistant 與 content。')
                text_value(row['content'])
            expected = case['expect']
            if kind == 'communication':
                allowed = {'length_hint', 'possible_correction', 'has_prior_user_turn', 'hints_only'}
                if not isinstance(expected, dict) or not expected or not set(expected).issubset(allowed):
                    raise ValueError('溝通預期需指定已支援的提示欄位。')
                for key, value in expected.items():
                    if key == 'length_hint':
                        if value not in ('concise', 'detailed', 'mixed', 'unspecified'):
                            raise ValueError('無效的回答長度預期。')
                    elif type(value) is not bool:
                        raise ValueError('提示欄位預期需為布林值。')
            elif expected is not None:
                text_value(expected, 16000)
        else:
            if set(case) != {'id', 'kind', 'query', 'memories', 'limit', 'expect'}:
                raise ValueError('檢索案例欄位不符。')
            text_value(case['query'])
            memories = case['memories']
            if not isinstance(memories, list) or not 1 <= len(memories) <= 30 or type(case['limit']) is not int or not 1 <= case['limit'] <= 8:
                raise ValueError('檢索案例需 1–30 筆記憶與 1–8 筆結果上限。')
            ids = set()
            for row in memories:
                if not isinstance(row, dict) or set(row) != {'id', 'kind', 'content'} or type(row['id']) is not int or row['id'] < 1 or row['id'] in ids or row['kind'] not in ('fact', 'preference', 'lesson'):
                    raise ValueError('記憶案例格式或 ID 不符。')
                ids.add(row['id'])
                text_value(row['content'])
            expected = case['expect']
            if not isinstance(expected, list) or len(expected) > case['limit'] or any(type(item) is not int or item not in ids for item in expected) or len(set(expected)) != len(expected):
                raise ValueError('檢索預期需為排序後的記憶 ID 清單。')
    if len(json.dumps(suite, ensure_ascii=False).encode('utf-8')) > 200_000:
        raise ValueError('驗收案例總量不可超過 200 KB。')
    # Detach caller data so an in-flight run cannot change its own expectations.
    return strict_json(json.dumps(suite, ensure_ascii=False))


def load_suite(path, selected=None):
    with Path(path).open('rb') as source:
        raw = source.read(200_001)
    if len(raw) > 200_000:
        raise ValueError('驗收檔案不可超過 200 KB。')
    return validate_suite(strict_json(raw.decode('utf-8-sig')), selected)


def grade(suite, observations):
    """Only host-provided expectations determine pass/fail, never model grades."""
    if not isinstance(observations, list) or len(observations) != len(suite['cases']):
        raise ValueError('驗收觀察筆數不符。')
    results = []
    for case, row in zip(suite['cases'], observations):
        if not isinstance(row, dict) or row.get('id') != case['id'] or not set(row).issubset({'id', 'actual', 'error'}) or ('actual' in row) == ('error' in row):
            raise ValueError('驗收觀察格式或案例順序不符。')
        actual, expected = row.get('actual'), case['expect']
        passed = 'error' not in row
        if case['kind'] == 'communication':
            passed = passed and isinstance(actual, dict) and all(
                key in actual and type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items())
        else:
            passed = passed and type(actual) is type(expected) and actual == expected
            if case['kind'] == 'retrieval' and isinstance(actual, list):
                passed = passed and all(type(item) is int for item in actual)
        results.append({'id': case['id'], 'expected': expected, **row, 'passed': bool(passed)})
    return results


def compare(suite, baseline, candidate):
    before, after = grade(suite, baseline), grade(suite, candidate)
    resolved = [old['id'] for old, new in zip(before, after) if not old['passed'] and new['passed']]
    regressed = [old['id'] for old, new in zip(before, after) if old['passed'] and not new['passed']]
    all_passed = all(row['passed'] for row in after)
    return {'status': 'verified_improvement' if all_passed and resolved else
            'already_satisfied' if all_passed else 'failed',
            'baseline_passed': sum(row['passed'] for row in before),
            'candidate_passed': sum(row['passed'] for row in after), 'total': len(after),
            'resolved_ids': resolved, 'regressed_ids': regressed,
            'baseline': before, 'candidate': after}
