"""Apply a saved user's explicit change to a goal's original file permission."""
import json
import re

from . import goal_guidance_sources, goal_reply_sources
from .acceptance import strict_json


_SCOPE = r'(?:(?:這次|本次|這回|這一輪|本輪|這個目標|此目標)\s*)?'
_TEXT = r'(?:文字(?:成果|回覆)?|純文字)'
_TEXT_ACTION = rf'(?:改(?:為|成){_TEXT}|(?:直接|只)(?:在(?:這裡|這邊|視窗|對話中))?(?:交(?:付)?|回(?:覆|答)?|貼){_TEXT})'
_CANCEL = re.compile(
    rf'{_SCOPE}(?:取消(?:本次)?(?:文件|檔案)交付|(?:先)?(?:不用|不必|不要|不需要|不需|無須)(?:再)?'
    rf'(?:出檔|交檔|交(?:付)?(?:文件|檔案)|(?:產生|生成|建立)檔案|寫檔|做成檔案))'
    rf'(?:[，,]\s*{_TEXT_ACTION})?|{_SCOPE}(?:交付形式|交付方式)改(?:為|成){_TEXT}'
    rf'|{_SCOPE}(?:改為|改成)(?:只回覆|只交付|直接回覆){_TEXT}|{_SCOPE}(?:改為|改成)文字回覆')
_RESTORE = re.compile(
    rf'{_SCOPE}(?:(?:改回|恢復)(?:出檔|交檔|(?:文件|檔案)交付)|照原要求(?:出檔|交檔)|'
    rf'(?:交付形式|交付方式)改回(?:本機)?(?:文件|檔案))')
_COMMA = re.compile(r'\s*')
_TEXT_ONLY = re.compile(_TEXT_ACTION)
GUIDANCE = ('delivery_mode_change 是使用者已保存且生效的交付方式更正。依目前 file_delivery 選擇工具。'
            '切換為文字時保留仍適用的工作內容，在視窗完成定稿；較早的檔案要求與草稿留在歷史。'
            '按需要引用更正原話的 citation，無須重問交付方式。')


def _directive(text):
    """Read standalone, unquoted directives with their exact source positions."""
    found = []
    for offset, clause, question in goal_guidance_sources.standalone_clauses(text):
        if question:
            continue
        if _CANCEL.fullmatch(clause):
            found.append((False, offset, clause))
        elif _RESTORE.fullmatch(clause):
            found.append((True, offset, clause))
    if not found:
        return None
    if len({item[0] for item in found}) > 1:
        raise ValueError('同一則補充同時取消及恢復文件交付，請明確選擇一種交付方式。')
    enabled, start, quote = found[-1]
    return {'file_delivery': enabled, 'start': start, 'end': start + len(quote), 'quote': quote}


def initially_enabled(db, gid):
    return db.execute('SELECT 1 FROM autonomous_delivery WHERE goal_id=? AND enabled=1', (gid,)).fetchone() is not None


def latest(db, gid):
    row = db.execute('SELECT receipt_json FROM autonomous_delivery_changes WHERE goal_id=? ORDER BY notes_start DESC LIMIT 1',
                     (gid,)).fetchone()
    if row is None:
        return None
    receipt = strict_json(row[0])
    if not isinstance(receipt, dict) or type(receipt.get('file_delivery')) is not bool:
        raise ValueError('交付方式更正紀錄格式不符。')
    return receipt


def enabled(db, gid):
    if not initially_enabled(db, gid):
        return False
    receipt = latest(db, gid)
    return receipt['file_delivery'] if receipt else True


def describe(receipt):
    mode = '本機文件' if receipt['file_delivery'] else '視窗文字成果'
    return f'已依 {receipt["citation"]} 更新交付方式為{mode}。'


def _receipt(source):
    directive = _directive(source.get('answer', source.get('content')))
    if directive is None:
        return None
    return {**directive, 'citation': source['citation'], 'source_sha256': source['sha256'],
            'steps_seen': source.get('ask_sequence', source.get('steps_seen'))}


def _plan_receipt(source):
    if 'answer' not in source:
        return None
    text = source['answer']
    parts = []
    for start, clause, question in goal_guidance_sources.standalone_clauses(text, comma_before=_COMMA):
        if question:
            continue
        end = start + len(clause)
        if _CANCEL.fullmatch(clause):
            mode = False
        elif _RESTORE.fullmatch(clause):
            mode = True
        else:
            if parts and not parts[-1]['file_delivery'] and _TEXT_ONLY.fullmatch(clause):
                combined = text[parts[-1]['start']:end]
                if _CANCEL.fullmatch(combined):
                    parts[-1].update(end=end, quote=combined)
            continue
        parts.append({'file_delivery': mode, 'start': start, 'end': end, 'quote': clause})
    if not parts:
        return None
    if len({part['file_delivery'] for part in parts}) > 1:
        raise ValueError('同一則補充同時取消及恢復文件交付，請明確選擇一種交付方式。')
    return {**parts[-1], 'citation': source['citation'], 'source_sha256': source['sha256'],
            'steps_seen': source['ask_sequence'], 'plan_parts': parts}


def plan_reply(db, gid, source):
    if not initially_enabled(db, gid):
        return None
    receipt = _plan_receipt(source)
    if receipt is None:
        return None
    return {'receipt': receipt, 'changed': receipt['file_delivery'] != enabled_before(db, gid, source['notes_start'])}


