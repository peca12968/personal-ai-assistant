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
若使用者交代先核對原安排再問更新，原規則和原條件齊備時先用此工具保存原核對，再詢問更新，以便後續保留原值與正式改動的區別。
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

NATURAL_UPDATE_GUIDANCE = '''同一目標已有決策核對及最新自然語句補充，且只需調整已知欄位時，用check_decision的{"known_update":{"區位":"南","門證":null},"source":"[補充#3]"}，填實際來源及完整欄名。只轉錄本次明確更正的值；原值撤回、改為待查，或原先確認值已不能再沿用時，填null移除已知值，不能寫成相反值。未改的欄位及原表規則由工具沿用。程式核對來源位置、欄位及相容性，文字是否支持轉錄須由你先核對。舊值、未採用提案、另案及引述不成為本次事實。若回覆只說「其中一欄變了」，這是目前待澄清的欄位，不會增加原表的 excluded。若哪一欄變動會影響本次決定，直接問一次是哪欄，等確定後以原核對做 known_update；無須先重算一張加入臨時排除的表。若缺漏不會改變使用者要的結果，保留未知後直接交付。若只求假設試算，加"what_if":true，試算結果在what_if_result，頂層仍為本次實際核對，原表及本次條件不改。正式格式維持revision；要修改規則或選項時沿用完整核對。
若補充同時談本批、相近名稱、另案或舊狀態，先辨認各句的對象、時間及是否採用。沒有本次改動就沿用原核對。對象不明而影響結論時只確認是哪個，待答覆後把原狀態短句和最新指代確認一起引用。遇到這類混合或跨輪補充，或使用者要求引用原話，請為每個更動欄位附evidence，例如{"時段":[{"citation":"[補充#3]","quote":"本批改成晚班"}]}。每欄一至兩則，由早到晚，最後一則必須是source。quote逐字擷取唯一可定位的原文，連同必要的對象或撤回措辭。不要重寫引文。核對短句確實支持該欄的改值、撤回或試算，再採用。成果需要交代採用的原話或範圍時，引用本次採用的短句，並簡短標記可能混淆的相近名稱或舊狀態各屬哪一案。'''

NATURAL_UPDATE_GUIDANCE += '''
同一則最新補充同時明確更新正式條件，並要求以更新後的正式條件為起點做一次假設時，用同一個check_decision。known_update、source及evidence放正式改動，另附"scenario":{"updates":{"原欄名":"試算值"},"evidence":{"原欄名":[{"citation":"[補充#3]","quote":"假設…"}]}}，不用what_if:true。正式與試算各附逐字短句，最新來源的正式短句與試算短句不能重疊；試算短句從原文的假設、如果、若或試算標記開始。只改明列欄位，撤回用null。工具先算正式結果，再從更新後的條件只套用試算欄位，回傳what_if_result與what_if_update。只保存正式條件供之後接續；交付分開標示兩種結果及各自原話。未要求試算的提案不填scenario。'''

NATURAL_UPDATE_GUIDANCE += '''
最新原話明列本輪只可能是二至八個配對，且每組都明列相同的兩個以上完整欄位時，在known_update旁加"alternatives":{"cases":[{"日期":"週三","件數":"18"},{"日期":"週五","件數":"12"}],"quote":"本輪只剩兩種可能：週三18件或週五12件"}。用實際source、原表的完整選項及逐字的限制短句。已撤回的原已知值用null，沒有欄位要改可給空物件。這些配對只限制本輪，不改原表excluded和規則。未採用提案、獨立待查，或只說某個欄變動但未列完整配對時，不填alternatives。current_alternatives.remaining_cases是仍可能的配對，implied_known是依仍可能配對可推得的欄位，引用配對原話及正式確認。若各配對結果一致或使用者只要條件成果，直接交付；須確認時只問會區分結果的欄位。收到確認只更新該欄，工具會沿用本輪配對。'''

