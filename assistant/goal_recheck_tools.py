"""Typed reads and source-bound arithmetic for an explicitly started partial recheck."""

from . import document_ranges, expression_math, goal_sources, operand_evidence
from .documents import check_citations
from .goal_stages import digest


MAX_ACTIONS = 3

GUIDANCE = '''如本次更正須補查已匯入的原行或重算，先各用必要工具，每次只輸出一個 JSON 動作 {"tool":"工具名","args":{...}}，收到結果後再繼續。最多三個工具動作，已有適用依據的欄位沿用。
read_ranges 的 args 為 {"ranges":[{"document_id":2,"start_line":2,"end_line":2}]}。依目標文件目錄，只讀確實缺少且會影響本次暫表的範圍。找不到時保留該欄及依賴它的結果為待查。
calculate_expression 的 args 為 {"expression":{"operation":"product","values":["17","3"]},"operands":[{"path":"/values/0","citation":"[D2:L2]","quote":"17 袋"},{"path":"/values/1","citation":"[D1:L3]","quote":"3 件"}]}。
算式使用數字字串或巢狀的 sum、difference、product、quotient 運算。每個數字須有在來源中可唯一定位的逐字短句，來源限已提供的單一文件行、實際補充、更正或 [目標]。先核對對象、版本和單位，再送入精確計算。
有工具結果時，暫表使用當前已核對的精確值及原數來源。欠缺依據的欄位依原要求標待查。用完三次工具或成果已能更新時，回傳暫表及撤下建議的 JSON 物件。'''


def _ranges(args, scope, excerpts):
    document_ranges.validate_batch(args)
    by_id = {row['document_id']: row for row in scope}
    for row in args['ranges']:
        did, start, end = (row[name] for name in document_ranges.BATCH_FIELDS)
        if did not in by_id:
            raise ValueError('重核只能補讀此目標已選取的匯入文件。')
        if any(check['range_valid'] for check in check_citations(f'[D{did}:L{start}-L{end}]', excerpts)):
            raise ValueError('暫表所指定的完整原行已提供，可直接沿用。')
    return by_id


def _documents(by_id, documents):
    return [doc for doc in documents if doc['id'] in by_id
            and (doc['sha256'], doc['name']) == (by_id[doc['id']]['sha256'], by_id[doc['id']]['name'])]


def _calculate(args, excerpts, user_sources, cancel=None):
    if not isinstance(args, dict) or set(args) != {'expression', 'operands'}:
        raise ValueError('暫表重算須提供算式及每個運算元的來源。')
    expression = {'expression': args['expression']}
    expression_math.validate(expression)
    operand_evidence.validate(args['operands'], args['expression'], None)
    full_lines = [row for row in excerpts if not row.get('partial_line')]
    observed = operand_evidence.bind(args['operands'], args['expression'], full_lines, user_sources)
    return {**expression_math.calculate(expression, cancel), 'operands': observed}


def execute(tool, args, documents, scope, excerpts, user_sources, cancel):
    if tool == 'read_ranges':
        by_id = _ranges(args, scope, excerpts)
        matched = _documents(by_id, documents)
        if not {row['document_id'] for row in args['ranges']} <= {doc['id'] for doc in matched}:
            raise ValueError('重核期間的匯入文件已變更。')
        return document_ranges.read_batch(matched, args)
    if tool == 'calculate_expression':
        return _calculate(args, excerpts, user_sources, cancel)
    raise ValueError('暫表重核只提供指定原行查讀及有逐字來源的精確計算。')


def replay(records, documents, scope, excerpts, user_sources):
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_ACTIONS:
        raise ValueError('保存的暫表重核步數不符。')
    accumulated = list(excerpts)
    read_excerpts, calculations = [], []
    for record in records:
        if not isinstance(record, dict) or set(record) != {'tool', 'args', 'result'}:
            raise ValueError('保存的暫表重核工具格式不符。')
        tool, args, result = record['tool'], record['args'], record['result']
        if tool == 'calculate_expression':
            if digest(result) != digest(_calculate(args, accumulated, user_sources)):
                raise ValueError('暫表重算與精確運算或原始數字不符。')
            calculations.append(record)
        elif tool == 'read_ranges':
            by_id = _ranges(args, scope, accumulated)
            matched = _documents(by_id, documents)
            _verify_read(args, result, scope, matched)
            accumulated.extend(result['excerpts'])
            read_excerpts.extend(result['excerpts'])
        else:
            raise ValueError('保存的暫表重核含不支援的工具。')
    return read_excerpts, calculations


def _verify_read(args, result, scope, documents):
    available = {doc['id']: doc for doc in documents}
    ranges = args['ranges']
    if {row['document_id'] for row in ranges} <= available.keys():
        if digest(result) != digest(document_ranges.read_batch(documents, args)):
            raise ValueError('暫表補讀原行與匯入副本不符。')
        return
    # The underlying copy may have been replaced or detached later. Verify
    # the historical excerpt shape and compare any matching copy still present.
    if (not isinstance(result, dict) or set(result) != {'excerpts', 'requested_ranges'}
            or not isinstance(result['requested_ranges'], list) or len(result['requested_ranges']) != len(ranges)
            or not isinstance(result['excerpts'], list) or len(result['excerpts']) > len(ranges)):
        raise ValueError('保存的暫表補讀紀錄不符。')
    for request, row in zip(ranges, result['requested_ranges'], strict=True):
        if (not isinstance(row, dict) or set(row) != set(request) | {'status', 'explanation'}
                or any(type(row[key]) is not int or row[key] != value for key, value in request.items())
                or not isinstance(row['status'], str) or row['status'] not in document_ranges.STATUS_TEXT
                or row['explanation'] != document_ranges.STATUS_TEXT[row['status']]):
            raise ValueError('暫表補讀範圍或狀態不符。')
    provided = [{name: row[name] for name in document_ranges.BATCH_FIELDS}
                for row in result['requested_ranges'] if row['status'] == 'provided']
    for excerpt in result['excerpts']:
        if not isinstance(excerpt, dict) or not set(document_ranges.BATCH_FIELDS) <= excerpt.keys():
            raise ValueError('暫表缺少補讀範圍。')
        request = {name: excerpt[name] for name in document_ranges.BATCH_FIELDS}
        if any(type(value) is not int for value in request.values()) or request not in provided:
            raise ValueError('暫表補讀超出保存的查讀範圍。')
        goal_sources.verify_excerpts([excerpt], scope, [request])
        did, start, end = (request[name] for name in document_ranges.BATCH_FIELDS)
        lines = excerpt['text'].split('\n')
        if (not 1 <= end - start + 1 <= document_ranges.MAX_LINES or len(lines) != end - start + 1
                or any(not line.startswith(f'L{index}: ') for index, line in enumerate(lines, start))):
            raise ValueError('暫表補讀缺少連續完整原行。')
        if did in available and document_ranges.read_batch([available[did]], {'ranges': [request]})['excerpts'] != [excerpt]:
            raise ValueError('暫表補讀原行與匯入副本不符。')
    for request in provided:
        did, start, end = (request[name] for name in document_ranges.BATCH_FIELDS)
        if not any(check['range_valid'] for check in check_citations(f'[D{did}:L{start}-L{end}]', result['excerpts'])):
            raise ValueError('暫表補讀缺少標示已提供的原行。')
