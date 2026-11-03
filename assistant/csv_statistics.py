"""Optional bounded CSV order statistics with exact values and fixed display."""
import re
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from .tasks import MAX_ROWS, MAX_GROUPS, check_running

MEDIAN_POLICY = {'method':'middle_or_mean_of_middle_pair', 'basis':'retained_row_values',
                 'places':4, 'rounding':'ROUND_HALF_EVEN'}
MEDIAN_FIELDS = ('median', 'median_exact', 'median_rounded', 'returned_median',
                 'returned_median_exact', 'returned_median_rounded', 'median_policy')


def display(value):
    with localcontext(Context(prec=140, rounding=ROUND_HALF_EVEN)):
        rounded = value.quantize(Decimal('0.0001'))
        if rounded == 0:
            rounded = abs(rounded)
        return format(rounded, 'f'), rounded != value


def median_statistics(values, cancel, deadline):
    check_running(cancel, deadline)
    if len(values) > MAX_ROWS:
        raise ValueError('CSV中位數最多50000筆資料。')
    ordered = sorted(values)
    check_running(cancel, deadline)
    count = len(ordered)
    if not count:
        return {'median':None, 'median_exact':None, 'median_rounded':False}
    with localcontext(Context(prec=140, rounding=ROUND_HALF_EVEN)):
        middle = count // 2
        value = ordered[middle] if count % 2 else (ordered[middle-1] + ordered[middle]) / 2
        if value == 0:
            value = abs(value)
        shown, rounded = display(value)
    check_running(cancel, deadline)
    return {'median':shown, 'median_exact':format(value,'f'), 'median_rounded':rounded}


def valid_median_report(report):
    """Check saved shape/policy/display consistency, not truth against source rows."""
    try:
        groups = report['groups']
        if not isinstance(groups,list) or len(groups)>MAX_GROUPS:
            return False
        enabled = report.get('workflow',{}).get('include_median',False)
        if type(enabled) is not bool:
            return False
        if not enabled:
            return not any(k in report for k in MEDIAN_FIELDS) and all(
                isinstance(g,dict) and not any(k in g for k in ('median','median_exact','median_rounded')) for g in groups)
        if (report.get('median_policy') != MEDIAN_POLICY or
                report.get('verification',{}).get('median_basis_matches') is not True):
            return False
        def valid(row, count, prefix=''):
            if type(count) is not int or not 0<=count<=MAX_ROWS:
                return False
            exact,shown,rounded = (row[prefix+k] for k in ('median_exact','median','median_rounded'))
            if type(rounded) is not bool:
                return False
            if not count:
                return exact is None and shown is None and rounded is False
            if not isinstance(exact,str) or len(exact)>140 or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?',exact):
                return False
            return (shown,rounded)==display(Decimal(exact))
        return (valid(report,report['row_count']) and valid(report,report['returned_row_count'],'returned_')
                and 0<=report['returned_row_count']<=report['row_count']
                and all(isinstance(g,dict) and g['count']>0 and valid(g,g['count']) for g in groups))
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):
        return False
