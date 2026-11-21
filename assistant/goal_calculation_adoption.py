"""Resolve explicit single-line document selections in checked ordinary goals."""
import re

from . import operand_evidence
from .goal_guidance_sources import standalone_clauses


_CITATION = r'\[D(?P<document>[1-9][0-9]{0,11}):L[1-9][0-9]{0,11}\]'
_LINE = r'\[D[1-9][0-9]{0,11}:L[1-9][0-9]{0,11}\]'
_REF_LIST = rf'{_LINE}(?:\s*(?:或|、)\s*{_LINE}){{0,3}}'
_SCOPE = r'(?:本次|這次|本輪|本批|這批|目前)'
_NAME = r'[^\s「」『』“”\[\]：:；;、，,。！？?!]{1,60}'
_QUOTED_NAME = r'[^「」『』“”\[\]：:；;、，,。！？?!\r\n]{1,80}'
_FIELD = rf'(?:(?:欄位)?「(?P<quoted>{_QUOTED_NAME})」|(?P<bare>{_NAME}?))'
_DESCRIPTION = r'[^「」『』“”\[\]：:；;、，,。！？?!\r\n]{1,80}'
_USE = r'(?:正式)?(?:改採|改用|採用|就用|就照|還沒決定用|尚未決定用|未定用|撤回採用|取消採用|不再採用)'
_KEEP = r'(?:繼續沿用|仍沿用|照用|沿用|維持)'
_VIA = re.compile(rf'(?:(?:{_FIELD})\s*)?以\s*(?:(?P<description>{_DESCRIPTION})\s*)?'
                  rf'(?P<refs>{_REF_LIST})\s*為準')
_CHOICE = re.compile(rf'(?:{_FIELD}\s*)?(?P<verb>{_USE}|{_KEEP})\s*'
                     rf'(?:(?P<description>{_DESCRIPTION})\s*)?(?P<refs>{_REF_LIST})'
                     r'\s*(?:為準|這一行|這行|那一行|那行|這筆|那筆)?')
_LABEL = re.compile(rf'{_FIELD}\s*[：:]\s*\S')
_ASSIGNMENT = re.compile(rf'{_FIELD}\s*(?:已確認為|確認為|為)\s*\S')
_RATE = re.compile(rf'(?P<label>每{_NAME})\s+-?[0-9０-９]')
_REPLY_ASSIGNMENT = re.compile(rf'{_FIELD}\s*(?:正式改為|已確認為|確認為|[：:])\s*(?P<value>\S.*)')
_REPLY_UNRESOLVED = re.compile(r'不要|不選|不確定|不改|撤回|取消|沒說|僅|只是|但|反而|除非|並且|或是')
_COMMA = re.compile(r'(?![0-9])')
_NONOPERATIVE = re.compile(r'如果|假如|假設|僅作示例|試算|暫定|暫估|暫用|引述|轉述|是否|能否|先別|別用|不採|不用|未採用')
_QUOTED_REFERENCE = re.compile(rf'「[^」]*{_LINE}[^」]*」|『[^』]*{_LINE}[^』]*』|“[^”]*{_LINE}[^”]*”|'
                               rf'"[^"]*{_LINE}[^"]*"|`[^`]*{_LINE}[^`]*`')
_SOURCE_WORDS = {'新版', '新版本', '最新', '新資料', '這一行', '這行', '該行', '原行', '該版', '最新資料'}
_UNKNOWN_WORDS = ('還沒決定', '尚未決定', '未定', '撤回', '取消', '不再採用')
_RETENTION = frozenset({'繼續沿用', '仍沿用', '照用', '沿用', '維持'})


def clauses(text):
    return standalone_clauses(text, comma_before=_COMMA)


def reply_assignments(text, choice_offset, choice_field, valid_clause):
    result, fields, complete = [], set(), True
    for offset, clause, question in clauses(text):
        if offset == choice_offset:
            continue
        match = _REPLY_ASSIGNMENT.fullmatch(clause)
        if (question or not match or not valid_clause(text, offset, clause)
                or _NONOPERATIVE.search(clause) or _REPLY_UNRESOLVED.search(clause)
                or re.search(_LINE, clause)):
            complete = False
            continue
        field = match['quoted'] or match['bare']
        if field == choice_field:
            return None
        if field in fields or choice_field in match['value']:
            complete = False
        fields.add(field)
        result.append({'field': field, 'quote': clause, 'quote_start': offset})
    return result if complete else []


