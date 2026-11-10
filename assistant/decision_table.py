"""Evaluate a finite table of possible scenarios and choose the next useful question."""
import json
from functools import cache
from itertools import product
from itertools import combinations
from math import prod


GUIDANCE = '''check_decision: {"conditions":["入口","通行證"],"rows":[{"when":["北門","有"],"result":"入場"},{"when":["北門","無"],"result":"等待"}],"known":{"入口":"北門"}}。
目標或已讀原文已列明完整可行情境，或明定各欄選項及哪些組合不可行時，使用此工具核對結論、找缺列或挑選最少追問。conditions依原欄位順序，rows逐列保留原對應，when依欄位順序給值，known只填已確認且適用於本次的值。最多5個條件、32列，每個條件最多4種值。不把待查、舊值、假設或模型推測填進known。
若已知各欄完整選項，且各種組合皆可發生或已明文排除例外，另傳domains（如{"入口":["北門","南門"],"通行證":["有","無"]}）與excluded（依欄位順序列明文排除的組合，沒有則[]）。此模式各欄選項組合最多32種。保留已公布的對應；不同同級資料若針對同一組合給不同結果，在rows為該組合分列，不自行選定。只有明文不可能或排除的組合放excluded，缺規則與另案安排都不是排除，也不能代填缺列。分清兩份表是否適用同一事項。無法確定適用範圍或完整選項時沿用一般分析。收到正式補充後只新增有效對應或修正已解決的列，保留舊來源及適用範圍。
incomplete_table表示目前相容組合中有缺列或不同結果。coverage列出相容的缺列、衝突與明文排除，recorded_results僅為已記載結果。保留仍有來源的部分；需要唯一結果時，只問影響本次決策的處置規則或哪份說法生效，勿重問已確認欄位。收到可解決缺列或衝突的正式補充後，先更新該筆對應並再次執行check_decision，再交付。其餘適用的欄位、規則和排除照原資料，舊步驟保留。要求只交條件式成果時，標示缺漏或衝突，不追問。其他範圍的缺列不阻擋本次可確定的結果。
matching_rows是所送表格的1起算列號，並非原文件行號。resolved表示符合已知條件的各列結果相同，直接交付並保留無須核對的欄位為待查。needs_check時，question給最少化最壞追問輪數的第一欄與各答案可能結果，同分依表列欄位順序。若使用者需要唯一結果且允許提問，先交仍有依據的暫表，只問該欄；收到確定答覆後，沿用相同表列，只更新known並重算。若使用者只要條件式交付或明確不提問，保留所有相容分支後直接交付。no_match表示已知值與所送表列不相容，核對適用範圍及來源，不補造結論。
這個工具只計算送入的對照表，不能證明表格完整性或條件已核對。保留原需求和已知欄位，原文、使用者更正及回覆仍各用相應來源。
原文已明定可行情境、規則優先級及同級衝突時，優先用rules取代rows，讓程式展開並核對每個相容情境，並提供conditions、domains、excluded、known。例如rules為[{"priority":0,"when":{},"result":"前台"},{"priority":2,"when":{"級別":"高"},"result":"南架"}]。最多16條，priority為0至100整數且較大優先，只按原文明定關係轉錄。when只列該規則明定的限制，空物件只用於明文通用規則。不要推定最後一條或較具體的規則優先。
priority_cases逐一列適用的最高級規則、被覆蓋規則和仍缺依據的情境。同級有不同處置時保留各結果；沒有適用規則時保留缺口。winning_rules及overridden_rules是所送規則的1起算編號，引用仍用已讀的實際原行。若有question，先確認會區分結果或是否需要來源的欄位。max_questions只計確認欄位，不計補正式規則。沒有可區分的欄位且needs_evidence為真時，核對仍缺的依據。收到確認值後沿用同一規則重算。只要條件式暫表時按要求交付，不追問。
原文只有成對的無條件優先記載時，同樣用rules，各條省略priority，另傳precedence，例如[[1,2]]表示第1條優先於第2條。只送原文明定的關係，連彼此矛盾的方向也照實保留；沒有記載時可用空陣列。不要將部分順序轉成數字或推成同級。若關係只在特定條件成立，不可當作通用連線。
若優先關係只在特定條件或版本生效，用{"higher":1,"lower":2,"when":{"班別":"晚"}}替代該對編號，可與無條件編號對混用。when只填原文明定的條件與值。逐一在相容情境內核對順序，同一情境才可能構成循環。active_precedence與disputed_precedence是送入precedence的1起算序號，引用時仍用實際原文。未生效的關係不參與傳遞。
disputed_relations列出參與矛盾循環的原連線，工具不以它們推導其他順序。不矛盾的無條件關係可以傳遞，即使中間規則未觸發。未被覆蓋的候選若有不同結果，unranked_pairs列出尚無有效優先依據的組合，cyclic_pairs列出受循環影響的組合。只問仍會影響本次的候選關係；正式補充只修改它確實確認或取代的關係，再保留原規則重算。'''


