"""Bounded public task plans, backed by local actions, never hidden reasoning."""
import hashlib
import json
import re

STATES = {'pending': '待處理', 'working': '處理中', 'done': '助理回報完成',
          'blocked': '受阻', 'superseded': '已調整需求'}
GUIDANCE = '''可用update_plan保存並更新公開工作清單：args精確為{"items":[{"id":1,"task":"要交付的具體事項","state":"pending|working|done|blocked|superseded","evidence_steps":[],"note":"簡短進度或受阻原因"}]}。
使用者要求進度清單或工作有多個需要追蹤的交付時使用；簡單單步問題不必額外列計畫。每次更新也算一步，不要每做一個工具就重複更新；在重要進展、遇到問題或交付前更新即可。
清單列資料核對、計算、內容準備等實質工作，不要把最後的finish／deliver交付動作另列成待辦。交付與否由實際目標狀態呈現；尚未執行的事情不能先標完成，也不要為了清單全完成而宣稱已交付。既有清單若已包含交付事項，保留它並在note說明實際進度，不能在交付前標done。
清單最多8項，id為1到8的整數；最多一項working。更新必須保留已存在的id及原task，不能偷偷移除要求；新要求可新增項目，已取代的舊要求用superseded並解釋原因。blocked也需寫明缺少什麼，仍需必要補充時使用ask，不把清單當作已發問。
done需連到至少一個已執行、沒有錯誤且屬於目前目標條件的實際工具步驟sequence；最多每項6個。不接受計畫、問題、交付宣告或其他目標的紀錄當作完成證據。舊版未記錄條件的步驟也需重新觀察。證據參照只是可追溯，不證明語意完整或使用者驗收；不要把讀到部分資料說成全部完成。
work_plan是最近一次清單；conditions_current=false代表目標補充或來源已變，舊完成標記待重新核對。依最新明確要求重新計算／讀取，再更新；尚未核對的項目恢復pending或working，不可沿用過時證據。既有步驟的goal_conditions_sha256由程式產生，不要自行輸出它。
清單只記錄公開任務及簡短進度，不輸出內部思考過程、不增加工具權限、不啟動新工作。需要檔案交付仍用deliver，文字成果仍用finish；完成清單本身不是交付。上一階段清單僅是歷史，本階段要重新建立清單並核對證據，不可重用其步驟編號。
'''


def conditions(goal):
    value = {key: goal[key] for key in ('id', 'objective', 'notes', 'sources')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate(args):
    if not isinstance(args, dict) or set(args) != {'items'} or not isinstance(args['items'], list) or not 1 <= len(args['items']) <= 8:
        raise ValueError('工作清單需為1至8項。')
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
            return {'sequence': step['sequence'], 'items': step['observation']['items'],
                    'goal_conditions_sha256': step['observation']['goal_conditions_sha256']}
    return None


def observe(args, steps, scope):
    validate(args)
    previous = latest(steps)
    by_id = {row['id']: row for row in args['items']}
    if previous and any(row['id'] not in by_id or row['task'] != by_id[row['id']]['task'] for row in previous['items']):
        raise ValueError('更新清單需保留原事項與編號；被取代的事項請標記superseded並說明原因。')
    by_sequence = {row['sequence']: row for row in steps}
    for item in args['items']:
        for number in item['evidence_steps']:
            step = by_sequence.get(number)
            if step is None or step['action']['tool'] in {'update_plan', 'ask', 'finish', 'deliver'}:
                raise ValueError('證據需指向本目標先前已執行的實際工具，不能引用清單或宣告。')
            observed = step['observation']
            if item['state'] == 'done' and (step['action']['tool'] == 'invalid' or observed.get('goal_conditions_sha256') != scope or observed.get('error') or
                    observed.get('status') in ('failed', 'cancelled') or
                    any(row['status'] != 'provided' for row in observed.get('requested_ranges', []))):
                raise ValueError('完成證據失敗、不完整或來自舊條件；請重新取得適用證據。')
    return {'status': 'plan_recorded', 'items': json.loads(json.dumps(args['items'])),
            'goal_conditions_sha256': scope,
            'review_note': '助理自行回報進度；工具關聯不等於語意正確或使用者驗收，清單不授予任何新權限。'}


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
    for item in value['items']:
        refs = '；依據步驟 ' + '、'.join(map(str, item['evidence_steps'])) if item['evidence_steps'] else ''
        lines.append(f"{item['id']}. {item['task']} — {STATES[item['state']]}{refs}")
        if item['note']:
            lines.append('   ' + item['note'])
    return '\n'.join(lines)


def validate_saved(db):
    for (gid,) in db.execute("SELECT DISTINCT goal_id FROM autonomous_steps WHERE action_json LIKE '%update_plan%'").fetchall():
        prior = []
        for sequence, action_json, observation_json in db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,)):
            action, observation = json.loads(action_json), json.loads(observation_json)
            if action['tool'] == 'update_plan':
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch('[a-f0-9]{64}', scope):
                    raise ValueError('工作清單缺少有效的條件紀錄。')
                expected = observe(action['args'], prior, scope)
                if observation.get('status') != expected['status'] or observation.get('items') != expected['items']:
                    raise ValueError('工作清單與保存動作不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