NATURAL_UPDATE_GUIDANCE += '''
原表用明列domains和rows，且有可行組合未公布結果或同一組有多個結果時，送本輪alternatives請同時填"track_coverage":true在alternatives內。原表的缺列仍缺，互相衝突的來源仍並列。coverage_cases逐組列已知結果、缺規則或衝突，recorded_results是各組原有記載。若使用者要本次確定結果，question可先確認能決定是否或哪一組須補規則的欄位；先交可核對部分，再只問該欄。等確定仍適用的缺口才補問規則，不能把未公布當成不可能。max_questions只計確認條件。只要條件報告或禁止追問時，各組照實交付，不問。'''

NATURAL_UPDATE_GUIDANCE += '''
本輪配對已確認唯一完整組合，仍缺原表結果或有同組衝突時，只問該組正式規則。可請使用者用整則原話「核對#6｜補規則｜週四｜6｜西廳」或「核對#6｜選規則｜側門｜有｜北桌」，核對編號用最後實際check_decision步驟，選項照原表欄序及原值，最後一格是處置。補規則用於缺列，選規則限同組原有結果。收到完整格式後只送{"revision":"[補充#7]"}或實際更正編號，不重建原表。current_row_rules列本輪採用、原列及正式補充。原表的記載與其他組的缺口仍可核對。沒有唯一生效組合或只需條件報告時，按原要求處理。'''

NATURAL_UPDATE_GUIDANCE += '''
本輪配對唯一且仍缺規則時，也可讓使用者直接說「這一組正式用西廳」或「本輪正式採用丁台」。同一則可先明寫「入口正式改為西口」，再寫「這一組正式採用丁台」；欄名與值須是原表完整文字。句子以句號或分行分開。可附「群聊提議『換東廳』，未採用」等明確不採用的引述。收到符合的自然原話，用check_decision的{"row_confirmation":"[補充#7]"}（實際來源編號），工具從原話套用正式欄位、確認唯一組合，再核對規則。只轉述或尚未決定就保留待查，不送此動作，也不把提案寫入新表。'''


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


def _calculate_priority(args, compatible, excluded):
    conditions, rules = args['conditions'], args['rules']
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
    results = list(dict.fromkeys(result for case in cases for result in case['recorded_results']))
    observation.update(_case_questions(conditions, cases))
    if observation['needs_evidence']:
        return {'status': 'incomplete_table', **observation, 'recorded_results': results}
    return {'status': 'needs_check' if 'question' in observation else 'resolved', **observation, 'possible_results': results}


def _case_questions(conditions, cases, *, distinguish_gaps=False):
    outcomes = [case['recorded_results'][0] if case['status'] == 'resolved' else None for case in cases]
    labels = [(outcome, tuple(case['when']) if outcome is None and distinguish_gaps else ())
              for outcome, case in zip(outcomes, cases)]
    depth, first, groups = question_order([case['when'] for case in cases], labels, tuple(range(len(cases))))
    result = {'max_questions': depth, 'needs_evidence': None in outcomes}
    if first is not None:
        branches = []
        for value, indices, remaining in groups:
            distinct = dict.fromkeys(outcomes[index] for index in indices)
            branches.append({'value': value, 'max_questions': remaining,
                             'possible_results': [outcome for outcome in distinct if outcome is not None],
                             'needs_evidence': None in distinct})
        result['question'] = {'condition': conditions[first], 'branches': branches}
    return result


