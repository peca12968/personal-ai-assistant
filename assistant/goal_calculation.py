"""Check evidence for ordinary goal arithmetic and render explicitly checked deliveries."""
import json
import re
from pathlib import Path

from . import artifacts, expression_math, goal_guidance_sources, goal_reply_sources, goal_sources, operand_evidence
from .acceptance import strict_json
from .goal_stages import digest


DIRECTIVE = '算式須逐項核對來源。'
RECEIPT = 'checked_calculation_report'
PROOF = 'calculation_evidence'
REPORT_FIELDS = goal_reply_sources.REPORT_FIELDS

OPTIONAL_GUIDANCE = (
    '一般文件解題如需重算，可在 calculate_expression 的 expression 之外提供 operands，'
    '每個數字葉節點各一筆 {"path":"/values/0","citation":"[D1:L2]","quote":"箱數：12 箱","field":"箱數","unit":"箱"}。'
    'quote須為已讀原行或當前補充的逐字短句，path是數字樹的實際路徑。單位換算改用 conversion {"from":"kg","to":"g"}。'
    '直接沿用仍適用的已核對結果，在欄位旁留原始依據。')
GUIDANCE = (
    OPTIONAL_GUIDANCE + '\n本目標已要求逐項核對。計算只能用 calculate_expression 的 operands；'
    '每筆須有完整欄位名 field、單位 unit 或 conversion，quote 使用明確確認該欄的完整原句（去掉句末標點）。'
    '未決、候選、被更正或撤回的數字不能代入。只算有證據的部分。'
    '交付、寫檔及附暫表提問使用 calculation_report 陣列，替換 report、content 或 partial_result。'
    '計算列 {"label":"總件數","calculation_step":3,"unit":"件"} 只送實際步驟號，不送答案。'
    '獨立事實列 {"label":"窗口","citation":"[D1:L4]","quote":"窗口：北門"} 使用完整逐字原句。'
    '缺漏、撤回、候選或未決欄位一律用待查列 {"label":"扣除量","pending":true}，'
    '即使可引用撤回原句也只用待查列。label 只填欄名，說明及引用可放在 ask 的 question。'
    '留下有依據的獨立事實列，只問會影響交付的待補欄位。'
    '若 pending_recheck_evidence.calculations 提供仍有效的編號，可用 '
    '{"label":"總件數","recheck_calculation":1,"unit":"件"} 沿用。'
    '先寫檔再用同一組 calculation_report 與 filenames 交付，檔案會由工具產生。')

_CALC_FIELDS = {'label', 'calculation_step', 'unit'}
_FACT_FIELDS = {'label', 'citation', 'quote'}
_PENDING_FIELDS = {'label', 'pending'}
_RECHECK_FIELDS = {'label', 'recheck_calculation', 'unit'}
_UNCONFIRMED = re.compile(r'尚未|未確認|待確認|待查|未定|待定|候選|暫估|暫定|約莫|估計|預估|推測|可能|假如|假設|如果|是否|撤回|取消|不再採用|未採用|不採用|不作準|不算|歷史|舊版|前批|上一批|上次|原值|舊值|草案|引述|轉述|試算')
_UNCERTAIN_PREFIX = re.compile(r'假如|假設|如果|若是|若採|若用|僅作示例|只是假設|引述|轉述')
_REVISION = re.compile(r'改為|改成|更正為|改用|為準|撤回|取消|不再|不採|不算|待確認|未確認')
_COMMA = re.compile(r'(?![0-9])')
_UNSAFE_LABEL = re.compile(r'[\x00-\x1f\x7f\[\]<>#|＝=]')


def required(objective):
    return DIRECTIVE in objective.splitlines()[:2]


def _clauses(text):
    return goal_guidance_sources.standalone_clauses(text, comma_before=_COMMA)


def _valid_clause(text, start, quote):
    if _UNCONFIRMED.search(quote):
        return False
    for offset, clause, question in _clauses(text):
        if start == offset and clause == quote and not question:
            before = re.split(r'[。；;!?！？\r\n]', text[:start])[-1]
            return not _UNCERTAIN_PREFIX.search(before)
    return False


def _later_change(text, field):
    pattern = re.compile(rf'(?:^|欄位「|「){re.escape(field)}(?=」|[：:\s]|\d|已確認|確認|正式|為|改|更正|撤回|取消|仍|待|未|不|以|共)')
    for _, clause, question in _clauses(text):
        if not question and (match := pattern.search(clause)):
            suffix = clause[match.end():]
            if _REVISION.search(suffix) or re.match(r'」?\s*(?:[：:]|已確認|確認為|為|共|\d)', suffix):
                return True
    return False


