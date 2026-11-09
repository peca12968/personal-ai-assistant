"""Bounded public task plans, backed by local actions, never hidden reasoning."""
import hashlib
import json
import re

from .documents import check_citations

STATES = {'pending': '待處理', 'working': '處理中', 'done': '助理回報完成',
          'blocked': '受阻', 'superseded': '已調整需求'}
RANGE_READERS = {'read', 'read_ranges'}
RANGE_FIELDS = ('document_id', 'start_line', 'end_line')
GUIDANCE = '''可用update_plan保存並更新公開工作清單：args精確為{"items":[{"id":1,"task":"要交付的具體事項","state":"pending|working|done|blocked|superseded","evidence_steps":[],"note":"簡短進度或受阻原因"}]}。
使用者要求進度清單或工作有多個需要追蹤的交付時使用；簡單單步問題不必額外列計畫。每次更新也算一步，不要每做一個工具就重複更新；在重要進展、遇到問題或交付前更新即可。
清單列資料核對、計算、內容準備等實質工作，不要把最後的finish／deliver交付動作另列成待辦。交付與否由實際目標狀態呈現；尚未執行的事情不能先標完成，也不要為了清單全完成而宣稱已交付。既有清單若已包含交付事項，保留它並在note說明實際進度，不能在交付前標done。
清單最多8項，id為1到8的整數；最多一項working。更新必須保留已存在的id及原task，不能偷偷移除要求；新要求可新增項目，已取代的舊要求用superseded並解釋原因。blocked也需寫明缺少什麼，仍需必要補充時使用ask，不把清單當作已發問。
done需連到至少一個已執行、沒有錯誤且屬於目前目標條件的實際工具步驟sequence；最多每項6個。不接受計畫、問題、交付宣告或其他目標的紀錄當作完成證據。舊版未記錄條件的步驟也需重新觀察。證據參照只是可追溯，不證明語意完整或使用者驗收；不要把讀到部分資料說成全部完成。
work_plan是最近一次清單；conditions_current=false代表目標補充或來源已變，舊完成標記待重新核對。先判斷哪些項目受新補充影響。更正後的計算要用目前條件重新執行，尚未核對的項目恢復pending或working。既有步驟的goal_conditions_sha256由程式產生，不要自行輸出它。
如果同一清單的已完成項目原先只以read或read_ranges完整查讀為依據，而且查讀範圍仍適用，可在本次update_plan的args另加reuse_document_steps，例如{"items":[...],"reuse_document_steps":[2]}。原項目須保留整組原有evidence_steps，程式會核對保存的讀取範圍與同版本文件。只列本次實際沿用的舊查讀步驟，不必重讀同樣範圍。原先依賴計算、搜尋或其他工具的項目不能用這個例外。沿用原文件原行不代表保留被更正的值；依最新確認重算受影響的結果。已完整查讀的文件項目可標完成；另一個缺漏欄位可用ask補充，不需要把查讀項目改成受阻。
清單只記錄公開任務及簡短進度，不輸出內部思考過程、不增加工具權限、不啟動新工作。需要檔案交付仍用deliver，文字成果仍用finish；完成清單本身不是交付。上一階段清單僅是歷史，本階段要重新建立清單並核對證據，不可重用其步驟編號。
'''