def calculate(args, *, alternatives=None, require_all=False, row_rules=()):
    declared, excluded = validate(args)
    conditions, known = args['conditions'], args['known']
    indices = {name: index for index, name in enumerate(conditions)}
    fixed = [(indices[name], value) for name, value in known.items()]
    cases = alternatives['cases'] if alternatives is not None else ()
    pairs = [tuple((indices[name], value) for name, value in case.items()) for case in cases]

    def fits(assignment, requirements):
        return all(assignment[index] == value for index, value in requirements)

    def matches(assignment):
        return fits(assignment, fixed) and (not pairs or any(fits(assignment, pair) for pair in pairs))

    if 'rules' in args:
        result = _calculate_priority(args, [row for row in declared if matches(row)], excluded)
    else:
        result = _calculate_rows(args, declared, excluded, matches,
                                 track_coverage=bool(alternatives and alternatives.get('track_coverage')),
                                 row_rules=row_rules)
    if alternatives is None:
        return result

    universe = declared if declared is not None else [tuple(row['when']) for row in args['rows']]
    active = [row for row in universe if row not in excluded and fits(row, fixed)]
    remaining = [case for case, pair in zip(cases, pairs) if any(fits(row, pair) for row in active)]
    if require_all and len(remaining) != len(cases):
        raise ValueError('本輪配對有與原表可行情境或正式條件不相容的選項，未套用配對。')
    implied = {name: value for name, value in (remaining[0] if remaining else {}).items()
               if name not in known and all(case[name] == value for case in remaining)}
    return {**result, 'current_alternatives': {**alternatives, 'remaining_cases': remaining, 'implied_known': implied}}


def _calculate_rows(args, declared, excluded, matches, *, track_coverage=False, row_rules=()):
    conditions, rows = args['conditions'], args['rows']
    selected = {tuple(rule['when']): rule['row'] for rule in row_rules}
    if row_rules:
        rows = rows + [{'when': rule['when'], 'result': rule['result']} for rule in row_rules if not rule['original_rows']]
    active = tuple(index for index, row in enumerate(rows)
                   if matches(row['when']) and selected.get(tuple(row['when']), index + 1) == index + 1)

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
            if track_coverage:
                coverage_cases = []
                for assignment in compatible:
                    if assignment in excluded:
                        continue
                    indices = reported.get(assignment, [])
                    status = 'missing_rule' if not indices else 'conflicting_rules' if len(indices) > 1 else 'resolved'
                    coverage_cases.append({'when': list(assignment), 'status': status,
                                           'matching_rows': [index + 1 for index in indices],
                                           'recorded_results': outcomes(indices)})
                observation.update(coverage_cases=coverage_cases,
                                   **_case_questions(conditions, coverage_cases, distinguish_gaps=True))
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
_SCENARIO_START = re.compile(r'^(?:假設|假如|如果|若|(?:(?:另|只|僅)(?:做|想|供)?)?試算)')
_PAIR_LIMIT = re.compile(r'(?:只|僅)(?:有|剩|可能|限|能)|限於|二選一')
_ROW_REVISION_KINDS = {'補規則': 'row_completion', '選規則': 'row_selection'}
_ROW_SENTENCE = re.compile(
    r'(?:(?:這一組|本輪|本次)(?:的結果)?正式(?:使用|採用|定為|用)|正式確認(?:這一組|本輪|本次)(?:的?結果)?(?:為|是|採用|用)) *'
    r'(?:「(?P<quoted>[^「」｜\x00\r\n]{1,120})」|(?P<plain>[\u3400-\u9fffA-Za-z0-9 -]{0,23}[廳台架區位口席桌門庫站室]))')
_UNADOPTED_SENTENCE = re.compile(
    r'(?:群聊提議|有人提議|有人建議|轉述|引述)(?:「[^「」]{1,200}」|『[^『』]{1,200}』)[，,] *(?:未採用|不採用|僅供參考)')
