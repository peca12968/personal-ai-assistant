"""Evaluate a finite table of possible scenarios and choose the next useful question."""
import json
import re
from functools import cache
from itertools import product
from itertools import combinations
from math import prod

from . import goal_guidance_sources, goal_reply_sources
from .goal_stages import digest


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
disputed_relations列出參與矛盾循環的原連線，工具不以它們推導其他順序。不矛盾的無條件關係可以傳遞，即使中間規則未觸發。未被覆蓋的候選若有不同結果，unranked_pairs列出尚無有效優先依據的組合，cyclic_pairs列出受循環影響的組合。只問仍會影響本次的候選關係；正式補充只修改它確實確認或取代的關係，再保留原規則重算。
若使用者以「核對#2｜條件｜班別｜晚」或「核對#2｜優先｜2｜2｜1」作為完整單行正式答覆或主動更正，可只送{"revision":"[補充#3]"}或實際更正編號。程式核對原話指定的最新決策步驟，沿用原表及規則。條件格式為步驟、條件、欄名、值；優先格式為步驟、優先、關係序號、優先規則編號、被覆蓋規則編號，只能確認或反轉該關係原方向，適用條件不變。來源須是該核對後唯一一則已保存的補充或更正。自然語句或不同格式沿用完整重算；不要自行把原話改寫為正式格式。結果引用實際補充或更正及原行。
同一則已保存的補充若有2至4行正式更正，每行皆須帶同一個核對編號，可混合條件及優先更正。仍只傳該來源的revision編號。各行按同一原表驗證，全部有效且新條件相容才整批重算。批次以changes列每項改動。任一行無效或重複時，標示整批未採用並沿用原核對。依使用者要求說明無效原因或請修正。若使用者之後另行正式確認部分更正，再按新來源重算。
使用者要求對照前後或說明未受影響範圍時，送{"revision":"[補充#3]","compare":true}。revision.comparison的before、after分別是舊核對和這次重算，retained_known保留未改的已知欄位。優先關係有明文範圍時，scope_comparison另外列各種假設條件的前後結果，其餘已知欄位分別沿用前後核對的值。用它核對如「晚班有變、早班仍用原規則」，不要把假設分支當成本次已確認。未決時只列仍有依據的候選；引用仍用原文或已保存的補充。只要求本次結果時維持單一revision參數。'''

REPLACEMENT_GUIDANCE = '''正式整批補充被check_decision退回後，需要補正時，只問一次完整的新補充。請使用者在首行寫「取代｜[補充#3]」（用實際退回的補充或更正編號），接著寫一至四行原核對編號與要採用的更正。收到後仍用{"revision":"[補充#5]"}，只傳新補充的實際編號。新補充取代整批退回內容，未重述欄位沿用原核對。revision.replaces保存上次退回步驟、來源與原因。若新批再因內容無效被退回，下次須指向該次退回的來源。取代編號不符時保留原核對，依要求交代原因或請使用者確認，勿把未核實的值改送完整表。'''


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


_REVISION_SOURCE = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_STEP_METADATA = {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}


class _RejectedRevision(ValueError):
    def __init__(self, message, source, citations):
        super().__init__(message)
        self.source = source
        self.citations = citations


def validate_tool(args):
    if isinstance(args, dict) and 'revision' in args:
        if (set(args) not in ({'revision'}, {'revision', 'compare'}) or not isinstance(args['revision'], str)
                or not _REVISION_SOURCE.fullmatch(args['revision'])
                or ('compare' in args and type(args['compare']) is not bool)):
            raise ValueError('局部重算只接受一個已保存的補充或更正編號。')
    else:
        validate(args)


def _edge_key(edge):
    if isinstance(edge, dict):
        return (edge['higher'], edge['lower']), frozenset(edge['when'].items())
    return tuple(edge), frozenset()


def _precedence_change(base, values):
    if any(not re.fullmatch(r'[1-9][0-9]?', value) for value in values):
        raise ValueError('優先更正須指定關係序號及兩個規則編號。')
    number, higher, lower = map(int, values)
    edges = base.get('precedence', ())
    if number > len(edges):
        raise ValueError('優先更正須對應原核對中已列的關係。')
    old = edges[number - 1]
    pair, _ = _edge_key(old)
    if (higher, lower) not in (pair, pair[::-1]):
        raise ValueError('優先更正只能確認或反轉同一對規則。')
    new = {**old, 'higher': higher, 'lower': lower} if isinstance(old, dict) else [higher, lower]
    return {'kind': 'precedence', 'relation': number, 'before': old, 'after': new}


def _revised_precedence(original, edits):
    edges = list(original)
    for index, change in edits.items():
        edges[index - 1] = change['after']
    retained = {}
    for index, edge in enumerate(edges, 1):
        key = _edge_key(edge)
        prior = retained.get(key)
        if prior is None or (prior in edits and index not in edits):
            retained[key] = index
    order = sorted(retained.values())
    final_indexes = {original_index: index for index, original_index in enumerate(order, 1)}
    for index, change in edits.items():
        kept = retained[_edge_key(change['after'])]
        if kept != index:
            change['retained_relation'] = final_indexes[kept]
    return [edges[index - 1] for index in order]


def _revision_changes(text, base_step, base):
    lines = text.splitlines()
    if text.strip() != text or not 1 <= len(lines) <= 4:
        raise ValueError('正式補充須為一至四行，整批未套用。')
    changes, seen = [], set()
    for number, line in enumerate(lines, 1):
        try:
            parts = line.split('｜')
            if parts[0] != f'核對#{base_step}':
                raise ValueError('須指定本目標最新的決策核對編號。')
            if len(parts) == 4 and parts[1] == '條件':
                field, value = parts[2:]
                if value not in base.get('domains', {}).get(field, ()):
                    raise ValueError('條件更正須使用原核對的宣告欄位及選項。')
                key = 'condition', field
                change = {'kind': 'condition', 'field': field, 'before': base['known'].get(field), 'after': value}
            elif len(parts) == 5 and parts[1] == '優先':
                change = _precedence_change(base, parts[2:])
                key = 'precedence', change['relation']
            else:
                raise ValueError('補充原話未使用條件或優先更正格式。')
            if key in seen:
                raise ValueError('同一欄位或關係不可在同批重複更正。')
            seen.add(key)
            changes.append(change)
        except ValueError as exc:
            if len(lines) == 1:
                raise
            raise ValueError(f'第 {number} 行未通過，整批未套用。{exc}') from exc
    return changes


def _result_summary(result):
    return {key: result[key] for key in ('status', 'possible_results', 'recorded_results') if key in result}


def _revision_comparison(base, plan, changes, result):
    changed_fields = {change['field'] for change in changes if change['kind'] == 'condition'}
    comparison = {'before': _result_summary(calculate(base)), 'after': _result_summary(result),
                  'retained_known': {name: value for name, value in base['known'].items()
                                     if name not in changed_fields}}
    scopes = [dict(_edge_key(change['before'])[1]) for change in changes if change['kind'] == 'precedence']
    fields = [name for name in base['conditions'] if any(name in scope for scope in scopes)]
    if not fields:
        return comparison
    branches = []
    for values in product(*(base['domains'][name] for name in fields)):
        when = dict(zip(fields, values))
        branches.append({'when': when, 'within_revision_scope': any(scope.items() <= when.items() for scope in scopes),
                         'before': _result_summary(calculate({**base, 'known': {**base['known'], **when}})),
                         'after': _result_summary(calculate({**plan, 'known': {**plan['known'], **when}}))})
    comparison['scope_comparison'] = branches
    return comparison


def _revision_source(citation, base_step, sequence, replies, guidance, rejected):
    sources = {row['citation']: row for row in replies if base_step < row['ask_sequence'] < sequence}
    sources.update((row['citation'], row) for row in guidance if base_step <= row['steps_seen'] < sequence)
    source = sources.get(citation)
    if source is None:
        raise ValueError('局部重算須引用最新核對後唯一保存的正式補充或更正。')
    text = source.get('answer', source.get('content'))
    header, _, body = text.partition('\n')
    if header.startswith('取代｜'):
        replaces = rejected['receipt'] if rejected else None
        prior_steps = source.get('steps_seen', source.get('ask_sequence', 0) - 1)
        if (replaces is None or header.removesuffix('\r') != f"取代｜{replaces['citation']}"
                or sources.keys() - {citation} != rejected['citations']
                or citation in rejected['citations']
                or prior_steps < replaces['step']):
            raise ValueError('取代首行須指向同一原核對最後一次退回的正式補充，原核對保留。')
        return source, body, replaces, sources.keys()
    if len(sources) != 1:
        raise ValueError('局部重算須引用最新核對後唯一保存的正式補充或更正。')
    return source, text, None, sources.keys()


def _revision_plan(base_step, base, text):
    changes = _revision_changes(text, base_step, base)
    plan = dict(base)
    fields = {change['field']: change['after'] for change in changes if change['kind'] == 'condition'}
    edges = {change['relation']: change for change in changes if change['kind'] == 'precedence'}
    if fields:
        plan['known'] = {**base['known'], **fields}
    if edges:
        plan['precedence'] = _revised_precedence(base['precedence'], edges)
    return plan, changes, calculate(plan)


def _apply_revision(args, previous, sequence, replies, guidance, rejected):
    if previous is None:
        raise ValueError('局部重算需有同一目標已保存的決策核對。')
    base_step, base = previous
    source, text, replaces, citations = _revision_source(args['revision'], base_step, sequence, replies, guidance,
                                                         rejected)
    try:
        plan, changes, result = _revision_plan(base_step, base, text)
        if (replaces is not None or len(changes) > 1) and result['status'] == 'no_match':
            raise ValueError('新條件與宣告的可行情境不相容，整批未套用。')
    except ValueError as exc:
        if replaces is not None or (text.startswith(f'核對#{base_step}｜') and len(text.splitlines()) > 1):
            raise _RejectedRevision(str(exc), source, set(citations)) from exc
        raise
    receipt = {'base_step': base_step, 'base_sha256': digest(base),
               'citation': source['citation'], 'source_sha256': source['sha256']}
    if len(changes) > 1:
        receipt['changes'] = changes
    else:
        receipt['change'] = changes[0]
    if replaces is not None:
        receipt['replaces'] = replaces
    if args.get('compare'):
        receipt['comparison'] = _revision_comparison(base, plan, changes, result)
    return plan, {**result, 'revision': receipt}


def failed_revision(step):
    observation = step['observation']
    failed = observation.get('failed_action')
    if (step['action'].get('tool') != 'invalid' or observation.get('error_phase') != 'execution'
            or not isinstance(failed, dict) or failed.get('tool') != 'check_decision'):
        return None
    args = failed.get('args')
    return args if isinstance(args, dict) and 'revision' in args else None


def _verified_rejection(step, previous, replies, guidance, rejected):
    args = failed_revision(step)
    if args is None:
        return rejected
    try:
        validate_tool(args)
        _apply_revision(args, previous, step['sequence'], replies, guidance, rejected)
    except _RejectedRevision as exc:
        if step['observation'].get('error') == str(exc):
            return {'citations': exc.citations, 'receipt': {'step': step['sequence'],
                    'citation': exc.source['citation'], 'source_sha256': exc.source['sha256'], 'error': str(exc)}}
    except ValueError:
        pass
    return rejected


def _recheck_saved(steps, replies, guidance):
    previous = rejected = None
    for step in steps:
        action = step['action']
        if action.get('tool') == 'invalid':
            rejected = _verified_rejection(step, previous, replies, guidance, rejected)
        if action.get('tool') != 'check_decision':
            continue
        args, sequence = action['args'], step['sequence']
        validate_tool(args)
        if 'revision' in args:
            plan, expected = _apply_revision(args, previous, sequence, replies, guidance, rejected)
        else:
            plan, expected = args, calculate(args)
        actual = {key: value for key, value in step['observation'].items() if key not in _STEP_METADATA}
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError('決策表結果與保存的可行情境不一致。')
        previous = sequence, plan
        rejected = None
    return previous, rejected


def observe(args, steps, replies=(), guidance=()):
    if 'revision' not in args:
        return calculate(args)
    validate_tool(args)
    previous, rejected = _recheck_saved(steps, replies, guidance)
    return _apply_revision(args, previous, len(steps) + 1, replies, guidance, rejected)[1]


def validate_saved(db, *, replies_present=False, guidance_present=False):
    for gid, notes in db.execute(
            "SELECT DISTINCT g.id,g.notes FROM autonomous_goals g JOIN autonomous_steps s ON g.id=s.goal_id "
            "WHERE s.action_json LIKE '%\"check_decision\"%'"):
        steps = [{'sequence': sequence, 'action': json.loads(action), 'observation': json.loads(observation)}
                 for sequence, action, observation in db.execute(
                     "SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? "
                     "AND (action_json LIKE '%\"check_decision\"%' OR action_json LIKE '%\"invalid\"%') "
                     "ORDER BY sequence", (gid,))]
        revised = any(step['action'].get('tool') == 'check_decision'
                      and 'revision' in step['action'].get('args', {}) for step in steps)
        replies = goal_reply_sources.read(db, gid, notes) if revised and replies_present else []
        guidance = []
        if revised and guidance_present:
            count = db.execute('SELECT COUNT(*) FROM autonomous_steps WHERE goal_id=?', (gid,)).fetchone()[0]
            guidance = goal_guidance_sources.read(db, gid, notes, count)
        _recheck_saved(steps, replies, guidance)
