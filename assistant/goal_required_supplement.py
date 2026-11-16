"""Bind explicitly selected source lines in an ordinary waiting goal."""
import json
import re

from . import (conversation_history, document_ranges, goal_guidance_sources, goal_reply_sources, goal_required_reads,
               goal_sources, goal_stages, work_plan)
from .documents import check_citations

GUIDANCE = ('required_document_supplement是使用者為原缺行明確指定的補件。先查讀replacement原行，'
            '核對它是否支持本次工作及該欄。使用實際補件行號引用，原已讀文件中仍適用的欄位繼續沿用。'
            '若紀錄有replaces，該步所指的補件已被本次replacement取代。只用目前有效的補件作為現行依據。'
            '若目前補件不適用，在暫表將該欄標為待查，引用目前補件原行說明不適用原因，只問正確的原文。'
            '舊提問及暫表可供查閱；只有仍未解決的缺口才另行提問。')

_REQUEST = re.compile(
    r'(?:(?:請|麻煩|能否|可否|可以))?(?:提供|補上|補齊|補充|貼上)'
    r'(?:□(?:[、與及和]□)*|該行|該段|這一行|這一段)'
    r'(?:從(?:第一字|頭)到(?:最後一字|尾))?(?:的)?'
    r'(?P<label>[A-Za-z0-9㐀-鿿]{1,24}?)?(?:完整)?(?:原文|全文|內容)')
_OTHER_REQUEST = re.compile(r'以及|並|另外|另問|還要|還有|再問|也請|同時|誰|哪|何時|多少|幾[位個張件份]')
_GAP = re.compile(
    r'(?:(?:目前|本次|本批)?(?:匯入副本)?(?:中)?(?:缺少|尚缺|缺漏)□'
    r'|□(?:目前|仍|尚)?(?:缺少|缺漏|未提供|未匯入))')
_ID_MISSING = re.compile(
    r'\bD(?P<doc>[1-9][0-9]{0,11})\s*的?\s*(?:匯入副本\s*)?(?:缺少|尚缺|缺漏)\s*'
    r'第\s*(?P<first>[0-9]{1,12})\s*(?:(?:至|到|[-–])\s*(?:第\s*)?(?P<last>[0-9]{1,12})\s*)?行')
_DESCRIBED_REQUEST = re.compile(
    r'(?:(?:請|麻煩|能否|可否|可以))?(?:提供|補上|補齊|補充|貼上)'
    r'(?P<label>[A-Za-z0-9㐀-鿿-]{1,110})(?:原文|全文|內容)')
_PENDING = re.compile(r'待查|待補|待提供|仍缺|尚缺|缺少|未核定|未確認|不適用')
_CITATION = re.compile(r'\[D(\d{1,12}):L(\d{1,12})(?:-L(\d{1,12})|:C\d{1,12}(?:-C\d{1,12})?)?\]')


def _question_ranges(question, sources):
    if '□' in question:
        return []
    names, pattern = goal_required_reads._named_pattern(sources, include_missing=True)
    found, requested = [], False
    for _, clause, _ in goal_guidance_sources.standalone_clauses(question, comma_before=re.compile('')):
        entries = []

        def explicit(match):
            ranges, _ = document_ranges.requested_ranges(match[0])
            entries.extend((row['document_id'], row['start_line'], row['end_line']) for row in ranges)
            return '□'

        def absent_id(match):
            entries.append((int(match['doc']), int(match['first']), int(match['last'] or match['first'])))
            return '缺少□'

        def named(match):
            entries.append(goal_required_reads._named_range(match, names))
            return '缺少□' if match['missing'] else '□'

        normalized = document_ranges.RANGES.sub(explicit, clause)
        normalized = _ID_MISSING.sub(absent_id, normalized)
        if pattern is not None:
            normalized = pattern.sub(named, normalized)
        normalized = re.sub(r'\s+', '', normalized)
        if (request := _REQUEST.fullmatch(normalized)) and not _OTHER_REQUEST.search(request['label'] or ''):
            if not entries and not found:
                return []
            requested = True
        elif not entries or not _GAP.fullmatch(normalized):
            return []
        found.extend(entries)
    return list(dict.fromkeys(found)) if requested and None not in found else []


def target(goal, step):
    action, observation = step['action'], step['observation']
    if action['tool'] != 'ask' or action['args'].keys() - {'question', 'partial_result'}:
        return []
    question = goal_reply_sources.checked_question(action, observation)
    ranges = _question_ranges(question, goal['sources'])
    targets = goal_required_reads.requirements(goal)
    wanted = {doc for doc, _, _ in targets}
    if not ranges or len({doc for doc, _, _ in ranges}) != 1:
        return []
    for did, low, high in ranges:
        if (did not in wanted or not 1 <= low <= high
                or any(goal_required_reads._missing([(a, b) for doc, a, b in targets if doc == did], low, high))):
            return []
    return ranges


def _read_range(step):
    return step['action']['args']['replacement']