def conditions(goal):
    value = {key: goal[key] for key in ('id', 'objective', 'notes', 'sources')}
    if goal.get('conversation_history') is not None:
        value['conversation_history_sha256'] = goal['conversation_history']['sha256']
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate(args):
    if (not isinstance(args, dict) or set(args) not in ({'items'}, {'items', 'reuse_document_steps'})
            or not isinstance(args['items'], list) or not 1 <= len(args['items']) <= 8):
        raise ValueError('工作清單需為1至8項。')
    if 'reuse_document_steps' in args:
        reused = args['reuse_document_steps']
        if (not isinstance(reused, list) or not 1 <= len(reused) <= 32
                or any(type(n) is not int or not 1 <= n <= 32 for n in reused) or len(set(reused)) != len(reused)):
            raise ValueError('沿用查讀需列出1至32個不同的舊步驟編號。')
    ids = set()
    working = 0
    for row in args['items']:
        if not isinstance(row, dict) or set(row) != {'id', 'task', 'state', 'evidence_steps', 'note'}:
            raise ValueError('工作項目欄位不符。')
        if type(row['id']) is not int or not 1 <= row['id'] <= 8 or row['id'] in ids:
            raise ValueError('工作編號需為不重複的1至8。')
        ids.add(row['id'])
        if any(not isinstance(row[k], str) or len(row[k]) > limit or '\x00' in row[k]
               for k, limit in (('task', 300), ('note', 400))) or not row['task'].strip():
            raise ValueError('事項需為1至300字，進度說明最多400字。')
        if not isinstance(row['state'], str) or row['state'] not in STATES:
            raise ValueError('工作狀態不支援。')
        working += row['state'] == 'working'
        refs = row['evidence_steps']
        if (not isinstance(refs, list) or len(refs) > 6 or
                any(type(n) is not int or not 1 <= n <= 32 for n in refs) or len(set(refs)) != len(refs)):
            raise ValueError('證據需為最多6個不重複的本目標步驟編號。')
        if row['state'] == 'done' and not refs:
            raise ValueError('回報完成需附實際工具步驟。')
        if row['state'] in ('blocked', 'superseded') and not row['note'].strip():
            raise ValueError('受阻或調整需求需說明原因。')
    if working > 1:
        raise ValueError('同一時間最多一項工作標記為處理中。')


def latest(steps):
    for step in reversed(steps):
        if step['action']['tool'] == 'update_plan' and step['observation'].get('status') == 'plan_recorded':
            observation = step['observation']
            value = {'sequence': step['sequence'], 'items': observation['items'],
                     'goal_conditions_sha256': observation['goal_conditions_sha256']}
            if 'reused_document_steps' in observation:
                value['reused_document_steps'] = observation['reused_document_steps']
            return value
    return None


def _range_key(row):
    return tuple(row.get(field) for field in RANGE_FIELDS) if isinstance(row, dict) else ()


def _read_proof(step, scope, sources):
    action, observation = step['action'], step['observation']
    old_scope = observation.get('goal_conditions_sha256')
    if (action['tool'] not in RANGE_READERS or not isinstance(old_scope, str)
            or not re.fullmatch(r'[a-f0-9]{64}', old_scope) or old_scope == scope):
        raise ValueError('沿用證據須為已記錄舊條件的完整文件查讀。')
    requested = observation.get('requested_ranges')
    original = [action['args']] if action['tool'] == 'read' else action['args'].get('ranges')
    if not isinstance(requested, list) or not isinstance(original, list) or not original:
        raise ValueError('沿用證據缺少原查讀範圍。')
    ranges = [_range_key(row) for row in original]
    if (ranges != [_range_key(row) for row in requested]
            or any(len(key) != 3 or any(type(n) is not int or not 1 <= n < 10**12 for n in key) or key[2] < key[1]
                   for key in ranges)):
        raise ValueError('沿用證據的原查讀範圍不一致。')
    excerpts = observation.get('excerpts')
    if not isinstance(excerpts, list) or not excerpts:
        raise ValueError('沿用證據沒有已讀文件原行。')
    by_id = {row['document_id']: row for row in sources}
    for excerpt in excerpts:
        key = _range_key(excerpt)
        source = by_id.get(key[0]) if key in ranges else None
        if (source is None or any(type(n) is not int for n in key) or not isinstance(excerpt.get('text'), str)
                or excerpt.get('partial_line') is not False
                or source['name'] != excerpt.get('name') or source['sha256'] != excerpt.get('sha256')):
            raise ValueError('沿用證據與本目標文件版本或範圍不一致。')
    tokens = ' '.join(f'[D{did}:L{start}-L{end}]' for did, start, end in ranges)
    checks = check_citations(tokens, excerpts)
    if len(checks) != len(ranges) or any(not check['range_valid'] for check in checks):
        raise ValueError('沿用證據沒有完整覆蓋原查讀範圍。')
    raw = json.dumps(step, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'sequence': step['sequence'], 'document_ids': sorted({key[0] for key in ranges}),
            'read_sha256': hashlib.sha256(raw).hexdigest()}