_UNCERTAIN_PLAIN = re.compile(r'不|未|非|暫|待|若|或|可能|但|假設|試算|還要|再確認')
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
    elif isinstance(args, dict) and 'row_confirmation' in args:
        if (args.keys() != {'row_confirmation'} or not isinstance(args['row_confirmation'], str)
                or not _REVISION_SOURCE.fullmatch(args['row_confirmation'])):
            raise ValueError('自然規則確認只接受同一目標已保存的補充或更正編號。')
    elif isinstance(args, dict) and 'known_update' in args:
        if (args.keys() - {'known_update', 'source', 'what_if', 'evidence', 'scenario', 'alternatives'}
                or not isinstance(args.get('source'), str) or not _REVISION_SOURCE.fullmatch(args['source'])
                or ('what_if' in args and type(args['what_if']) is not bool)):
            raise ValueError('已知欄位重算需指定一至五個欄位及最新補充來源。')
        _validate_update_fields(args['known_update'], allow_empty='alternatives' in args)
        if 'evidence' in args:
            _validate_update_evidence(args['evidence'], args['known_update'], args['source'])
        if 'scenario' in args:
            scenario = args['scenario']
            if ('what_if' in args or 'evidence' not in args or not isinstance(scenario, dict)
                    or scenario.keys() != {'updates', 'evidence'}):
                raise ValueError('同則正式更新與試算須分別提供欄位及原話證據。')
            _validate_update_fields(scenario['updates'])
            _validate_update_evidence(scenario['evidence'], scenario['updates'], args['source'])
        if 'alternatives' in args:
            if args.get('what_if'):
                raise ValueError('本輪正式配對不能當成單獨試算。')
            _validate_alternatives(args['alternatives'])
    else:
        validate(args)


def _validate_update_fields(updates, *, allow_empty=False):
    if not isinstance(updates, dict) or not (0 if allow_empty else 1) <= len(updates) <= 5:
        raise ValueError('已知欄位重算需指定一至五個欄位及最新補充來源。')
    for field, value in updates.items():
        _label(field)
        if value is not None:
            _label(value)


def _validate_alternatives(alternatives):
    required = {'cases', 'quote'}
    if (not isinstance(alternatives, dict) or not required <= alternatives.keys() <= required | {'track_coverage'}
            or ('track_coverage' in alternatives and alternatives['track_coverage'] is not True)):
        raise ValueError('本輪配對須提供各組條件及同則補充的逐字原話。')
    _label(alternatives['quote'])
    cases = alternatives['cases']
    if not isinstance(cases, list) or not 2 <= len(cases) <= 8:
        raise ValueError('本輪配對需列出二至八組。')
    unique = set()
    for case in cases:
        if not isinstance(case, dict) or not 2 <= len(case) <= 5 or case.keys() != cases[0].keys():
            raise ValueError('本輪各配對須使用相同的二至五個完整欄位。')
        for name, value in case.items():
            _label(name)
            _label(value)
        unique.add(frozenset(case.items()))
    if len(unique) != len(cases):
        raise ValueError('本輪配對不得重複。')


def _validate_update_evidence(evidence, updates, latest_citation):
    if not isinstance(evidence, dict) or evidence.keys() != updates.keys():
        raise ValueError('欄位證據須涵蓋每一個更新欄位。')
    for excerpts in evidence.values():
        if not isinstance(excerpts, list) or not 1 <= len(excerpts) <= 2:
            raise ValueError('每個更新欄位須附一至兩則證據。')
        for excerpt in excerpts:
            if (not isinstance(excerpt, dict) or excerpt.keys() != {'citation', 'quote'}
                    or not isinstance(excerpt['citation'], str) or not _REVISION_SOURCE.fullmatch(excerpt['citation'])):
                raise ValueError('欄位證據須提供已保存的來源編號與逐字短句。')
            _label(excerpt['quote'])
        if excerpts[-1]['citation'] != latest_citation:
            raise ValueError('每個更新欄位的證據須以最新補充作結。')


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


def _current_result(plan, *, require_all=False):
    if 'current_alternatives' not in plan:
        return calculate(plan)
    core = {key: value for key, value in plan.items() if key not in {'current_alternatives', 'current_row_rules'}}
    rules = plan.get('current_row_rules', ())
    result = calculate(core, alternatives=plan['current_alternatives'], require_all=require_all, row_rules=rules)
    if rules:
        result['current_row_rules'] = [{**rule, 'applicable': rule['row'] in result['matching_rows']} for rule in rules]
    return result


