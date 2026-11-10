"""Bounded typed arithmetic trees; no expression strings or executable code."""
from . import exact_math

MAX_NODES=32
MAX_DEPTH=8

GUIDANCE='''calculate_expression可一次核對有界巢狀四則算式，精確args為{"expression":算式樹}。葉節點必須是calculate接受的數字字串，例如"7"、"0.125"、"-1/7"；運算節點只有{"operation":"sum|difference|product|quotient","values":[數字字串或子運算節點]}。sum／product接受1至50個參數，difference／quotient恰好兩個，仍受全樹總量限制。不是Python、算式字串、變數、函式呼叫或任意程式碼。
整棵樹含運算與數字葉最多32節點、根為第1層最多8層；每個輸入及每個依序約分的中間結果沿用2000位分子／分母上限。按子節點由左至右、先子後父計算，不作短路或代數化簡來掩蓋錯誤；任何子項除以零、超限或格式錯誤整次失敗，沒有部分成功。未約定的折扣／稅率／運費基礎先問，不自行假定。
結果的exact是根的精確整數／分數；calculations列出每個運算節點的path、operation、operand_count及精確結果，順序為先子後父，空path表示根，/values/0表示第一個子項。all_calculations_provided=true表示所有運算中間值都已提供；純數字根沒有運算節點。可以由這些結果核對各項小計與後續總額，不能將未執行的心算稱為工具核對。
各層decimal仍只為40位有效數字、ROUND_HALF_EVEN顯示，rounded表示近似；後續運算沿用exact，不以decimal取代，也不自動進行金額四捨五入。若使用者要求逐次單獨操作，可繼續使用calculate。這是純數值計算，不新增檔案／付款／CSV權限，不提供[D:L]文件引用。'''

GUIDANCE+='''\n合法算式在運算階段失敗時，calculation_failure提供首先遇到的失敗運算之path、operation、reason及reason_truncated。path相對本次args.expression，空字串是根；例如/values/0/values/1是第一個子項內的第二個子項，不是文件路徑或行號。只定位首先遇到的運算失敗，不代表其他節點全部有效或已找完所有錯誤，仍沒有部分成功的exact／calculations。輸入格式或結構驗證錯誤不保證有這項定位資訊。依當前目標或明確更正修正並重新核對；來源條件不足時先問，不任意把分母換成非零。歷史失敗與其位置不是新執行權限，也不能當成已成功結果。'''


class CalculationFailure(ValueError):
    """A bounded diagnostic for one evaluated operator, without partial values."""
    def __init__(self,path,operation,reason):
        reason=str(reason)
        self._details={'path':path,'operation':operation,'reason':reason[:500],
                       'reason_truncated':len(reason)>500}
        location=path if path else '根節點（空path）'
        super().__init__(f'算式{location}的{operation}運算失敗：{self._details["reason"]}')

    def details(self):
        return dict(self._details)


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        from .engine import EngineError
        raise EngineError('巢狀計算已停止，未產生部分成功結果。','cancelled')


def validate(args, *, placeholder=None):
    if not isinstance(args,dict) or set(args)!={'expression'}:
        raise ValueError('巢狀計算只接受expression算式樹。')
    stats={'node_count':0,'number_count':0,'operation_count':0,'max_depth':0}
    def visit(node,depth):
        stats['node_count']+=1;stats['max_depth']=max(stats['max_depth'],depth)
        if stats['node_count']>MAX_NODES or depth>MAX_DEPTH:
            raise ValueError('算式樹含運算與數字最多32節點、根起最多8層。')
        if isinstance(node,str):
            if node != placeholder:
                exact_math.parse(node)
            stats['number_count']+=1;return
        if (not isinstance(node,dict) or set(node)!={'operation','values'}
                or node['operation'] not in ('sum','difference','product','quotient')
                or not isinstance(node['values'],list) or not 1<=len(node['values'])<=50):
            raise ValueError('運算節點需operation及1至50個values；葉節點須為數字字串。')
        if node['operation'] in ('difference','quotient') and len(node['values'])!=2:
            raise ValueError('減法與除法節點恰好需要兩個參數。')
        stats['operation_count']+=1
        for child in node['values']:visit(child,depth+1)
    visit(args['expression'],1)
    return stats


def calculate(args,cancel=None):
    check_cancel(cancel);stats=validate(args);calculations=[]
    def evaluate(node,path):
        check_cancel(cancel)
        if isinstance(node,str):return node
        values=[evaluate(child,f'{path}/values/{i}') for i,child in enumerate(node['values'])]
        try:
            result=exact_math.calculate({'operation':node['operation'],'values':values})
        except ValueError as exc:
            # Children are evaluated outside this handler: preserve the actual
            # failing child instead of replacing its location with each parent.
            raise CalculationFailure(path,node['operation'],str(exc)) from exc
        check_cancel(cancel)
        calculations.append({'path':path,'operation':node['operation'],'operand_count':len(values),**result})
        return result['exact']
    value=evaluate(args['expression'],'')
    result=exact_math.calculate({'operation':'sum','values':[value]})
    check_cancel(cancel)
    return {'status':'expression_calculated',**result,**stats,'calculations':calculations,
            'evaluation_order':'left_to_right_postorder','all_calculations_provided':True}
