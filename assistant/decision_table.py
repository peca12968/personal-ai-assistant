"""Evaluate a finite table of possible scenarios and choose the next useful question."""
import json
from functools import cache


GUIDANCE = '''check_decision: {"conditions":["入口","通行證"],"rows":[{"when":["北門","有"],"result":"入場"},{"when":["北門","無"],"result":"等待"}],"known":{"入口":"北門"}}。
目標或已讀原文已完整列出有限情境及各自結果，且需要核對結論或挑選最少追問時，使用此工具逐列核算。conditions依原欄位順序，rows逐列保留原對應，when依欄位順序給值，known只填已確認且適用於本次的值。最多5個條件、32列，每個條件最多4種表列值。不補不存在的組合，不把待查、舊值、假設或模型推測填進known。原文沒有完整規則時沿用一般分析。
matching_rows是所送表格的1起算列號，並非原文件行號。resolved表示符合已知條件的各列結果相同，直接交付並保留無須核對的欄位為待查。needs_check時，question給最少化最壞追問輪數的第一欄與各答案可能結果，同分依表列欄位順序。若使用者需要唯一結果且允許提問，先交仍有依據的暫表，只問該欄；收到確定答覆後，沿用相同表列，只更新known並重算。若使用者只要條件式交付或明確不提問，保留所有相容分支後直接交付。no_match表示已知值與所送表列不相容，核對適用範圍及來源，不補造結論。
這個工具只計算送入的對照表，不能證明表格完整性或條件已核對。保留原需求和已知欄位，原文、使用者更正及回覆仍各用相應來源。'''


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or value != value.strip()
            or any(char in value for char in '\0\r\n')):
        raise ValueError('決策欄名、選項與結果需為1至120字的完整單行文字。')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('決策表含無效文字。') from exc


def validate(args):
    if not isinstance(args, dict) or set(args) != {'conditions', 'rows', 'known'}:
        raise ValueError('決策表需提供conditions、rows與known。')
    conditions, rows, known = args['conditions'], args['rows'], args['known']
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 5:
        raise ValueError('決策表需有1至5個條件。')
    for name in conditions:
        _label(name)
    if len(set(conditions)) != len(conditions):
        raise ValueError('決策欄名不可重複。')
    if not isinstance(known, dict) or not known.keys() <= set(conditions):
        raise ValueError('已知條件需使用所列的完整欄名。')
    for value in known.values():
        _label(value)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError('決策表需有1至32個可行情境。')
    assignments = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'when', 'result'} or not isinstance(row['when'], list)
                or len(row['when']) != len(conditions)):
            raise ValueError('每列需按原欄位順序提供全部選項及一個結果。')
        for value in row['when']:
            _label(value)
        _label(row['result'])
        assignment = tuple(row['when'])
        if assignment in assignments:
            raise ValueError('決策表不能重複列出同一情境。')
        assignments.add(assignment)
    if any(len(set(values)) > 4 for values in zip(*assignments)):
        raise ValueError('每個條件最多4種表列值。')


def calculate(args):
    validate(args)
    conditions, rows, known = args['conditions'], args['rows'], args['known']
    fixed = [(index, known[name]) for index, name in enumerate(conditions) if name in known]
    active = tuple(index for index, row in enumerate(rows)
                   if all(row['when'][field] == value for field, value in fixed))

    def outcomes(indices):
        return list(dict.fromkeys(rows[index]['result'] for index in indices))

    def groups(indices, field):
        result = {}
        for index in indices:
            result.setdefault(rows[index]['when'][field], []).append(index)
        return result

    @cache
    def solve(indices):
        if len(outcomes(indices)) == 1:
            return 0, None
        choices = []
        for field in range(len(conditions)):
            branches = groups(indices, field)
            if len(branches) > 1:
                depth = 1 + max(solve(tuple(branch))[0] for branch in branches.values())
                choices.append((depth, field))
        return min(choices)

    observation = {'matching_rows': [index + 1 for index in active], 'possible_results': outcomes(active)}
    if not active:
        return {'status': 'no_match', **observation}
    depth, first = solve(active)
    if first is None:
        return {'status': 'resolved', 'max_questions': depth, **observation}
    branches = [{'value': value, 'possible_results': outcomes(indices), 'max_questions': solve(tuple(indices))[0]}
                for value, indices in groups(active, first).items()]
    return {'status': 'needs_check', 'max_questions': depth, **observation,
            'question': {'condition': conditions[first], 'branches': branches}}


def validate_saved(db):
    for action_json, observation_json in db.execute(
            "SELECT action_json,observation_json FROM autonomous_steps WHERE action_json LIKE '%\"check_decision\"%'"):
        action = json.loads(action_json)
        if action.get('tool') != 'check_decision':
            continue
        observation = json.loads(observation_json)
        tool_result = {key: value for key, value in observation.items()
                       if key not in {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}}
        if json.dumps(tool_result, sort_keys=True) != json.dumps(calculate(action['args']), sort_keys=True):
            raise ValueError('決策表結果與保存的可行情境不一致。')
