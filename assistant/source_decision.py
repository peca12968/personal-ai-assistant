"""Build a finite decision table from explicit, already read scoped rule lines."""
import json
import re
from itertools import product

from . import decision_table, document_ranges, goal_guidance_sources, goal_reply_sources, goal_stages
from .work_plan import DELIVERY_TOOLS


GUIDANCE = '''check_source_decision可逐行核對固定格式的規則。若原文使用「範圍｜狀態｜條件一、條件二：結果」，用此工具代替自行轉錄。傳入scope完整範圍名稱、conditions欄序、domains完整選項、known本次已確認值，以及ranges已完整查讀的原規則區段（最多8段共32行，包含另案及未採用的行，不含標題）。範圍必須完整相同。文件狀態只能是正式、未採用或排除。排除行省略冒號和結果。範例「甲批｜正式｜高、室內：甲架」。
若本次有明確保存的單筆更正，且原話整則符合「甲批｜更正｜高、室內：新架」，可加amendment，值為實際的補充或更正引用編號。只替換同範圍、同條件的一列或補缺列，不改其他批次。只用本次適用且已保存的正式更正，不使用引文或假設。格式或範圍不同時回到原文核對，不改寫原行冒充精確來源。
工具回傳由原行整理的表與decision核算，以及原行來源與是否採用。區分另案、未採用、正式排除與被更正取代。保留已確認的數量、窗口等資料，按實際來源引用。決策有缺列時只問缺的正式規則，收到符合格式的答覆後帶amendment重算。此工具核對你指定的已讀區段與字面範圍，是否涵蓋全部適用規則仍依原始資料。
原文也可有「甲批｜v2｜正式｜高：新架」和「甲批｜採用版本｜v2」。把版號規則和採用行都納入check_source_decision的已讀ranges。不以較新編號推定生效。只依同批明文採用行篩選版本，無版號的共同規則仍保留。沒有採用行時保留各版正式記載，有影響才請補必要資訊。若工具給出adopted_version，交付時把規則行與採用行各自引用。
若同批採用記載互相矛盾，工具分別核算最多四個候選版，保留採用來源。shared_result表示各版皆已能獨立得出相同結論，可直接交付共同結果並保留矛盾採用記載，不宣稱已選定版本。version_uncertain表示各版結果不同或其中一版資料不足，先列可確認的資料與各版差異，再只問影響結果的依據。缺版不能當成另一版相同。收到同一批已保存的正式單行採用答覆，例如「甲批｜採用版本｜v2」，用amendment帶入實際補充或更正編號，重算該候選版並引用該答覆。
採用版本及欄位都待確認，且工作要求最少追問時，傳clarify:true。clarification_plan比較每個未知欄位及正式採用版本，最小化最壞追問輪數。同分先依原欄位順序，再問版本。只問計畫的下一項，保存答覆後沿用原規則重算。已知欄位只採實際確認，採用答覆用amendment連結。needs_evidence表示其中仍有缺版、缺列、相容衝突或不適用的情境，追問輪數不含之後補來源的工作。只要條件式交付時依使用者要求直接列結果與缺漏，不啟動提問。'''

_AMENDMENT = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_STATES = {'正式', '未採用', '排除'}
_SCOPE_PREFIX = '決策規則範圍：'
_RANGE = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})(?:-L([1-9][0-9]{0,11}))?\]')
SCOPE_GUIDANCE = ('decision_rule_scope來自使用者目標首行的規則範圍。missing_ranges是尚未完整查讀的區段。'
                  '只用read_ranges補讀缺口，不重讀已有原行。若缺行不可取得，先列可確認資料，'
                  '只請提供含該行的完整副本供重新匯入及另建目標。文字答覆無法補齊這份已宣告副本的原行。'
                  '核算時ranges須涵蓋整份宣告，'
                  '不可只送已知會符合的部分或加入其他區段。交付或產檔前須有當前條件的原文決策核對。')


def _positions(ranges):
    return [(item['document_id'], number) for item in ranges
            for number in range(item['start_line'], item['end_line'] + 1)]


def _validate_ranges(ranges):
    document_ranges.validate_batch({'ranges': ranges})
    if any(not 0 <= item['end_line'] - item['start_line'] < 32 for item in ranges):
        raise ValueError('每段原文規則須指定有效的連續行。')
    positions = _positions(ranges)
    if len(positions) > 32 or len(set(positions)) != len(positions):
        raise ValueError('原文規則最多32個不重複的行。')


