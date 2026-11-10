"""Render an opted-in text report from a verified branch calculation."""
import json
import re

from . import conversation_history, goal_guidance_sources, goal_reply_sources, quantity_evidence, quantity_report_fields, work_plan


DIRECTIVE = '文字交付：只列已核對的分版結果。'
GUIDANCE = ('quantity_report_required表示使用者要求從已核對的分版步驟直接交付文字報告。'
            '核對與提問照原要求執行，使用者要暫表時仍可用ask的partial_result。'
            '最後用finish，args只有{"quantity_report_step":實際分版步驟序號}。'
            '工具從目前條件的最後一個calculate_quantity_branches產出算式、結果、待查欄及來源。'
            '保留的試想只出現在備註，結果區由工具的已執行算式產生。')

_MARKDOWN = str.maketrans({'[': '［', ']': '］', '|': '｜', '`': '｀', '\\': '＼',
                           '<': '＜', '>': '＞', '*': '＊', '_': '＿', '#': '＃'})
_OPERATORS = {'sum': ' + ', 'difference': ' − ', 'product': ' × ', 'quotient': ' ÷ '}
_MISSING = re.compile(r'\[D([0-9]+):L([0-9]+)\]')
_STATE_TEXT = {'outside_adoption': '受此欄採用限制', 'candidate_unavailable': '缺同版數值',
               'unit_unconfirmed': '缺單位', 'incompatible_unit': '單位無法換算',
               'quantity_unconfirmed': '數值待確認'}
_ADOPTION_TEXT = {'incomplete': '採用範圍仍有缺行。', 'conflicting': '採用記載互相矛盾。',
                  'unrecorded': '本欄未有明文採用記載。',
                  'candidate_unavailable': '採用記載所指版本缺少可核對的數值。'}
_FIELD_PENDING = {'incomplete': '指定文件缺行', 'conflicting': '文件記載不一致',
                  'unconfirmed': '尚無一致的明文值', 'user_pending': '使用者確認待查'}


def required(objective):
    lines = objective.splitlines()[:2]
    return DIRECTIVE in lines or quantity_report_fields.DIRECTIVE in lines


def validate(args):
    if type(args['quantity_report_step']) is not int or not 1 <= args['quantity_report_step'] <= 32:
        raise ValueError('分版交付須指定實際的分版算式步驟編號。')


def _plain(text):
    return text.translate(_MARKDOWN)


def _formula(node):
    if isinstance(node, str):
        return node
    children = [_formula(child) for child in node['values']]
    return '(' + _OPERATORS[node['operation']].join(children) + ')'


def _result_unit(branch, shared_operands):
    if shared_operands or any(row['operation'] not in {'sum', 'difference'}
                              for row in branch['calculation']['calculations']):
        return ''
    units = {row['unit'] for row in branch['inputs']}
    if len(units) != 1 or 'ratio' in units:
        return ''
    unit, = units
    return ' ' + unit


def _quantity(quantity):
    original = _plain(quantity['number'])
    if quantity['source_unit']:
        original += ' ' + quantity['source_unit']
    return original + ' ' + quantity['citation']


def _input(row):
    line = _plain(row['field']) + ' · '
    quantity = row.get('quantity')
    if quantity is not None:
        line += _quantity(quantity)
        if 'original_quantity' in row:
            line += '（原值 ' + _quantity(row['original_quantity']) + '）'
    if row['status'] != 'confirmed':
        line += ('，' if quantity is not None else '') + _STATE_TEXT[row['status']]
    return line


def _text_value(value):
    return f'「{_plain(value)}」' if value is not None else '待查'


def _fields_section(fields):
    lines = ['', '### 全表附加欄位']
    for row in fields:
        display = _text_value(row['value'])
        if row['status'] != 'confirmed':
            display += f"（{_FIELD_PENDING[row['status']]}）"
        if row['confirmation'] is not None:
            display += ' ' + row['confirmation']['citation']
        lines.append(f"- {_plain(row['field'])} · {display}")
        if row['records']:
            records = '、'.join(_text_value(record['value']) + ' ' + record['citation'] for record in row['records'])
            lines.append(f'  - 文件記載 · {records}')
        if row['missing']:
            missing = '、'.join(f"D{record['document_id']} 第 {record['line']} 行" for record in row['missing'])
            lines.append(f'  - 指定原行待查 · {missing}')
    return lines


