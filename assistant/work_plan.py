"""Bounded public task plans, backed by local actions, never hidden reasoning."""
import hashlib
import json
import re

from . import document_lines, document_ranges, expression_math, goal_guidance_sources, goal_reply_sources, goal_sources, operand_evidence
from .documents import check_citations

STATES = {'pending': '待處理', 'working': '處理中', 'done': '助理回報完成',
          'blocked': '受阻', 'superseded': '已調整需求'}
RANGE_READERS = {'read', 'read_ranges'}
TYPED_READERS = RANGE_READERS | {'read_line', goal_sources.PLAN_SUPPLEMENT}
RANGE_FIELDS = ('document_id', 'start_line', 'end_line')
DELIVERY_TOOLS = {'finish', 'write_artifact', 'deliver'}
CALCULATION_RECHECK_REASONS = {
    'shared_operand_fields': '與更正項的運算元使用同一個明列欄位',
    'shared_document_ranges': '與更正項的指定文件行範圍重疊',
    'shared_operand_citations': '與更正項的運算元引用同一來源',
    'missing_source_requirements': '缺少可分開核對的文件範圍或運算元來源',
}
GUIDANCE = '''可用update_plan保存並更新公開工作清單：args精確為{"items":[{"id":1,"task":"要交付的具體事項","state":"pending|working|done|blocked|superseded","evidence_steps":[],"note":"簡短進度或受阻原因"}]}。
使用者要求進度清單或工作有多個需要追蹤的交付時使用；簡單單步問題不必額外列計畫。每次更新也算一步，不要每做一個工具就重複更新；在重要進展、遇到問題或交付前更新即可。
清單列資料核對、計算、內容準備等實質工作，不要把最後的finish／deliver交付動作另列成待辦。交付與否由實際目標狀態呈現；尚未執行的事情不能先標完成，也不要為了清單全完成而宣稱已交付。既有清單若已包含交付事項，保留它並在note說明實際進度，不能在交付前標done。
清單最多8項，id為1到8的整數；最多一項working。更新必須保留已存在的id及原task，不能偷偷移除要求；新要求可新增項目，已取代的舊要求用superseded並解釋原因。blocked也需寫明缺少什麼，仍需必要補充時使用ask，不把清單當作已發問。
done需連到至少一個已執行、沒有錯誤且屬於目前目標條件的實際工具步驟sequence；最多每項6個。不接受計畫、問題、交付宣告或其他目標的紀錄當作完成證據。舊版未記錄條件的步驟也需重新觀察。證據參照只是可追溯，不證明語意完整或使用者驗收；不要把讀到部分資料說成全部完成。
work_plan是最近一次清單；conditions_current=false代表目標補充或來源已變，舊完成標記待重新核對。先判斷哪些項目受新補充影響。受更正影響的算式要重算，尚未核對的項目恢復pending或working。既有步驟的goal_conditions_sha256由程式產生，不要自行輸出它。
如果同一清單的已完成項目原先只以read或read_ranges完整查讀為依據，而且查讀範圍仍適用，可在本次update_plan的args另加reuse_document_steps，例如{"items":[...],"reuse_document_steps":[2]}。原項目須保留整組原有evidence_steps，程式會核對保存的讀取範圍與同版本文件。只列本次實際沿用的舊查讀步驟，不必重讀同樣範圍。原先依賴計算、搜尋或其他工具的項目不能用這個例外。沿用原文件原行不代表保留被更正的值；依最新確認重算受影響的結果。已完整查讀的文件項目可標完成；另一個缺漏欄位可用ask補充，不需要把查讀項目改成受阻。
使用者要求交付前核對工作清單及完成證據時，保存清單要加"verify_delivery":true。一旦開啟，本目標每次產檔及正式交付前，都須以目前條件更新原清單，未完成或受阻項目先處理；已取代項目需說明原因。work_plan.delivery_review指出仍待處理的項目、需要重驗的舊證據及可沿用的完整文件查讀。收到明確更正時，使用現有依據重算受影響的項目，避免再詢問已確認事項。仍需必要答覆時可用ask附暫表。若文件產於舊條件，按目前核對重新產檔，內容更新時使用新檔名。普通工作沿用現有清單及交付流程。
使用者要求逐項核對具體算式或原文範圍時，在update_plan附evidence_requirements，例如[{"id":1,"expression":{"operation":"difference","values":["20","4"]}},{"id":2,"ranges":[{"document_id":1,"start_line":2,"end_line":2}]}]。同一項可同時要求算式及原文。知道精確範圍或算式時先記要求，待資料確定時可再新增。完成項的evidence_steps須引用該項完整原行及同一棵實際calculate_expression算式；結果相同也不能混用其他算式。補充後在更新清單時重新確認本次要求的算式。對有指定範圍的項目，delivery_review.reusable_documents可列出已完整覆蓋原行的步驟。即使項目先前還未完成或仍需算式，直接將這些舊查讀加到evidence_steps和reuse_document_steps，另補本次新計算，無需重讀。證據接錯時，先更正引用的步驟，已取得正確證據無需重做。
使用者要求保存每個運算元的依據時，在該項evidence_requirements的expression旁附operands，列出每個數字葉節點的path、citation與quote。例如{"path":"/values/0","citation":"[D1:L1]","quote":"本批有 20 件"}。path用算式樹的完整位置，巢狀例如/values/0/values/1。citation限實際完整查讀的單行、本目標補充或更正，或以[目標]引用使用者目標原話。文件原行須列在該項ranges。每段逐字擷取短句，包含本次適用的欄位及唯一相符數值，分數與等值小數可核對。更正採實際補充，文件原值作對照。即使另批或另一欄有同樣的數字，也依本次對象和欄位選來源。證據齊備時可在本次完成清單一次保存，不必逐個更新。若已完整查讀原行，沿用同版讀取。數值與短句位置由工具核對，內容適用仍由你判讀。
來源若明寫「欄位」和完整名稱，例如原句為『欄位「榆灣原備」為 25 包』，把完整原句放在quote，另在同一筆operands填field為「榆灣原備」，不含引號。欄位名須緊接數字之前的「為」或冒號，並與原話逐字相同。不同批次或欄位即使同值，也取實際適用的完整名稱及短句。同一個具名欄位在別處抄錄時，沿用相同名稱，整組確認仍須重算共用該欄的項目。沒有明列欄位名的原文使用既有格式。
尚缺新數字而將項目標為pending、working或blocked時，可只更新items，原evidence_requirements會保留。若重列部分未變的要求，仍沿用原完整規約和舊條件。取得資料後，完成前再依目前條件確認完整的算式及來源；不要把暫表、待補註記或沿用的舊規約當成完成證據。
delivery_review.reusable_calculations列出最新原話已以獨立一行「原項目全名」的數量與算式均照舊。確認的已完成算式。確認前後的需求相容且沒有更正共用輸入時，保持該項done及原evidence_steps，舊查讀仍列入reuse_document_steps，沿用原指定證據。其他項依目前資料核對；即使數值碰巧相同，算式或任何原數改變仍須重算。若仍需補問，可先保存目前可完成項並在ask中清楚標示暫表。
最新回覆也可在最後一行寫『本次只更正「原項目全名」，其餘項目的數量與算式均照舊。』。reusable_calculations列出可沿用的已完成項。兩項即使同引一行或一則回覆，若每段數字原話在該來源中的位置互不重疊，也可能沿用。separate_operand_excerpts表示使用了這項核對。保持可沿用項的原evidence_steps，重算更正項並保存其完整新規約和證據。數字短句用足以辨認欄位的原話，不以整行代替不同欄位的短句。新證據重疊時須重驗相關項目。
delivery_review.calculation_rechecks列出整組確認後仍須重算的項目。id是待重算項，changed_item是更正項，reason指出共用明列欄位、共用文件範圍、共用運算元來源或缺少分開核對的來源規約。數值已知時直接依適用的原文和補充重算，再更新清單，不要先嘗試提交舊算式或要求使用者再次確認照舊。reusable_documents仍可用於沿用已完整查讀的原行。若只缺更正項的必要數字，先完成仍可核對的項目，再只問缺項。
delivery_review.document_read_progress列出有指定原文範圍的待辦項目。missing_ranges是尚缺的完整原行，優先查讀這些範圍。批次有缺漏時，已完整提供的其他範圍仍可作為指定原文的證據；超出本次字數的範圍可分開補讀。read_steps是可覆蓋已查讀部分的步驟，reuse_document_steps是其中屬於舊條件且可沿用的步驟。證據數仍受每項六步上限約束，必要時合併查讀。沒有缺行時，仍須完成該項其他核對並更新清單後才可交付。
若該項有recovery，依其範圍補讀。split_range先把尚缺範圍分開或縮小；read_line依start_char續讀同一原行的字元，已讀分頁不用重讀。原行各字元無缺口時，分頁步驟會出現在read_steps，可連同其他步驟更新該項；報告引用分頁原文仍使用字元範圍。source_missing表示指定範圍含匯入副本缺少的行，或文件不存在。用目錄的line_count判斷哪幾行確實缺少；仍未讀且存在的行才補讀。已取得的其他原行可直接引用於暫表，只請使用者補缺少的來源。
plan_document_supplement是使用者明確指定並已實際查讀的補件。採用work_plan的現行範圍；原要求若只有部分缺行，保留其他原行。在evidence_steps引用補件步驟及仍適用的舊查讀，只補讀尚缺的原文。新值引用補件實際行號；原缺行及被撤下的問題留在歷史。同一工作項目有多個缺行分問時，每問寫出各自的文件編號和確切行號，例如「請提供 D2 第 3 至 4 行的原文。」；補件須涵蓋選定分問所指的全部行。
delivery_review.retained_reply_items列出本次補件保留的獨立確認項。更新清單時維持其done和原reply_step，這些步驟不用列入reuse_document_steps。定稿引用該項的citation，文件項依本次核對的原行完成。
若算式葉節點已換算單位，在該運算元加conversion，例如{"from":"公克","to":"公斤"}。quote仍逐字引用含原數字及原單位的短句，例如「本批配料 450 公克」，算式放換算後的0.45。百分比轉比例用{"from":"%","to":"ratio"}，例如原文8%及算式0.08。工具核對精確倍率、完整原數量及原單位，保留原數與換算值；不同量綱不合併。分組數量若以箱數和每箱件數相乘，分開引用兩個欄位。使用者只要求原式及計算時沿用既有入口。
清單只記錄公開任務及簡短進度，不輸出內部思考過程、不增加工具權限、不啟動新工作。需要檔案交付仍用deliver，文字成果仍用finish；完成清單本身不是交付。上一階段清單僅是歷史，本階段要重新建立清單並核對證據，不可重用其步驟編號。
'''