def _label(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or value != value.strip()
            or any(char in value for char in '\0\r\n')):
        raise ValueError('決策欄名、選項與結果需為1至120字的完整單行文字。')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('決策表含無效文字。') from exc


def _assignment(values, size):
    if not isinstance(values, list) or len(values) != size:
        raise ValueError('每個組合需按原欄位順序提供全部選項。')
    for value in values:
        _label(value)
    return tuple(values)


def _declared_scenarios(args, conditions):
    if 'domains' not in args:
        return None, set()
    domains, excluded = args['domains'], args['excluded']
    if not isinstance(domains, dict) or domains.keys() != set(conditions):
        raise ValueError('宣告選項需涵蓋每個決策欄。')
    options = []
    for name in conditions:
        values = domains[name]
        if not isinstance(values, list) or not 1 <= len(values) <= 4:
            raise ValueError('每個條件需有1至4種宣告值。')
        options.append(_assignment(values, len(values)))
        if len(set(options[-1])) != len(values):
            raise ValueError('同一決策欄的宣告值不可重複。')
    if prod(map(len, options)) > 32:
        raise ValueError('宣告選項的全部組合最多32種。')
    declared = tuple(product(*options))
    if not isinstance(excluded, list) or len(excluded) > len(declared):
        raise ValueError('排除需列出有限且不同的宣告組合。')
    omitted = {_assignment(row, len(conditions)) for row in excluded}
    if len(omitted) != len(excluded) or not omitted <= set(declared):
        raise ValueError('排除的組合需在宣告選項內且不得重複。')
    return declared, omitted


def validate(args):
    base = {'conditions', 'known'}
    cases = {'domains', 'excluded'}
    rule_fields = base | cases | {'rules'}
    if not isinstance(args, dict) or set(args) not in (
            base | {'rows'}, base | cases | {'rows'}, rule_fields, rule_fields | {'precedence'}):
        raise ValueError('決策表需提供conditions、known及rows，或提供domains、excluded及rules。')
    conditions, known = args['conditions'], args['known']
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
    declared, excluded = _declared_scenarios(args, conditions)
    if 'rules' in args:
        relative = 'precedence' in args
        _validate_priority_rules(args['rules'], args['domains'], relative=relative)
        if relative:
            _validate_precedence(args['precedence'], len(args['rules']), args['domains'])
        return declared, excluded
    rows = args['rows']
    minimum = 1 if declared is None else 0
    if not isinstance(rows, list) or not minimum <= len(rows) <= 32:
        raise ValueError('決策表最多32列；未宣告欄位選項時至少需要一列。')
    assignments = set()
    reports = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'when', 'result'}:
            raise ValueError('每列需按原欄位順序提供全部選項及一個結果。')
        assignment = _assignment(row['when'], len(conditions))
        _label(row['result'])
        if declared is None and assignment in assignments:
            raise ValueError('決策表不能重複列出同一情境。')
        if declared is not None and (assignment not in declared or assignment in excluded):
            raise ValueError('表列情境需在宣告選項內，且不能同時排除。')
        report = assignment, row['result']
        if report in reports:
            raise ValueError('同一情境的相同結果不得重複列出。')
        assignments.add(assignment)
        reports.add(report)
    if any(len(set(values)) > 4 for values in zip(*assignments)):
        raise ValueError('每個條件最多4種表列值。')
    return declared, excluded


def _validate_priority_rules(rules, domains, *, relative):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 16:
        raise ValueError('決策需提供1至16條明文規則。')
    fields = {'when', 'result'} if relative else {'priority', 'when', 'result'}
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != fields:
            raise ValueError('規則欄位須符合所選的優先順序格式。')
        condition = rule['when']
        if not relative and (type(rule['priority']) is not int or not 0 <= rule['priority'] <= 100):
            raise ValueError('規則優先級須為0至100的整數。')
        _validate_rule_condition(condition, domains)
        _label(rule['result'])


def _validate_rule_condition(condition, domains):
    if not isinstance(condition, dict) or not condition.keys() <= domains.keys():
        raise ValueError('規則條件須使用宣告欄名。')
    for field, value in condition.items():
        _label(value)
        if value not in domains[field]:
            raise ValueError('規則條件值須為宣告選項。')