def correction_targets(goal, ask):
    action, observation = ask['action'], ask['observation']
    if action['tool'] != 'ask' or action['args'].keys() - {'question', 'partial_result'}:
        return []
    question = goal_reply_sources.checked_question(action, observation)
    references = _question_ranges(question, goal['sources'])
    if not references:
        clauses = goal_guidance_sources.standalone_clauses(question)
        request = _DESCRIBED_REQUEST.fullmatch(re.sub(r'\s+', '', clauses[0][1])) if len(clauses) == 1 else None
        if not request or _OTHER_REQUEST.search(request['label']):
            return []
    partial = observation.get('partial_result', '')
    steps = goal['steps']
    latest = {}
    for step in steps:
        if step['action']['tool'] == goal_sources.REQUIRED_SUPPLEMENT:
            missing = step['action']['args']['missing']
            latest[tuple(missing[key] for key in document_ranges.BATCH_FIELDS)] = step
    required = goal_required_reads.bound_requirements(goal)
    sources = {row['document_id']: row for row in goal['sources']}
    candidates = []
    for step in latest.values():
        did, low, high = (_read_range(step)[key] for key in document_ranges.BATCH_FIELDS)
        old_scope = tuple(step['action']['args']['missing'][key] for key in document_ranges.BATCH_FIELDS)
        if references and any(
                not (doc == did and low <= first <= last <= high
                     or doc == old_scope[0] and old_scope[1] <= first <= last <= old_scope[2])
                for doc, first, last in references):
            continue
        intervals = [(start, end) for doc, start, end, binding in required
                     if doc == did and binding == step['sequence']]
        if any(goal_required_reads._missing(intervals, low, high)):
            continue
        source = sources.get(did)
        if source is None:
            continue
        excerpts = [row for prior in steps if not prior['observation'].get('error')
                    for row in prior['observation'].get('excerpts', ())
                    if (row['document_id'], row.get('name'), row.get('sha256')) == (did, source['name'], source['sha256'])]
        citation = f'[D{did}:L{low}-L{high}]'
        if not check_citations(citation, excerpts, allow_joined_lines=True)[0]['range_valid']:
            continue
        if any(_PENDING.search(row) and any(
            int(match[1]) == did and low <= int(match[2]) <= int(match[3] or match[2]) <= high
            and check_citations(match[0], excerpts, allow_joined_lines=True)[0]['range_valid']
            for match in _CITATION.finditer(row)) for row in partial.splitlines()):
            candidates.append(step)
    return candidates if len(candidates) == 1 else []


def correction_target(goal, ask, origin):
    return next((step for step in correction_targets(goal, ask)
                 if origin in (step['action']['args']['missing'], _read_range(step))), None)


def _receipt(ask, source, original, added, missing, replacement, counts, *, previous=None):
    args = {'guidance': source['citation'], 'original_sources': original, 'added_sources': added,
            'missing': missing, 'replacement': replacement}
    note = '綁定使用者指定的缺行補件'
    if previous is not None:
        args['replaces'] = previous['sequence']
        note = '依使用者指定更換不適用的補件'
    action = {'tool': goal_sources.REQUIRED_SUPPLEMENT, 'args': args, 'note': note}
    observation = {'status': 'pending_read', 'ask_sequence': ask['sequence'],
                   'ask_sha256': goal_stages.digest([ask['sequence'], ask['action'], ask['observation']]), **counts}
    observation['supplement_sha256'] = goal_stages.digest([action, observation, source['sha256']])
    return action, observation, goal_sources.merge_sources(original, added)


def bind(goal, ask, source, added, original_line_count, replacement_line_count):
    original = goal['sources']
    missing, replacement, chosen = goal_sources.plan_mapping(source['content'])
    did, low, high = (missing[key] for key in document_ranges.BATCH_FIELDS)
    required = target(goal, ask)
    question = ask['observation']['question']
    if (not required or source['steps_seen'] != ask['sequence']
            or (chosen is not None and chosen != question)
            or any(doc != did or low > start or end > high for doc, start, end in required)
            or any(goal_required_reads._missing([(start, end) for _, start, end in required], low, high))):
        raise ValueError('補件須與目前單一缺行問題指定的原文範圍完全對應。')
    if type(original_line_count) is not int or not 0 <= original_line_count < low:
        raise ValueError('原指定範圍含有可讀原行，請保留它們。')
    if not added or [row['document_id'] for row in added] != [replacement['document_id']]:
        raise ValueError('請指定目前對話中已匯入的新補件副本。')
    if type(replacement_line_count) is not int or replacement_line_count < replacement['end_line']:
        raise ValueError('補件副本未包含指定的完整行。')
    return _receipt(ask, source, original, added, missing, replacement,
                    {'original_line_count': original_line_count, 'replacement_line_count': replacement_line_count})


