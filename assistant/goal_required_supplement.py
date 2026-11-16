"""Bind an explicit replacement to a genuinely absent line in a waiting goal."""
import json
import re

from . import (conversation_history, document_ranges, goal_guidance_sources, goal_reply_sources, goal_required_reads,
               goal_sources, goal_stages, work_plan)

GUIDANCE = ('required_document_supplement是使用者為原缺行明確指定的補件。先查讀replacement原行，'
            '核對它是否支持本次工作及該欄。使用實際補件行號引用，原已讀文件中仍適用的欄位繼續沿用。'
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
    merged = goal_sources.merge_sources(original, added)
    args = {'guidance': source['citation'], 'original_sources': original, 'added_sources': added,
            'missing': missing, 'replacement': replacement}
    action = {'tool': goal_sources.REQUIRED_SUPPLEMENT, 'args': args, 'note': '綁定使用者指定的缺行補件'}
    observation = {'status': 'pending_read', 'ask_sequence': ask['sequence'],
                   'ask_sha256': goal_stages.digest([ask['sequence'], ask['action'], ask['observation']]),
                   'original_line_count': original_line_count, 'replacement_line_count': replacement_line_count}
    observation['supplement_sha256'] = goal_stages.digest([action, observation, source['sha256']])
    return action, observation, merged


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
            for count_key, range_key, scope in (
                ('original_line_count', 'missing', args['original_sources']),
                ('replacement_line_count', 'replacement', args['added_sources']),
            ):
                if not isinstance(args.get(range_key), dict):
                    raise ValueError('保存的補件缺少範圍。')
                document = next((row for row in scope if row['document_id'] == args[range_key].get('document_id')), None)
                if document is None:
                    raise ValueError('保存的補件缺少來源。')
                actual = line_count(documents, document)
                if actual is not None and actual != obs.get(count_key):
                    raise ValueError('保存的原文補件行數與匯入副本不一致。')
            expected_action, expected, merged = bind(
                goal, ask, source, args['added_sources'], obs.get('original_line_count'), obs.get('replacement_line_count'))
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