def _validate_precedence(edges, rule_count, domains):
    if not isinstance(edges, list) or len(edges) > 64:
        raise ValueError('最多可提供64個成對優先關係。')
    seen = set()
    for edge in edges:
        condition = {}
        if isinstance(edge, dict):
            if set(edge) != {'higher', 'lower', 'when'}:
                raise ValueError('條件式優先關係須指定higher、lower及when。')
            condition = edge['when']
            _validate_rule_condition(condition, domains)
            pair = [edge['higher'], edge['lower']]
        else:
            pair = edge
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(index) is not int or not 1 <= index <= rule_count for index in pair)
                or pair[0] == pair[1]):
            raise ValueError('每個優先關係須指定兩個不同的已列規則。')
        key = tuple(pair), frozenset(condition.items())
        if key in seen:
            raise ValueError('同條件的同方向優先關係不可重複。')
        seen.add(key)


def question_order(assignments, labels, active):
    def groups(indices, field):
        result = {}
        for index in indices:
            result.setdefault(assignments[index][field], []).append(index)
        return result

    @cache
    def solve(indices):
        if len({labels[index] for index in indices}) == 1:
            return 0, None
        choices = []
        for field in range(len(assignments[indices[0]])):
            branches = groups(indices, field)
            if len(branches) > 1:
                depth = 1 + max(solve(tuple(branch))[0] for branch in branches.values())
                choices.append((depth, field))
        return min(choices)

    depth, first = solve(active)
    branches = [] if first is None else [(value, indices, solve(tuple(indices))[0])
                                         for value, indices in groups(active, first).items()]
    return depth, first, branches


def _matched_rules(assignment, conditions, rules):
    values = dict(zip(conditions, assignment))
    return {index: rule for index, rule in enumerate(rules, 1)
            if all(values[field] == value for field, value in rule['when'].items())}


def _rule_case(assignment, matched, winners, **details):
    outcomes = list(dict.fromkeys(matched[index]['result'] for index in winners))
    status = 'missing_rule' if not winners else 'resolved' if len(outcomes) == 1 else 'conflicting_rules'
    return {'when': list(assignment), 'status': status, **details, 'winning_rules': winners,
            'overridden_rules': [index for index in matched if index not in winners],
            'recorded_results': outcomes}


def _priority_case(assignment, conditions, rules):
    matched = _matched_rules(assignment, conditions, rules)
    priority = max((rule['priority'] for rule in matched.values()), default=None)
    winners = [index for index, rule in matched.items() if rule['priority'] == priority]
    return _rule_case(assignment, matched, winners, priority=priority)


def _reachable_rules(rule_count, edges):
    reachable = {index: set() for index in range(1, rule_count + 1)}
    for higher, lower in edges:
        reachable[higher].add(lower)
    for through, successors in reachable.items():
        for destinations in reachable.values():
            if through in destinations:
                destinations.update(successors)
    return reachable


def _precedence_case(assignment, conditions, rules, reachable, stated_paths):
    matched = _matched_rules(assignment, conditions, rules)
    overridden = set().union(*(reachable[index] for index in matched))
    winners = [index for index in matched if index not in overridden]
    unranked, cyclic = [], []
    for first, second in combinations(winners, 2):
        if matched[first]['result'] == matched[second]['result']:
            continue
        pairs = cyclic if second in stated_paths[first] and first in stated_paths[second] else unranked
        pairs.append([first, second])
    return _rule_case(assignment, matched, winners, unranked_pairs=unranked, cyclic_pairs=cyclic)


def _precedence_paths(rule_count, edges):
    stated_paths = _reachable_rules(rule_count, edges)
    disputed = [edge for edge in edges if edge[0] in stated_paths[edge[1]]]
    # Keep recorded contradictions visible, but do not infer through them.
    reachable = _reachable_rules(rule_count, [edge for edge in edges if edge not in disputed]) if disputed else stated_paths
    return reachable, stated_paths, disputed


def _scoped_precedence_cases(assignments, conditions, rules, edges):
    relations = [(index, (edge['higher'], edge['lower']), edge['when']) if isinstance(edge, dict)
                 else (index, tuple(edge), {}) for index, edge in enumerate(edges, 1)]
    cases, disputes = [], set()
    for assignment in assignments:
        values = dict(zip(conditions, assignment))
        applicable = [(index, pair) for index, pair, when in relations if when.items() <= values.items()]
        active_pairs = list(dict.fromkeys(pair for _, pair in applicable))
        reachable, stated_paths, disputed = _precedence_paths(len(rules), active_pairs)
        case = _precedence_case(assignment, conditions, rules, reachable, stated_paths)
        case['active_precedence'] = [index for index, _ in applicable]
        case['disputed_precedence'] = [index for index, pair in applicable if pair in disputed]
        disputes.update(case['disputed_precedence'])
        cases.append(case)
    return cases, [edge for index, edge in enumerate(edges, 1) if index in disputes]