def _source_order(replies, guidance, sequence):
    return sorted([(r['notes_start'], r['citation'], r['answer']) for r in replies if r['ask_sequence'] < sequence]
                  + [(r['notes_start'], r['citation'], r['content']) for r in guidance if r['steps_seen'] < sequence])


def _complete_excerpts(steps, scope, recheck):
    versions = {(row['document_id'], row['name'], row['sha256']) for row in scope}
    excerpts = [row for step in steps for row in step['observation'].get('excerpts', [])]
    if recheck:
        excerpts.extend(recheck['excerpts'])
    return [row for row in excerpts if row.get('partial_line') is False
            and (row['document_id'], row['name'], row['sha256']) in versions]


class Sources:
    def __init__(self, objective, steps, scope, replies, guidance, recheck, documents):
        sequence = len(steps) + 1
        self.steps = steps
        self.excerpts = _complete_excerpts(steps, scope, recheck)
        self.user = operand_evidence.user_texts(objective, replies, guidance, sequence)
        self.order = _source_order(replies, guidance, sequence)
        self.documents = {(row['id'], row['name'], row['sha256']): row['content'].splitlines() for row in documents}
        self.recheck = recheck
        self.strict = required(objective)

    def literal(self, citation):
        text, sha = operand_evidence.source_text(citation, self.excerpts, self.user)
        if match := re.fullmatch(r'\[D(\d+):L(\d+)\]', citation):
            did, line = map(int, match.groups())
            key = next((key for key in self.documents if key[0] == did and key[2] == sha), None)
            if key:
                original = self.documents[key]
                if line > len(original) or original[line - 1] != text:
                    raise ValueError('運算元或事實原行與匯入副本不一致。')
        return text, sha

    def confirm(self, row):
        text, _ = self.literal(row['citation'])
        if not self.strict:
            return
        unit_end = row.get('unit_end', row.get('conversion', {}).get('unit_end'))
        if (not row.get('field') or unit_end is None
                or text[unit_end:row['quote_end']].strip() not in {'', '」'}
                or not _valid_clause(text, row['quote_start'], row['quote'])):
            raise ValueError('運算元須使用完整且已確認的欄位原句與單位。候選或待查數值請保留待查。')
        # A later conflicting assignment in one source carries the same citation.
        remainder = text[row['quote_end']:]
        if _later_change(remainder, row['field']):
            raise ValueError('同一來源後段另有更正或待確認值，請先核對欄位。')
        self.confirm_fact(row['field'], row['citation'])

    def calculate(self, args, cancel=None):
        expression = {'expression': args['expression']}
        validate(args)
        bound = operand_evidence.bind(args['operands'], args['expression'], self.excerpts, self.user, ordinary=True)
        for row in bound:
            self.confirm(row)
        return {**expression_math.calculate(expression, cancel),
                PROOF: {'version': 1, 'operands': bound, 'recheck_sha256': self.recheck['sha256'] if self.recheck else None}}

    def rows(self, rows):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            raise ValueError('核對成果需列出一至十二個欄位。')
        seen, output = set(), []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('核對成果欄位格式不符。')
            label = _label(row.get('label'), 80)
            if label in seen:
                raise ValueError('核對成果的欄位名不可重複。')
            seen.add(label)
            if row.keys() in (_CALC_FIELDS, _RECHECK_FIELDS):
                args, observation = self._report_calculation(row)
                proof = observation.get(PROOF)
                if not proof or observation.get('status') != 'expression_calculated':
                    raise ValueError('成果須引用已逐項核對來源的算式步驟。')
                bound = operand_evidence.bind(args['operands'], args['expression'], self.excerpts, self.user, ordinary=True)
                if bound != proof['operands']:
                    raise ValueError('算式來源與目前可核對的證據不一致。')
                for item in bound:
                    self.confirm(item)
                unit = _label(row['unit'], 16)
                if not re.fullmatch(r'[^\W\d_][\w%°µ/^²³·-]*|[%°]', unit):
                    raise ValueError('結果單位須為簡短單位名稱。')
                output.append({'欄位': label, '精確結果': observation['exact'], '單位': unit,
                               '算式': _formula(args['expression']),
                               '來源': [{'欄位': item['field'], '原文': item['quote'], '引用': item['citation']}
                                        for item in bound]})
            elif row.keys() == _FACT_FIELDS:
                citation, quote = row['citation'], row['quote']
                if not isinstance(citation, str) or not isinstance(quote, str) or not 1 <= len(quote) <= 180:
                    raise ValueError('事實列須使用完整原句與原始引用。')
                text, _ = self.literal(citation)
                start = text.find(quote)
                if (start < 0 or text.find(quote, start + 1) >= 0 or not _valid_clause(text, start, quote)
                        or not re.match(rf'(?:欄位「{re.escape(label)}」|{re.escape(label)})(?:[:：]|\s|確認為|已確認為|正式改為|為)', quote)):
                    raise ValueError('事實列須引用唯一完整且目前確認同一欄位的原句。')
                self.confirm_fact(label, citation)
                output.append({'欄位': label, '原文': quote, '引用': citation})
            elif row.keys() == _PENDING_FIELDS and row['pending'] is True:
                output.append({'欄位': label, '狀態': '待查'})
            else:
                raise ValueError('核對成果只能列算式步驟、原文事實或待查欄位。')
        return output

    def _report_calculation(self, row):
        if 'recheck_calculation' in row:
            number = row['recheck_calculation']
            available = self.recheck['calculations'] if self.recheck else ()
            if type(number) is not int or not 1 <= number <= len(available):
                raise ValueError('查核成果須引用目前暫表重核的算式編號。')
            return available[number - 1]['args'], available[number - 1]['result']
        number = row['calculation_step']
        if type(number) is not int or not 1 <= number <= len(self.steps):
            raise ValueError('核對成果須引用此目標較早的算式步驟。')
        step = self.steps[number - 1]
        if step['sequence'] != number or step['action']['tool'] != 'calculate_expression':
            raise ValueError('成果須引用已逐項核對來源的算式步驟。')
        return step['action']['args'], step['observation']

    def confirm_fact(self, label, citation):
        position = next((offset for offset, key, _ in self.order if key == citation), -1)
        if any(offset > position and _later_change(body, label) for offset, _, body in self.order):
            raise ValueError(f'欄位「{label}」另有較新的確認或更正。請引用現行值，或標示待查。')