def declared_ranges(objective):
    first = objective.partition('\n')[0].removesuffix('\r')
    if not first.startswith(_SCOPE_PREFIX):
        return []
    ranges = []
    for token in first.removeprefix(_SCOPE_PREFIX).split('、'):
        match = _RANGE.fullmatch(token.strip())
        if not match:
            raise ValueError('決策規則範圍請在目標首行列出文件及行號，例如[D2:L2-L5]。')
        did, start, end = match.groups()
        ranges.append({'document_id': int(did), 'start_line': int(start), 'end_line': int(end or start)})
    _validate_ranges(ranges)
    return ranges


def save_scope(db, gid, ranges, sources):
    if not {row['document_id'] for row in ranges} <= {row['document_id'] for row in sources}:
        raise ValueError('請先在目前對話匯入宣告的規則文件。')
    db.execute('INSERT INTO autonomous_decision_scopes(goal_id,ranges_json) VALUES(?,?)',
               (gid, json.dumps(ranges, ensure_ascii=False)))


def read_scope(db, gid, objective):
    row = db.execute('SELECT ranges_json FROM autonomous_decision_scopes WHERE goal_id=?', (gid,)).fetchone()
    if row is None:
        return []
    ranges = json.loads(row[0])
    if not ranges or ranges != declared_ranges(objective):
        raise ValueError('保存的決策規則範圍與目標原文不一致。')
    return ranges


def validate(args):
    fields = {'scope', 'conditions', 'domains', 'known', 'ranges'}
    if not isinstance(args, dict) or not fields <= args.keys() <= fields | {'amendment', 'clarify'}:
        raise ValueError('原文決策表需提供範圍、欄位、宣告選項、已知值與已讀行。')
    decision_table._label(args['scope'])
    if '｜' in args['scope']:
        raise ValueError('範圍名稱不可包含規則分隔符號。')
    decision_table.validate({key: args[key] for key in ('conditions', 'domains', 'known')} | {'rows': [], 'excluded': []})
    _validate_ranges(args['ranges'])
    if 'amendment' in args and (not isinstance(args['amendment'], str) or not _AMENDMENT.fullmatch(args['amendment'])):
        raise ValueError('須指定本目標已保存的更正或補充編號。')
    if 'clarify' in args and type(args['clarify']) is not bool:
        raise ValueError('clarify須為布林值。')


def _parse(text, size, *, user=False):
    if not isinstance(text, str) or len(text.splitlines()) != 1:
        raise ValueError('原文規則或保存更正須為單一完整行。')
    parts = text.removesuffix('。').split('｜')
    version = None
    if len(parts) == 3 and parts[1] == '採用版本':
        scope, status, version = parts
        decision_table._label(scope)
        decision_table._label(version)
        return scope, status, [], '', version
    if not user and len(parts) == 4:
        scope, version, status, body = parts
        decision_table._label(version)
        parts = [scope, status, body]
    if len(parts) != 3 or parts[1] not in ({'更正'} if user else _STATES):
        raise ValueError('原文規則的範圍、狀態或分隔格式不符。')
    scope, status, body = parts
    decision_table._label(scope)
    condition, separator, result = body.partition('：')
    values = condition.split('、')
    if len(values) != size or bool(separator) == (status == '排除'):
        raise ValueError('原文規則的欄數或處置格式不符。')
    for value in values:
        decision_table._label(value)
    if separator:
        decision_table._label(result)
    return scope, status, values, result, version


def _gather_lines(ranges, steps, sources):
    positions = _positions(ranges)
    catalog = {row['document_id']: row for row in sources}
    found = {}
    for step in steps:
        if step['action']['tool'] not in {'read', 'read_ranges'}:
            continue
        for excerpt in step['observation'].get('excerpts', []):
            if excerpt.get('partial_line') is not False or excerpt.get('requested_range') is not True:
                continue
            did, start, end = (excerpt[key] for key in document_ranges.BATCH_FIELDS)
            relevant = [number for doc, number in positions if doc == did and start <= number <= end]
            if not relevant:
                continue
            source = catalog.get(did)
            lines = excerpt['text'].splitlines()
            if (source is None or excerpt['sha256'] != source['sha256'] or excerpt['name'] != source['name']
                    or len(lines) != end - start + 1):
                raise ValueError('已讀規則的範圍或文件版本不一致。')
            for number in relevant:
                prefix = f'L{number}: '
                line = lines[number - start]
                if not line.startswith(prefix):
                    raise ValueError('已讀規則的原行號不一致。')
                value = (step['sequence'], source['sha256'], line[len(prefix):])
                previous = found.setdefault((did, number), value)
                if previous[1:] != value[1:]:
                    raise ValueError('同一版本規則行有不同內容。')
    return positions, found


def missing_ranges(ranges, steps, sources):
    positions, found = _gather_lines(ranges, steps, sources)
    missing = []
    for did, number in positions:
        if (did, number) in found:
            continue
        if missing and missing[-1]['document_id'] == did and missing[-1]['end_line'] == number - 1:
            missing[-1]['end_line'] = number
        else:
            missing.append({'document_id': did, 'start_line': number, 'end_line': number})
    return missing