def conditions(goal):
    value = {key: goal[key] for key in ('id', 'objective', 'notes', 'sources')}
    if goal.get('conversation_history') is not None:
        value['conversation_history_sha256'] = goal['conversation_history']['sha256']
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def validate(args):
    if (not isinstance(args, dict) or args.keys() - {'items', 'reuse_document_steps', 'verify_delivery', 'evidence_requirements'}
            or not isinstance(args.get('items'), list) or not 1 <= len(args['items']) <= 8):
        raise ValueError('工作清單需為1至8項。')
    if 'verify_delivery' in args and args['verify_delivery'] is not True:
        raise ValueError('交付核對只能明確開啟。')
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
    if 'evidence_requirements' in args:
        _validate_requirements(args['evidence_requirements'], ids)


def _validate_requirements(requirements, ids):
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= len(ids):
        raise ValueError('指定證據須對應一項或多項工作。')
    seen = set()
    for row in requirements:
        if (not isinstance(row, dict) or row.keys() - {'id', 'ranges', 'expression', 'operands'}
                or 'id' not in row or not row.keys() & {'ranges', 'expression'}
                or ('operands' in row and 'expression' not in row)
                or type(row['id']) is not int or row['id'] not in ids or row['id'] in seen):
            raise ValueError('指定證據須使用不同的工作項目編號，並提供原文範圍或算式。')
        seen.add(row['id'])
        if 'ranges' in row:
            document_ranges.validate_batch({'ranges': row['ranges']})
            if any(not 1 <= end - start + 1 <= document_ranges.MAX_LINES
                   for _, start, end in map(_range_key, row['ranges'])):
                raise ValueError('指定證據的文件範圍不符。')
        if 'expression' in row:
            expression_math.validate({'expression': row['expression']})
        if 'operands' in row:
            operand_evidence.validate(row['operands'], row['expression'], row.get('ranges', ()))


def latest(steps):
    supplemented = None
    for step in reversed(steps):
        if step['action']['tool'] == goal_sources.PLAN_SUPPLEMENT and supplemented is None:
            supplemented = step['observation']
        if step['action']['tool'] == 'update_plan' and step['observation'].get('status') == 'plan_recorded':
            observation = step['observation']
            value = {'sequence': step['sequence'], 'items': observation['items'],
                     'goal_conditions_sha256': observation['goal_conditions_sha256']}
            if 'reused_document_steps' in observation:
                value['reused_document_steps'] = observation['reused_document_steps']
            if observation.get('verify_delivery') is True:
                value['verify_delivery'] = True
            for key in ('evidence_requirements', 'evidence_matches', 'reused_calculations', 'scope_change', 'scope_reply',
                        'scope_cancelled'):
                if key in observation:
                    value[key] = observation[key]
            if supplemented is not None:
                value['evidence_requirements'] = supplemented['evidence_requirements']
                if retained := supplemented.get('retained_reply_items'):
                    value['retained_reply_items'] = retained
            return value
    return None


def _range_key(row):
    return tuple(row.get(field) for field in RANGE_FIELDS) if isinstance(row, dict) else ()


def _verified_read(step, sources, *, allow_partial=False):
    action, observation = step['action'], step['observation']
    requested = observation.get('requested_ranges')
    args = action.get('args', {})
    if action['tool'] == goal_sources.PLAN_SUPPLEMENT:
        original = [args['replacement']]
    else:
        original = [args] if action['tool'] == 'read' else args.get('ranges')
    if not isinstance(requested, list) or not isinstance(original, list) or not original:
        raise ValueError('沿用證據缺少原查讀範圍。')
    ranges = [_range_key(row) for row in original]
    if (ranges != [_range_key(row) for row in requested]
            or any(len(key) != 3 or any(type(n) is not int or not 1 <= n < 10**12 for n in key) for key in ranges)):
        raise ValueError('沿用證據的原查讀範圍不一致。')
    statuses = [row.get('status') for row in requested]
    provided = [key for key, status in zip(ranges, statuses, strict=True) if status == 'provided']
    partial = len(provided) != len(ranges)
    if (observation.get('error') or observation.get('status') in ('failed', 'cancelled') or not provided or
            (partial and (not allow_partial or action['tool'] != 'read_ranges'
                          or any(not isinstance(status, str) or status not in document_ranges.STATUS_TEXT
                                 for status in statuses)))):
        raise ValueError('完成證據失敗或不完整。')
    if any(not 1 <= end - start + 1 <= document_ranges.MAX_LINES for _, start, end in provided):
        raise ValueError('沿用證據的原查讀範圍不一致。')
    excerpts = observation.get('excerpts')
    if not isinstance(excerpts, list) or not excerpts:
        raise ValueError('沿用證據沒有已讀文件原行。')
    by_id = {row['document_id']: row for row in sources}
    for excerpt in excerpts:
        key = _range_key(excerpt)
        source = by_id.get(key[0]) if key in provided else None
        if (source is None or any(type(n) is not int for n in key) or not isinstance(excerpt.get('text'), str)
                or excerpt.get('partial_line') is not False
                or source['name'] != excerpt.get('name') or source['sha256'] != excerpt.get('sha256')):
            raise ValueError('沿用證據與本目標文件版本或範圍不一致。')
        if partial:
            lines = excerpt['text'].splitlines()
            if (excerpt.get('requested_range') is not True or len(lines) != key[2] - key[1] + 1 or
                    any(not line.startswith(f'L{n}: ') for n, line in enumerate(lines, key[1]))):
                raise ValueError('批次查讀證據缺少完整的原文行。')
    tokens = ' '.join(f'[D{did}:L{start}-L{end}]' for did, start, end in provided)
    checks = check_citations(tokens, excerpts)
    if len(checks) != len(provided) or any(not check['range_valid'] for check in checks):
        raise ValueError('沿用證據沒有完整覆蓋原查讀範圍。')
    return provided, excerpts, partial


def _old_read_scope(step, scope, *, typed=False):
    old_scope = step['observation'].get('goal_conditions_sha256')
    if (step['action']['tool'] not in (TYPED_READERS if typed else RANGE_READERS) or not isinstance(old_scope, str)
            or not re.fullmatch(r'[a-f0-9]{64}', old_scope) or old_scope == scope):
        raise ValueError('沿用證據須為已記錄舊條件的完整文件查讀。')