def validate(args):
    if not isinstance(args, dict) or args.keys() != {'expression', 'operands'}:
        raise ValueError('逐項核對的算式須提供 expression 及 operands。')
    expression_math.validate({'expression': args['expression']})
    operand_evidence.validate(args['operands'], args['expression'], None, ordinary=True)


def _label(value, limit):
    if (not isinstance(value, str) or not 1 <= len(value) <= limit or value.strip() != value
            or _UNSAFE_LABEL.search(value)):
        raise ValueError('核對成果須使用簡短欄位名與單位。')
    return value


def _formula(node):
    if isinstance(node, str):
        return node
    separator = {'sum': ' + ', 'difference': ' − ', 'product': ' × ', 'quotient': ' ÷ '}[node['operation']]
    return '(' + separator.join(_formula(child) for child in node['values']) + ')'


def render(rows, filename=None):
    if filename and Path(filename).suffix.lower() == '.json':
        return json.dumps(rows, ensure_ascii=False, indent=2) + '\n'
    lines = []
    for row in rows:
        label = row['欄位']
        if '精確結果' in row:
            evidence = '；'.join(f'{item["原文"]} {item["引用"]}' for item in row['來源'])
            lines.append(f'{label}：{row["算式"]} ＝ {row["精確結果"]} {row["單位"]}。{evidence}')
        elif '引用' in row:
            lines.append(f'{row["原文"]} {row["引用"]}')
        else:
            lines.append(f'{label}：待查')
    return '\n'.join(lines)


def needs_report(tool, args):
    return tool in {'finish', 'write_artifact', 'deliver'} or tool == 'ask' and bool(
        {'partial_result', 'calculation_report'} & args.keys())


def prepare(action, sources):
    tool, args = action['tool'], action['args']
    if not sources.strict or not needs_report(tool, args):
        return action, None, None
    field = REPORT_FIELDS[tool]
    replaced = set(args) - {'calculation_report'}
    if ('calculation_report' not in args or field in replaced
            or (tool != 'ask' and replaced != {
                'finish': set(), 'write_artifact': {'filename'}, 'deliver': {'filenames'},
            }[tool])):
        raise ValueError('本目標要求逐項核對，請用 calculation_report 交付或附暫表。')
    rows = args['calculation_report']
    output = sources.rows(rows)
    args = {key: value for key, value in args.items() if key != 'calculation_report'}
    args[field] = render(output, args.get('filename'))
    expected_files = None
    if tool == 'deliver':
        artifacts.validate_names(args['filenames'])
        expected_files = check_deliveries(args['filenames'], output, sources.steps)
    return {**action, 'args': args}, {'version': 1, 'rows': rows}, expected_files


def check_deliveries(names, output, steps):
    saved = {step['action']['args']['filename'].casefold(): step['action']['args']['content']
             for step in steps if step['action']['tool'] == 'write_artifact' and RECEIPT in step['observation']}
    expected = {name.casefold(): render(output, name) for name in names}
    if any(saved.get(key) != body for key, body in expected.items()):
        raise ValueError('交付檔案須與目前已核對的成果一致，請先用新檔名重新寫入有異動的成果。')
    return expected


