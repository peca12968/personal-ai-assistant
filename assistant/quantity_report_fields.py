"""Resolve independent text fields declared for a checked quantity report."""
import re

from . import source_decision


DIRECTIVE = '文字交付：已核對的分版結果及附加欄位。'
_PREFIX = '附加欄位：'
_NAME = r'(?P<field>[^「」｜\x00\r\n]{1,80})'
_VALUE = r'(?P<value>[^「」｜\x00\r\n]{1,100})'
_DECLARATION = re.compile(rf'{_PREFIX}欄位「{_NAME}」｜(?P<source>.+)')
_RECORD = re.compile(rf'欄位「{_NAME}」｜文字值｜(?:「{_VALUE}」|待查)')
_CONFIRMATION = re.compile(rf'正式確認欄位「{_NAME}」(?:為「{_VALUE}」|待查)。')
_INVALID = '附加欄位須緊接交付宣告，逐行列完整名稱和來源，例如附加欄位：欄位「本批簽收人」｜[D3:L1-L2]。'
_NATURAL = '原文安排｜'
_ACTIONS = {'簽收人': '簽收', '送件人': '送件|送交', '核對人': '核對',
            '複核人': '複核', '紀錄人': '填寫紀錄|記錄'}
_ACTOR = r'(?:「(?P<quoted>[^「」｜\x00\r\n]{1,40})」|(?P<plain>(?P<name>[\u3400-\u9fff]{1,3})(?:同仁|先生|女士|小姐)))'
_UNSTATED_ACTOR = re.compile(r'[、，。；！？]|或|暫由|尚未|待確認|未定|待排|待查')
_PENDING = r'(?:待查|待排|待確認|尚未指派|尚未安排|未定)'

GUIDANCE = ('quantity_report_fields是使用者明列的全表附加欄位，與各版算式分開。'
            '先讀取missing所列的指定原文行，若行不存在或使用者允許待查，保留缺口。'
            'confirmed可直接交付。只有缺的欄位會影響交付或使用者要求先確認時才問。'
            '可請使用者整則回覆「正式確認欄位「完整名稱」為「文字值」。」或「正式確認欄位「完整名稱」待查。」。'
            '沒有正式確認或明文時，不把引述、別欄或數量當作附加欄位的值。'
            'quantity_note_review的field_confirmation只更新附加欄位。若mode為reviewed_notes，沿用原批次與算式，列齊citations。'
            '仍用只有quantity_report_step的finish。工具會將附加欄位與來源另列，無須自己撰寫報告。')
NATURAL_GUIDANCE = ('natural_assignment欄位依同名範圍的整行安排核對。原文明定由誰負責只表示安排；'
                    '已簽收、另案、引述、暫排或條件式提案都無法確認本欄。已讀但無適用原行時保留待查。')


def declared(objective, sources):
    lines = objective.splitlines()
    if DIRECTIVE not in lines[:2]:
        return []
    start = lines.index(DIRECTIVE) + 1
    declarations = [index for index, line in enumerate(lines) if line.startswith(_PREFIX)]
    if not 1 <= len(declarations) <= 4 or declarations != list(range(start, start + len(declarations))):
        raise ValueError(_INVALID)
    fields = {}
    catalog = {source['document_id'] for source in sources}
    for index in declarations:
        match = _DECLARATION.fullmatch(lines[index])
        if match is None or match['field'] != match['field'].strip() or match['field'] in fields:
            raise ValueError(_INVALID)
        field, source = match['field'], match['source']
        natural = source.startswith(_NATURAL)
        if natural and (not any(field.endswith(role) for role in _ACTIONS) or source == _NATURAL + '使用者確認'):
            raise ValueError('原文安排須指定簽收人、送件人、核對人、複核人或紀錄人欄位及文件原行。')
        source = source.removeprefix(_NATURAL)
        ranges = [] if source == '使用者確認' else source_decision.parse_range_list(source, invalid=_INVALID)
        if any(row['document_id'] not in catalog for row in ranges):
            raise ValueError('請先在目前對話匯入附加欄位宣告的文件。')
        fields[field] = {'field': field, 'ranges': ranges}
        if natural:
            fields[field]['natural_assignment'] = True
    return list(fields.values())