def _read_digest(step):
    raw = json.dumps(step, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _read_proof(step, scope, sources, *, typed=False):
    _old_read_scope(step, scope, typed=typed)
    if step['action']['tool'] == 'read_line':
        document_ids = [document_lines.verified_page(step, sources)['document_id']]
    else:
        ranges, _, _ = _verified_read(step, sources, allow_partial=typed)
        document_ids = sorted({key[0] for key in ranges})
    return {'sequence': step['sequence'], 'document_ids': document_ids,
            'read_sha256': _read_digest(step)}


def _merge_requirements(rows, previous, scope, sources, items, supplemented):
    requirements = {row['id']: row for row in (previous or {}).get('evidence_requirements', ())}
    document_ids = {source['document_id'] for source in sources}
    for row in rows:
        old = requirements.get(row['id'])
        if old:
            prior = {key: value for key, value in old.items() if key != 'defined_scope'}
            if row['id'] in supplemented and 'ranges' in row and row['ranges'] != prior['ranges']:
                raise ValueError(f'項目 {row["id"]} 的指定範圍已有正式補件，請沿用核對後的原文範圍。')
            if (items[row['id']]['state'] in ('pending', 'working', 'blocked')
                    and row.keys() < prior.keys() and all(prior[key] == value for key, value in row.items())):
                continue  # Retain the complete requirement and its original condition fingerprint.
            if (not prior.keys() <= row.keys()
                    or (old['defined_scope'] == scope and any(row[key] != value for key, value in prior.items()))):
                raise ValueError(f'項目 {row["id"]} 已保存指定證據。請保留證據種類及目前條件下的原要求。')
        if any(source['document_id'] not in document_ids for source in row.get('ranges', ())):
            raise ValueError(f'項目 {row["id"]} 的指定範圍不在本目標文件內。')
        requirements[row['id']] = {**row, 'defined_scope': scope}
    return requirements


def _latest_note_after(plan_step, sequence, replies, guidance):
    reply = next((row for row in reversed(replies) if plan_step < row['ask_sequence'] < sequence), None)
    note = next((row for row in reversed(guidance) if plan_step <= row['steps_seen'] < sequence), None)
    if reply and note:
        if reply['ask_sequence'] == note['steps_seen']:
            return None  # The two channels can record notes on either side of the same ask.
        if reply['ask_sequence'] > note['steps_seen']:
            note = None
    if note:
        return note['content'], note['citation'], note['sha256']
    if reply:
        return reply['answer'], reply['citation'], reply['sha256']
    return None


def _confirmed_line(line, lines):
    if line not in lines:
        return False
    # An unfinished lead-in can make the exact line conditional or quoted.
    leading = '\n'.join(lines[:lines.index(line)]).rstrip()
    return not leading or leading.endswith('。')


def _separate_operand_excerpts(shared_ranges, first_operands, second_operands):
    if not first_operands or not second_operands:
        return False
    left_citations = {row['citation'] for row in first_operands}
    right_citations = {row['citation'] for row in second_operands}
    shared_citations = left_citations & right_citations
    if any(f'[D{document_id}:L{line}]' not in shared_citations
           for document_id, start, end in shared_ranges for line in range(start, end + 1)):
        return False
    for left in first_operands:
        for right in second_operands:
            if left['citation'] == right['citation'] and (
                    left['source_sha256'] != right['source_sha256']
                    or left['quote_start'] < right['quote_end'] and right['quote_start'] < left['quote_end']):
                return False
    return True


def _group_reuse_blocker(first, second, first_operands=(), second_operands=()):
    if not all(row.get('ranges') and row.get('operands') for row in (first, second)):
        return 'missing_source_requirements'
    first_fields = {row['field'] for row in first['operands'] if 'field' in row}
    if any(row.get('field') in first_fields for row in second['operands']):
        return 'shared_operand_fields'
    left = list(map(_range_key, first['ranges']))
    right = list(map(_range_key, second['ranges']))
    shared_ranges = [(left_id, max(left_start, right_start), min(left_end, right_end))
                     for left_id, left_start, left_end in left for right_id, right_start, right_end in right
                     if left_id == right_id and left_start <= right_end and right_start <= left_end]
    shared_citations = {row['citation'] for row in first['operands']} & {row['citation'] for row in second['operands']}
    if not shared_ranges and not shared_citations:
        return None
    if _separate_operand_excerpts(shared_ranges, first_operands, second_operands):
        return None
    return 'shared_document_ranges' if shared_ranges else 'shared_operand_citations'


def _calculation_recheck_message(row):
    reason = CALCULATION_RECHECK_REASONS[row['reason']]
    return f'項目 {row["id"]} 無法依整組確認沿用算式，{reason}（更正項 {row["changed_item"]}）'


def _calculation_reuse_review(plan, scope, sequence, replies, guidance):
    if plan is None:
        return {}, {}
    if plan['goal_conditions_sha256'] == scope:
        return {row['id']: row for row in plan.get('reused_calculations', ())}, {}
    source = _latest_note_after(plan['sequence'], sequence, replies, guidance)
    if source is None:
        return {}, {}
    text, citation, sha = source
    if '```' in text or '~~~' in text:
        return {}, {}
    lines = text.splitlines()
    items = plan['items']
    names = [row['task'] for row in items]
    unique_names = {row['id']: row['task'] for row in items
                    if names.count(row['task']) == 1 and not any(char in row['task'] for char in '「」\r\n')}
    changed = None
    closing_line = next((line for line in reversed(lines) if line.strip()), '')
    if text.count('本次只更正「') == 1 and _confirmed_line(closing_line, lines):
        changed = next((row['id'] for row in items if row['state'] != 'superseded' and row['id'] in unique_names
                        and closing_line == f'本次只更正「{row["task"]}」，其餘項目的數量與算式均照舊。'), None)
    requirements = {row['id']: row for row in plan.get('evidence_requirements', ())}
    matches = {row['id']: row for row in plan.get('evidence_matches', ()) if row.get('operands')}
    candidates = {}
    rechecks = {}
    for item in items:
        number, task = item['id'], item['task']
        if (item['state'] != 'done' or number not in matches or 'expression_step' not in matches[number]
                or number not in unique_names):
            continue
        proof = {'id': number, 'expression_step': matches[number]['expression_step'],
                 'prior_plan_step': plan['sequence'], 'citation': citation, 'source_sha256': sha}
        if text.count(task) == 1 and _confirmed_line(f'「{task}」的數量與算式均照舊。', lines):
            candidates[number] = proof
        elif changed is not None and changed != number and task not in text:
            own_spec, changed_spec = requirements.get(number, {}), requirements.get(changed, {})
            reason = _group_reuse_blocker(own_spec, changed_spec)
            if reason and not _group_reuse_blocker(own_spec, changed_spec, matches[number]['operands'],
                                                   matches.get(changed, {}).get('operands', ())):
                proof['separate_operand_excerpts'] = True
                reason = None
            if reason:
                rechecks[number] = {'id': number, 'changed_item': changed, 'reason': reason}
            else:
                candidates[number] = {**proof, 'changed_item': changed}
    return candidates, rechecks


def _match_item_evidence(item, requirement, steps, scope, sources, user_sources):
    number = item['id']
    ranges = list(map(_range_key, requirement.get('ranges', ())))
    expression = {'expression': requirement['expression']} if 'expression' in requirement else None
    if expression is not None and requirement['defined_scope'] != scope:
        raise ValueError(f'項目 {number} 的算式要求來自舊條件，請依目前條件重新確認原式。')
    match = {'id': number}
    excerpts, pages = [], {}
    for ref in item['evidence_steps']:
        step = steps[ref]
        action = step['action']
        if ranges and action['tool'] in TYPED_READERS:
            paged = action['tool'] == 'read_line'
            if paged:
                actual, partial = [document_lines.verified_page(step, sources)], False
            else:
                _, actual, partial = _verified_read(step, sources, allow_partial=True)
            relevant = [row for row in actual if any(
                row['document_id'] == did and row['start_line'] <= end and start <= row['end_line']
                for did, start, end in ranges)]
            if not relevant:
                raise ValueError(f'項目 {number} 的步驟 {ref} 未查讀指定文件範圍。')
            if paged:
                row = relevant[0]
                pages.setdefault((row['document_id'], row['start_line']), {})[ref] = row
                match.setdefault('line_page_proofs', []).append({'sequence': ref, 'read_sha256': _read_digest(step)})
            else:
                excerpts.extend(relevant)
            match.setdefault('read_steps', []).append(ref)
            if partial:
                match.setdefault('partial_read_proofs', []).append({'sequence': ref, 'read_sha256': _read_digest(step)})
            if action['tool'] == goal_sources.PLAN_SUPPLEMENT:
                match.setdefault('supplement_read_proofs', []).append({'sequence': ref, 'read_sha256': _read_digest(step)})
        elif expression is not None and action['tool'] == 'calculate_expression':
            if action['args'].get('expression') != expression['expression'] or 'expression_step' in match:
                raise ValueError(f'項目 {number} 的步驟 {ref} 與指定算式不符。')
            expected = expression_math.calculate(expression)
            if any(step['observation'].get(key) != value for key, value in expected.items()):
                raise ValueError(f'項目 {number} 的步驟 {ref} 缺少可重驗的精確算式結果。')
            match['expression_step'] = ref
        else:
            raise ValueError(f'項目 {number} 的步驟 {ref} 不屬於指定的查讀或算式。')
    if expression is not None and 'expression_step' not in match:
        raise ValueError(f'項目 {number} 缺少指定算式的實際計算。')
    for line_pages in pages.values():
        merged, _, _ = document_lines.join_pages(line_pages)
        if merged is not None:
            excerpts.append(merged)
    tokens = ' '.join(f'[D{did}:L{start}-L{end}]' for did, start, end in ranges)
    if any(not check['range_valid'] for check in check_citations(tokens, excerpts)):
        raise ValueError(f'項目 {number} 缺少完整指定文件範圍。')
    if 'operands' in requirement:
        try:
            match['operands'] = operand_evidence.bind(requirement['operands'], requirement['expression'],
                                                     excerpts, user_sources)
        except ValueError as exc:
            raise ValueError(f'項目 {number} 的運算元證據未通過。{exc}') from exc
    return match


def _latest_revised_operands(steps, scope):
    requirements = {}
    for step in reversed(steps):
        observation = step['observation']
        proof = observation.get('scope_reply') or observation.get('scope_change')
        if not proof or observation.get('goal_conditions_sha256') != scope:
            continue
        for revision in reversed(proof.get('revisions', ())):
            for affected in revision['operand_paths']:
                if affected['id'] in revision['recheck_items']:
                    for path in affected['paths']:
                        requirements.setdefault((affected['id'], path), (proof, revision))
        scope = proof['prior_scope']
    return requirements


def _check_revised_operands(match, revisions):
    operands = {row['path']: row for row in match.get('operands', ())}
    for (item_id, path), (proof, revision) in revisions.items():
        if item_id != match['id']:
            continue
        actual = operands.get(path, {})
        span = revision['evidence']
        if (actual.get('citation') != proof['citation'] or actual.get('source_sha256') != proof['source_sha256']
                or not span['start'] <= actual['number_start'] < actual['number_end'] <= span['end']):
            raise ValueError(f'項目 {item_id} 的「{revision["field"]}」已更正。請用 {proof["citation"]} 的新值重算並核對運算元。')


def observe(args, steps, scope, sources=(), *, objective='', reply_sources=(), guidance_sources=()):
    from .work_plan_scope import PARTIAL_DOCUMENT_REPLY_VERSION

    validate(args)
    previous = latest(steps)
    by_id = {row['id']: row for row in args['items']}
    if previous and any(row['id'] not in by_id or row['task'] != by_id[row['id']]['task'] for row in previous['items']):
        raise ValueError('更新清單需保留原事項與編號；被取代的事項請標記superseded並說明原因。')
    by_sequence = {row['sequence']: row for row in steps}
    reused = args.get('reuse_document_steps', [])
    previous_items = {row['id']: row for row in previous['items']} if previous else {}
    previous_requirements = {row['id']: row for row in (previous or {}).get('evidence_requirements', ())}
    supplemented = {item for step in steps if step['action']['tool'] == goal_sources.PLAN_SUPPLEMENT
                    for item in step['observation']['items']}
    requirements = _merge_requirements(args.get('evidence_requirements', ()), previous, scope, sources, by_id, supplemented)
    same_scope = previous is not None and previous['goal_conditions_sha256'] == scope
    if same_scope and _pending_revision_fields(previous):
        raise ValueError('須先保存待選更正的實際答覆。')
    prior_cancelled = previous.get('scope_cancelled', ()) if previous else ()
    if same_scope and any(by_id[number]['state'] != 'superseded' for number in prior_cancelled):
        raise ValueError('使用者已取消的工作項目須保留取消狀態。')
    cancelled = [number for number in prior_cancelled if by_id[number]['state'] == 'superseded']
    scope_reviewed = bool(cancelled) or (same_scope and any(
        step['action']['tool'] == 'update_plan' and 'scope_reply' in step['observation']
        and step['observation'].get('goal_conditions_sha256') == scope for step in steps))
    sequence = len(steps) + 1
    user_sources = operand_evidence.user_texts(objective, reply_sources, guidance_sources, sequence)
    revised_operands = _latest_revised_operands(steps, scope) if requirements else {}
    reusable, rechecks = _calculation_reuse_review(previous, scope, sequence, reply_sources, guidance_sources)
    proofs = {}
    calculation_proofs = []
    position_checks = {}
    matches = []
    retained_replies = _retained_replies_for_scope(previous, steps, scope)
    for item in args['items']:
        item_id = item['id']
        if item_id in retained_replies and (item['state'] != 'done' or item['evidence_steps'] != retained_replies[item_id]):
            raise ValueError(f'項目 {item_id} 已由原答覆確認，請保留其完成狀態及答覆步驟。')
        calculation_step = None
        old = previous_items.get(item_id)
        carried = (same_scope and scope_reviewed and old is not None and old['state'] == 'done'
                   and old['evidence_steps'] == item['evidence_steps']
                   and previous_requirements.get(item_id) == requirements.get(item_id))
        candidate = reusable.get(item_id)
        if (candidate and item['state'] == 'done'
                and item['evidence_steps'] == previous_items[item_id]['evidence_steps']):
            prior = previous_requirements[item_id]
            current = {**requirements[item_id], 'defined_scope': scope}
            changed = candidate.get('changed_item')
            if current == {**prior, 'defined_scope': scope}:
                reason = _group_reuse_blocker(current, requirements[changed]) if changed is not None else None
                if reason in ('shared_document_ranges', 'shared_operand_citations') and by_id[changed]['state'] == 'done':
                    position_checks[item_id] = changed
                    candidate = {**candidate, 'separate_operand_excerpts': True}
                    reason = None
                if reason:
                    rechecks[item_id] = {'id': item_id, 'changed_item': changed, 'reason': reason}
                else:
                    calculation_step = candidate['expression_step']
                    requirements[item_id] = current
                    calculation_proofs.append(candidate)
        for number in item['evidence_steps']:
            step = by_sequence.get(number)
            if step is None or step['action']['tool'] in {'ask', 'finish', 'deliver'}:
                raise ValueError('證據需指向本目標先前已執行的實際工具，不能引用清單或宣告。')
            observed = step['observation']
            if step['action']['tool'] == 'update_plan':
                verified = observed.get('scope_reply', {})
                original = {'scope_reply': verified.get('citation')}
                versions = (original, *({**original, 'scope_reply_version': version}
                                        for version in range(2, PARTIAL_DOCUMENT_REPLY_VERSION + 1)))
                if (step['action'].get('args') not in versions
                        or item_id not in verified.get('completed_items', ())):
                    raise ValueError('清單步驟只可引用程式已核對且完成同一項的實際使用者答覆。')
            typed_ranges = requirements.get(item_id, {}).get('ranges')
            typed_batch = typed_ranges and step['action']['tool'] == 'read_ranges'
            if item['state'] == 'done' and (step['action']['tool'] == 'invalid' or observed.get('error') or
                    (step['action']['tool'] == goal_sources.PLAN_SUPPLEMENT and not typed_ranges) or
                    observed.get('status') in ('failed', 'cancelled') or
                    (not typed_batch and any(row['status'] != 'provided' for row in observed.get('requested_ranges', []))) or
                    (observed.get('goal_conditions_sha256') != scope and number not in reused and not carried
                     and retained_replies.get(item_id) != item['evidence_steps']
                     and number != calculation_step)):
                if (item_id in rechecks and step['action']['tool'] == 'calculate_expression'
                        and observed.get('goal_conditions_sha256') != scope):
                    raise ValueError(_calculation_recheck_message(rechecks[item_id]) + '。請按目前條件重算。')
                raise ValueError('完成證據失敗、不完整或來自舊條件；請重新取得適用證據。')
            if item['state'] == 'done' and number in reused:
                old = previous_items.get(item['id'])
                if (not typed_ranges
                        and (old is None or old['state'] != 'done' or item['evidence_steps'] != old['evidence_steps']
                             or any(by_sequence.get(ref, {}).get('action', {}).get('tool') not in RANGE_READERS
                                    for ref in old['evidence_steps']))):
                    raise ValueError('只能沿用同一個原已完成且只以文件查讀為依據的項目。')
                if number not in proofs:
                    proofs[number] = _read_proof(step, scope, sources, typed=bool(typed_ranges))
        if item['state'] == 'done' and item['id'] in requirements:
            match = _match_item_evidence(item, requirements[item['id']], by_sequence, scope, sources, user_sources)
            _check_revised_operands(match, revised_operands)
            matches.append(match)
    if position_checks:
        by_item = {row['id']: row['operands'] for row in matches if 'operands' in row}
        for number, changed in position_checks.items():
            reason = _group_reuse_blocker(requirements[number], requirements[changed], by_item[number], by_item[changed])
            if reason:
                detail = {'id': number, 'changed_item': changed, 'reason': reason}
                raise ValueError(_calculation_recheck_message(detail) + '。請按目前條件重算。')
    if set(proofs) != set(reused):
        raise ValueError('沿用清單只能列本次完成項目實際引用的舊查讀步驟。')
    result = {'status': 'plan_recorded', 'items': json.loads(json.dumps(args['items'])),
            'goal_conditions_sha256': scope,
            'review_note': '助理自行回報進度；工具關聯不等於語意正確或使用者驗收，清單不授予任何新權限。'}
    if proofs:
        result['reused_document_steps'] = [proofs[number] for number in reused]
    if calculation_proofs:
        result['reused_calculations'] = calculation_proofs
    if args.get('verify_delivery') or (previous and previous.get('verify_delivery')):
        result['verify_delivery'] = True
    if requirements:
        result['evidence_requirements'] = [requirements[item['id']] for item in args['items'] if item['id'] in requirements]
        result['evidence_matches'] = matches
    if cancelled:
        result['scope_cancelled'] = cancelled
    return result


def _open_items(plan):
    return [row['id'] for row in plan['items'] if row['state'] not in ('done', 'superseded')]


def _retained_replies_for_scope(plan, steps, scope):
    if not plan:
        return {}
    supplement = next((step for step in reversed(steps) if step['action']['tool'] == goal_sources.PLAN_SUPPLEMENT), None)
    if not supplement or supplement['observation']['goal_conditions_sha256'] != scope:
        return {}
    return {row['id']: [row['reply_step']] for row in supplement['observation'].get('retained_reply_items', ())}


def _select_reads(requirement, excerpts_by_step, limit):
    targets = {(did, line) for did, start, end in map(_range_key, requirement['ranges'])
               for line in range(start, end + 1)}
    coverage = {}
    for number, excerpts in excerpts_by_step.items():
        intervals = [_range_key(row) for row in excerpts]
        coverage[number] = {(did, line) for did, line in targets
                            if any(doc == did and start <= line <= end for doc, start, end in intervals)}
    selected = []
    while targets and coverage and len(selected) < limit:
        number = max(coverage, key=lambda ref: len(coverage[ref] & targets))
        covered = coverage.pop(number) & targets
        if not covered:
            break
        selected.append(number)
        targets -= covered
    return sorted(selected), targets


def _covering_read_steps(requirement, old_excerpts, old_pages):
    limit = 5 if 'expression' in requirement else 6
    steps, missing = _select_reads(requirement, old_excerpts, limit)
    completed, _ = _read_page_progress(missing, old_pages)
    combined = sorted(set(steps).union(*completed.values()))
    return combined if not missing.difference(completed) and len(combined) <= limit else []


def _read_evidence(goal, scope):
    excerpts_by_step, pages, old_steps = {}, {}, set()
    for step in goal['steps']:
        observed = step['observation']
        seen_scope = observed.get('goal_conditions_sha256')
        tool, sequence = step['action']['tool'], step['sequence']
        if tool not in TYPED_READERS:
            continue
        try:
            if seen_scope != scope:
                _old_read_scope(step, scope, typed=True)
            if tool == 'read_line':
                page = document_lines.verified_page(step, goal['sources'])
                pages.setdefault((page['document_id'], page['start_line']), {})[sequence] = page
            else:
                _, excerpts, _ = _verified_read(step, goal['sources'], allow_partial=True)
                excerpts_by_step[sequence] = excerpts
        except ValueError:
            continue
        if seen_scope != scope:
            old_steps.add(sequence)
    # Prefer current observations over equally useful reads that require explicit reuse.
    reads = dict(sorted(excerpts_by_step.items(), key=lambda row: (row[0] in old_steps, row[0])))
    pages = {location: dict(sorted(refs.items(), key=lambda row: (row[0] in old_steps, row[0])))
             for location, refs in pages.items()}
    return reads, pages, old_steps


def _read_page_progress(missing, pages):
    completed, continuing = {}, {}
    for location in sorted(missing & pages.keys()):
        try:
            _, next_start, refs = document_lines.join_pages(pages[location])
        except ValueError:
            continue
        if next_start is None:
            completed[location] = refs
        else:
            continuing[location] = next_start
    return completed, continuing


def _range_rows(locations):
    ranges = []
    for did, line in sorted(locations):
        if (ranges and ranges[-1]['document_id'] == did and ranges[-1]['end_line'] + 1 == line
                and line - ranges[-1]['start_line'] < document_ranges.MAX_LINES):
            ranges[-1]['end_line'] = line
        else:
            ranges.append({'document_id': did, 'start_line': line, 'end_line': line})
    return ranges


def _observed_missing_ranges(steps):
    missing = set()
    for step in steps:
        action, observation = step['action'], step['observation']
        if action['tool'] not in RANGE_READERS or observation.get('error') or observation.get('status') in ('failed', 'cancelled'):
            continue
        requested = [action['args']] if action['tool'] == 'read' else action['args']['ranges']
        observed = observation.get('requested_ranges', ())
        if list(map(_range_key, requested)) == list(map(_range_key, observed)):
            missing.update(_range_key(row) for row in observed if row.get('status') == 'line_out_of_bounds')
    return missing


def _overlaps_range(first, second):
    return first[0] == second[0] and first[1] <= second[2] and second[1] <= first[2]


def supplement_targets(steps):
    plan = latest(steps)
    if not plan or not plan.get('verify_delivery'):
        return []
    active = {row['id'] for row in plan['items'] if row['state'] not in ('done', 'superseded')}
    missing = _observed_missing_ranges(steps)
    return [row for row in plan.get('evidence_requirements', ()) if row['id'] in active
            and any(_overlaps_range(_range_key(request), observed)
                    for request in row.get('ranges', ()) for observed in missing)]


def are_missing_document_questions(steps, parts):
    targets = {row['id']: row['ranges'] for row in supplement_targets(steps)}
    missing = _observed_missing_ranges(steps)
    for part in parts:
        ranges, limited = document_ranges.requested_ranges(part['question'])
        if limited or not ranges:
            return False
        for doc, first, last in map(_range_key, ranges):
            if (not any(did == doc and low <= first <= last <= high for did, low, high in missing)
                    or not all(any(did == doc and low <= first <= last <= high
                                   for did, low, high in map(_range_key, targets.get(number, ())))
                               for number in part['items'])):
                return False
    return True


def _supplement_question_parts(steps):
    from . import decision_table, work_plan_scope

    ask = steps[-1]
    action, observation = ask['action'], ask['observation']
    args = action['args']
    if (action['tool'] != 'ask' or args.keys() - {'question', 'partial_result', 'plan_questions'}
            or decision_table.reference_question_for_steps(steps[:-1])):
        raise ValueError('補件須對應目前待答的缺行問題。')
    if 'plan_questions' not in args:
        return []
    binding = work_plan_scope.bind_questions(args, steps[:-1])
    if observation.get('work_plan_question') != binding:
        raise ValueError('待答分問與工作清單的關聯不一致。')
    return binding['parts']


def can_supplement_question(steps):
    targets = {row['id'] for row in supplement_targets(steps)}
    if not targets:
        return False
    try:
        parts = _supplement_question_parts(steps)
    except ValueError:
        return False
    return not parts or any(set(part['items']) <= targets for part in parts)


def _verify_supplement_question_ranges(selected, parts, missing):
    ranges, limited = document_ranges.requested_ranges(selected['question'])
    items = set(selected['items'])
    if not ranges and any(part != selected and items.intersection(part['items']) for part in parts):
        raise ValueError('同一工作項目的分問須寫明所選分問的文件編號與行號。')
    doc, first, last = _range_key(missing)
    if limited or any(did != doc or not first <= start <= end <= last
                      for did, start, end in map(_range_key, ranges)):
        raise ValueError('補件未涵蓋所選分問要求的全部文件行。')
    return ranges


def supplement_receipt(steps, original, source, replacement_read, *, check_question_ranges=True, retain_replies=False):
    from . import goal_partials, goal_question_withdrawals, work_plan_scope

    missing, replacement, target = goal_sources.plan_mapping(source['content'])
    ask = steps[-1]
    if ask['sequence'] != source['steps_seen']:
        raise ValueError('補件須對應目前待答的缺行問題。')
    parts = _supplement_question_parts(steps)
    if target is None and len(parts) > 1:
        raise ValueError('目前有多個分問，請逐字指定要撤下的分問。')
    selected = next((part for part in parts if part['question'] == target), None) if target is not None else next(iter(parts), None)
    if target is not None and selected is None:
        raise ValueError('請逐字指定目前工作清單的一個完整分問。')
    question_items = set(selected['items']) if selected else set()
    goal_partials.waiting(ask['observation']['question'], ask['action'], ask['observation'])
    did, start, end = _range_key(missing)
    if not any(doc == did and first <= start <= end <= last for doc, first, last in _observed_missing_ranges(steps[:-1])):
        raise ValueError('補件範圍須在曾超出匯入副本行數的查讀範圍內。')
    reads, pages, _ = _read_evidence({'steps': steps[:-1], 'sources': original}, ask['observation']['goal_conditions_sha256'])
    original_lines = {(did, number) for number in range(start, end + 1)}
    missing_citations = {f'[D{did}:L{number}]' for _, number in original_lines}
    _, uncovered = _select_reads({'ranges': [missing]}, reads, len(reads))
    completed, _ = _read_page_progress(uncovered, pages)
    if uncovered.difference(completed) != original_lines:
        raise ValueError('原範圍已有讀到的原行，請只補真正缺少的完整範圍。')
    targets = supplement_targets(steps)
    required = {number for spec in targets for doc, first, last in map(_range_key, spec['ranges']) if doc == did
                for number in range(max(start, first), min(end, last) + 1)}
    if len(required) != end - start + 1:
        raise ValueError('補件的每一行都須對應未完成項目的指定原文。')
    matches = []
    for spec in targets:
        if not any(_overlaps_range(_range_key(row), (did, start, end)) for row in spec['ranges']):
            continue
        if any(operand['citation'] in missing_citations for operand in spec.get('operands', ())):
            raise ValueError('此缺行同時指定了原算式運算元，請依新來源另建階段重算。')
        matches.append(spec['id'])
    if not question_items <= set(matches):
        raise ValueError('補件未涵蓋目前提問所列的工作項目。')
    question_ranges = _verify_supplement_question_ranges(selected, parts, missing) if selected and check_question_ranges else []
    requested, excerpts = replacement_read['requested_ranges'], replacement_read['excerpts']
    if (len(requested) != 1 or _range_key(requested[0]) != _range_key(replacement)
            or requested[0].get('status') != 'provided'):
        raise ValueError('無法完整查讀指定補件，請核對文件與行號。')
    receipt = {'status': 'provided', 'items': matches, 'requested_ranges': requested, 'excerpts': excerpts,
               'ask_sequence': ask['sequence'],
               'ask_sha256': goal_question_withdrawals._receipt(ask['sequence'], ask['action'], ask['observation'])['question_sha256'],
               'guidance_sha256': source['sha256']}
    if question_ranges:
        receipt['question_ranges'] = question_ranges
    if retain_replies and (len(steps) >= 2 and steps[-2]['action']['tool'] == 'update_plan'
            and steps[-2]['action']['args'].get('scope_reply_version') == work_plan_scope.PARTIAL_DOCUMENT_REPLY_VERSION):
        reply = steps[-2]['observation']['scope_reply']
        if reply['successor'] == ask['action']['args']:
            receipt['retained_reply_items'] = [{'id': number, 'reply_step': steps[-2]['sequence'], 'citation': reply['citation']}
                                               for number in reply['completed_items']]
    if remaining := [part for part in parts if part != selected]:
        receipt['successor'] = {'question': '\n'.join(part['question'] for part in remaining), 'plan_questions': remaining}
    return receipt


def supplemented_requirements(steps, receipt, missing, replacement):
    missing_key = _range_key(missing)
    _, start, end = missing_key
    replacement_start = replacement['start_line']
    updated = []
    for spec in latest(steps)['evidence_requirements']:
        if spec['id'] not in receipt['items']:
            updated.append(spec)
            continue
        ranges = {}
        for row in spec['ranges']:
            key = _range_key(row)
            doc, first, last = key
            if not _overlaps_range(key, missing_key):
                ranges[key] = row
                continue
            low, high = max(first, start), min(last, end)
            segments = [(doc, first, low - 1),
                        (replacement['document_id'], replacement_start + low - start, replacement_start + high - start),
                        (doc, high + 1, last)]
            for segment in segments:
                if segment[1] <= segment[2]:
                    ranges[segment] = dict(zip(RANGE_FIELDS, segment, strict=True))
        required = list(ranges.values())
        if len(required) > document_ranges.MAX_RANGES:
            raise ValueError(f'項目 {spec["id"]} 補件後超過八個指定範圍，請整理清單後另建階段。')
        updated.append({**spec, 'ranges': required})
    return updated


def _read_recovery(missing, continuing, steps):
    if not missing:
        return []
    hints = {}
    for step in steps:
        action, observed = step['action'], step['observation']
        if action['tool'] not in RANGE_READERS or observed.get('error') or observed.get('status') in ('failed', 'cancelled'):
            continue
        args = action['args']
        requested = [args] if action['tool'] == 'read' else args['ranges']
        rows = observed.get('requested_ranges', ())
        if list(map(_range_key, rows)) != list(map(_range_key, requested)):
            continue
        for index, row in enumerate(rows):
            did, start, end = _range_key(row)
            status = row.get('status')
            if status in ('line_out_of_bounds', 'document_not_attached'):
                mode = 'source_missing'
            elif status == 'character_limit' and start == end and index == 0:
                mode = 'read_line'
            elif status in ('character_limit', 'range_too_large', 'excerpt_limit'):
                mode = 'split_range'
            else:
                continue
            for location in missing:
                if location[0] == did and start <= location[1] <= end:
                    hints[location] = mode
    hints.update(dict.fromkeys(continuing, 'read_line'))
    recovery = []
    for mode in ('split_range', 'source_missing'):
        recovery.extend({**row, 'action': mode} for row in _range_rows(
            location for location, hint in hints.items() if hint == mode))
    recovery.extend({'document_id': did, 'line_number': line, 'action': 'read_line',
                     'start_char': continuing.get((did, line), 1)}
                    for (did, line), hint in sorted(hints.items()) if hint == 'read_line')
    return recovery


def _document_read_progress(typed_reads, plan, reads, pages, old_steps, saved_steps):
    progress = []
    for item in plan['items']:
        requirement = typed_reads.get(item['id'])
        if not requirement or item['state'] == 'superseded' or (plan['conditions_current'] and item['state'] == 'done'):
            continue
        steps, missing = _select_reads(requirement, reads, len(reads))
        completed, continuing = _read_page_progress(missing, pages)
        missing.difference_update(completed)
        steps = sorted(set(steps).union(*completed.values()))
        row = {'id': item['id'], 'read_steps': steps, 'missing_ranges': _range_rows(missing),
               'reuse_document_steps': [number for number in steps if number in old_steps]}
        if recovery := _read_recovery(missing, continuing, saved_steps):
            row['recovery'] = recovery
        progress.append(row)
    return progress


def _delivery_review(plan, goal, scope, sources_current):
    by_sequence = {step['sequence']: step for step in goal['steps']}
    open_items = _open_items(plan)
    pending_fields = _pending_revision_fields(plan)
    review = {'ready': plan['conditions_current'] and not open_items and not pending_fields, 'open_items': open_items,
              'recheck_items': [], 'reusable_documents': []}
    retained_replies = _retained_replies_for_scope(plan, goal['steps'], scope) if sources_current else {}
    if retained_replies and 'retained_reply_items' in plan:
        review['retained_reply_items'] = plan['retained_reply_items']
    if pending_fields:
        review['pending_revision_fields'] = pending_fields
    typed_reads = {row['id']: row for row in plan.get('evidence_requirements', ()) if 'ranges' in row}
    old_excerpts, old_pages = {}, {}
    if typed_reads and sources_current:
        reads, pages, old_steps = _read_evidence(goal, scope)
        if progress := _document_read_progress(typed_reads, plan, reads, pages, old_steps, goal['steps']):
            review['document_read_progress'] = progress
        old_excerpts = {number: excerpts for number, excerpts in reads.items() if number in old_steps}
        for location, refs in pages.items():
            if old := {number: page for number, page in refs.items() if number in old_steps}:
                old_pages[location] = old
    if plan['conditions_current']:
        return review
    calculations, rechecks = _calculation_reuse_review(
        plan, scope, len(goal['steps']) + 1, goal.get('reply_sources', ()), goal.get('guidance_sources', ())
    ) if sources_current else ({}, {})
    if rechecks:
        review['calculation_rechecks'] = list(rechecks.values())
    if calculations:
        review['reusable_calculations'] = [
            {**calculations[row['id']], 'evidence_steps': row['evidence_steps']}
            for row in plan['items'] if row['id'] in calculations]
    for item in plan['items']:
        typed = typed_reads.get(item['id'])
        if typed and item['state'] != 'superseded':
            refs = _covering_read_steps(typed, old_excerpts, old_pages)
            if refs:
                review['reusable_documents'].append({'id': item['id'], 'evidence_steps': refs})
            if item['state'] == 'done' and (not refs or 'expression' in typed and item['id'] not in calculations):
                review['recheck_items'].append(item['id'])
            continue
        if item['state'] != 'done' or item['id'] in retained_replies:
            continue
        refs = item['evidence_steps']
        reusable = []
        if sources_current:
            for number in refs:
                try:
                    _read_proof(by_sequence[number], scope, goal['sources'])
                except (ValueError, KeyError):
                    continue
                reusable.append(number)
        if reusable != refs and item['id'] not in calculations:
            review['recheck_items'].append(item['id'])
        if reusable and reusable == refs:
            review['reusable_documents'].append({'id': item['id'], 'evidence_steps': reusable})
    return review


def _pending_revision_fields(plan):
    proof = plan.get('scope_reply') or plan.get('scope_change') or {}
    return [row['field'] for row in proof.get('language', {}).get('pending_revisions', ())]


def check_delivery(plan, scope, action, steps):
    if plan and plan['goal_conditions_sha256'] == scope and (pending_fields := _pending_revision_fields(plan)):
        raise ValueError('交付前請確認「' + '、'.join(pending_fields) + '」的待選更正。')
    if plan is None or not plan.get('verify_delivery'):
        return None
    if plan['goal_conditions_sha256'] != scope:
        raise ValueError('交付核對發現工作清單仍依舊條件。請按目前資料更新清單及受影響的完成證據。')
    pending = _open_items(plan)
    if pending:
        raise ValueError('交付核對仍有未完成項目 ' + '、'.join(map(str, pending)) + '。')
    if action['tool'] == 'deliver':
        checked_files = {step['action']['args']['filename'].casefold() for step in steps
                         if step['action']['tool'] == 'write_artifact'
                         and step['observation'].get('goal_conditions_sha256') == scope
                         and type(step['observation'].get('plan_check_step')) is int}
        if any(name.casefold() not in checked_files for name in action['args']['filenames']):
            raise ValueError('交付文件缺少目前條件下的清單核對，請按目前條件重新產檔。')
    return plan['sequence']


def for_goal(goal, current_sources):
    value = latest(goal['steps'])
    if value is not None:
        scope = conditions(goal)
        sources_current = current_sources == goal['sources']
        value['conditions_current'] = value['goal_conditions_sha256'] == scope and sources_current
        if value.get('verify_delivery'):
            value['delivery_review'] = _delivery_review(value, goal, scope, sources_current)
    return value


def describe(value):
    if value is None:
        return '尚未保存工作清單。'
    lines = ['工作清單（助理回報，仍需核對成果）',
             '紀錄時的目標補充與文件版本仍相同。' if value.get('conditions_current') else '歷史清單：條件已變或來自上一階段，完成標記需重新核對。']
    if value.get('reused_document_steps'):
        numbers = '、'.join(str(row['sequence']) for row in value['reused_document_steps'])
        lines.append('沿用同版本文件查讀 · 步驟 ' + numbers)
    if value.get('reused_calculations'):
        numbers = '、'.join(f'{row["id"]} {row["citation"]}' for row in value['reused_calculations'])
        lines.append('依使用者確認沿用算式 · 項目 ' + numbers)
    if 'delivery_review' in value:
        review = value['delivery_review']
        lines.append('工作清單已通過交付核對。' if review['ready'] else '交付前仍需核對目前的工作清單。')
        if pending_fields := review.get('pending_revision_fields'):
            lines.append('仍待確認的更正 · ' + '、'.join(pending_fields))
        lines.extend(_calculation_recheck_message(row) for row in review.get('calculation_rechecks', ()))
        for row in review.get('document_read_progress', ()):
            if row['missing_ranges']:
                ranges = '、'.join(f'D{did}:L{start}-L{end}' if start != end else f'D{did}:L{start}'
                                  for did, start, end in map(_range_key, row['missing_ranges']))
                lines.append(f'項目 {row["id"]} 待查讀 · {ranges}')
            if row['read_steps']:
                numbers = '、'.join(map(str, row['read_steps']))
                lines.append(f'項目 {row["id"]} 已查讀依據 · 步驟 {numbers}')
            for hint in row.get('recovery', ()):
                if hint['action'] == 'read_line':
                    lines.append(f'D{hint["document_id"]}:L{hint["line_number"]} · 從字元 {hint["start_char"]} 續讀')
                else:
                    did, start, end = _range_key(hint)
                    label = '分開或縮小範圍查讀' if hint['action'] == 'split_range' else '匯入副本缺少來源，核對可讀行並補件'
                    lines.append(f'D{did}:L{start}-L{end} · {label}')
    if value.get('evidence_matches'):
        numbers = '、'.join(str(row['id']) for row in value['evidence_matches'])
        lines.append('指定算式或原文已核對 · 項目 ' + numbers)
    for item in value['items']:
        refs = '；依據步驟 ' + '、'.join(map(str, item['evidence_steps'])) if item['evidence_steps'] else ''
        lines.append(f"{item['id']}. {item['task']} — {STATES[item['state']]}{refs}")
        if item['note']:
            lines.append('   ' + item['note'])
    return '\n'.join(lines)


def validate_saved(db, *, replies_present=False, guidance_present=False):
    from . import artifact_names, conversation_history, goal_sources, work_plan_scope

    available = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    delivery_present = {'autonomous_delivery', 'autonomous_delivery_changes'} <= available
    goals = db.execute("SELECT DISTINCT g.id,g.session_id,g.sources_json,g.objective,g.notes,g.status,g.question FROM autonomous_goals g JOIN autonomous_steps s "
                       "ON s.goal_id=g.id WHERE s.action_json LIKE '%update_plan%' "
                       "OR s.observation_json LIKE '%\"plan_check_step\"%' OR s.action_json LIKE '%\"artifact_filename_question\"%' "
                       "OR s.observation_json LIKE '%\"artifact_filename_question\"%' "
                       "OR json_extract(s.action_json, '$.tool')=?", (goal_sources.PLAN_SUPPLEMENT,))
    for gid, sid, source_json, objective, notes, status, question in goals:
        sources = json.loads(source_json)
        rows = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? '
                          'ORDER BY sequence', (gid,)).fetchall()
        has_operands = any('"operands"' in row[1] for row in rows)
        has_scope_reply = any('"scope_reply"' in row[1] for row in rows)
        has_filename_question = any(f'"{artifact_names.ASK_MARK}"' in field for row in rows for field in row[1:])
        has_scope_change = has_scope_reply or has_filename_question or any('"scope_correction"' in row[1] for row in rows)
        replies = (goal_reply_sources.read(db, gid, notes, include_positions=has_scope_reply or has_filename_question)
                   if (has_operands or has_scope_reply or has_filename_question) and replies_present else [])
        steps = [{'sequence': sequence, 'action': json.loads(action), 'observation': json.loads(observation)}
                 for sequence, action, observation in rows]
        has_plan_supplement = any(step['action']['tool'] == goal_sources.PLAN_SUPPLEMENT for step in steps)
        guidance = (goal_guidance_sources.read(db, gid, notes, len(rows))
                    if (has_operands or has_scope_change or has_plan_supplement) and guidance_present else [])
        if status == 'needs_input' and steps[-1]['observation'].get(artifact_names.ASK_MARK) and question != steps[-1]['observation'].get('question'):
            raise ValueError('目前定稿檔名問題與已保存提問不一致。')
        scope_goal = {'id': gid, 'objective': objective, 'notes': notes, 'sources': sources}
        history = {}
        if has_scope_change or has_plan_supplement:
            if 'autonomous_conversation_history' in available and (sha := conversation_history.saved_fingerprint(db, gid)):
                scope_goal['conversation_history'] = {'sha256': sha}
            if goal_sources.has_supplement(steps):
                documents = {row[0]: dict(zip(('id', 'name', 'sha256', 'content'), row)) for row in db.execute(
                    'SELECT id,name,sha256,content FROM documents WHERE session_id=?', (sid,))}
                history = goal_sources.verify_history(steps, sources, documents)
        prior = []
        checked_filename = False
        for index, step in enumerate(steps):
            sequence, action, observation = step['sequence'], step['action'], step['observation']
            args = action['args']
            if action['tool'] == goal_sources.PLAN_SUPPLEMENT:
                successor = steps[index + 1] if index + 1 < len(steps) else None
                pending = question if status == 'needs_input' and index + 2 == len(steps) else None
                _verify_saved_supplement(db, scope_goal, prior, step, guidance, documents, successor, pending)
            if artifact_names.ASK_MARK in args or artifact_names.ASK_MARK in observation:
                if not delivery_present:
                    raise ValueError('定稿檔名問題缺少文件交付紀錄。')
                artifact_names.verify_question(db, {**scope_goal, 'sources': history.get(sequence, sources)},
                                               prior, step, replies, guidance)
            if action['tool'] == 'ask' and ('plan_questions' in args or 'work_plan_question' in observation):
                if ('plan_questions' not in args
                        or observation.get('work_plan_question') != work_plan_scope.bind_questions(args, prior)):
                    raise ValueError('保存的提問與工作清單不一致。')
            if action['tool'] == 'update_plan':
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch('[a-f0-9]{64}', scope):
                    raise ValueError('工作清單缺少有效的條件紀錄。')
                next_step = steps[index + 1] if index + 1 < len(steps) else None
                if 'scope_correction' in args:
                    expected = work_plan_scope.replay(db, args, prior, next_step,
                        {**scope_goal, 'sources': history.get(sequence, sources)}, guidance, replies,
                        withdrawals_present='autonomous_question_withdrawals' in available, delivery_present=delivery_present)
                elif 'scope_reply' in args:
                    expected = work_plan_scope.replay_reply(db, args, prior, next_step,
                        {**scope_goal, 'sources': history.get(sequence, sources)}, replies, guidance, delivery_present=delivery_present)
                else:
                    expected = observe(args, prior, scope, history.get(sequence, sources), objective=objective,
                                       reply_sources=replies, guidance_sources=guidance)
                if any(observation.get(key) != expected.get(key)
                       for key in ('status', 'items', 'reused_document_steps', 'verify_delivery',
                                   'goal_conditions_sha256', 'evidence_requirements', 'evidence_matches',
                                   'reused_calculations', 'scope_change', 'scope_reply', 'scope_cancelled')):
                    raise ValueError('工作清單與保存動作不一致。')
                reply = expected.get('scope_reply', {})
                checked_filename = checked_filename or 'filename_declaration' in args or artifact_names.GUIDANCE_MARK in args or bool(
                    reply.get('filename_choice') or reply.get('filename_pending'))
            if checked_filename and action['tool'] in {'write_artifact', 'deliver'}:
                active_name = artifact_names.requirement({**scope_goal, 'steps': prior,
                    'reply_sources': [row for row in replies if row['ask_sequence'] < sequence],
                    'guidance_sources': [row for row in guidance if row['steps_seen'] < sequence]})
                if active_name:
                    artifact_names.check_action(active_name, action,
                                                pending=artifact_names.awaiting_confirmation(prior, active_name))
            if action['tool'] in DELIVERY_TOOLS or 'plan_check_step' in observation:
                scope = observation.get('goal_conditions_sha256')
                check = check_delivery(latest(prior), scope, action, prior) if action['tool'] in DELIVERY_TOOLS else None
                if ('plan_check_step' in observation or check is not None) and (
                        type(observation.get('plan_check_step')) is not int or observation['plan_check_step'] != check):
                    raise ValueError('交付步驟與保存的清單核對不一致。')
            prior.append(step)


