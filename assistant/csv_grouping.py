"""Exact ordered CSV pair keys and bounded display, without delimiter merging."""
import json

from .tasks import MAX_GROUPS

GROUPING_POLICY = {'key': 'ordered_exact_text_pair', 'label': 'json_array',
                   'tie_order': 'columns_ascending'}


def columns(spec):
    return [spec['group_column']] + ([spec['secondary_group_column']] if 'secondary_group_column' in spec else [])


def label(values):
    return json.dumps(values, ensure_ascii=False, separators=(',', ':'))


def result_key(row):
    return tuple(row['group_values']) if 'group_values' in row else row['group']


def preview(row, *, label_flag='group_truncated'):
    result = {**row, 'group': row['group'][:200], label_flag: len(row['group']) > 200}
    if 'group_values' in row:
        result['group_values'] = [value[:200] for value in row['group_values']]
        result['group_values_truncated'] = [len(value) > 200 for value in row['group_values']]
    return result


def valid_grouping_report(report):
    """Validate saved key/label/policy consistency, not source truth or identity."""
    try:
        groups = report['groups']
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            return False
        workflow = report.get('workflow', {})
        if not isinstance(workflow, dict):
            return False
        if 'secondary_group_column' not in workflow:
            return not any(key in report for key in ('group_columns', 'grouping_policy')) and all(
                isinstance(group, dict) and 'group_values' not in group for group in groups)
        primary, secondary = workflow['group_column'], workflow['secondary_group_column']
        if (not isinstance(primary, str) or not isinstance(secondary, str) or primary == secondary
                or report.get('group_column') != primary or report.get('group_columns') != [primary, secondary]
                or report.get('grouping_policy') != GROUPING_POLICY):
            return False
        seen = set()
        for group in groups:
            values = group['group_values']
            if not isinstance(values, list) or len(values) != 2 or not all(isinstance(value, str) for value in values):
                return False
            key = tuple(values)
            if key in seen or group['group'] != label(values):
                return False
            seen.add(key)
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