def _revision_comparison(base, plan, changes, result):
    changed_fields = {change['field'] for change in changes if change['kind'] == 'condition'}
    comparison = {'before': _result_summary(_current_result(base)), 'after': _result_summary(result),
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
                         'before': _result_summary(_current_result({**base, 'known': {**base['known'], **when}})),
                         'after': _result_summary(_current_result({**plan, 'known': {**plan['known'], **when}}))})
    comparison['scope_comparison'] = branches
    return comparison


def _followup_sources(base_step, sequence, replies, guidance):
    sources = {row['citation']: row for row in replies if base_step < row['ask_sequence'] < sequence}
    sources.update((row['citation'], row) for row in guidance if base_step <= row['steps_seen'] < sequence)
    return sources


def _source_text(source):
    return source.get('answer', source.get('content'))


def _revision_source(citation, base_step, sequence, replies, guidance, rejected, what_if_sources):
    sources = _followup_sources(base_step, sequence, replies, guidance)
    for ignored in what_if_sources:
        sources.pop(ignored, None)
    source = sources.get(citation)
    if source is None:
        raise ValueError('局部重算須引用最新核對後唯一保存的正式補充或更正。')
    text = _source_text(source)
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
    return plan, changes, _current_result(plan)


def _row_revision_plan(base_step, base, text, source):
    parts = text.split('｜')
    if (parts[0] != f'核對#{base_step}' or len(parts) != len(base['conditions']) + 3
            or 'rows' not in base or 'domains' not in base or 'current_alternatives' not in base):
        raise ValueError('本輪規則補充須以最後決策編號及原表欄序明列唯一生效組合。')
    when = _assignment(parts[2:-1], len(base['conditions']))
    result = parts[-1]
    _label(result)
    expected, missing = _pending_rule_assignment(base)
    if when != expected:
        raise ValueError('只可補充目前唯一生效且仍有規則缺口的完整組合。')
    evidence = {'citation': source['citation'], 'source_sha256': source['sha256'], 'quote': text}
    plan, change = _resolve_row_rule(base, when, result, _ROW_REVISION_KINDS[parts[1]], missing, evidence)
    return plan, [change], _current_result(plan)


def _pending_rule_assignment(base):
    invalid = '只可補充目前唯一生效且仍有規則缺口的完整組合。'
    if not {'rows', 'domains', 'current_alternatives'} <= base.keys():
        raise ValueError(invalid)
    observed = _current_result(base)
    rules = base.get('current_row_rules', [])
    matched = set(observed['matching_rows'])
    covered = {tuple(row['when']) for index, row in enumerate(base['rows'], 1) if index in matched}
    covered.update(tuple(rule['when']) for rule in rules if rule['row'] in matched)
    missing = {tuple(row) for row in observed['coverage']['missing_rows']}
    if observed['status'] != 'incomplete_table' or len(covered | missing) != 1:
        raise ValueError(invalid)
    when, = covered | missing
    return when, when in missing


def _resolve_row_rule(base, when, result, kind, missing, evidence):
    rules = base.get('current_row_rules', [])
    original_rows = [index for index, row in enumerate(base['rows'], 1) if tuple(row['when']) == when]
    if kind == 'row_completion':
        if not missing:
            raise ValueError('補規則只適用原表未記載的結果。')
        row = len(base['rows']) + sum(not rule['original_rows'] for rule in rules) + 1
    else:
        chosen = [index for index in original_rows if base['rows'][index - 1]['result'] == result]
        if len(original_rows) < 2 or len(chosen) != 1:
            raise ValueError('選規則須指定同組原有衝突結果之一。')
        row, = chosen
    change = {'kind': kind, 'when': list(when), 'result': result, 'row': row, 'original_rows': original_rows}
    return {**base, 'current_row_rules': [*rules, {**change, **evidence}]}, change


