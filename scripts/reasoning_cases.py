"""Original, reproducible reasoning tasks; no human IQ norms or licensed items."""
import itertools
import math
import random
from fractions import Fraction

CATEGORIES = {'logic':'邏輯推論','quantitative':'數量推理','spatial':'空間符號',
              'tracking':'順序與條件追蹤','constraints':'排列約束','induction':'規則歸納'}


def statement(op, values, bits):
    a,b=(bits[i] for i in values)
    return {'implies':not a or b,'iff':a==b,'xor':a!=b,'or':a or b,
            'nand':not(a and b),'and':a and b}[op]


def wording(op, values):
    a,b=('ABCDEF'[i] for i in values)
    return {'implies':f'若{a}為真，則{b}為真','iff':f'{a}與{b}真值相同',
            'xor':f'{a}與{b}恰有一個為真','or':f'{a}與{b}至少一個為真',
            'nand':f'{a}與{b}不能同時為真','and':f'{a}與{b}同時為真'}[op]


def logic_case(rng, index):
    assignments=list(itertools.product((False,True),repeat=6))
    pool=[(op,pair) for op in ('implies','iff','xor','or','nand')
          for pair in itertools.permutations(range(6),2)]
    desired=('必真','必假','不確定')[index%3]
    for attempt in range(10000):
        secret=rng.choice(assignments)
        clauses=rng.sample([p for p in pool if statement(*p,secret)],7)
        solutions=[bits for bits in assignments if all(statement(*p,bits) for p in clauses)]
        if not 2<=len(solutions)<=16:continue
        query=(rng.choice(('and','xor','implies')),tuple(rng.sample(range(6),2)))
        if query in clauses:continue
        outcomes={statement(*query,bits) for bits in solutions}
        answer='必真' if outcomes=={True} else '必假' if outcomes=={False} else '不確定'
        if answer!=desired:continue
        prompt='A至F各為真或假。下列七個前提全部成立：'+ '；'.join(wording(*p) for p in clauses)
        prompt+='。判斷「'+wording(*query)+'」是否必然成立。只答必真、必假或不確定；不確定表示有符合前提的真假兩種情況。'
        return prompt,answer,{'clauses':clauses,'query':query,'solution_count':len(solutions)}
    raise ValueError('無法產生指定邏輯案例。')


def quantitative_case(rng, index):
    if index==0:
        a,b,c=rng.choice([(8,12,18),(9,15,20),(12,18,24)])
        return f'空池由甲管單獨注滿需{a}小時，乙管需{b}小時，丙管單獨排空滿池需{c}小時。三管流速固定且同時開啟，空池多久注滿？回答精確小時數。',str(1/(Fraction(1,a)+Fraction(1,b)-Fraction(1,c))),{'rates':[a,b,c]}
    if index==1:
        markup,discount=rng.choice([(35,18),(42,25),(28,15)])
        value=(1+Fraction(markup,100))*(1-Fraction(discount,100))-1
        return f'物品售價先定為成本加{markup}%，再把這個售價打折，減去{discount}%。無其他費用。最後利潤是成本的幾倍？回答精確比例，不要百分號。',str(value),{'markup':markup,'discount':discount}
    if index==2:
        red,blue=rng.randint(5,9),rng.randint(4,8)
        value=Fraction(math.comb(red,2)*blue,math.comb(red+blue,3))
        return f'袋中{red}顆紅球、{blue}顆藍球，各球等可能。一次不放回取3顆，恰有2紅1藍的機率是多少？',str(value),{'red':red,'blue':blue}
    if index==3:
        n=rng.randint(4,7);win=rng.randint(7,13);loss=rng.randint(2,5)
        value=Fraction(win,n)-Fraction((n-1)*loss,n)
        return f'公平{n}面骰只有一面標星。擲一次，星面淨得{win}點，其他面各淨失{loss}點。平均每次淨得幾點？可為負數，回答精確值。',str(value),{'faces':n,'win':win,'loss':loss}
    if index==4:
        distance,v1,v2,delay=rng.choice([(173,17,23,2),(211,19,29,3),(167,13,31,1)])
        value=Fraction(distance-v1*delay,v1+v2)
        return f'兩地相距{distance}公里。甲以每小時{v1}公里先出發往乙地，{delay}小時後乙以每小時{v2}公里從乙地往甲地，同路線相向且不停。從乙出發起多久相遇？回答精確小時數。',str(value),{'distance':distance,'v1':v1,'v2':v2,'delay':delay}
    n=rng.randint(190,290);a,b,c=4,7,6
    value=sum((i%a==0 or i%b==0) and i%c!=0 for i in range(1,n+1))
    return f'從1到{n}的整數（含端點）中，能被4或7至少一個整除，但不能被6整除的數共有幾個？',str(value),{'n':n}


def transform(grid, operation):
    n=len(grid)
    if operation[0]=='rotate':return [[grid[n-1-c][r] for c in range(n)] for r in range(n)]
    if operation[0]=='horizontal':return [list(reversed(row)) for row in grid]
    if operation[0]=='vertical':return list(reversed(grid))
    result=[row[:] for row in grid];r,c=operation[1:]
    result[r][c]=1-result[r][c]
    return result