def enabled_before(db, gid, notes_end):
    if not initially_enabled(db, gid):
        return False
    previous = db.execute('SELECT receipt_json FROM autonomous_delivery_changes WHERE goal_id=? AND notes_start<? '
                          'ORDER BY notes_start DESC LIMIT 1', (gid, notes_end)).fetchone()
    return strict_json(previous[0])['file_delivery'] if previous else True


def _record(db, gid, source, receipt):
    db.execute('INSERT INTO autonomous_delivery_changes(goal_id,citation,notes_start,receipt_json) VALUES(?,?,?,?)',
               (gid, source['citation'], source['notes_start'], json.dumps(receipt, ensure_ascii=False)))


def record_plan_reply(db, gid, source, directive):
    if directive['changed']:
        _record(db, gid, source, directive['receipt'])


def _saved_change(db, gid, citation):
    row = db.execute('SELECT receipt_json FROM autonomous_delivery_changes WHERE goal_id=? AND citation=?',
                     (gid, citation)).fetchone()
    return strict_json(row[0]) if row else None


def verify_plan_reply(db, gid, source):
    directive = plan_reply(db, gid, source)
    if directive is None or _saved_change(db, gid, source['citation']) != (directive['receipt'] if directive['changed'] else None):
        raise ValueError('工作清單答覆缺少相符的交付方式紀錄。')
    return directive


def verify_plan_guidance(db, gid, source):
    receipt = _receipt(source)
    if not initially_enabled(db, gid) or receipt is None or _saved_change(db, gid, source['citation']) != receipt:
        raise ValueError('工作清單更正缺少相符的交付方式紀錄。')
    return receipt


def capture(db, gid, notes, step_count, *, reply=False):
    if not initially_enabled(db, gid):
        return None
    sources = (goal_reply_sources.read(db, gid, notes, include_positions=True) if reply
               else goal_guidance_sources.read(db, gid, notes, step_count))
    if not sources:
        return None
    source = sources[-1]
    if source.get('ask_sequence', source.get('steps_seen')) != step_count:
        return None
    receipt = _receipt(source)
    if receipt is None or receipt['file_delivery'] == enabled(db, gid):
        return None
    _record(db, gid, source, receipt)
    return receipt


def validate_saved(db, *, changes_present, replies_present, guidance_present):
    if changes_present and db.execute('SELECT 1 FROM autonomous_delivery_changes c LEFT JOIN autonomous_delivery d '
                                      'ON c.goal_id=d.goal_id AND d.enabled=1 WHERE d.goal_id IS NULL LIMIT 1').fetchone():
        raise ValueError('交付方式更正缺少原目標的文件交付設定。')
    for gid, notes in db.execute('SELECT g.id,g.notes FROM autonomous_delivery d JOIN autonomous_goals g ON d.goal_id=g.id'):
        steps = db.execute('SELECT sequence,action_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,)).fetchall()
        rows = db.execute('SELECT citation,notes_start,receipt_json FROM autonomous_delivery_changes '
                          'WHERE goal_id=? ORDER BY notes_start', (gid,)).fetchall() if changes_present else []
        sources = goal_reply_sources.read(db, gid, notes, include_positions=True) if rows and replies_present else []
        if rows and guidance_present:
            sources.extend(goal_guidance_sources.read(db, gid, notes, len(steps)))
        by_citation = {source['citation']: source for source in sources}
        changes = []
        mode = True
        position = -1
        for citation, start, raw in rows:
            source = by_citation.get(citation)
            receipt = strict_json(raw)
            combined = isinstance(receipt, dict) and 'plan_parts' in receipt
            expected = (_plan_receipt(source) if combined else _receipt(source)) if source is not None else None
            if (source is None or start != source['notes_start'] or type(start) is not int or start <= position
                    or not isinstance(receipt, dict) or type(receipt.get('file_delivery')) is not bool
                    or any(type(receipt.get(key)) is not int for key in ('start', 'end', 'steps_seen'))
                    or receipt != expected or receipt['file_delivery'] == mode
                    or (changes and receipt['steps_seen'] < changes[-1]['steps_seen'])):
                raise ValueError('交付方式更正與保存的原話或先後順序不一致。')
            if combined:
                linked = db.execute('SELECT action_json,observation_json FROM autonomous_steps WHERE goal_id=? AND sequence=?',
                                    (gid, receipt['steps_seen'] + 1)).fetchone()
                action, observation = map(strict_json, linked) if linked else (None, None)
                proof = observation.get('scope_reply') if isinstance(observation, dict) else None
                if (not isinstance(action, dict) or action.get('tool') != 'update_plan'
                        or action.get('args') not in ({'scope_reply': citation, 'scope_reply_version': version}
                                                       for version in (5, 7))
                        or not isinstance(proof, dict) or proof.get('delivery_mode') != {'receipt': receipt, 'changed': True}):
                    raise ValueError('合併交付方式更正缺少相符的工作清單答覆。')
            position = start
            mode = receipt['file_delivery']
            changes.append(receipt)
        mode = True
        cursor = 0
        for sequence, raw in steps:
            while cursor < len(changes) and changes[cursor]['steps_seen'] < sequence:
                mode = changes[cursor]['file_delivery']
                cursor += 1
            action = strict_json(raw)
            if not isinstance(action, dict) or not isinstance(action.get('tool'), str):
                raise ValueError('已保存的交付步驟格式不符。')
            tool = action['tool']
            if (tool in {'write_artifact', 'deliver'} and not mode) or (tool == 'finish' and mode):
                raise ValueError('已保存的交付步驟與當時生效的交付方式不一致。')