def _apply_revision(args, previous, sequence, replies, guidance, rejected, what_if_sources):
    if previous is None:
        raise ValueError('局部重算需有同一目標已保存的決策核對。')
    base_step, base = previous
    source, text, replaces, citations = _revision_source(args['revision'], base_step, sequence, replies, guidance,
                                                         rejected, what_if_sources)
    try:
        parts = text.split('｜', 2)
        if len(parts) > 1 and parts[1] in _ROW_REVISION_KINDS:
            if replaces is not None or len(text.splitlines()) != 1:
                raise ValueError('本輪規則補充須為一則完整單行原話。')
            plan, changes, result = _row_revision_plan(base_step, base, text, source)
        else:
            plan, changes, result = _revision_plan(base_step, base, text)
        if (replaces is not None or len(changes) > 1) and result['status'] == 'no_match':
            raise ValueError('新條件與宣告的可行情境不相容，整批未套用。')
        if 'current_alternatives' in base and result['status'] == 'no_match':
            raise ValueError('新條件與目前配對不相容，原核對保留。')
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


def _updated_plan(base, updates):
    domains = base.get('domains')
    if domains is None:
        domains = dict(zip(base['conditions'], zip(*(row['when'] for row in base['rows']))))
    known = dict(base['known'])
    changes = []
    for field, value in updates.items():
        if field not in domains or (value is not None and value not in domains[field]):
            raise ValueError('已知欄位重算須使用原表欄名及選項。')
        changes.append({'field': field, 'before': known.get(field), 'after': value})
        if value is None:
            known.pop(field, None)
        else:
            known[field] = value
    return {**base, 'known': known}, changes


def _natural_clauses(text):
    return [clause.strip() for clause in re.split(r'[。；\r\n]+', text) if clause.strip()]


def _unadopted_note(source):
    clauses = _natural_clauses(_source_text(source))
    return clauses and all(_UNADOPTED_SENTENCE.fullmatch(clause) for clause in clauses)


def _natural_condition(base, clause):
    for field, values in base['domains'].items():
        if clause == f'{field}正式撤回':
            return field, None
        match = re.fullmatch(rf'{re.escape(field)}正式(?:改為|確認為|確認是) *(.*)', clause)
        if match:
            value = match[1]
            if value.startswith('「') and value.endswith('」'):
                value = value[1:-1]
            if value in values:
                return field, value
    return None


def _natural_row_clauses(base, source):
    updates, result = {}, None
    for clause in _natural_clauses(_source_text(source)):
        if _UNADOPTED_SENTENCE.fullmatch(clause) or clause in {'原配對仍適用', '先前配對仍適用', '其他欄位照舊'}:
            continue
        formal = _ROW_SENTENCE.fullmatch(clause)
        if formal and result is None:
            value = formal['quoted'] or formal['plain']
            _label(value)
            if (value in {'待查', '未定', '尚未公布', '暫定'} or '或' in value
                    or (formal['plain'] and _UNCERTAIN_PLAIN.search(value))):
                raise ValueError('尚未確認的結果不能作為本輪正式規則。')
            result = value, _source_excerpt(source, clause)
            continue
        condition = _natural_condition(base, clause) if result is None else None
        if condition is None or condition[0] in updates:
            raise ValueError('正式規則確認須用獨立完整原句，欄位更正須在結果確認前。')
        field, value = condition
        updates[field] = value, _source_excerpt(source, clause)
    if result is None:
        raise ValueError('這則補充沒有明確正式確認本輪規則。')
    return updates, result


def _apply_natural_row_confirmation(args, previous, sequence, replies, guidance, what_if_sources):
    if previous is None or not {'rows', 'domains', 'current_alternatives'} <= previous[1].keys():
        raise ValueError('自然規則確認須先核對本輪配對及原表。')
    base_step, base = previous
    sources = _followup_sources(base_step, sequence, replies, guidance)
    latest, _ = _latest_source(args['row_confirmation'], sources)
    if any(not _unadopted_note(source) for citation, source in sources.items()
           if citation != latest['citation'] and citation not in what_if_sources):
        raise ValueError('另有尚未核對的補充，請先確認其影響。')
    updates, (result, proof) = _natural_row_clauses(base, latest)
    plan, changes = _updated_plan(base, {field: value for field, (value, _) in updates.items()})
    when, missing = _pending_rule_assignment(plan)
    kind = 'row_completion' if missing else 'row_selection'
    plan, change = _resolve_row_rule(plan, when, result, kind, missing, proof)
    receipt = {'base_step': base_step, 'base_sha256': digest(base), 'citation': latest['citation'],
               'source_sha256': latest['sha256'], 'change': change}
    if changes:
        receipt['condition_changes'] = [{**item, 'evidence': updates[item['field']][1]} for item in changes]
    return plan, {**_current_result(plan), 'revision': receipt}