def _read_lines(ranges, steps, sources):
    positions, found = _gather_lines(ranges, steps, sources)
    if len(found) != len(positions):
        raise ValueError('規則範圍有尚未完整查讀的行。請先查讀原文。')
    return [(did, number, *found[(did, number)]) for did, number in positions]


def _evaluate(args, parsed, selected, amendment):
    records, rows, excluded = [], [], []
    indexed = {}
    for did, number, sequence, (scope, status, values, result, version) in parsed:
        record = {'citation': f'[D{did}:L{number}]', 'read_step': sequence}
        if version is not None:
            record['version'] = version
        if scope != args['scope']:
            disposition = 'other_scope'
        elif status == '採用版本':
            disposition = 'version_adopted'
        elif selected and version is not None and version != selected:
            disposition = 'other_version'
        elif status == '未採用':
            disposition = 'not_adopted'
        elif status == '排除':
            disposition = 'excluded'
            if values not in excluded:
                excluded.append(values)
        elif amendment and values == amendment['when']:
            disposition = 'superseded'
        else:
            disposition = 'included'
            key = (tuple(values), result)
            if key not in indexed:
                rows.append({'when': values, 'result': result})
                indexed[key] = len(rows)
            record['table_row'] = indexed[key]
        records.append({**record, 'status': disposition})
    if amendment:
        rows.append({key: amendment[key] for key in ('when', 'result')})
        amendment = {**amendment, 'table_row': len(rows)}
    table = {key: args[key] for key in ('conditions', 'domains', 'known')} | {'rows': rows, 'excluded': excluded}
    observation = {'source_rows': records, 'table': table, 'decision': decision_table.calculate(table)}
    if amendment:
        observation['amendment'] = amendment
    return observation


def _clarification(args, versions):
    fields, domains, known = args['conditions'], args['domains'], args['known']
    if any(value not in domains[field] for field, value in known.items()):
        return {'max_questions': 0, 'needs_evidence': True}
    combinations = list(product(*([known[field]] if field in known else domains[field] for field in fields)))
    assignments, labels = [], []
    for version in versions:
        table = version.get('table', {})
        excluded = {tuple(when) for when in table.get('excluded', [])}
        recorded = {}
        for row in table.get('rows', []):
            recorded.setdefault(tuple(row['when']), set()).add(row['result'])
        for when in combinations:
            outcomes = recorded.get(when, set())
            outcome = next(iter(outcomes)) if when not in excluded and len(outcomes) == 1 else None
            assignments.append((*when, version['name']))
            labels.append(outcome)

    depth, first, groups = decision_table.question_order(assignments, labels, tuple(range(len(assignments))))
    plan = {'max_questions': depth, 'needs_evidence': None in labels}
    if first is not None:
        branches = []
        for value, indices, remaining in groups:
            outcomes = list(dict.fromkeys(labels[index] for index in indices))
            branches.append({'value': value, 'max_questions': remaining,
                             'possible_results': [outcome for outcome in outcomes if outcome is not None],
                             'needs_evidence': None in outcomes})
        plan['next'] = ({'kind': 'version'} if first == len(fields)
                        else {'kind': 'condition', 'field': fields[first]})
        plan['branches'] = branches
    return plan


