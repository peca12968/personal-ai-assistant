"""A bounded batch of independent civil-month offsets from one fixed anchor."""
from . import date_tools
from .engine import EngineError


MAX_ITEMS = 24
GUIDANCE = '''month_schedule一次核對多個月份日期與星期，args精確為{"date":"2027-01-31","offsets":[1,2,3],"day_policy":"clamp"}。
date為有效YYYY-MM-DD原始基準日；offsets是1到24個明列整數，每個在-120到120內，保留輸入順序與重複。每一筆都獨立從原始date加該偏移，不從上一筆結果續算、不排序、不去重。只要需要相同基準與規則的多期日期，優先一次核對所需清單。
day_policy必填：same_day嚴格保留日數，任何目標月沒有同日就失敗；clamp僅缺同日才改月底；month_end固定每個目標月底。0個月仍套用規則，month_end可能改變原日。日期結果限0001-01-01到9999-12-31，不加工作日順延、地區假日、農曆或時區規則；必要短月規則未明時先問，不擅自取代。
成功回傳anchor_date、offsets、day_policy、count與完整items，依輸入順序含零起算index、months、date、weekday／weekday_iso、target_month_days、day_adjusted及calendar_days；day_adjusted只表示日數和原日起日不同。all_items_provided=true代表本次清單全數返回，不代表使用者已確認排程或外部事件已建立。
先驗證完整輸入格式，再逐項計算；任一筆運算失敗或取消，整次不回傳items或部分成功。運算錯誤文字會指出首先失敗的第幾筆（從1起算）及偏移，不能聲稱其他項目已全數檢查。需要更正時依使用者明確規則重新核對。階段摘要只保留基準／規則／偏移／筆數及schedule_details_omitted，日期明細需本階段重新算，歷史紀錄不增加引用或工具權限。此工具只試算，不設定行事曆、付款、提醒或背景排程。'''


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise EngineError('月份清單計算已停止，未產生部分成功結果。', 'cancelled')


def validate(args):
    if not isinstance(args, dict) or set(args) != {'date', 'offsets', 'day_policy'}:
        raise ValueError('月份清單需明確提供date、offsets及day_policy。')
    offsets = args['offsets']
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= MAX_ITEMS:
        raise ValueError('月份清單需為1到24個偏移。')
    # Validate every entry before invoking any calculation.
    for offset in offsets:
        date_tools.validate({'operation': 'add_months', 'date': args['date'],
                             'months': offset, 'day_policy': args['day_policy']})


def calculate(args, cancel=None):
    check_cancel(cancel)
    validate(args)
    items = []
    for index, offset in enumerate(args['offsets']):
        check_cancel(cancel)
        try:
            result = date_tools.calculate({'operation': 'add_months', 'date': args['date'],
                                           'months': offset, 'day_policy': args['day_policy']})
        except ValueError as exc:
            raise ValueError(f'月份清單第{index + 1}筆（偏移{offset}月）失敗：{exc}') from exc
        check_cancel(cancel)
        items.append({'index': index, 'months': offset, **result['end'],
                      **{key: result[key] for key in ('target_month_days', 'day_adjusted', 'calendar_days')}})
    check_cancel(cancel)
    return {'status': 'month_schedule_calculated', 'anchor_date': args['date'],
            'offsets': list(args['offsets']), 'day_policy': args['day_policy'],
            'count': len(items), 'items': items, 'all_items_provided': True}
