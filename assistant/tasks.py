"""Explicit local CSV tasks with frozen plans, exact arithmetic, and evidence."""
import csv
import hashlib
import io
import json
import os
import re
import time
from collections import defaultdict
from decimal import Decimal, localcontext
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .documents import read_document
from .task_failures import failure_guidance

MAX_ROWS = 50000
MAX_GROUPS = 5000
MAX_REPORT_BYTES = 4 * 1024 * 1024


class TaskCancelled(Exception):
    pass


def check_running(cancel, deadline):
    if cancel and cancel.is_set():
        raise TaskCancelled('已停止；沒有完成結果。')
    if time.monotonic() >= deadline:
        raise TimeoutError('工作超過處理時間上限。')


def load_csv(path, cancel=None, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 30
    check_running(cancel, deadline)
    if Path(path).suffix.lower() != '.csv':
        raise ValueError('工作工具目前只接受 CSV。')
    document = read_document(path)
    header, rows = parse_csv(document['content'], cancel, deadline)
    return document, header, rows


def parse_csv(content, cancel=None, deadline=None):
    """Parse a bounded, already imported text snapshot without reopening its path."""
    deadline = deadline if deadline is not None else time.monotonic() + 30
    check_running(cancel, deadline)
    reader = csv.reader(io.StringIO(content, newline=''), strict=True)
    try:
        header = [cell.strip() for cell in next(reader)]
        if not header or any(not cell for cell in header) or len(set(header)) != len(header):
            raise ValueError('CSV 標頭不能空白或重複。')
        if len(header) > 200:
            raise ValueError('CSV 最多 200 欄。')
        if any(len(cell) > 200 for cell in header):
            raise ValueError('每個欄位名稱最多 200 字。')
        rows = []
        for record in reader:
            check_running(cancel, deadline)
            if not record:
                continue
            if len(record) != len(header):
                raise ValueError(f'CSV 第 {reader.line_num} 行附近的欄數與標頭不一致。')
            rows.append(record)
            if len(rows) > MAX_ROWS:
                raise ValueError('CSV 最多 50000 筆資料。')
    except StopIteration:
        raise ValueError('CSV 缺少標頭。') from None
    except csv.Error as exc:
        raise ValueError('CSV 格式無效：' + str(exc)) from None
    return header, rows


def plan_csv(store, sid, path, group_column, value_column):
    document, headers, rows = load_csv(path)
    if group_column not in headers or value_column not in headers:
        raise ValueError('分組欄與數值欄都必須存在於 CSV 標頭。')
    plan = {'version': 1, 'operation': 'csv_group_sum', 'source_path': document['source_path'],
            'source_sha256': document['sha256'], 'source_name': document['name'],
            'group_column': group_column, 'value_column': value_column, 'headers': headers,
            'rows': len(rows), 'preview_rows': rows[:5], 'app_version': __version__,
            'steps': ['核對來源版本與欄位', '按分組欄以十進位數字加總', '核對分組筆數與總和並重查來源', '保存本機結果；另行匯出 JSON 報告'],
            'rules': '標頭去除前後空白；分組值原樣保留，空字串也是一組。數值不可空白，不接受千分位、貨幣符號或科學記號。'}
    return store.task(sid, store.create_task(sid, plan))


def number(value, row):
    value = value.strip()
    if len(value) > 60 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', value):
        raise ValueError(f'第 {row} 筆資料的數值不是支援的十進位數字；未略過或當成零。')
    return Decimal(value)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def basic_report(plan, document, headers, rows, cancel, deadline):
    group_index, value_index = headers.index(plan['group_column']), headers.index(plan['value_column'])
    with localcontext() as context:
        context.prec = 140
        grouped = defaultdict(lambda: [0, Decimal(0)])
        values = []
        for index, row in enumerate(rows, 1):
            check_running(cancel, deadline)
            value = number(row[value_index], index)
            values.append(value)
            group = grouped[row[group_index]]
            group[0] += 1
            group[1] += value
            if len(grouped) > MAX_GROUPS:
                raise ValueError('分組超過 5000 組，請縮小輸入。')
        # Cross-check partition coverage and totals against the parsed input.
        total = sum(values, Decimal(0))
        if sum(item[0] for item in grouped.values()) != len(rows) or sum((item[1] for item in grouped.values()), Decimal(0)) != total:
            raise ValueError('分組結果與來源總和不一致。')
        output = [{'group': key, 'count': count, 'sum': format(value, 'f')} for key, (count, value) in sorted(grouped.items())]
        report = {'operation': plan['operation'], 'source_name': plan['source_name'], 'source_sha256': document['sha256'],
                  'group_column': plan['group_column'], 'value_column': plan['value_column'],
                  'row_count': len(rows), 'group_count': len(output), 'total': format(total, 'f'), 'groups': output,
                  'verification': {'row_count_matches': True, 'group_total_matches': True, 'source_unchanged': True}}
    return report


def execute_task(store, sid, task_id, cancel=None, timeout=30):
    task = store.task(sid, task_id)
    store.claim_task(sid, task_id)
    deadline = time.monotonic() + timeout
    try:
        plan = task['plan']
        if (plan.get('version'), plan.get('operation')) not in {(1, 'csv_group_sum'), (2, 'csv_workflow'), (3, 'csv_workflow')}:
            raise ValueError('不支援的任務計畫版本或操作。')
        document, headers, rows = load_csv(plan['source_path'], cancel, deadline)
        if document['sha256'] != plan['source_sha256'] or headers != plan['headers'] or len(rows) != plan['rows']:
            raise ValueError('來源已在預覽後改變，請建立並確認新計畫。')
        if plan['version'] in (2, 3):
            from .workflows import calculate
            report = calculate(plan, document, headers, rows, cancel, deadline)
        else:
            report = basic_report(plan, document, headers, rows, cancel, deadline)
        check_running(cancel, deadline)
        if read_document(plan['source_path'])['sha256'] != plan['source_sha256']:
            raise ValueError('來源在執行期間改變，結果未標記完成。')
        check_running(cancel, deadline)
        payload = canonical(report)
        if len(payload) > MAX_REPORT_BYTES:
            raise ValueError('結果超過 4 MiB，請縮小輸入。')
        result = {'report': report, 'report_sha256': hashlib.sha256(payload).hexdigest()}
        store.finish_task(sid, task_id, 'succeeded', result)
    except TaskCancelled as exc:
        store.finish_task(sid, task_id, 'cancelled', error=str(exc))
    except Exception as exc:
        store.finish_task(sid, task_id, 'failed', error=str(exc))
    return store.task(sid, task_id)


def export_task(store, sid, task_id, destination):
    task = store.task(sid, task_id)
    if task['status'] != 'succeeded' or not task['result']:
        raise ValueError('只有已驗證完成的任務可以匯出報告。')
    payload = canonical(task['result']['report'])
    expected = task['result']['report_sha256']
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('儲存的報告指紋不符，未匯出。')
    from .csv_statistics import valid_median_report
    from .csv_grouping import valid_grouping_report
    if not valid_median_report(task['result']['report']):
        raise ValueError('儲存的中位數格式或規則不一致，未匯出。')
    if not valid_grouping_report(task['result']['report']):
        raise ValueError('儲存的分組鍵、標籤或規則不一致，未匯出。')
    destination = Path(destination)
    if destination.suffix.lower() != '.json':
        raise ValueError('工作報告請使用 .json 副檔名。')
    with destination.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise ValueError('匯出檔案驗證失敗。')
    record = {'path': str(destination.resolve()), 'sha256': expected, 'verified': True,
              'verified_at': datetime.now(timezone.utc).isoformat()}
    try:
        store.record_task_export(sid, task_id, record)
    except Exception as exc:
        raise ValueError('檔案已建立並驗證，但匯出紀錄未保存：' + str(exc)) from exc
    return record


def task_context(store, sid, question):
    if not re.search(r'工作|任務|彙總|加總|平均|中位數|報告|結果|完成|匯出|CSV|\b(?:task|sum|mean|median|average|summary|result|report|export|finished|done)\b', question, re.I):
        return []
    summaries = []
    for row in store.tasks(sid)[:3]:
        task = store.task(sid, row['id'])
        item = {'task_id': task['id'], 'status': task['status'], 'source_name': task['plan']['source_name'],
                'operation': task['plan']['operation'], 'finished_at': task['finished_at']}
        guidance = failure_guidance(task)
        if guidance:
            item['failure_guidance'] = guidance
        if task['status'] == 'succeeded' and task['result']:
            report = task['result']['report']
            from .csv_statistics import MEDIAN_FIELDS, valid_median_report
            from .csv_grouping import valid_grouping_report, preview
            mean_fields = ('mean', 'mean_rounded', 'returned_mean', 'returned_mean_rounded', 'returned_row_count', 'mean_policy')
            invalid_mean = task['plan'].get('version') == 3 and (
                not all(key in report for key in mean_fields) or
                report.get('verification', {}).get('mean_basis_matches') is not True or
                report.get('mean_policy') != task['plan'].get('mean_policy'))
            if (hashlib.sha256(canonical(report)).hexdigest() != task['result']['report_sha256'] or
                invalid_mean or not valid_median_report(report) or not valid_grouping_report(report) or not all(report.get('verification', {}).get(key) is True for key in ('row_count_matches', 'group_total_matches', 'source_unchanged'))):
                item['status'] = 'unverified'
            else:
                item.update({key: report[key] for key in ('row_count', 'group_count', 'total', 'group_column', 'value_column', 'source_sha256', 'verification')})
                if report['operation'] == 'csv_workflow':
                    item.update({key: report[key] for key in ('workflow', 'filter_stages', 'input_row_count', 'excluded_row_count',
                                'returned_group_count', 'selection_limited', 'returned_total')})
                    if task['plan'].get('version') == 3:
                        item.update({key: report[key] for key in mean_fields})
                        if report['workflow'].get('include_median'):
                            item.update({key:report[key] for key in MEDIAN_FIELDS})
                if 'group_columns' in report:
                    item.update({key: report[key] for key in ('group_columns', 'grouping_policy')})
                item['groups_preview'] = [preview(group, label_flag='label_truncated') for group in report['groups'][:10]]
                item['groups_truncated'] = len(report['groups']) > 10
                item['report_sha256'] = task['result']['report_sha256']
                item['verified_exports'] = [{'file_name': Path(record['path']).name, 'sha256': record['sha256'], 'verified_at': record['verified_at']}
                                             for record in task['result'].get('exports', [])[-3:]]
        summaries.append(item)
    return summaries