def spatial_case(rng,index):
    grid=[[rng.randrange(2) for c in range(5)] for r in range(5)]
    initial=[row[:] for row in grid]
    ops=[('rotate',),('toggle',rng.randrange(5),rng.randrange(5)),
         (rng.choice(('horizontal','vertical')),),('rotate',)]
    if index%2:ops.append(('toggle',rng.randrange(5),rng.randrange(5)))
    instructions=[]
    for op in ops:
        grid=transform(grid,op)
        instructions.append({'rotate':'整格順時針旋轉90度','horizontal':'左右鏡射（每列左右反轉）','vertical':'上下鏡射（列順序反轉）'}.get(op[0])
                            or f'將當時第{op[1]+1}列第{op[2]+1}欄的0/1反轉')
    encode=lambda g:'/'.join(''.join(map(str,row)) for row in g)
    prompt='5×5方格以由上到下的列表示，/分隔各列：'+encode(initial)+'。依序執行：'+'；'.join(instructions)
    prompt+='。座標每一步都以當時方格左上角為第1列第1欄。回答最後5列，以/分隔，只用0與1。'
    return prompt,encode(grid),{'initial':initial,'operations':ops}


def tracking_case(rng,index):
    seq=rng.sample(list('ABCDEFGH'),8);initial=seq[:]
    i,j=sorted(rng.sample(range(8),2));shift=rng.randint(1,4);delete=rng.randrange(8);insert=rng.randrange(7)
    seq.reverse();seq[i],seq[j]=seq[j],seq[i];seq=seq[shift:]+seq[:shift];seq.pop(delete);seq.insert(insert,'K')
    prompt='初始序列為'+','.join(initial)+f'。依序：①反轉整個序列；②交換當時第{i+1}與第{j+1}項；③向左循環移動{shift}項（左端移到右端且順序不變）；④刪除當時第{delete+1}項；⑤在當時第{insert+1}項前插入K。位置從1起算。回答最後序列，使用英文逗號分隔。'
    return prompt,','.join(seq),{'initial':initial,'swap':[i,j],'shift':shift,'delete':delete,'insert':insert}


def order_valid(order,clause):
    op,a,b,n=clause;ia,ib=order.index(a),order.index(b)
    if op=='before':return ia<ib
    if op=='distance':return abs(ia-ib)==n
    if op=='not_adjacent':return abs(ia-ib)!=1
    return ia!=n


def order_words(clause):
    op,a,b,n=clause
    if op=='before':return f'{a}在{b}之前（不一定相鄰）'
    if op=='distance':return f'{a}與{b}的位置編號相差{n}'
    if op=='not_adjacent':return f'{a}與{b}不相鄰'
    return f'{a}不在第{n+1}位'


def constraints_case(rng,index):
    permutations=list(itertools.permutations('ABCDEF'))
    secret=rng.choice(permutations)
    pool=[(op,a,b,n) for a,b in itertools.permutations('ABCDEF',2)
          for op,n in [('before',0),('not_adjacent',0),('distance',1),('distance',2),('distance',3)]]
    pool += [('not_position',a,'A',n) for a in 'ABCDEF' for n in range(6)]
    rng.shuffle(pool);clauses=[];solutions=permutations
    for clause in pool:
        if not order_valid(secret,clause):continue
        reduced=[p for p in solutions if order_valid(p,clause)]
        if len(reduced)==len(solutions):continue
        clauses.append(clause);solutions=reduced
        if len(solutions)==1:break
    if len(solutions)!=1:raise ValueError('排列答案不唯一。')
    prompt='A、B、C、D、E、F各出現一次，排在1至6位。條件全部成立：'+'；'.join(order_words(c) for c in clauses)+'。求唯一完整排列，從第1到第6位，以英文逗號分隔。'
    return prompt,','.join(secret),{'clauses':clauses,'solution_count':1}


def induction_case(rng,index):
    modulus=rng.choice((11,13,17));a,b=rng.sample((-3,-2,-1,1,2,3),2);c=rng.randrange(modulus)
    candidates=list(itertools.product(range(-3,4),range(-3,4),range(modulus)))
    examples=[]
    while len(candidates)>1:
        x,y=rng.randrange(1,21),rng.randrange(1,21);z=(a*x+b*y+c)%modulus
        examples.append([x,y,z])
        candidates=[(aa,bb,cc) for aa,bb,cc in candidates if (aa*x+bb*y+cc)%modulus==z]
        if len(examples)>30:raise ValueError('歸納條件不足。')
    x,y=rng.randrange(21,41),rng.randrange(21,41)
    prompt=f'機器規則固定為z=(a×x+b×y+c)除以{modulus}的非負餘數。a、b各為-3至3的整數，c為0至{modulus-1}的整數。觀測'+ '；'.join(f'({xx},{yy})→{zz}' for xx,yy,zz in examples)
    prompt+=f'。這些條件在指定係數範圍中唯一決定規則。輸入({x},{y})時z是多少？只答整數。'
    return prompt,str((a*x+b*y+c)%modulus),{'modulus':modulus,'coefficients':[a,b,c],'examples':examples,'target':[x,y]}


def make_cases(seed):
    rng=random.Random(seed);cases=[]
    for category,create in [('logic',logic_case),('quantitative',quantitative_case),('spatial',spatial_case),
                            ('tracking',tracking_case),('constraints',constraints_case),('induction',induction_case)]:
        for index in range(6):
            prompt,answer,proof=create(rng,index)
            cases.append({'id':category+'_'+str(index+1),'category':category,'prompt':prompt,'answer':answer,'proof':proof})
    return cases
