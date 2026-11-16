"""Bind a short source-choice reply to its saved question and document evidence."""
import re
import unicodedata

from . import document_ranges, goal_calculation_order, goal_sources


_LINE = r'\[D[1-9][0-9]{0,11}:L[1-9][0-9]{0,11}\]'
_LINE_ID = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_ALIAS = re.compile(rf'(?<!欄位)「(?P<name>[^「」\r\n]{{1,80}})」\s*(?P<ref>{_LINE})')
_FIELD = re.compile(r'欄位「(?P<field>[^「」\r\n]{1,80})」\s*')
_VERB = re.compile(r'^(?:這次|本次|本批|本輪)?\s*(?:就用|就照|改用|改採|採用|選用|選|用)\s*')
_TAIL = re.compile(r'\s*(?:的)?(?:那一份|這一份|那份|這份|那一版|這一版|那版|這版|那筆|這筆)$')
_CHOICE = re.compile(r'哪(?:一)?(?:份|筆|版|個來源|個版本|個文件)|哪份|哪一個|還是|要(?:改用|採用)|(?:採用|改用|就用).{0,100}嗎$')
_SOURCE = re.compile(r'採用|改用|採哪|改採|以哪|要用|選|還是')
_HYPOTHESIS = re.compile(r'先別|別用|不用|不選|不採|若|倘若|如果|假設|試算|暫定|未定|還沒|候選|可能|是否|或')
_DEICTIC = {'剛才', '方才', '剛剛', '前面', '上面', '上一個', '那個', '那份', '這份'}
_ORDINAL = re.compile(r'第[一二三四五六七八1-8](?:個|筆|份|版)?')
_BAD_TEXT = re.compile(r'[\x00-\x1f\[\]「」『』“”"\'、，,！？?!。；;]')
_PENDING = 'pending'
_AMOUNT = re.compile(r'[-+０-９0-9]+(?:[.．][0-9０-９]+)?\s*(?:袋|匣|份|項|瓶|箱|件|人|趟|克|公斤|g|kg)$')


def _key(text):
    return unicodedata.normalize('NFC', text).strip().casefold()


def _parts(clause, has_question):
    named = _FIELD.match(clause)
    field = named['field'] if named else None
    text = clause[named.end():].strip() if named else clause
    if _HYPOTHESIS.search(text) or (not has_question and not field):
        return None
    verb = _VERB.match(text)
    text = text[verb.end():].strip() if verb else text
    if not text or (not verb and not has_question):
        return None
    name = text if text in _DEICTIC else _TAIL.sub('', text)
    if name.startswith('「') and name.endswith('」'):
        name = name[1:-1]
    if not 1 <= len(name) <= 80 or _BAD_TEXT.search(name) or _AMOUNT.fullmatch(name):
        return None
    return field, name, bool(verb)


def _question(reply, clauses):
    if reply is None:
        return None
    question = reply['question']
    prompts = [text for _, text, is_question in clauses(question) if is_question]
    choices = [text for text in prompts if _CHOICE.search(text) and (_SOURCE.search(text) or _references(text))]
    if len(choices) != 1 or any(text != choices[0] and not (
            len(_references(text)) >= 2 and '或' in text) for text in prompts):
        return None
    return question


def _references(text):
    return tuple(dict.fromkeys(ref[0] for ref in _LINE_ID.finditer(text)))


def _complete_lookup(steps, document, field, count, literal, originals):
    """Reuse the exhaustive first-page search when its exact hits are present."""
    did, sha = document['document_id'], document['sha256']
    for step in reversed(steps):
        if step['action']['tool'] != 'find_document_text':
            continue
        obs = step['observation']
        for source in obs.get('sources', [obs]):
            if (source.get('document_id'), source.get('sha256'), source.get('searched_line_count')) != (did, sha, count):
                continue
            pages = source.get('queries')
            if pages is None:
                query = step['action']['args'].get('query')
                pages = [{**source, 'query': query}]
            for page in pages:
                hits = page.get('matches')
                if (page.get('query') != field or page.get('start_line') != 1 or
                        page.get('next_start_line') is not None or not isinstance(hits, list)
                        or type(page.get('total_matching_lines')) is not int
                        or len(hits) != page['total_matching_lines'] or any(not isinstance(hit, dict) for hit in hits)):
                    continue
                numbers = [hit.get('line_number') for hit in hits]
                if (any(type(number) is not int or not 1 <= number <= count for number in numbers)
                        or sorted(set(numbers)) != numbers):
                    raise ValueError('採用欄位的文件查找範圍不符。')
                if originals is not None and numbers != [
                        index for index, line in enumerate(originals, 1) if field in line]:
                    raise ValueError('採用欄位的查找命中與匯入副本不符。')
                try:
                    checked = [(number, literal(f'[D{did}:L{number}]')[0]) for number in numbers]
                except ValueError:
                    return None
                if any(field not in line for _, line in checked):
                    raise ValueError('採用欄位的查找命中缺少原文字面。')
                return checked
    return None


