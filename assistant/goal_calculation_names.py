"""Bind a short source-choice reply to its saved question and document evidence."""
import re
import unicodedata

from . import (document_ranges, goal_calculation_confirmation, goal_calculation_lookup,
               goal_calculation_order, goal_sources, operand_evidence)


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
_VALUE_VERB = re.compile(r'^(?:就用|採用|選用|選擇)\s*')
_VALUE_NEGATED = re.compile(r'^(?:不要(?:用|選)?|不用|先別用|不採(?:用)?|不選|不是)\s*')
_VALUE_QUOTE_PAIRS = {'「': '」', '『': '』', '“': '”', '"': '"', "'": "'", '`': '`'}
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
    def __init__(self, steps, scope, replies, excerpts, documents, counts, literal, field_labels, clauses, *,
                 ordinal_guidance=None, direct_change=None, named_ordinals=False, paged_lookup=False,
                 line_pages=None, field_quantity=None, reply_assignments=None, confirm_single_line=False,
                 confirm_approval=False, confirm_repeated_waits=False, confirm_other_approvals=False,
                 confirm_approval_groups=False, field_text=None):
        self.steps = steps
        self.scope = scope
        self.history = goal_sources.verify_history(steps, scope, {}) if goal_sources.has_supplement(steps) else {}
        self.replies = {row['citation']: row for row in replies if row['ask_sequence'] <= len(steps)}
        self.excerpts = excerpts
        self.documents = documents
        self.counts = {row['document_id']: row['line_count'] for row in counts}
        self.literal = literal
        self.field_labels = field_labels
        self.clauses = clauses
        self.ordinal_guidance = ordinal_guidance
        self.direct_change = direct_change
        self.named_ordinals = named_ordinals
        self.paged_lookup = paged_lookup
        self.line_pages = line_pages
        self.field_quantity = field_quantity
        self.reply_assignments = reply_assignments
        self.confirm_single_line = confirm_single_line
        self.confirm_approval = confirm_approval
        self.confirm_repeated_waits = confirm_repeated_waits
        self.confirm_other_approvals = confirm_other_approvals
        self.confirm_approval_groups = confirm_approval_groups
        self.field_text = field_text
        self.field_lookups = {}
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
                self.observed[ref] = self.field_labels(text)
            result.extend((field, ref) for field in self.observed[ref])
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

    def _choice_guidance(self, reply, since=None):
        boundary = reply['ask_sequence'] if since is None else since
        return (row for row in reversed(self.ordinal_guidance)
                if row['steps_seen'] >= boundary and row['notes_start'] < reply['notes_start'])

    def _changed_order(self, reply, field, since=None):
        for source in self._choice_guidance(reply, since):
            text = source['content']
            if (goal_calculation_order.reordered(text, self.clauses)
                    or field and self.direct_change(text, field)):
                return source['citation']
        return None

    def _resolve_ordinal(self, reply, field, name, selection, *, permit=True):
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
        if not permit or not options or not 1 <= position <= len(options):
            result['reason'] = 'choice_order_unresolved'
            return result

        if change := self._changed_order(reply, field):
            result.update(reason='choice_order_changed', changed_by=change)
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

    def _exact_title_ids(self, name, scope):
        if name is None:
            return set()
        return {doc['document_id'] for doc in scope
                if _key(name) in {_key(doc['name']), _key(doc['name'].rsplit('.', 1)[0])}}

    def _field_lines(self, document, field):
        did = document['document_id']
        count = self.counts.get(did)
        seen = self._lines(document)
        if count is not None and len(seen) == count:
            return {'matches': [ref for label, ref in self._fields(document, seen) if label == field]}
        key = (did, field)
        if key not in self.field_lookups:
            self.field_lookups[key] = self._inspect_lookup(document, field, count, seen)
        return self.field_lookups[key]

    def _inspect_lookup(self, document, field, count, seen):
        did = document['document_id']
        originals = self.documents.get((did, document['name'], document['sha256']))
        first = {'document_id': did, 'query': field, 'start_line': 1}
        if self.paged_lookup:
            covered = goal_calculation_lookup.coverage(
                self.steps, document, field, count, originals=originals, seen=seen, literal=self.literal)
            if 'next_page' in covered:
                return {'needs_lookup': {**first, 'start_line': covered['next_page']}}
            if 'missing_lines' in covered:
                if self.line_pages is not None:
                    return self.line_pages.next_read(did, covered['missing_lines'], covered['line_chars'])
                return {'needs_read': {'ranges': [
                    {'document_id': did, 'start_line': number, 'end_line': number}
                    for number in covered['missing_lines'][:document_ranges.MAX_RANGES]]}}
            matches = [ref for label, ref in self._fields(document, covered['numbers']) if label == field]
            return {'matches': matches, 'lookup_evidence': covered['lookup_evidence']}

        checked = _complete_lookup(self.steps, document, field, count, self.literal, originals)
        if checked is None:
            return {'needs_lookup': first}
        return {'matches': [ref for label, ref in self._fields(document, (n for n, _ in checked)) if label == field]}

    def _displayed_candidates(self, options, scope, question):
        documents = {doc['document_id']: doc for doc in scope}
        # Only separate, earlier bindings may supply citations to an option by alias.
        preface = question[:options[0]['question_start']]
        aliases = {}
        for match in _ALIAS.finditer(preface):
            aliases.setdefault(_key(match['name']), set()).add(match['ref'])
        rows = []
        for option in options:
            name = option['name']
            ref = option['displayed_citation']
            alias_refs = aliases.get(_key(name), set()) if name else set()
            title_ids = self._exact_title_ids(name, scope)
            if ref:
                did = int(_LINE_ID.fullmatch(ref)[1])
                status = 'reference_conflict' if (
                    title_ids and did not in title_ids or alias_refs and alias_refs != {ref}) else None
            elif alias_refs:
                ref = next(iter(alias_refs)) if len(alias_refs) == 1 else None
                did = int(_LINE_ID.fullmatch(ref)[1]) if ref else None
                status = ('reference_conflict' if not ref or title_ids and did not in title_ids else None)
            else:
                did = next(iter(title_ids)) if len(title_ids) == 1 else None
                status = None if did is not None else 'ambiguous_title' if title_ids else 'unknown_title'
            if did is not None and did not in documents:
                status = 'outside_scope'
            row = {**option, 'citation': ref, 'document_id': did, 'status': status}
            if title_ids and status in ('ambiguous_title', 'reference_conflict'):
                row['matching_document_ids'] = sorted(title_ids)
            rows.append(row)
        return rows, documents

    def _displayed_field(self, rows, documents, prompt, declared):
        known, complete = [], True
        for row in rows:
            doc = documents.get(row['document_id'])
            if doc is None or row['status']:
                complete = False
                continue
            seen = self._lines(doc)
            if row['citation']:
                line = int(_LINE_ID.fullmatch(row['citation'])[2])
                numbers = seen & {line}
                complete &= bool(numbers)
            else:
                numbers = seen
                complete &= len(seen) == self.counts.get(doc['document_id'])
            known.append({field for field, _ in self._fields(doc, numbers)})
        fields = set().union(*known) if known else set()
        mentioned = {field for field in fields if field in prompt}
        mentioned = {field for field in mentioned if not any(field != other and field in other for other in mentioned)}
        if declared:
            return declared, {declared}
        if len(mentioned) == 1:
            return next(iter(mentioned)), mentioned
        if mentioned:
            return None, mentioned
        if complete and known:
            common = set.intersection(*known)
            if len(common) == 1:
                return next(iter(common)), common
        return None, fields

    def _fill_displayed_lines(self, rows, documents, field):
        missing = []
        for row in rows:
            if row['status']:
                continue
            doc = documents[row['document_id']]
            ref = row['citation']
            if ref:
                number = int(_LINE_ID.fullmatch(ref)[2])
                matches = [found for label, found in self._fields(doc, self._lines(doc) & {number})
                           if label == field]
                if not matches:
                    row['status'] = 'unverified_line_or_field'
                continue
            looked_up = self._field_lines(doc, field)
            matches = looked_up.get('matches')
            if matches is None:
                instruction, = looked_up.keys()
                row['status'] = instruction
                missing.append(looked_up)
                continue
            if 'lookup_evidence' in looked_up:
                row['lookup_evidence'] = looked_up['lookup_evidence']
            if len(matches) == 1:
                row['citation'] = matches[0]
            else:
                row.update(status='competing_lines' if matches else 'field_not_found', matching_lines=matches)
        return missing

    def _matching_value(self, rows, field, reply_quantity):
        matches = []
        for position, row in enumerate(rows, 1):
            quantity = self.field_quantity(row['citation'], field)
            if not quantity:
                row['status'] = 'quantity_unverified'
                continue
            row['quantity'] = quantity
            actual = (quantity['exact'], quantity['unit'])
            shown = (operand_evidence.displayed_choice_quantities(row['name'] or '')
                     if row['displayed_citation'] else [])
            if shown:
                row['displayed_quantities'] = shown
            if any(actual != (value['exact'], value['unit']) for value in shown):
                row['status'] = 'displayed_quantity_conflict'
            elif actual == (reply_quantity['exact'], reply_quantity['unit']):
                matches.append(position)
        return matches

    def _resolve_named_choice(self, reply, claimed_field, name, selection):
        question = reply['question']
        options = goal_calculation_order.named_options(question, _LINE, self.clauses)
        mode, number = selection
        by_value = mode == 'quantity'
        if not options:
            return None if by_value else self._resolve_ordinal(reply, claimed_field, name, selection, permit=False)
        sequence = reply['ask_sequence']
        scope = self.history.get(sequence, self.scope)
        rows, documents = self._displayed_candidates(options, scope, question)
        prompt = goal_calculation_order.source_prompt(question, self.clauses)
        hint = _FIELD.search(prompt)
        field, possible = self._displayed_field(rows, documents, prompt, hint['field'] if hint else None)
        if not field and not any(label in prompt for label in possible):
            observed, _ = self._pick(prompt, scope, (), ())
            if observed and observed in prompt:
                field, possible = observed, {observed}

        position = None if by_value else len(rows) if mode == 'last' else number
        basis = {'name': name, 'question': question, 'ask_sequence': sequence,
                 'question_references': [option['displayed_citation'] for option in options if option['displayed_citation']],
                 'candidate_documents': sorted({row['document_id'] for row in rows if row['document_id'] is not None}),
                 'selected_documents': []}
        if by_value:
            basis['value_choice'] = {'reply_quantity': number, 'position': None, 'options': rows}
        else:
            basis['ordinal'] = {'mode': mode, 'position': position, 'options': rows}
        result = {'field': field, 'status': _PENDING, 'candidates': [], 'field_candidates': sorted(possible),
                  'spoken_selection': basis}
        reason = 'choice_order_unresolved'
        if by_value or 1 <= position <= len(rows):
            reason = 'choice_field_unresolved'
            if claimed_field and claimed_field != field:
                reason = 'reply_field_conflict'
            elif change := self._changed_order(reply, field):
                result['changed_by'] = change
                reason = 'choice_order_changed'
            elif field:
                needed = self._fill_displayed_lines(rows, documents, field)
                refs = [row['citation'] for row in rows]
                if all(row['status'] is None for row in rows) and len(set(refs)) == len(refs):
                    if by_value:
                        matches = self._matching_value(rows, field, number)
                        basis['value_choice']['matching_positions'] = matches
                        if any(row['status'] for row in rows):
                            reason = 'choice_quantity_unverified'
                        elif len(matches) == 1:
                            position = matches[0]
                            basis['value_choice']['position'] = position
                        else:
                            reason = 'choice_quantity_ambiguous' if matches else 'choice_quantity_no_match'
                    if position is not None:
                        chosen = rows[position - 1]
                        basis['selected_documents'] = [chosen['document_id']]
                        result['references'] = (chosen['citation'],)
                        return result
                if needed and all(row['status'] in (None, 'needs_lookup', 'needs_read', 'needs_line') for row in rows):
                    result.update(needed[0])
        result['choices'] = [row['citation'] or row['name'] for row in rows]
        result['reason'] = reason
        return result

    def _approval_proposal(self, question):
        proposal = goal_calculation_confirmation.approval_question(question, self.clauses)
        if proposal is None and self.confirm_repeated_waits:
            proposal = goal_calculation_confirmation.approval_wait(
                question, self.clauses, other_approvals=self.confirm_other_approvals)
        return proposal

    def _earlier_asks(self, sequence):
        return (step['sequence'] for step in reversed(self.steps)
                if step['sequence'] < sequence and step['action']['tool'] == 'ask')

    def _pending_notice_check(self, proposal):
        approval = proposal['approval']
        if notice := approval.get('reported_pending'):
            if any(notice[key] != approval[key] for key in ('actor', 'verb')):
                return None, 'approval_actor_or_action_changed'
            return notice['citation'], None
        if notice := approval.get('reported_other_approval'):
            if notice['actor'] == approval['actor']:
                return None, 'approval_actor_or_action_changed'
            return notice['citation'], None
        return None, None

    def _waiting_reply(self, earlier, prior, waiting_approval):
        approval = prior['approval']
        offset, quote, status, whole = goal_calculation_confirmation.approval_response(
            earlier['answer'], self.clauses, approval, repeated=True)
        companions = [] if whole else self.reply_assignments(earlier['answer'], offset, prior['field'])
        cited_other = waiting_approval.get('reported_other_approval')
        if status == 'declined' and (whole or companions):
            if cited_other:
                return None, 'approval_wait_citation_conflict'
            return {'quote': quote, 'quote_start': offset}, None
        if not self.confirm_other_approvals:
            return None, 'approval_condition_unverified'
        report = goal_calculation_confirmation.other_approval_report(
            earlier['answer'], self.clauses, approval['actor'])
        if report is None:
            return None, 'approval_wait_citation_conflict' if cited_other else 'approval_condition_unverified'
        offset, quote = report['quote_start'], report['quote']
        raw = earlier['answer'].strip()
        if raw not in (quote, quote + '。', quote + '！', quote + '!'):
            if not self.reply_assignments(earlier['answer'], offset, prior['field']):
                return None, 'approval_condition_unverified'
        if cited_other and any(cited_other[key] != report[key] for key in ('actor', 'verb')):
            return None, 'approval_wait_citation_conflict'
        if waiting_approval.get('reported_pending', {}).get('citation'):
            return None, 'approval_wait_citation_conflict'
        return {**report, 'kind': 'other_approval'}, None

    def _waiting_trace(self, proposal, earlier_asks):
        deferred = []
        expected, reason = self._pending_notice_check(proposal)
        if reason:
            return None, [], reason
        waiting_approval = proposal['approval']
        for sequence in earlier_asks:
            earlier = self.replies.get(f'[補充#{sequence}]')
            if earlier is None:
                break
            if expected and earlier['citation'] != expected:
                return None, [], 'approval_wait_citation_conflict'
            if self.direct_change(earlier['answer'], proposal['field']):
                return sequence, [], 'approval_superseded'
            if goal_calculation_confirmation.question_source(earlier['question'], self.clauses):
                if expected or waiting_approval.get('reported_other_approval'):
                    return None, [], 'approval_wait_citation_conflict'
                return sequence, deferred, None
            prior = self._approval_proposal(earlier['question'])
            if prior is None:
                break
            if any(prior[key] != proposal[key] for key in ('field', 'citation')):
                return None, [], 'approval_proposal_changed'
            approval = prior['approval']
            if any(approval[key] != proposal['approval'][key] for key in ('actor', 'verb')):
                return None, [], 'approval_actor_or_action_changed'
            next_expected, reason = self._pending_notice_check(prior)
            if reason:
                return None, [], reason
            evidence, reason = self._waiting_reply(earlier, prior, waiting_approval)
            if reason:
                return None, [], reason
            expected, waiting_approval = next_expected, approval
            deferred.append({'citation': earlier['citation'], 'sha256': earlier['sha256'], 'ask_sequence': sequence,
                             'question': earlier['question'], 'proposal': prior, **evidence})
        return None, [], 'approval_condition_unverified'

    def _verify_deferred_approvals(self, result, deferred, document):
        evidence = []
        for reply in reversed(deferred):
            prior = reply['proposal']
            scope = self.history.get(reply['ask_sequence'], self.scope)
            earlier_doc = next((row for row in scope if row['document_id'] == prior['document_id']), None)
            if earlier_doc != document:
                return 'approval_proposal_changed'
            scratch = {'spoken_selection': {'confirmation': {}}}
            if reason := self._confirm_proposal(scratch, prior, scope, earlier_doc):
                result.update({key: scratch[key] for key in ('needs_read', 'needs_line') if key in scratch})
                return reason
            evidence.append({**reply, **scratch['spoken_selection']['confirmation']})
        if evidence:
            result['spoken_selection']['confirmation']['deferred_replies'] = evidence
        return None

    def _approval_group(self, reply, proposal):
        trail = []
        sequences = [reply['ask_sequence'], *self._earlier_asks(reply['ask_sequence'])]
        for sequence in sequences:
            source = self.replies.get(f'[補充#{sequence}]')
            if source is None:
                break
            prior = (proposal if source is reply else
                     goal_calculation_confirmation.question_source(source['question'], self.clauses)
                     or self._approval_proposal(source['question']))
            if prior is None and self.field_text is not None:
                prior = goal_calculation_confirmation.group_approval_question(source['question'], self.clauses)
            if prior and prior['field'] != proposal['field']:
                break
            trail.append((source, prior))
            if prior and 'approval' not in prior:
                condition = goal_calculation_confirmation.waiting_group(source['answer'], self.clauses)
                if condition:
                    return condition, list(reversed(trail))
        return None

    def _group_source(self, reply, proposal, document, result):
        scope = self.history.get(reply['ask_sequence'], self.scope)
        original = next((row for row in scope if row['document_id'] == proposal['document_id']), None)
        if original != document:
            return {}, 'approval_proposal_changed'
        scratch = {'spoken_selection': {'confirmation': {}}}
        reason = self._confirm_proposal(scratch, proposal, scope, original)
        result.update({key: scratch[key] for key in ('needs_read', 'needs_line') if key in scratch})
        return scratch['spoken_selection']['confirmation'], reason

    def _group_reports(self, trail, condition, document, result, group):
        first, original = trail[0]
        source, reason = self._group_source(first, original, document, result)
        group['origin'].update(source)
        if reason:
            return None, reason
        outstanding = set(condition['actors'])
        last = None
        for reply, proposal in trail[1:]:
            if not proposal or proposal.get('citation') != original['citation']:
                return None, 'approval_proposal_changed'
            approval = proposal.get('approval')
            if not approval or approval['verb'] != condition['verb']:
                return None, 'approval_actor_or_action_changed'
            source, reason = self._group_source(reply, proposal, document, result)
            if reason:
                return None, reason
            for key, status in (('reported_pending', 'declined'), ('reported_other_approval', 'affirmed')):
                notice = approval.get(key)
                if notice and (last is None or last['status'] != status
                               or any(last[name] != notice[name] for name in ('actor', 'verb'))
                               or notice['citation'] not in (None, last['citation'])):
                    return None, 'approval_wait_citation_conflict'
            if notice := approval.get('reported_limited_scope'):
                if not last or last['status'] != 'limited' or any(
                        notice[key] != last[key] for key in ('actor', 'verb', 'other_field', 'citation')):
                    return None, 'approval_wait_citation_conflict'
            response = goal_calculation_confirmation.group_response(
                reply['answer'], self.clauses, approval, limited_scope=self.field_text is not None)
            if response is None:
                return None, 'confirmation_unconfirmed'
            report, whole = response
            companions = [] if whole else self.reply_assignments(reply['answer'], report['quote_start'], original['field'])
            if not whole and not companions:
                return None, 'confirmation_unconfirmed'
            if report['status'] == 'limited':
                scope = self.history.get(reply['ask_sequence'], self.scope)
                other = report['other_field']
                if other == original['field'] or not any(other in fields for fields in self._known_fields(scope, ()).values()):
                    return None, 'approval_scope_unverified'
            last = {**report, **source, 'citation': reply['citation'], 'sha256': reply['sha256'],
                    'ask_sequence': reply['ask_sequence'], 'question': reply['question'], 'proposal': proposal}
            if companions:
                last['companion_clauses'] = companions
            group['reports'].append(last)
            actor = last['actor']
            if actor in condition['actors']:
                if last['status'] != 'limited' and last['verb'] != condition['verb']:
                    return None, 'approval_actor_or_action_changed'
                if last['status'] == 'affirmed':
                    outstanding.discard(actor)
                else:
                    outstanding.add(actor)
                group['awaiting'] = [actor for actor in condition['actors'] if actor in outstanding]
        return last, None

    def _group_confirmation(self, reply, proposal, condition, trail):
        first, original = trail[0]
        boundary, field, ref = first['ask_sequence'], original['field'], original['citation']
        if (any(self.direct_change(previous['answer'], field) for previous, _ in trail[:-1])
                or any(self.direct_change(row['content'], field) for row in self._choice_guidance(reply, boundary))):
            return None
        sequence, did = reply['ask_sequence'], proposal['document_id']
        scope = self.history.get(sequence, self.scope)
        document = next((row for row in scope if row['document_id'] == did), None)
        offset = len(reply['answer']) - len(reply['answer'].lstrip())
        clause = reply['answer'].strip()
        origin = {**condition, 'citation': first['citation'], 'sha256': first['sha256'], 'ask_sequence': boundary,
                  'question': first['question'], 'proposal': original}
        group = {'origin': origin, 'reports': [], 'awaiting': list(condition['actors'])}
        proof = {**proposal, 'reply_status': 'unconfirmed', 'approval_group': group}
        basis = {'name': clause, 'question': reply['question'], 'ask_sequence': sequence,
                 'question_references': [ref], 'candidate_documents': [original['document_id']],
                 'selected_documents': [], 'confirmation': proof}
        result = {'field': field, 'status': _PENDING, 'candidates': [], 'field_candidates': [field],
                  'choices': [ref], 'spoken_selection': basis}
        changed = self._changed_order(reply, field, boundary)
        last, reason = (None, 'confirmation_question_changed') if changed else self._group_reports(
            trail, condition, document, result, group)
        if changed:
            result['changed_by'] = changed
        if last:
            offset, clause = last['quote_start'], last['quote']
            basis['name'] = clause
            proof['reply_status'] = last['status']
        elif sequence == boundary:
            offset, clause = condition['quote_start'], condition['quote']
            basis['name'] = clause
        if not reason and (not last or group['awaiting'] or last['actor'] not in condition['actors']):
            reason = 'approval_group_pending'
        if not reason:
            reason = self._confirm_proposal(result, proposal, scope, document)
        if reason:
            result['reason'] = reason
        return result, offset, clause

    def confirmation(self, citation):
        reply = self.replies.get(citation) if self.confirm_single_line else None
        if reply is None:
            return None
        proposal = goal_calculation_confirmation.question_source(reply['question'], self.clauses)
        if proposal is None and self.confirm_approval:
            proposal = self._approval_proposal(reply['question'])
        if proposal is None and self.field_text is not None:
            extended = goal_calculation_confirmation.group_approval_question(reply['question'], self.clauses)
            if extended and (group := self._approval_group(reply, extended)):
                return self._group_confirmation(reply, extended, *group)
        if proposal is None:
            return None
        if self.confirm_approval_groups and (group := self._approval_group(reply, proposal)):
            return self._group_confirmation(reply, proposal, *group)
        approval = proposal.get('approval')
        field, ref = proposal['field'], proposal['citation']
        sequence = reply['ask_sequence']
        earlier_asks = list(self._earlier_asks(sequence)) if approval else []
        previous_ask = earlier_asks[0] if earlier_asks else None
        origin, deferred, trace_error = previous_ask, [], None
        if approval and self.confirm_repeated_waits:
            origin, deferred, trace_error = self._waiting_trace(proposal, earlier_asks)
            if trace_error == 'approval_superseded':
                return None
        boundary = origin if origin is not None else previous_ask
        scope = self.history.get(sequence, self.scope)
        did, line = proposal['document_id'], proposal['line_number']
        document = next((row for row in scope if row['document_id'] == did), None)
        if approval:
            offset, clause, status, whole = goal_calculation_confirmation.approval_response(
                reply['answer'], self.clauses, approval, repeated=self.confirm_repeated_waits)
        else:
            offset, clause, status, whole = goal_calculation_confirmation.response(reply['answer'], self.clauses)
        companions = [] if whole else self.reply_assignments(reply['answer'], offset, field)
        if companions is None:
            return None
        basis = {'name': clause, 'question': reply['question'], 'ask_sequence': sequence,
                 'question_references': [ref], 'candidate_documents': [did], 'selected_documents': [],
                 'confirmation': {**proposal, 'reply_status': status}}
        if companions:
            basis['confirmation']['companion_clauses'] = companions
        result = {'field': field, 'status': _PENDING, 'candidates': [], 'field_candidates': [field],
                  'choices': [ref], 'spoken_selection': basis}
        reason = 'confirmation_unconfirmed'
        if any(self.direct_change(row['content'], field) for row in self._choice_guidance(reply, boundary)):
            return None
        if changed := self._changed_order(reply, field, boundary):
            result['changed_by'] = changed
            reason = 'confirmation_question_changed'
        elif status == 'declined':
            reason = 'confirmation_declined'
        elif status == 'affirmed' and (whole or companions):
            reason = trace_error
            if approval and not reason:
                reason = (self._approval_condition(result, proposal, origin, document)
                          or self._verify_deferred_approvals(result, deferred, document))
            if not reason:
                reason = self._confirm_proposal(result, proposal, scope, document)
        if reason:
            result['reason'] = reason
        return result, offset, clause

    def _approval_condition(self, result, proposal, earlier_ask, document):
        if not (earlier := self.replies.get(f'[補充#{earlier_ask}]')):
            return 'approval_condition_unverified'
        prior = goal_calculation_confirmation.question_source(earlier['question'], self.clauses)
        condition = goal_calculation_confirmation.waiting_condition(earlier['answer'], self.clauses)
        if not prior or not condition:
            return 'approval_condition_unverified'
        if (prior['field'], prior['citation']) != (proposal['field'], proposal['citation']):
            return 'approval_proposal_changed'
        if any(condition[key] != proposal['approval'][key] for key in ('actor', 'verb')):
            return 'approval_actor_or_action_changed'

        scope = self.history.get(earlier_ask, self.scope)
        earlier_doc = next((row for row in scope if row['document_id'] == prior['document_id']), None)
        if earlier_doc != document:
            return 'approval_proposal_changed'
        candidate = {'spoken_selection': {'confirmation': {}}}
        reason = self._confirm_proposal(candidate, prior, scope, earlier_doc)
        if reason:
            for key in ('needs_read', 'needs_line'):
                if key in candidate:
                    result[key] = candidate[key]
            return reason
        result['spoken_selection']['confirmation']['prior_condition'] = {
            **condition, 'citation': earlier['citation'], 'sha256': earlier['sha256'], 'ask_sequence': earlier_ask,
            'question': earlier['question'], 'proposal': prior,
            **candidate['spoken_selection']['confirmation']}
        return None

    def _confirm_proposal(self, result, proposal, scope, document):
        did, line, field, ref = (proposal[key] for key in ('document_id', 'line_number', 'field', 'citation'))
        title, shown_line = proposal['displayed_title'], proposal['displayed_line']
        if (document is None or line > self.counts.get(did, 0)
                or shown_line is not None and shown_line != line
                or title and did not in self._exact_title_ids(title, scope)):
            return 'confirmation_reference_conflict'
        if line not in self._lines(document):
            originals = self.documents.get((did, document['name'], document['sha256']))
            size = len(originals[line - 1]) if originals is not None else 0
            result.update(self.line_pages.next_read(did, [line], {line: size}))
            return 'confirmation_not_read'
        if (field, ref) not in self._fields(document, {line}):
            return 'confirmation_field_conflict'
        if shown := proposal['displayed_quantities']:
            quantity = self.field_quantity(ref, field)
            if not quantity or any((item['exact'], item['unit']) != (quantity['exact'], quantity['unit'])
                                   for item in shown):
                return 'confirmation_quantity_conflict'
            result['spoken_selection']['confirmation']['source_quantity'] = quantity
        if shown_text := proposal.get('displayed_value'):
            value = self.field_text(ref, field)
            if not value or value['value'] != shown_text:
                return 'confirmation_value_conflict'
            result['spoken_selection']['confirmation']['source_value'] = value
        result['references'] = (ref,)
        result['spoken_selection']['selected_documents'] = [did]
        return None

    def _compound_quantity_choice(self, reply, clause, offset, quantity, *, blocked, standalone):
        entry = self._resolve_named_choice(reply, None, clause, ('quantity', quantity))
        if not entry or not entry['field']:
            return entry
        companions = self.reply_assignments(reply['answer'], offset, entry['field'])
        if companions is None:
            return None
        basis = entry['spoken_selection']
        choice = basis['value_choice']
        if companions:
            choice['companion_clauses'] = companions
        if blocked or not (standalone or companions):
            entry.pop('references', None)
            basis['selected_documents'] = []
            choice['position'] = None
            entry['choices'] = [row['citation'] or row['name'] for row in choice['options']]
            entry['reason'] = 'choice_reply_not_confirmed'
        return entry

    def resolve(self, citation, clause, offset, *, inert=False):
        if inert and self.reply_assignments is None:
            return None
        reply = self.replies.get(citation)
        if reply is not None and self.field_quantity is not None:
            value = _VALUE_VERB.sub('', clause.removesuffix('.').rstrip())
            blocked = inert
            if self.reply_assignments is not None:
                if negated := _VALUE_NEGATED.match(value):
                    value, blocked = value[negated.end():], True
                if len(value) >= 2 and _VALUE_QUOTE_PAIRS.get(value[0]) == value[-1]:
                    value, blocked = value[1:-1].strip(), True
            if quantity := operand_evidence.choice_quantity(value):
                answer = reply['answer'].strip()
                standalone = clause in (answer, answer.removesuffix('。').rstrip())
                if standalone and not blocked:
                    return self._resolve_named_choice(reply, None, clause, ('quantity', quantity))
                if self.reply_assignments is not None:
                    return self._compound_quantity_choice(
                        reply, clause, offset, quantity, blocked=blocked, standalone=standalone)
        if inert:
            return None
        if reply is not None and self.ordinal_guidance is not None and (parsed := _parts(clause, True)):
            field, name, _ = parsed
            if selection := goal_calculation_order.parse(name):
                return (self._resolve_named_choice(reply, field, name, selection) if self.named_ordinals
                        else self._resolve_ordinal(reply, field, name, selection))
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
        looked_up = self._field_lines(document, field)
        matches = looked_up.get('matches')
        if matches is None:
            result.update(looked_up)
            return result
        if 'lookup_evidence' in looked_up:
            basis['lookup_evidence'] = looked_up['lookup_evidence']
        if len(matches) == 1:
            result['references'] = tuple(matches)
        elif matches:
            result['status'] = 'competing'
            result['choices'] = matches
            result['candidates'] = [{'citation': ref, 'field': field, 'status': 'same_field'} for ref in matches]
        return result
