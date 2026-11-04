"""Reference dimensions from a rigid cube and one doorway with unmeasured width."""
import re
from decimal import Decimal

NUMBER = r'(?:[1-9][0-9]{0,4}(?:\.[0-9]{1,3})?|0\.[0-9]{1,3})'
UNIT = r'(?:毫米|公釐|公分|公尺)'
LENGTH = rf'({NUMBER})\s*({UNIT})'
SIZE = rf'{NUMBER}\s*{UNIT}'
SIDE = re.compile(r'(?:每邊|邊長)\s*' + LENGTH + r'(?=[，、,。；;])')
HEIGHT = re.compile(r'(?:門高|淨高)\s*' + LENGTH + r'(?=[，、,。；;])')
WIDTH_UNKNOWN = re.compile(r'(?:寬度|淨寬)(?:尚未量測|還沒量|沒有紀錄|不詳)')
MEASURED = re.compile(r'(?:剛量了[，,]\s*|實測)?(?:門的|門口|門)淨寬(?:是|量到|為)?\s*' + LENGTH +
                      r'(?:[。；]\s*(?:現在)?能通過嗎[？?](?:\s*只回「[^「」]{1,12}」、「[^「」]{1,12}」或「[^「」]{1,12}」[。！]?)?|[。！]?)$')
SCALE = {'毫米':1, '公釐':1, '公分':10, '公尺':1000}


# Verify only complete single-door scenes with a recognized access condition.
SPACE = r'(?:庫房|倉庫|房間|展廳)'
SCENE = re.compile(
    rf'(?:(?:搬運員要把|要把)一個硬質正方體(?:箱子|木箱)送進{SPACE}。'
    rf'(?:箱子|木箱)每邊\s*{SIZE}[，,]\s*不可拆(?:卸|箱)'
    rf'|一個不可拆的硬質正方體(?:木箱|箱子)[，,]\s*邊長\s*{SIZE}[，,]\s*要原樣搬進{SPACE})。'
    rf'唯一的門(?:淨高|高)\s*{SIZE}[，、,]\s*(?:淨寬|寬度)(?:尚未量測|還沒量|沒有紀錄|不詳)[，,]\s*'
    r'(?:(?P<closed>門已上鎖[，,](?:現在|目前)無法打開|門已封閉[，,](?:現在|目前)禁止通行)|兩側空曠平坦且無其他障礙|門前後空曠)。'
    r'(?:按現有條件[，,])?能否(?:把物件原樣運到目的地|把箱子搬進去|搬入|通過)[？?]'
    r'(?P<format>只回「可行」、「不可行」或「資料不足」。?|(?:請)?只問我還要量哪個尺寸[。？?]?)$')


def proof_format(question):
    scene = SCENE.fullmatch(question)
    return scene['format'] if scene else None


def measured_choice(question):
    return MEASURED.fullmatch(question) and question.rstrip('。！').endswith('只回「可行」、「不可行」或「資料不足」')


def verified_answer(context, raw):
    reference = opening_reference(context)
    requested = proof_format(context['current_request'])
    followup = measured_choice(context['current_request']) and context.get('clearance_source')
    if not (requested or followup) or not reference or reference != context.get('door_measurements'):
        raise ValueError('無法核對門口的通行條件。')
    if '阻礙原因' in reference:
        answer = '不可行' if followup or requested.startswith('只回') else '不用再量尺寸。' + reference['阻礙原因'] + '。'
        schema = 'door_passage_blocked_v1'
    elif followup:
        width, side = Decimal(reference['本輪淨寬毫米']), Decimal(reference['箱邊毫米'])
        if width == side:
            raise ValueError('門寬剛好等於箱邊，題目未提供搬運所需間隙。')
        answer = '可行' if width > side else '不可行'
        schema = 'door_measured_passage_v1'
    else:
        if '尚缺尺寸' not in reference:
            raise ValueError('找不到待量的尺寸。')
        answer = '資料不足' if requested.startswith('只回') else '門的淨寬是多少？'
        schema = 'door_width_uncertainty_v1'
    return answer, dict(schema=schema, model_called=True, raw_answer=raw,
                        delivered=answer, raw_exact=(answer == raw))