class Names:
    def __init__(self, steps, scope, replies, excerpts, documents, counts, literal, field_at, clauses, *,
                 ordinal_guidance=None, direct_change=None):
        self.steps = steps
        self.scope = scope
        self.history = goal_sources.verify_history(steps, scope, {}) if goal_sources.has_supplement(steps) else {}
        self.replies = {row['citation']: row for row in replies if row['ask_sequence'] <= len(steps)}
        self.excerpts = excerpts
        self.documents = documents
        self.counts = {row['document_id']: row['line_count'] for row in counts}
        self.literal = literal
        self.field_at = field_at
        self.clauses = clauses
        self.ordinal_guidance = ordinal_guidance
        self.direct_change = direct_change
        self.observed = {}
        self.documents_seen = {}

    def _lines(self, document):
        did = document['document_id']
        if did not in self.documents_seen:
            lines = set()
            for row in self.excerpts:
                if row['document_id'] != did:
                    continue
                start, end = row['start_line'], row['end_line']
                if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= self.counts.get(did, 0)
                        or end - start + 1 > document_ranges.MAX_LINES):
                    raise ValueError('已讀原行與保存的文件範圍不符。')
                lines.update(range(start, end + 1))
            self.documents_seen[did] = lines
        return self.documents_seen[did]

    def _fields(self, document, lines):
        did = document['document_id']
        result = []
        for number in sorted(lines):
            ref = f'[D{did}:L{number}]'
            if ref not in self.observed:
                text, _ = self.literal(ref)
                self.observed[ref] = self.field_at(text)
            record = self.observed[ref]
            if record:
                field, *_ = record
                result.append((field, ref))
        return result

    def _possibilities(self, scope, refs):
        if refs:
            dids = {int(_LINE_ID.fullmatch(ref)[1]) for ref in refs}
            scope = [row for row in scope if row['document_id'] in dids]
        return scope

    def _known_fields(self, scope, refs):
        checked = {}
        for doc in scope:
            did = doc['document_id']
            wanted = {int(m[2]) for ref in refs if (m := _LINE_ID.fullmatch(ref)) and int(m[1]) == did}
            seen = self._lines(doc)
            checked[did] = {field for field, _ in self._fields(doc, seen & wanted if refs else seen)}
        return checked

    def _pick(self, text, scope, refs, ids):
        known = self._known_fields(scope, refs)
        labels = set().union(*known.values()) if known else set()
        labels = {label for label in labels if label in text}
        labels = {label for label in labels if not any(label != other and label in other for other in labels)}
        if len(labels) == 1:
            return next(iter(labels)), labels
        if labels:
            return None, labels
        if len(refs) == 1 and len(scope) == 1 and len(known[scope[0]['document_id']]) == 1:
            only = known[scope[0]['document_id']]
            return next(iter(only)), only
        if len(scope) == 2 and (refs or all(len(self._lines(doc)) == self.counts.get(doc['document_id'])
                                          for doc in scope)):
            shared = set.intersection(*known.values())
            if len(shared) == 1:
                return next(iter(shared)), shared
            return None, shared
        selected = set().union(*(known.get(did, set()) for did in ids)) if ids else set()
        return None, selected

    def _resolve_ordinal(self, reply, field, name, selection):
        question = reply['question']
        options = goal_calculation_order.options(question, _LINE, self.clauses)
        if not options and not goal_calculation_order.source_list(question, _LINE):
            return None

        refs = tuple(row['citation'] for row in options) if options else _references(question)
        sequence = reply['ask_sequence']
        scope = self.history.get(sequence, self.scope)
        documents = self._possibilities(scope, refs)
        prompt = goal_calculation_order.source_prompt(question, self.clauses)
        hint = _FIELD.search(prompt)
        field = field or (hint['field'] if hint else None)
        if field:
            possible = {field}
        else:
            field, possible = self._pick(prompt, documents, refs, ())
        if not field and not possible and refs:
            known = self._known_fields(documents, refs)
            possible.update(label for fields in known.values() for label in fields)
        mode, number = selection
        position = len(options) if mode == 'last' else number
        basis = {'name': name, 'question': question, 'ask_sequence': sequence,
                 'question_references': list(refs),
                 'candidate_documents': [doc['document_id'] for doc in documents], 'selected_documents': [],
                 'ordinal': {'mode': mode, 'position': position, 'options': options}}
        result = {'field': field, 'status': _PENDING, 'candidates': [], 'choices': list(refs),
                  'field_candidates': sorted(possible), 'spoken_selection': basis}
        if not options or not 1 <= position <= len(options):
            result['reason'] = 'choice_order_unresolved'
            return result

        for source in reversed(self.ordinal_guidance):
            if source['steps_seen'] < sequence or source['notes_start'] >= reply['notes_start']:
                continue
            text = source['content']
            if (goal_calculation_order.reordered(text, self.clauses)
                    or field and self.direct_change(text, field)):
                result.update(reason='choice_order_changed', changed_by=source['citation'])
                return result

        indexed = {}
        for doc in documents:
            did = doc['document_id']
            wanted = {int(match[2]) for ref in refs if (match := _LINE_ID.fullmatch(ref)) and int(match[1]) == did}
            indexed.update((ref, label) for label, ref in self._fields(doc, self._lines(doc) & wanted))
        if set(indexed) != set(refs) or len(set(indexed.values())) != 1 or indexed[refs[0]] != field:
            result['field_candidates'] = sorted(possible | set(indexed.values()))
            result['reason'] = 'choice_field_unresolved'
            return result

        chosen = refs[position - 1]
        basis['selected_documents'] = [int(_LINE_ID.fullmatch(chosen)[1])]
        result['references'] = (chosen,)
        return result

    def resolve(self, citation, clause):
        reply = self.replies.get(citation)
        if reply is not None and self.ordinal_guidance is not None and (parsed := _parts(clause, True)):
            field, name, _ = parsed
            if selection := goal_calculation_order.parse(name):
                return self._resolve_ordinal(reply, field, name, selection)
        question = _question(reply, self.clauses)
        parsed = _parts(clause, question is not None)
        if not parsed:
            return None
        field, name, had_verb = parsed
        if question is None and field is None:
            return None
        annotated_question = question or ''
        refs = _references(annotated_question)
        scope = self.history.get(reply['ask_sequence'], self.scope) if reply else self.scope
        candidates = self._possibilities(scope, refs)
        aliases = tuple(dict.fromkeys(match['ref'] for match in _ALIAS.finditer(annotated_question)
                                     if _key(match['name']) == _key(name)))
        selected_refs = refs if name in _DEICTIC and len(refs) == 1 else aliases
        ids = {int(_LINE_ID.fullmatch(ref)[1]) for ref in selected_refs}
        if name not in _DEICTIC and not _ORDINAL.fullmatch(name):
            ids.update(doc['document_id'] for doc in candidates
                       if _key(name) in {_key(doc['name']), _key(doc['name'].rsplit('.', 1)[0])})
        if not had_verb and not ids:
            return None
        if refs and ids and not selected_refs:
            selected_refs = tuple(ref for ref in refs if int(_LINE_ID.fullmatch(ref)[1]) in ids)
        if field is None:
            hint = _FIELD.search(annotated_question)
            field = hint['field'] if hint else None
        if field is None:
            field, possible = self._pick(annotated_question, candidates, refs, ids)
        else:
            possible = {field}

        basis = {'name': name,
                 'question': annotated_question or None,
                 'ask_sequence': reply['ask_sequence'] if question else None,
                 'question_references': list(refs),
                 'candidate_documents': [doc['document_id'] for doc in candidates],
                 'selected_documents': sorted(ids)}
        result = {'field': field, 'status': _PENDING, 'candidates': [],
                  'field_candidates': sorted(possible), 'spoken_selection': basis}
        if not field or len(ids) != 1 or len(selected_refs) > 1:
            choices = selected_refs or refs
            if choices:
                result['choices'] = list(choices)
            return result
        did = next(iter(ids))
        document = next((doc for doc in candidates if doc['document_id'] == did), None)
        if document is None:
            return result
        if selected_refs:
            result['references'] = selected_refs
            return result
        count = self.counts.get(did)
        seen = self._lines(document)
        if count is not None and len(seen) == count and (not seen or min(seen) == 1 and max(seen) == count):
            hits = self._fields(document, seen)
        else:
            originals = self.documents.get((did, document['name'], document['sha256']))
            checked = _complete_lookup(self.steps, document, field, count, self.literal, originals)
            if checked is None:
                result['needs_lookup'] = {'document_id': did, 'query': field, 'start_line': 1}
                return result
            hits = self._fields(document, (number for number, _ in checked))
        matches = [ref for label, ref in hits if label == field]
        if len(matches) == 1:
            result['references'] = tuple(matches)
        elif matches:
            result['status'] = 'competing'
            result['choices'] = matches
            result['candidates'] = [{'citation': ref, 'field': field, 'status': 'same_field'} for ref in matches]
        return result