def observe(args, steps, scope, sources=()):
    validate(args)
    previous = latest(steps)
    by_id = {row['id']: row for row in args['items']}
    if previous and any(row['id'] not in by_id or row['task'] != by_id[row['id']]['task'] for row in previous['items']):
        raise ValueError('更新清單需保留原事項與編號；被取代的事項請標記superseded並說明原因。')
    by_sequence = {row['sequence']: row for row in steps}
    reused = args.get('reuse_document_steps', [])
    previous_items = {row['id']: row for row in previous['items']} if previous else {}
    proofs = {}
    for item in args['items']:
        for number in item['evidence_steps']:
            step = by_sequence.get(number)
            if step is None or step['action']['tool'] in {'update_plan', 'ask', 'finish', 'deliver'}:
                raise ValueError('證據需指向本目標先前已執行的實際工具，不能引用清單或宣告。')
            observed = step['observation']
            if item['state'] == 'done' and (step['action']['tool'] == 'invalid' or observed.get('error') or
                    observed.get('status') in ('failed', 'cancelled') or
                    any(row['status'] != 'provided' for row in observed.get('requested_ranges', [])) or
                    (observed.get('goal_conditions_sha256') != scope and number not in reused)):
                raise ValueError('完成證據失敗、不完整或來自舊條件；請重新取得適用證據。')
            if item['state'] == 'done' and number in reused:
                old = previous_items.get(item['id'])
                if (old is None or old['state'] != 'done' or item['evidence_steps'] != old['evidence_steps']
                        or any(by_sequence.get(ref, {}).get('action', {}).get('tool') not in RANGE_READERS
                               for ref in old['evidence_steps'])):
                    raise ValueError('只能沿用同一個原已完成且只以文件查讀為依據的項目。')
                if number not in proofs:
                    proofs[number] = _read_proof(step, scope, sources)
    if set(proofs) != set(reused):
        raise ValueError('沿用清單只能列本次完成項目實際引用的舊查讀步驟。')
    result = {'status': 'plan_recorded', 'items': json.loads(json.dumps(args['items'])),
            'goal_conditions_sha256': scope,
            'review_note': '助理自行回報進度；工具關聯不等於語意正確或使用者驗收，清單不授予任何新權限。'}
    if proofs:
        result['reused_document_steps'] = [proofs[number] for number in reused]
    return result


def for_goal(goal, current_sources):
    value = latest(goal['steps'])
    if value is not None:
        value['conditions_current'] = value['goal_conditions_sha256'] == conditions(goal) and current_sources == goal['sources']
    return value


def describe(value):
    if value is None:
        return '尚未保存工作清單。'
    lines = ['工作清單（助理回報，仍需核對成果）',
             '紀錄時的目標補充與文件版本仍相同。' if value.get('conditions_current') else '歷史清單：條件已變或來自上一階段，完成標記需重新核對。']
    if value.get('reused_document_steps'):
        numbers = '、'.join(str(row['sequence']) for row in value['reused_document_steps'])
        lines.append('沿用同版本文件查讀 · 步驟 ' + numbers)
    for item in value['items']:
        refs = '；依據步驟 ' + '、'.join(map(str, item['evidence_steps'])) if item['evidence_steps'] else ''
        lines.append(f"{item['id']}. {item['task']} — {STATES[item['state']]}{refs}")
        if item['note']:
            lines.append('   ' + item['note'])
    return '\n'.join(lines)


def validate_saved(db):
    goals = db.execute("SELECT DISTINCT g.id,g.sources_json FROM autonomous_goals g JOIN autonomous_steps s "
                       "ON s.goal_id=g.id WHERE s.action_json LIKE '%update_plan%'")
    for gid, source_json in goals:
        sources = json.loads(source_json)
        prior = []
        for sequence, action_json, observation_json in db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,)):
            action, observation = json.loads(action_json), json.loads(observation_json)
            if action['tool'] == 'update_plan':
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch('[a-f0-9]{64}', scope):
                    raise ValueError('工作清單缺少有效的條件紀錄。')
                expected = observe(action['args'], prior, scope, sources)
                if any(observation.get(key) != expected.get(key) for key in ('status', 'items', 'reused_document_steps')):
                    raise ValueError('工作清單與保存動作不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