def _calculate_priority(args, declared, excluded):
    conditions, known, rules = args['conditions'], args['known'], args['rules']
    fixed = [(index, known[name]) for index, name in enumerate(conditions) if name in known]
    compatible = [row for row in declared if all(row[index] == value for index, value in fixed)]
    active = [row for row in compatible if row not in excluded]
    if 'precedence' in args:
        edges = args['precedence']
        if any(isinstance(edge, dict) for edge in edges):
            cases, disputed = _scoped_precedence_cases(active, conditions, rules, edges)
        else:
            reachable, stated_paths, disputed = _precedence_paths(len(rules), edges)
            cases = [_precedence_case(row, conditions, rules, reachable, stated_paths) for row in active]
    else:
        cases = [_priority_case(row, conditions, rules) for row in active]
    observation = {'priority_cases': cases, 'coverage': {
        'missing_rows': [case['when'] for case in cases if case['status'] == 'missing_rule'],
        'conflicting_rows': [{'when': case['when'], 'rules': case['winning_rules'], 'results': case['recorded_results']}
                             for case in cases if case['status'] == 'conflicting_rules'],
        'excluded_rows': [list(row) for row in compatible if row in excluded],
    }}
    if 'precedence' in args:
        observation['disputed_relations'] = disputed
    if not cases:
        return {'status': 'no_match', **observation, 'possible_results': []}
    labels = [case['recorded_results'][0] if case['status'] == 'resolved' else None for case in cases]
    depth, first, groups = question_order([case['when'] for case in cases], labels, tuple(range(len(cases))))
    needs_evidence = None in labels
    results = list(dict.fromkeys(result for case in cases for result in case['recorded_results']))
    observation.update(max_questions=depth, needs_evidence=needs_evidence)
    if first is not None:
        branches = []
        for value, indices, remaining in groups:
            outcomes = list(dict.fromkeys(labels[index] for index in indices))
            branches.append({'value': value, 'max_questions': remaining,
                             'possible_results': [outcome for outcome in outcomes if outcome is not None],
                             'needs_evidence': None in outcomes})
        observation['question'] = {'condition': conditions[first], 'branches': branches}
    if needs_evidence:
        return {'status': 'incomplete_table', **observation, 'recorded_results': results}
    return {'status': 'resolved' if first is None else 'needs_check', **observation, 'possible_results': results}


def calculate(args):
    declared, excluded = validate(args)
    if 'rules' in args:
        return _calculate_priority(args, declared, excluded)
    conditions, rows, known = args['conditions'], args['rows'], args['known']
    fixed = [(index, known[name]) for index, name in enumerate(conditions) if name in known]
    def matches(assignment):
        return all(assignment[field] == value for field, value in fixed)

    active = tuple(index for index, row in enumerate(rows) if matches(row['when']))

    def outcomes(indices):
        return list(dict.fromkeys(rows[index]['result'] for index in indices))

    observation = {'matching_rows': [index + 1 for index in active]}
    if declared is not None:
        compatible = [assignment for assignment in declared if matches(assignment)]
        reported = {}
        for index in active:
            reported.setdefault(tuple(rows[index]['when']), []).append(index)
        missing, conflicts = [], []
        for assignment in compatible:
            if assignment in excluded:
                continue
            indices = reported.get(assignment)
            if indices is None:
                missing.append(list(assignment))
            elif len(indices) > 1:
                conflicts.append({'when': list(assignment), 'rows': [index + 1 for index in indices],
                                  'results': outcomes(indices)})
        observation['coverage'] = {'missing_rows': missing, 'conflicting_rows': conflicts,
                                   'excluded_rows': [list(assignment) for assignment in compatible
                                                     if assignment in excluded]}
        if missing or conflicts:
            return {'status': 'incomplete_table', **observation, 'recorded_results': outcomes(active)}
    observation['possible_results'] = outcomes(active)
    if not active:
        return {'status': 'no_match', **observation}
    depth, first, groups = question_order([row['when'] for row in rows], [row['result'] for row in rows], active)
    if first is None:
        return {'status': 'resolved', 'max_questions': depth, **observation}
    branches = [{'value': value, 'possible_results': outcomes(indices), 'max_questions': remaining}
                for value, indices, remaining in groups]
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