def _bind_alternatives(alternatives, plan, source):
    cases, quote = alternatives['cases'], alternatives['quote']
    if not cases[0].keys() <= set(plan['conditions']):
        raise ValueError('本輪配對須使用原表欄位。')
    if not _PAIR_LIMIT.search(quote) or _SCENARIO_START.match(quote) or quote.startswith(('「', '『', '引用', '引述')):
        raise ValueError('本輪配對須引用明確限制目前可能組合的原話。')
    return {**alternatives, **_source_excerpt(source, quote)}


def _apply_known_update(args, previous, sequence, replies, guidance):
    if previous is None:
        raise ValueError('已知欄位重算需有同一目標已保存的決策核對。')
    base_step, base = previous
    sources = _followup_sources(base_step, sequence, replies, guidance)
    source, positions = _latest_source(args['source'], sources)
    if _source_text(source).startswith(('核對#', '取代｜')):
        raise ValueError('正式更正須以原話的正式格式重算。')
    plan, changes = _updated_plan(base, args['known_update'])
    if 'alternatives' in args:
        plan['current_alternatives'] = _bind_alternatives(args['alternatives'], plan, source)
        plan.pop('current_row_rules', None)
    result = _current_result(plan, require_all='alternatives' in args)
    receipt = {'base_step': base_step, 'base_sha256': digest(base), 'citation': args['source'],
               'source_sha256': source['sha256'], 'changes': changes}
    if 'evidence' in args:
        receipt['evidence'] = _bind_update_evidence(args['evidence'], sources, positions)
    if args.get('what_if'):
        return base, {**_current_result(base), 'known_update': receipt, 'what_if_result': result}
    if result['status'] == 'no_match':
        raise ValueError('更新值與原表的可行情境不相容，原核對保留。')
    if 'scenario' in args:
        scenario = args['scenario']
        trial_evidence = _bind_update_evidence(scenario['evidence'], sources, positions)
        _separate_scenario_evidence(receipt['evidence'], trial_evidence)
        trial_plan, trial_changes = _updated_plan(plan, scenario['updates'])
        result = {**result, 'what_if_result': _current_result(trial_plan),
                  'what_if_update': {'changes': trial_changes, 'evidence': trial_evidence}}
    return plan, {**result, 'known_update': receipt}


def _latest_source(citation, sources):
    positions = {key: source.get('steps_seen', source.get('ask_sequence')) for key, source in sources.items()}
    latest = max(positions.values(), default=-1)
    if [key for key, position in positions.items() if position == latest] != [citation]:
        raise ValueError('已知欄位重算須引用本次最新且可辨認的補充。')
    return sources[citation], positions


def _separate_scenario_evidence(official, trial):
    official = [excerpts[-1] for excerpts in official.values()]
    trial = [excerpts[-1] for excerpts in trial.values()]
    if any(_SCENARIO_START.match(row['quote']) for row in official):
        raise ValueError('正式更正須引用獨立正式原話，不可引用試算短句。')
    if any(not _SCENARIO_START.match(row['quote']) for row in trial):
        raise ValueError('試算短句須從原文的假設或試算標記開始。')
    if any(left['start'] < right['end'] and right['start'] < left['end'] for left in official for right in trial):
        raise ValueError('正式更新與試算須引用同一則補充中互不重疊的原話。')