def bind_correction(goal, ask, source, added, replacement_line_count):
    origin, replacement, chosen = goal_sources.plan_mapping(source['content'])
    previous = correction_target(goal, ask, origin)
    if (previous is None or source['steps_seen'] != ask['sequence']
            or (chosen is not None and chosen != ask['observation']['question'])):
        raise ValueError('更換補件須對應目前待答問題與仍有效的補件。')
    old_mapping = _read_range(previous)
    if old_mapping == replacement or replacement == previous['action']['args']['missing']:
        raise ValueError('請指定另一段完整且適用的補件原文。')
    if type(replacement_line_count) is not int or replacement_line_count < replacement['end_line']:
        raise ValueError('補件副本未包含指定的完整行。')
    original = goal['sources']
    known = {row['document_id'] for row in original}
    did = replacement['document_id']
    if [row['document_id'] for row in added] != ([] if did in known else [did]):
        raise ValueError('請指定目前對話中已匯入的補件副本。')
    return _receipt(ask, source, original, added, previous['action']['args']['missing'],
                    replacement, {'replacement_line_count': replacement_line_count}, previous=previous)


def line_count(documents, source):
    row = documents.get(source['document_id'])
    if row is None or (row['name'], row['sha256']) != (source['name'], source['sha256']):
        return None
    return len(row['content'].splitlines())


def validate_saved(db, *, replies_present, guidance_present, withdrawals_present):
    rows = db.execute('''SELECT g.id,g.session_id,g.objective,g.notes,g.sources_json
        FROM autonomous_goals g WHERE EXISTS (SELECT 1 FROM autonomous_steps s WHERE s.goal_id=g.id
        AND json_extract(s.action_json,'$.tool')=?)''', (goal_sources.REQUIRED_SUPPLEMENT,))
    for gid, sid, objective, notes, source_json in rows:
        if not guidance_present or not withdrawals_present:
            raise ValueError('保存的原文補件缺少主動更正或撤下紀錄。')
        steps = [{'sequence': seq, 'action': json.loads(action), 'observation': json.loads(obs)}
                 for seq, action, obs in db.execute(
                     'SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence',
                     (gid,))]
        guidance = goal_guidance_sources.read(db, gid, notes, len(steps))
        replies = goal_reply_sources.read(db, gid, notes, include_positions=True) if replies_present else []
        documents = {row[0]: dict(zip(('id','name','sha256','content'), row)) for row in db.execute(
            'SELECT id,name,sha256,content FROM documents WHERE session_id=?', (sid,))}
        goal_sources.verify_history(steps, json.loads(source_json), documents)
        history_sha = conversation_history.saved_fingerprint(db, gid)
        for index, step in enumerate(steps):
            action = step['action']
            if action['tool'] != goal_sources.REQUIRED_SUPPLEMENT:
                continue
            args, obs = action['args'], step['observation']
            source = next((row for row in guidance if row['citation'] == args.get('guidance')), None)
            if index == 0 or source is None or step['sequence'] != source['steps_seen'] + 1:
                raise ValueError('保存的原文補件未接續原提問及更正。')
            ask = steps[index - 1]
            goal = {'id': gid, 'objective': objective, 'sources': args['original_sources'], 'steps': steps[:index],
                    'guidance_sources': [row for row in guidance if row['notes_start'] < source['notes_start']],
                    'reply_sources': [row for row in replies if row['ask_sequence'] < ask['sequence']]}
            correction = 'replaces' in args
            if correction and type(args['replaces']) is not int:
                raise ValueError('保存的原文補件缺少有效的被取代對應。')
            replacement_scope = goal_sources.merge_sources(args['original_sources'], args['added_sources'])
            counts = [('replacement_line_count', 'replacement', replacement_scope)]
            if not correction:
                counts.append(('original_line_count', 'missing', args['original_sources']))
            for count_key, range_key, scope in counts:
                if not isinstance(args.get(range_key), dict):
                    raise ValueError('保存的補件缺少範圍。')
                document = next((row for row in scope if row['document_id'] == args[range_key].get('document_id')), None)
                if document is None:
                    raise ValueError('保存的補件缺少來源。')
                actual = line_count(documents, document)
                if actual is not None and actual != obs.get(count_key):
                    raise ValueError('保存的原文補件行數與匯入副本不一致。')
            if correction:
                expected_action, expected, merged = bind_correction(
                    goal, ask, source, args['added_sources'], obs.get('replacement_line_count'))
            else:
                expected_action, expected, merged = bind(
                    goal, ask, source, args['added_sources'],
                    obs.get('original_line_count'), obs.get('replacement_line_count'))
            note = goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at'])
            condition = {**goal, 'sources': merged, 'notes': notes[:source['notes_start'] + len(note)]}
            if history_sha is not None:
                condition['conversation_history'] = {'sha256': history_sha}
            expected['goal_conditions_sha256'] = work_plan.conditions(condition)
            withdrawal = db.execute('SELECT guidance_number,question_sha256,guidance_sha256 '
                                    'FROM autonomous_question_withdrawals WHERE goal_id=? AND ask_sequence=?',
                                    (gid, ask['sequence'])).fetchone()
            if (action != expected_action or obs != expected
                    or withdrawal != (source['number'], expected['ask_sha256'], source['sha256'])):
                raise ValueError('保存的原文補件與原提問、更正或撤下紀錄不一致。')