def calculate(args, steps, sources, replies=(), guidance=(), *, declared=()):
    validate(args)
    if declared and set(_positions(declared)) != set(_positions(args['ranges'])):
        raise ValueError('核算範圍必須與目標首行的全部宣告相同，請先補讀缺口。')
    lines = _read_lines(declared or args['ranges'], steps, sources)
    size = len(args['conditions'])
    amendment = selection = None
    if token := args.get('amendment'):
        source = next((row for row in (*replies, *guidance) if row['citation'] == token), None)
        if source is None:
            raise ValueError('找不到本目標已保存的更正或補充。')
        scope, status, values, result, version = _parse(source.get('answer', source.get('content')), size, user=True)
        if scope != args['scope']:
            raise ValueError('所選更正只適用其原話中的完整範圍。')
        receipt = {'citation': token, 'sha256': source['sha256']}
        if status == '採用版本':
            selection = {**receipt, 'name': version}
        else:
            amendment = {**receipt, 'when': values, 'result': result}

    parsed = [(did, number, sequence, _parse(text, size)) for did, number, sequence, _, text in lines]
    adopted, available = {}, set()
    for did, number, _, (scope, status, _, _, version) in parsed:
        if scope != args['scope']:
            continue
        if status == '採用版本':
            adopted.setdefault(version, []).append(f'[D{did}:L{number}]')
        elif status in {'正式', '排除'}:
            available.add(version)
    if len(adopted) > 4:
        raise ValueError('最多可分開核對四個互相矛盾的採用版本，請確認本批適用依據。')
    if selection and (len(adopted) < 2 or selection['name'] not in adopted):
        raise ValueError('正式採用答覆須選擇同批原有的候選採用版本。')

    observation = {'scope': args['scope']}
    if len(adopted) > 1:
        observation['competing_adoptions'] = [{'name': name, 'citations': refs} for name, refs in adopted.items()]
    if len(adopted) > 1 and not selection:
        versions = []
        for name in adopted:
            if name not in available:
                result = {'decision': {'status': 'missing_version', 'recorded_results': []}}
            else:
                try:
                    result = _evaluate(args, parsed, name, amendment)
                except ValueError as exc:
                    result = {'decision': {'status': 'invalid_version', 'recorded_results': []},
                              'error': str(exc)}
            versions.append({'name': name, **result})
        observation['candidate_versions'] = versions
        outcomes = list(dict.fromkeys(value for row in versions for value in
                        row['decision'].get('possible_results', row['decision'].get('recorded_results', []))))
        if len(outcomes) == 1 and all(row['decision']['status'] == 'resolved' for row in versions):
            observation['decision'] = {'status': 'shared_result', 'possible_results': outcomes}
        else:
            observation['decision'] = {'status': 'version_uncertain', 'recorded_results': outcomes}
            if args.get('clarify'):
                observation['clarification_plan'] = _clarification(args, versions)
        observation['evidence_sha256'] = goal_stages.digest([lines, amendment])
    else:
        selected = selection['name'] if selection else next(iter(adopted), None)
        if selected and selected not in available:
            raise ValueError('已讀區段缺少本批所採用版本的正式規則。')
        result = _evaluate(args, parsed, selected, amendment)
        saved_amendment = result.pop('amendment', None)
        observation.update(result)
        evidence = [lines, saved_amendment]
        if selection:
            evidence.append(selection)
        observation['evidence_sha256'] = goal_stages.digest(evidence)
        if saved_amendment:
            observation['amendment'] = saved_amendment
        if selected:
            observation['adopted_version'] = {'name': selected, 'citations': adopted[selected]}
        if selection:
            observation['version_selection'] = selection
    if declared:
        observation['declared_ranges'] = declared
    return observation


def checked_step(steps, declared, conditions_hash):
    return next((step['sequence'] for step in reversed(steps)
                 if step['action']['tool'] == 'check_source_decision'
                 and step['observation'].get('declared_ranges') == declared
                 and step['observation'].get('goal_conditions_sha256') == conditions_hash), None)


def validate_saved(db, *, replies_present, guidance_present, scope_present=False):
    query = '''SELECT DISTINCT g.id,g.notes,g.sources_json,g.objective FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id WHERE s.action_json LIKE '%"check_source_decision"%'
        OR s.observation_json LIKE '%"source_check_step"%' '''
    if scope_present:
        query += ' UNION SELECT g.id,g.notes,g.sources_json,g.objective FROM autonomous_goals g JOIN autonomous_decision_scopes d ON d.goal_id=g.id'
    for gid, notes, sources_json, objective in db.execute(query):
        raw_steps = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                               'WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        replies = goal_reply_sources.read(db, gid, notes) if replies_present else []
        guidance = goal_guidance_sources.read(db, gid, notes, len(raw_steps)) if guidance_present else []
        sources = json.loads(sources_json)
        declared = read_scope(db, gid, objective) if scope_present else []
        if declared and not {row['document_id'] for row in declared} <= {row['document_id'] for row in sources}:
            raise ValueError('決策規則範圍不屬於本目標的文件。')
        prior = []
        for sequence, raw_action, raw_observation in raw_steps:
            action, observation = json.loads(raw_action), json.loads(raw_observation)
            if action['tool'] == 'check_source_decision':
                expected = calculate(action['args'], prior, sources,
                                     [row for row in replies if row['ask_sequence'] < sequence],
                                     goal_guidance_sources.prior_sources(guidance, sequence), declared=declared)
                actual = {key: value for key, value in observation.items()
                          if key not in {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}}
                if goal_stages.digest(actual) != goal_stages.digest(expected):
                    raise ValueError('保存的原文決策表與查讀步驟不一致。')
            if (declared and action['tool'] in DELIVERY_TOOLS) or 'source_check_step' in observation:
                scope_hash = observation.get('goal_conditions_sha256')
                check = checked_step(prior, declared, scope_hash) if declared and isinstance(scope_hash, str) else None
                if check is None or type(observation.get('source_check_step')) is not int or observation['source_check_step'] != check:
                    raise ValueError('保存成果缺少相符的決策規則核對步驟。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