def _verify_saved_supplement(db, goal, prior, step, guidance, documents, successor, pending):
    args, observation = step['action']['args'], step['observation']
    original = args.get('original_sources')
    source = next((row for row in guidance if row['citation'] == args.get('guidance')), None)
    fields = {'guidance', 'original_sources', 'added_sources', 'missing', 'replacement'}
    check_question_ranges = args.get('check_question_ranges') is True
    retain_replies = args.get('retain_replies') is True
    if check_question_ranges:
        fields.add('check_question_ranges')
    if retain_replies:
        fields.add('retain_replies')
    if (source is None or not prior or step['sequence'] != source['steps_seen'] + 1
            or args.keys() != fields or (retain_replies and not check_question_ranges)
            or not goal_sources.valid_source_list(original) or not goal_sources.valid_source_list(args['added_sources'])):
        raise ValueError('清單補件缺少對應的原更正或來源範圍。')
    missing, replacement, _ = goal_sources.plan_mapping(source['content'])
    if (args['missing'] != missing or args['replacement'] != replacement
            or [row['document_id'] for row in args['added_sources']] != [replacement['document_id']]):
        raise ValueError('清單補件與更正原話指定的文件範圍不一致。')
    imported = documents.get(missing['document_id'])
    if (imported is not None and any(row['document_id'] == imported['id'] and row['sha256'] == imported['sha256']
                                     for row in original)
            and missing['start_line'] <= len(imported['content'].splitlines())):
        raise ValueError('保存的清單補件取代了原副本可讀的行。')
    merged = goal_sources.merge_sources(original, args['added_sources'])
    goal_sources.verify_excerpts(observation.get('excerpts'), merged, [replacement])
    _verified_read(step, merged)
    expected = supplement_receipt(prior, original, source, observation, check_question_ranges=check_question_ranges,
                                  retain_replies=retain_replies)
    expected['evidence_requirements'] = supplemented_requirements(prior, expected, missing, replacement)
    notes_end = source['notes_start'] + len(goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at']))
    expected['goal_conditions_sha256'] = conditions({**goal, 'notes': goal['notes'][:notes_end], 'sources': merged})
    if observation != expected:
        raise ValueError('清單補件與保存的查讀或原提問不一致。')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='autonomous_question_withdrawals'").fetchone() is None:
        raise ValueError('清單補件缺少原提問撤下資料表。')
    withdrawal = db.execute('SELECT guidance_number,question_sha256,guidance_sha256 FROM autonomous_question_withdrawals '
                            'WHERE goal_id=? AND ask_sequence=?', (goal['id'], expected['ask_sequence'])).fetchone()
    if withdrawal != (source['number'], expected['ask_sha256'], source['sha256']):
        raise ValueError('清單補件缺少對應的原提問撤下紀錄。')
    if retained := expected.get('successor'):
        if (successor is None or successor['sequence'] != step['sequence'] + 1
                or successor['action']['tool'] != 'ask' or successor['action']['args'] != retained
                or successor['observation'].get('question') != retained['question']
                or successor['observation'].get('goal_conditions_sha256') != expected['goal_conditions_sha256']
                or successor['observation'].get('partial_result') is not None
                or (pending is not None and pending != retained['question'])):
            raise ValueError('補件後保留的分問與原提問不一致。')