def _recheck(steps, guidance, rechecks):
    prior = goal_guidance_sources.prior_sources(guidance, len(steps) + 1)
    ask = next((step for step in reversed(steps) if step['action']['tool'] == 'ask'), None)
    if not prior or ask is None:
        return None
    sequence = ask['sequence']
    number = prior[-1]['number']
    latest = next((row for row in rechecks.values() if row['ask_sequence'] == sequence
                   and row['guidance_number'] == number), None)
    if latest:
        return latest
    if any(row['ask_sequence'] == sequence for row in rechecks.values()):
        return None
    from .goal_partial_recheck import _bridge
    carried = _bridge(steps, prior, sequence, rechecks)
    return carried if carried and carried['guidance_number'] == number else None


def validate_saved(db, *, replies_present, guidance_present, checked_rechecks, artifacts_present):
    query = """SELECT DISTINCT g.id,g.session_id,g.objective,g.notes,g.sources_json,g.status,g.result FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id WHERE instr(g.objective, ?) OR
        instr(s.action_json, '"operands"') OR instr(s.observation_json, ?) OR instr(s.observation_json, ?)"""
    for gid, sid, objective, notes, raw_scope, status, result in db.execute(query, (DIRECTIVE, PROOF, RECEIPT)):
        steps = [{'sequence': seq, 'action': strict_json(raw), 'observation': strict_json(observed)}
                 for seq, raw, observed in db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                                                     'WHERE goal_id=? ORDER BY sequence', (gid,))]
        if not required(objective) and not any(
                PROOF in step['observation'] or RECEIPT in step['observation'] or
                (step['action']['tool'] == 'calculate_expression' and 'operands' in step['action']['args'])
                for step in steps):
            continue
        replies = goal_reply_sources.read(db, gid, notes, include_positions=True) if replies_present else []
        guidance = goal_guidance_sources.read(db, gid, notes, len(steps)) if guidance_present else []
        scope = strict_json(raw_scope)
        docs = {row[0]: dict(zip(('id', 'name', 'sha256', 'content'), row, strict=True))
                for row in db.execute('SELECT id,name,sha256,content FROM documents WHERE session_id=?', (sid,))}
        history = goal_sources.verify_history(steps, scope, docs)
        if (required(objective) and status == 'report_ready'
                and (not steps or steps[-1]['action']['tool'] not in {'finish', 'deliver'})):
            raise ValueError('已完成的核對成果缺少交付步驟。')
        known_files = {}
        for index, step in enumerate(steps):
            action, seen = step['action'], step['observation']
            tool, args = action['tool'], action['args']
            calculated = tool == 'calculate_expression' and 'operands' in args
            reports = required(objective) and needs_report(tool, args)
            if required(objective) and tool in {'calculate', 'calculate_expression'} and not calculated:
                raise ValueError('逐項核對目標有未核對的算式。')
            if not calculated and not reports and not ({PROOF, RECEIPT} & seen.keys()):
                continue
            if step['sequence'] != index + 1:
                raise ValueError('算式或成果步驟順序不符。')
            prior = steps[:index]
            recheck = _recheck(prior, guidance, checked_rechecks.get(gid, {}))
            context = Sources(objective, prior, history.get(step['sequence'], scope), replies, guidance,
                              recheck, docs.values())
            if calculated:
                expected = context.calculate(args)
                if any(digest(seen.get(key)) != digest(value) for key, value in expected.items()):
                    raise ValueError('保存算式與原文、單位或精確結果不符。')
            if reports:
                field = REPORT_FIELDS[tool]
                receipt = seen.get(RECEIPT, {})
                raw = {**action, 'args': {key: value for key, value in args.items() if key != field}}
                raw['args']['calculation_report'] = receipt.get('rows')
                rendered, verified, _ = prepare(raw, context)
                if digest(receipt) != digest(verified) or digest(rendered) != digest(action):
                    raise ValueError('成果與已核對算式或原文不一致。')
                if tool == 'write_artifact':
                    name = args['filename']
                    known_files[name.casefold()] = args['content']
                elif (seen.get(field) != args[field] or tool in {'finish', 'deliver'} and
                      index == len(steps) - 1 and status == 'report_ready' and result != args[field]):
                    raise ValueError('保存的最終成果與核對內容不一致。')
            if (PROOF in seen) != calculated or (RECEIPT in seen) != reports:
                raise ValueError('算式或成果缺少對應步驟。')
        for key, content in known_files.items():
            row = db.execute('SELECT content FROM autonomous_artifacts WHERE goal_id=? AND file_key=?',
                             (gid, key)).fetchone() if artifacts_present else None
            if row is None or row[0] != content:
                raise ValueError('保存檔案與核對成果不一致。')