def _literal(pattern, text):
    match = pattern.fullmatch(text)
    if match is None or len(text.splitlines()) != 1:
        return None
    field, value = match['field'], match['value']
    if value is not None and (not value.strip() or value != value.strip() or value == '待查'):
        return None
    return field, value


def confirmation(text):
    return _literal(_CONFIRMATION, text)


def _natural_record(field, text):
    name = re.escape(field)
    if re.fullmatch(rf'{name}(?:為|是|：)?{_PENDING}。?', text):
        return {'value': None, 'kind': 'natural_pending'}
    role = next(role for role in _ACTIONS if field.endswith(role))
    scope = re.escape(field.removesuffix(role))
    direct = re.fullmatch(rf'{name}(?:為|是|：) *{_ACTOR}。?', text)
    delegated = direct or re.fullmatch(rf'{scope} *(?:由|安排由|將由|指定) *{_ACTOR} *(?:負責)?(?:{_ACTIONS[role]})。?', text)
    if delegated is None:
        return None
    if delegated['plain'] is not None and any(char in '或及暨與和代未待由暫' for char in delegated['name']):
        return None
    actor = delegated['quoted'] or delegated['plain']
    if actor != actor.strip() or _UNSTATED_ACTOR.search(actor) or re.fullmatch(_PENDING, actor):
        return None
    return {'value': actor, 'kind': 'natural_assignment'}


def _confirmations(goal, steps, replies, guidance, fields):
    from . import quantity_evidence

    available = quantity_evidence._available_corrections(0, len(steps) + 1, replies, guidance)
    parsed = {citation: parsed for citation, (text, _, _) in available.items()
              if (parsed := confirmation(text)) is not None and parsed[0] in fields}
    if not parsed:
        return {}
    ordered = quantity_evidence._ordered_review_sources(goal, available, replies, guidance)
    if not ordered:
        raise ValueError('無法核對附加欄位正式確認的保存順序。')
    result = {}
    for citation, (text, sha, _) in ordered.items():
        if citation in parsed:
            field, value = parsed[citation]
            result[field] = {'citation': citation, 'quote': text, 'source_sha256': sha, 'value': value}
    return result


def observe(goal, steps, replies, guidance):
    declarations = declared(goal['objective'], goal['sources'])
    if not declarations:
        return []
    confirmations = _confirmations(goal, steps, replies, guidance, {row['field'] for row in declarations})
    results = []
    for declaration in declarations:
        field, ranges = declaration['field'], declaration['ranges']
        positions, found = source_decision.gather_lines(ranges, steps, goal['sources'])
        records, missing = [], []
        for did, line in positions:
            if (did, line) not in found:
                missing.append({'document_id': did, 'line': line})
                continue
            sequence, sha, text = found[did, line]
            parsed = _literal(_RECORD, text)
            record = {'value': parsed[1]} if parsed is not None and parsed[0] == field else None
            if record is None and declaration.get('natural_assignment'):
                record = _natural_record(field, text)
            if record is not None:
                records.append({'citation': f'[D{did}:L{line}]', 'step': sequence, 'source_sha256': sha,
                                'quote': text, **record})
        current = confirmations.get(field)
        values = {row['value'] for row in records}
        value = None
        if current is not None:
            value = current['value']
            status = 'confirmed' if value is not None else 'user_pending'
        elif missing:
            status = 'incomplete'
        elif len(values) > 1:
            status = 'conflicting'
        elif values and None not in values:
            value, = values
            status = 'confirmed'
        else:
            status = 'unconfirmed'
        results.append({**declaration, 'status': status, 'value': value, 'records': records,
                        'missing': missing, 'confirmation': current})
    return results
