"""Build a finite decision table from explicit, already read scoped rule lines."""
import json
import re

from . import decision_table, document_ranges, goal_guidance_sources, goal_reply_sources, goal_stages


GUIDANCE = '''check_source_decision可逐行核對固定格式的規則。若原文使用「範圍｜狀態｜條件一、條件二：結果」，用此工具代替自行轉錄。傳入scope完整範圍名稱、conditions欄序、domains完整選項、known本次已確認值，以及ranges已完整查讀的原規則區段（最多8段共32行，包含另案及未採用的行，不含標題）。範圍必須完整相同。文件狀態只能是正式、未採用或排除。排除行省略冒號和結果。範例「甲批｜正式｜高、室內：甲架」。
若本次有明確保存的單筆更正，且原話整則符合「甲批｜更正｜高、室內：新架」，可加amendment，值為實際的補充或更正引用編號。只替換同範圍、同條件的一列或補缺列，不改其他批次。只用本次適用且已保存的正式更正，不使用引文或假設。格式或範圍不同時回到原文核對，不改寫原行冒充精確來源。
工具回傳由原行整理的表與decision核算，以及原行來源與是否採用。區分另案、未採用、正式排除與被更正取代。保留已確認的數量、窗口等資料，按實際來源引用。決策有缺列時只問缺的正式規則，收到符合格式的答覆後帶amendment重算。此工具核對你指定的已讀區段與字面範圍，是否涵蓋全部適用規則仍依原始資料。'''

_AMENDMENT = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_STATES = {'正式', '未採用', '排除'}


def validate(args):
    fields = {'scope', 'conditions', 'domains', 'known', 'ranges'}
    if not isinstance(args, dict) or set(args) not in (fields, fields | {'amendment'}):
        raise ValueError('原文決策表需提供範圍、欄位、宣告選項、已知值與已讀行。')
    decision_table._label(args['scope'])
    if '｜' in args['scope']:
        raise ValueError('範圍名稱不可包含規則分隔符號。')
    decision_table.validate({key: args[key] for key in ('conditions', 'domains', 'known')} | {'rows': [], 'excluded': []})
    document_ranges.validate_batch({'ranges': args['ranges']})
    seen = set()
    for item in args['ranges']:
        did, start, end = (item[key] for key in document_ranges.BATCH_FIELDS)
        if not 0 <= end - start < 32:
            raise ValueError('每段原文規則須指定有效的連續行。')
        for line in range(start, end + 1):
            position = (did, line)
            if position in seen or len(seen) == 32:
                raise ValueError('原文規則最多32個不重複的行。')
            seen.add(position)
    if 'amendment' in args and (not isinstance(args['amendment'], str) or not _AMENDMENT.fullmatch(args['amendment'])):
        raise ValueError('須指定本目標已保存的更正或補充編號。')


def _parse(text, size, *, user=False):
    if not isinstance(text, str) or len(text.splitlines()) != 1:
        raise ValueError('原文規則或保存更正須為單一完整行。')
    parts = text.removesuffix('。').split('｜')
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
    return scope, status, values, result


def _read_lines(ranges, steps, sources):
    positions = [(item['document_id'], number) for item in ranges
                 for number in range(item['start_line'], item['end_line'] + 1)]
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
    if len(found) != len(positions):
        raise ValueError('規則範圍有尚未完整查讀的行。請先查讀原文。')
    return [(did, number, *found[(did, number)]) for did, number in positions]


def calculate(args, steps, sources, replies=(), guidance=()):
    validate(args)
    lines = _read_lines(args['ranges'], steps, sources)
    size = len(args['conditions'])
    amendment = None
    if token := args.get('amendment'):
        source = next((row for row in (*replies, *guidance) if row['citation'] == token), None)
        if source is None:
            raise ValueError('找不到本目標已保存的更正或補充。')
        scope, _, values, result = _parse(source.get('answer', source.get('content')), size, user=True)
        if scope != args['scope']:
            raise ValueError('所選更正只適用其原話中的完整範圍。')
        amendment = {'citation': token, 'sha256': source['sha256'], 'when': values, 'result': result}

    records, rows, excluded = [], [], []
    indexed = {}
    for did, number, sequence, _, text in lines:
        scope, status, values, result = _parse(text, size)
        record = {'citation': f'[D{did}:L{number}]', 'read_step': sequence}
        if scope != args['scope']:
            disposition = 'other_scope'
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
        amendment['table_row'] = len(rows)
    table = {key: args[key] for key in ('conditions', 'domains', 'known')} | {'rows': rows, 'excluded': excluded}
    observation = {'scope': args['scope'], 'source_rows': records, 'table': table,
                   'decision': decision_table.calculate(table),
                   'evidence_sha256': goal_stages.digest([lines, amendment])}
    if amendment:
        observation['amendment'] = amendment
    return observation


def validate_saved(db, *, replies_present, guidance_present):
    goals = db.execute('''SELECT DISTINCT g.id,g.notes,g.sources_json FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id WHERE s.action_json LIKE '%"check_source_decision"%' ''')
    for gid, notes, sources_json in goals:
        raw_steps = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                               'WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        replies = goal_reply_sources.read(db, gid, notes) if replies_present else []
        guidance = goal_guidance_sources.read(db, gid, notes, len(raw_steps)) if guidance_present else []
        sources = json.loads(sources_json)
        prior = []
        for sequence, raw_action, raw_observation in raw_steps:
            action, observation = json.loads(raw_action), json.loads(raw_observation)
            if action['tool'] == 'check_source_decision':
                expected = calculate(action['args'], prior, sources,
                                     [row for row in replies if row['ask_sequence'] < sequence],
                                     goal_guidance_sources.prior_sources(guidance, sequence))
                actual = {key: value for key, value in observation.items()
                          if key not in {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}}
                if goal_stages.digest(actual) != goal_stages.digest(expected):
                    raise ValueError('保存的原文決策表與查讀步驟不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