def _bind_update_evidence(evidence, sources, positions):
    bound = {}
    for field, excerpts in evidence.items():
        previous = -1
        bound[field] = []
        for excerpt in excerpts:
            citation, quote = excerpt['citation'], excerpt['quote']
            position = positions.get(citation, -1)
            if position <= previous:
                raise ValueError('欄位證據須依序引用原核對後的同一目標補充。')
            bound[field].append(_source_excerpt(sources[citation], quote))
            previous = position
    return bound


def _source_excerpt(source, quote):
    text = _source_text(source)
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0 or text.startswith(('核對#', '取代｜')):
        raise ValueError('欄位證據須逐字引用自然補充中可唯一定位的短句。')
    return {'citation': source['citation'], 'quote': quote, 'source_sha256': source['sha256'],
            'start': start, 'end': start + len(quote)}


def failed_revision(step):
    observation = step['observation']
    failed = observation.get('failed_action')
    if (step['action'].get('tool') != 'invalid' or observation.get('error_phase') != 'execution'
            or not isinstance(failed, dict) or failed.get('tool') != 'check_decision'):
        return None
    args = failed.get('args')
    return args if isinstance(args, dict) and 'revision' in args else None


def _verified_rejection(step, previous, replies, guidance, rejected, what_if_sources):
    args = failed_revision(step)
    if args is None:
        return rejected
    try:
        validate_tool(args)
        _apply_revision(args, previous, step['sequence'], replies, guidance, rejected, what_if_sources)
    except _RejectedRevision as exc:
        if step['observation'].get('error') == str(exc):
            return {'citations': exc.citations, 'receipt': {'step': step['sequence'],
                    'citation': exc.source['citation'], 'source_sha256': exc.source['sha256'], 'error': str(exc)}}
    except ValueError:
        pass
    return rejected


def _recheck_saved(steps, replies, guidance):
    previous = rejected = None
    what_if_sources = set()
    for step in steps:
        action = step['action']
        if action.get('tool') == 'invalid':
            rejected = _verified_rejection(step, previous, replies, guidance, rejected, what_if_sources)
        if action.get('tool') != 'check_decision':
            continue
        args, sequence = action['args'], step['sequence']
        validate_tool(args)
        if 'revision' in args:
            plan, expected = _apply_revision(args, previous, sequence, replies, guidance, rejected, what_if_sources)
        elif 'row_confirmation' in args:
            plan, expected = _apply_natural_row_confirmation(args, previous, sequence, replies, guidance, what_if_sources)
        elif 'known_update' in args:
            plan, expected = _apply_known_update(args, previous, sequence, replies, guidance)
        else:
            plan, expected = args, calculate(args)
        actual = {key: value for key, value in step['observation'].items() if key not in _STEP_METADATA}
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError('決策表結果與保存的可行情境不一致。')
        if args.get('what_if'):
            what_if_sources.add(args['source'])
            continue
        previous = sequence, plan
        rejected = None
        what_if_sources.clear()
    return previous, rejected, what_if_sources


def observe(args, steps, replies=(), guidance=()):
    if not {'revision', 'known_update', 'row_confirmation'} & args.keys():
        return calculate(args)
    validate_tool(args)
    previous, rejected, what_if_sources = _recheck_saved(steps, replies, guidance)
    sequence = len(steps) + 1
    if 'known_update' in args:
        return _apply_known_update(args, previous, sequence, replies, guidance)[1]
    if 'row_confirmation' in args:
        return _apply_natural_row_confirmation(args, previous, sequence, replies, guidance, what_if_sources)[1]
    return _apply_revision(args, previous, sequence, replies, guidance, rejected, what_if_sources)[1]


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
                      and step['action']['args'].keys() & {'revision', 'known_update', 'row_confirmation'} for step in steps)
        replies = goal_reply_sources.read(db, gid, notes) if revised and replies_present else []
        guidance = []
        if revised and guidance_present:
            count = db.execute('SELECT COUNT(*) FROM autonomous_steps WHERE goal_id=?', (gid,)).fetchone()[0]
            guidance = goal_guidance_sources.read(db, gid, notes, count)
        _recheck_saved(steps, replies, guidance)