def _selection(clause, *, scoped_fields=False):
    source = re.sub(rf'^{_SCOPE}(?=\s*(?:以|{_USE}|{_KEEP}))\s*', '', clause)
    subject = None
    if scoped_fields and (scope := operand_evidence.SCOPED_FIELD.match(source)):
        subject = scope['subject']
        source = '「' + scope['field'] + '」' + source[scope.end():]
    match = _VIA.fullmatch(source) or _CHOICE.fullmatch(source)
    if not match:
        return None
    verb = match.groupdict().get('verb') or ''
    if _NONOPERATIVE.search(source):
        return None
    field = match['quoted'] or match['bare']
    if match['quoted'] is None and field == '正式':
        field = None
    if field and (re.match(r'^(?:不|別|先別|未|舊|歷史)', field) or field in {'若', '先', '先暫', '只可'}):
        return None
    refs = tuple(dict.fromkeys(re.findall(_LINE, match['refs'])))
    return {'field': field, 'quoted': match['quoted'] is not None, 'refs': refs,
            'description': match['description'], 'retain': verb in _RETENTION,
            'undecided': any(word in verb for word in _UNKNOWN_WORDS),
            **({'subject': subject} if subject else {})}


def literal_fields(text, valid_clause, *, scoped_fields=False):
    found = []
    for start, clause, question in clauses(text):
        if question:
            continue
        if match := _LABEL.match(clause) or _ASSIGNMENT.match(clause):
            field = (match['quoted'] or match['bare']).strip()
        elif match := _RATE.match(clause):
            field = match['label']
        elif (scoped_fields and (match := operand_evidence.SCOPED_FIELD.match(clause))
              and re.match(r'\s*(?:為|[：:])\s*\S', clause[match.end():])):
            field = match['field']
        else:
            continue
        found.append((field, clause, start, valid_clause(text, start, clause)))
    return found


def _literal_field(text, valid_clause, requested=None, *, scoped_fields=False, subject=None):
    found = literal_fields(text, valid_clause, scoped_fields=scoped_fields)
    if subject is not None:
        found = [record for record in found
                 if (scope := operand_evidence.SCOPED_FIELD.match(record[1])) is None or scope['subject'] == subject]
    if requested:
        targeted = [record for record in found if record[0] == requested]
        if targeted:
            found = targeted
    return found[0] if len(found) == 1 else None


def field_value(text, valid_clause, field):
    record = _literal_field(text, valid_clause, field)
    if record and record[0] == field and record[3]:
        quote, offset = record[1:3]
        match = _REPLY_ASSIGNMENT.fullmatch(quote)
        if match and (match['quoted'] or match['bare']) == field:
            return {'value': match['value'].strip(), 'quote': quote, 'quote_start': offset}
    return None


def _description_conflicts(description, filename):
    if not description:
        return False
    name = description.strip().removesuffix('的').strip()
    return name not in _SOURCE_WORDS and name not in {filename, filename.rsplit('.', 1)[0]}


def _resolve(selection, versions, literal, valid_clause, *, multi_fields, scoped_fields=False):
    candidates, unknown = [], False
    for reference in selection['refs']:
        document = int(re.fullmatch(_CITATION, reference)['document'])
        attached = versions.get(document)
        info = {'citation': reference}
        candidates.append(info)
        if attached is None or _description_conflicts(selection['description'], attached['name']):
            info['status'] = 'outside_scope' if attached is None else 'document_name_mismatch'
            unknown = True
            continue
        try:
            text, sha = literal(reference)
        except ValueError:
            info['status'] = 'unread'
            unknown = True
            continue
        found = _literal_field(text, valid_clause, selection['field'] if multi_fields else None,
                               scoped_fields=scoped_fields, subject=selection.get('subject'))
        if not found:
            info['status'] = 'field_unconfirmed'
            unknown = True
            continue
        field, quote, start, confirmed = found
        info.update(field=field, quote=quote, quote_start=start, source_sha256=sha,
                    status='available' if confirmed else 'field_unconfirmed')
        unknown |= not confirmed
        if scoped_fields and (scope := operand_evidence.SCOPED_FIELD.match(quote)):
            lo, hi = scope.span('subject')
            info['field_scope'] = {'name': scope['subject'], 'start': start + lo, 'end': start + hi}

    field = selection['field']
    read_fields = {row['field'] for row in candidates if 'field' in row}
    if selection['retain'] and not selection['quoted'] and field and read_fields and field not in read_fields:
        # A colloquial reminder about another field must not expand this selection.
        return None
    if field is None and len(read_fields) == 1:
        field = next(iter(read_fields))
    status = 'adopted'
    if not field or unknown:
        status = 'pending'
    elif read_fields != {field}:
        status = 'field_mismatch'
    elif selection['undecided'] or len(candidates) != 1:
        status = 'competing'
    subject = selection.get('subject')
    if scoped_fields and subject is None and candidates:
        scopes = {row.get('field_scope', {}).get('name') for row in candidates}
        if len(scopes) == 1:
            subject = next(iter(scopes))
    return {'field': field, 'status': status, 'candidates': candidates,
            **({'subject': subject} if subject else {})}


def _adopt_named(entry, versions, literal, valid_clause, multi_fields, scoped_fields=False):
    if entry is not None and (refs := entry.pop('references', None)):
        selection = {'refs': refs, 'field': entry['field'], 'description': None, 'retain': False, 'undecided': False}
        entry.update(_resolve(selection, versions, literal, valid_clause, multi_fields=multi_fields,
                              scoped_fields=scoped_fields))