def millimetres(match):
    return Decimal(match[1]) * SCALE[match[2]]


def opening_reference(context):
    q = context['current_request']
    if len(q) > 1000 or context.get('documents', {}).get('catalog'):
        return None
    m = MEASURED.fullmatch(q)
    text = q
    source_id = None
    if m:
        history = context.get('recent_conversation', [])
        if len(history) < 2:
            return None
        user, answer = history[-2:]
        tail = context.get('conversation_tail') or {}
        if (user['role'] != 'user' or user['truncated'] or answer['role'] != 'assistant' or answer.get('truncated')
                or tail.get('message_id') != answer['message_id']):
            return None
        text = user['content']
        source_id = user['message_id']
    if (len(text) > 1000 or not re.search(r'硬質正方體(?:木箱|箱子|箱)', text) or '唯一的門' not in text
            or not re.search(r'能否[^。？?]{0,30}(?:運|搬|送|通過)', text)
            or not re.search(r'不可拆|不能拆', text)
            or re.search(r'如果|假設|估計|大約|約有|至少|至多|最少|最多|舉例|翻譯|改寫|聽說|據說|[「『][^」』]*[？?]', text)):
        return None
    sides, heights = list(SIDE.finditer(text)), list(HEIGHT.finditer(text))
    unknown = list(WIDTH_UNKNOWN.finditer(text))
    if len(re.findall(r'淨寬|寬度',text)) != 1 or len(sides) != 1 or len(heights) != 1 or len(unknown) != 1:
        return None
    side, height = millimetres(sides[0]), millimetres(heights[0])
    scene = SCENE.fullmatch(text)
    if min(side, height) <= 0 or (m and millimetres(m) <= 0):
        return None
    result = dict(箱邊原文=sides[0][0], 門高原文=heights[0][0], 原門寬紀錄=unknown[0][0],
                  箱邊毫米=format(side, 'f'), 門高毫米=format(height, 'f'))
    barrier = None
    if scene and scene['closed']:
        barrier = ('唯一的門目前無法打開' if '無法打開' in scene['closed'] else '唯一的門目前禁止通行')
        result['通行限制原文'] = scene['closed']
    elif scene and height < side:
        barrier = '門高小於箱邊，即使轉動，箱子的內接球也無法通過'
    if barrier:
        result['阻礙原因'] = barrier
        if m:
            result.update(來源使用者訊息=source_id, 本輪門寬原文=m[1]+m[2], 本輪淨寬毫米=format(millimetres(m), 'f'))
        return result
    if height <= side or re.search(r'上鎖|封閉|禁止通行', text):
        return None
    if m:
        width = millimetres(m)
        if width == side:
            comparison = '淨寬等於箱邊；題目沒有提供實際搬運的間隙。'
        elif width < side:
            comparison = '實測淨寬小於箱邊，正方體內接球的直徑也大於門寬。'
        else:
            comparison = '實測淨寬與門高均大於箱邊；這兩個尺寸容得下直放箱子。'
        result.update(來源使用者訊息=source_id, 本輪門寬原文=m[1]+m[2], 本輪淨寬毫米=format(width, 'f'),
                      本輪尺寸比較=comparison)
    else:
        widths = [format(side / 2, 'f'), format(side * 2, 'f')]
        result.update(尚缺尺寸='門的淨寬', 假設示例非實測=widths,
                      假設一=f'若淨寬是 {widths[0]} 毫米，它小於箱邊，箱子過不去。',
                      假設二=f'若淨寬是 {widths[1]} 毫米，淨寬和已知門高都大於箱邊，箱子尺寸能通過。',
                      尺寸核對='兩個假設都符合未量門寬的敘述，卻有相反的尺寸結果。要分辨是哪一種，必須取得實測淨寬。再核對題中有無其他障礙。')
    return result