def _report(observation, fields):
    adoption = observation['adoption']
    lines = ['## 分版條件結果', '', _ADOPTION_TEXT[adoption['status']], '',
             '| 版本 | 欄位及來源 | 已核對算式與結果 |',
             '|---|---|---|']
    for branch in observation['branches']:
        inputs = '；'.join(_input(row) for row in branch['inputs'])
        if branch['status'] == 'calculated':
            result = _formula(branch['expression']) + ' = ' + branch['calculation']['exact']
            result += _result_unit(branch, observation['operands'])
        elif branch['status'] == 'calculation_failed':
            result = '算式未完成，請查看分版步驟'
        else:
            result = '待查'
        lines.append(f"| {_plain(branch['version'])} | {inputs} | {result} |")
    if fields:
        lines.extend(_fields_section(fields))
    lines.extend(['', '### 來源與補充'])
    for record in adoption['records']:
        lines.append(f"- 採用記載 · {_plain(record['field'])}採用{_plain(record['version'])} {record['citation']}")
    for citation in adoption.get('missing', ()):
        did, line = _MISSING.fullmatch(citation).groups()
        lines.append(f'- 採用行待查 · D{did} 第 {line} 行')
    for operand in observation['operands']:
        lines.append(f"- 共用運算元 · {_plain(operand['quote'])} {operand['citation']}")
    for correction in observation.get('correction_history', ()):
        lines.append(f"- 正式更正 · {_plain(correction['quote'])} {correction['citation']}")
    for note in observation.get('retained_notes', ()):
        lines.append(f"- 試想備註 · {_plain(note['quote'])} {note['citation']}")
    result = '\n'.join(lines)
    if len(result) > 12000:
        raise ValueError('分版報告超出文字交付上限，請縮小目標範圍。')
    return result


def render(args, steps, scope, goal, replies, guidance, cancel=None):
    validate(args)
    if not required(goal['objective']):
        raise ValueError('此文字交付需由目標宣告只列已核對的分版結果。')
    prior = next((step for step in reversed(steps) if step['action']['tool'] == 'calculate_quantity_branches'), None)
    if (prior is None or prior['sequence'] != args['quantity_report_step']
            or prior['observation'].get('goal_conditions_sha256') != scope
            or 'anchor_field' not in prior['action']['args']):
        raise ValueError('請引用目前條件下最後一個已核對的多欄分版算式。')
    observed = quantity_evidence.calculate_branches(
        prior['action']['args'], steps, goal['sources'], scope, goal['objective'], replies, guidance, cancel,
        declared=goal.get('quantity_adoption_scope'), goal=goal)
    if observed != {key: value for key, value in prior['observation'].items() if key not in quantity_evidence._METADATA}:
        raise ValueError('最後的分版算式與可重驗的條件或來源不符。')
    fields = quantity_report_fields.observe(goal, steps, replies, guidance)
    if {row['field'] for row in fields} & {row['field'] for row in observed['comparisons']}:
        raise ValueError('附加文字欄位名稱須與分版數量欄位分開。')
    result = {'report': _report(observed, fields)}
    if fields:
        result['quantity_report_fields'] = fields
    return result


def validate_saved(db, *, replies_present, guidance_present, scope_present):
    query = '''SELECT DISTINCT g.id,g.objective,g.notes,g.sources_json,g.status,g.result
               FROM autonomous_goals g JOIN autonomous_steps s ON s.goal_id=g.id
               WHERE s.action_json LIKE '%"quantity_report_step"%' OR s.observation_json LIKE '%"quantity_report_step"%'
                  OR s.observation_json LIKE '%"quantity_report_fields"%' '''
    for gid, objective, notes, sources, status, result in db.execute(query):
        rows = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                          'WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        replies = goal_reply_sources.read(db, gid, notes) if replies_present else []
        guidance = goal_guidance_sources.read(db, gid, notes, len(rows)) if guidance_present else []
        sources = json.loads(sources)
        history = conversation_history.saved_fingerprint(db, gid)
        declared = quantity_evidence.read_scope(db, gid, objective, sources) if scope_present else None
        goal = {'id': gid, 'objective': objective, 'notes': notes, 'sources': sources,
                'quantity_adoption_scope': declared, 'conversation_history': {'sha256': history} if history else None}
        prior = []
        for sequence, action, observation in rows:
            action, observation = json.loads(action), json.loads(observation)
            if ({'quantity_report_step', 'quantity_report_fields'} & observation.keys()
                    or (action['tool'] == 'finish' and 'quantity_report_step' in action['args'])):
                scope = observation.get('goal_conditions_sha256')
                if (action['tool'] != 'finish' or action['args'].keys() != {'quantity_report_step'}
                        or observation.get('quantity_report_step') != action['args']['quantity_report_step']
                        or not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{64}', scope)):
                    raise ValueError('保存的分版報告缺少有效的步驟或條件。')
                expected = render(action['args'], prior, scope, goal, replies, guidance)
                committed = status == 'report_ready' and sequence == len(rows)
                actual = {key: observation[key] for key in ('report', 'quantity_report_fields') if key in observation}
                if (actual != expected
                        or (committed and (result != expected['report'] or scope != work_plan.conditions(goal)))):
                    raise ValueError('保存的分版報告與已執行算式不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