class Review:
    def __init__(self, order, user, scope, literal, valid_clause, *, names=None, multi_fields=False,
                 scoped_fields=False):
        self.scoped_fields = scoped_fields
        versions = {row['document_id']: row for row in scope}
        self.notes = []
        for position, citation, text in order:
            records, direct, altered = [], [], False
            if names is None and not re.search(_LINE, text):
                self.notes.append((position, citation, [], (), text))
                continue
            for offset, clause, question in clauses(text):
                prefix = re.split(r'[。；;!?！？\r\n]', text[:offset])[-1]
                inert = re.search(_LINE, clause) and (
                    question or _QUOTED_REFERENCE.search(clause) or _NONOPERATIVE.search(clause)
                    or _NONOPERATIVE.search(prefix) or prefix.strip().startswith(('若', '倘若', '要是')))
                if inert:
                    altered = True
                    continue
                selected = _selection(clause, scoped_fields=scoped_fields) if not question else None
                if selected is not None:
                    entry = _resolve(selected, versions, literal, valid_clause, multi_fields=multi_fields,
                                     scoped_fields=scoped_fields)
                elif names is not None and not question and not re.search(_LINE, clause):
                    entry = names.resolve(citation, clause, offset, inert=bool(_NONOPERATIVE.search(prefix)))
                    _adopt_named(entry, versions, literal, valid_clause, multi_fields, scoped_fields)
                else:
                    entry = None
                if selected is not None or entry is not None:
                    altered = True
                elif not question:
                    direct.append(clause)
                if entry is not None:
                    records.append({**entry, 'user_citation': citation, 'user_sha256': user[citation][1],
                                    'user_quote': clause, 'user_quote_start': offset})
            if names is not None and (confirmation := names.confirmation(citation)):
                entry, offset, clause = confirmation
                field = entry['field']
                if not (any(_applies(row, field) for row in records)
                        or any(names.direct_change(item, field) for item in direct)):
                    _adopt_named(entry, versions, literal, valid_clause, multi_fields, scoped_fields)
                    records.append({**entry, 'user_citation': citation, 'user_sha256': user[citation][1],
                                    'user_quote': clause, 'user_quote_start': offset})
                    altered = True
            self.notes.append((position, citation, records, tuple(direct), None if altered else text))

    def effective(self, field, direct_change, *, note_change=None, subject=None):
        latest = None
        for position, citation, records, direct, original in self.notes:
            relevant = [row for row in records if _applies(row, field, subject)]
            if note_change is not None:
                changed = note_change(citation, field, direct if original is None else None)
            else:
                changed = (direct_change(original, field) if original is not None
                           else any(direct_change(clause, field) for clause in direct))
            if changed or relevant:
                latest = (position, citation, {'status': 'competing', 'candidates': relevant} if relevant and changed
                          else _combine(relevant) if relevant else None)
        return latest

    def preview(self, direct_change, *, note_change=None):
        fields = {row['field'] for _, _, records, _, _ in self.notes for row in records}
        fields.update(field for _, _, records, _, _ in self.notes for row in records
                      for field in row.get('field_candidates', ()))
        fields.update(item['field'] for _, _, records, _, _ in self.notes
                      for row in records if row['field'] is None for item in row['candidates'] if 'field' in item)
        identities = {(field, None) for field in fields}
        if self.scoped_fields:
            identities = {(field, row.get('subject')) for field in fields
                          for _, _, records, _, _ in self.notes for row in records if _applies(row, field)}
        result = []
        for field, subject in sorted(identities, key=lambda pair: (pair[0] is None, pair[0] or '', pair[1] or '')):
            def scoped_change(citation, label, direct):
                return note_change(citation, label, direct, subject)

            active = (self.effective(field, direct_change, subject=subject,
                                     note_change=scoped_change if note_change else None) if field else next(
                ((pos, citation, _combine([row for row in records if row['field'] is None]))
                 for pos, citation, records, _, _ in reversed(self.notes)
                 if any(row['field'] is None for row in records)), None))
            if active and active[2] is not None:
                _, citation, row = active
                if (row.get('field') is None and field is not None and row['status'] == 'pending'
                        and not _applies(row, field)):
                    continue
                entry = {**row, 'field': field, 'user_citation': citation,
                         **({'subject': subject} if subject else {})}
                if entry not in result:
                    result.append(entry)
        return result


def _applies(row, field, subject=None):
    if subject is not None and row.get('subject') not in (None, subject):
        return False
    if row['field'] is not None:
        return row['field'] == field
    if 'field_candidates' in row:
        return field in row['field_candidates']
    candidates = row['candidates']
    return any('field' not in item or item['field'] == field for item in candidates)


def _combine(records):
    if len(records) == 1:
        return records[0]
    key = lambda row: (row['status'], row.get('subject'), tuple(item['citation'] for item in row['candidates']))
    if all(key(row) == key(records[0]) for row in records):
        return records[-1]
    return {'status': 'competing', 'candidates': records}
