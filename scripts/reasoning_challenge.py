"""A separate, harder-designed task family; difficulty is not a human IQ scale."""
import itertools
import math
import random
from fractions import Fraction

from scripts.reasoning_cases import statement, order_valid, order_words


def logic(rng, index):
    assignments = list(itertools.product((False, True), repeat=8))
    pool = [(op, pair) for op in ('implies', 'iff', 'xor', 'or', 'nand')
            for pair in itertools.permutations(range(8), 2)]
    desired = ('必真', '必假', '不確定')[index % 3]
    def words(clause):
        op, pair = clause
        a, b = ('ABCDEFGH'[i] for i in pair)
        return {'implies': f'若{a}真則{b}真', 'iff': f'{a}與{b}真值相同',
                'xor': f'{a}與{b}恰一個真', 'or': f'{a}與{b}至少一個真',
                'nand': f'{a}與{b}不能同時真', 'and': f'{a}與{b}同時真'}[op]
    for _ in range(10000):
        secret = rng.choice(assignments)
        clauses = rng.sample([p for p in pool if statement(*p, secret)], 12)
        solutions = [bits for bits in assignments if all(statement(*p, bits) for p in clauses)]
        if not 2 <= len(solutions) <= 24:
            continue
        query = (rng.choice(('and', 'xor', 'implies')), tuple(rng.sample(range(8), 2)))
        if query in clauses:
            continue
        outcomes = {statement(*query, bits) for bits in solutions}
        answer = '必真' if outcomes == {True} else '必假' if outcomes == {False} else '不確定'
        if answer == desired:
            prompt = 'A至H各為真或假。十二個前提全部成立：' + '；'.join(words(p) for p in clauses)
            prompt += '。判斷「' + words(query) + '」：只答必真、必假或不確定；不確定表示符合前提的情況中，該命題有真也有假。'
            return prompt, answer, {'clauses': clauses, 'query': query, 'solution_count': len(solutions)}
    raise ValueError('無法產生挑戰邏輯題。')


def quantitative(rng, index):
    if index == 0:
        red, blue, green = rng.randint(7, 11), rng.randint(4, 7), rng.randint(3, 6)
        n = red + blue + green
        denominator = math.comb(n, 5) - math.comb(n-blue, 5) - math.comb(n-green, 5) + math.comb(red, 5)
        answer = Fraction(math.comb(red, 3) * blue * green, denominator)
        return (f'袋有{red}紅、{blue}藍、{green}綠球，各球等可能。不放回取5球。已知至少1藍且至少1綠，在此條件下恰有3紅的機率是多少？',
                str(answer), {'kind': 'conditional_balls', 'counts': [red, blue, green], 'draw': 5})
    if index == 1:
        volume = rng.choice((120, 150, 180))
        initial = rng.randint(30, 50)
        withdrawals = rng.sample(range(11, 39), 3)
        answer = Fraction(initial)
        for amount in withdrawals:
            answer *= Fraction(volume-amount, volume)
        prompt = f'槽內{volume}公升溶液含鹽{initial}克。依序抽走{withdrawals[0]}、{withdrawals[1]}、{withdrawals[2]}公升；每次抽前完全混勻，抽後立即補同量純水。沒有損耗或其他鹽來源。三次完成後槽內剩幾克鹽？'
        return prompt, str(answer), {'kind': 'dilution', 'volume': volume, 'initial': initial, 'withdrawals': withdrawals}
    if index == 2:
        a, b, leak = rng.choice(((18, 24, 72), (20, 30, 90), (24, 36, 108)))
        first, together = rng.randint(1, 3), rng.randint(2, 4)
        filled = Fraction(first, a) + together * (Fraction(1, a) + Fraction(1, b) - Fraction(1, leak))
        answer = first + together + (1-filled) / (Fraction(1, b)-Fraction(1, leak))
        prompt = f'空池甲管單獨注滿要{a}小時，乙管要{b}小時，漏水管單獨排空滿池要{leak}小時，流速固定。先只開甲{first}小時；再同開甲、乙與漏水管{together}小時；之後關甲，乙與漏水管持續開。從最初開始共多久注滿？'
        return prompt, str(answer), {'kind': 'staged_rates', 'rates': [a, b, leak], 'first': first, 'together': together}
    if index == 3:
        n = rng.choice((6, 8, 10)); threshold = n + rng.randint(1, 3); fee = rng.randint(5, 15)
        valid = [(a, b) for a in range(1, n+1) for b in range(1, n+1) if a != b and a+b >= threshold]
        answer = sum(Fraction(a*b-fee, len(valid)) for a, b in valid)
        prompt = f'兩顆獨立公平{n}面骰，各面1至{n}。若兩數相同或總和小於{threshold}，兩顆一起重擲，直到不相同且總和至少{threshold}。重擲不收費；成功時淨得兩數乘積減{fee}點。最終平均淨得幾點？'
        return prompt, str(answer), {'kind': 'conditional_dice', 'faces': n, 'threshold': threshold, 'fee': fee}
    if index == 4:
        n = rng.randint(1800, 2800); divisors = [3, 4, 5, 7]
        answer = sum(sum(i % d == 0 for d in divisors) == 2 and i % 11 != 0 for i in range(1, n+1))
        return (f'1至{n}（含端點）中，能被3、4、5、7這四個數中的恰好兩個整除，同時不能被11整除的整數有幾個？',
                str(answer), {'kind': 'exact_divisibility', 'n': n, 'divisors': divisors, 'exclude': 11})
    costs = rng.sample(range(5, 23), 10); values = rng.sample(range(13, 53), 10); budget = 66
    candidates = []
    for chosen in itertools.combinations(range(10), 4):
        if (1 in chosen and 5 in chosen) or (7 in chosen and 2 not in chosen):
            continue
        cost = sum(costs[i] for i in chosen)
        if cost <= budget:
            candidates.append((sum(values[i] for i in chosen), chosen))
    best = max(value for value, _ in candidates)
    items = '；'.join(f'{name}成本{cost}/收益{value}' for name, cost, value in zip('ABCDEFGHIJ', costs, values))
    prompt = f'十個專案最多各選一次：{items}。恰選4個，總成本不超過{budget}；B與F不可同選；若選H必須也選C。收益可直接相加。可得最高總收益是多少？只答收益數值。'
    return prompt, str(best), {'kind': 'selection', 'costs': costs, 'values': values, 'budget': budget,
                               'best_sets': [list(chosen) for value, chosen in candidates if value == best]}


def spatial(rng, index):
    n = 7
    grid = [[rng.randrange(2) for _ in range(n)] for _ in range(n)]
    initial = [row[:] for row in grid]
    ops = [('rotate',), ('toggle', rng.randrange(n), rng.randrange(n)), ('transpose',),
           ('row_shift', rng.randrange(n), rng.randint(1, 5)), ('vertical',),
           ('swap_rows', *rng.sample(range(n), 2)), ('rotate',),
           ('toggle', rng.randrange(n), rng.randrange(n)), ('horizontal',), ('transpose',)]
    instructions = []
    for op in ops:
        if op[0] == 'rotate':
            grid = [[grid[n-1-c][r] for c in range(n)] for r in range(n)]
            instructions.append('整格順時針旋轉90度')
        elif op[0] == 'transpose':
            grid = [list(row) for row in zip(*grid)]
            instructions.append('沿左上到右下主對角線轉置（列欄互換）')
        elif op[0] == 'horizontal':
            grid = [row[::-1] for row in grid]; instructions.append('整格左右鏡射')
        elif op[0] == 'vertical':
            grid = grid[::-1]; instructions.append('整格上下鏡射')
        elif op[0] == 'swap_rows':
            _, a, b = op; grid[a], grid[b] = grid[b], grid[a]
            instructions.append(f'交換第{a+1}與第{b+1}列')
        elif op[0] == 'row_shift':
            _, row, shift = op; grid[row] = grid[row][shift:] + grid[row][:shift]
            instructions.append(f'只將第{row+1}列向左循環移動{shift}格')
        else:
            _, row, column = op; grid[row][column] = 1-grid[row][column]
            instructions.append(f'反轉第{row+1}列第{column+1}欄的0/1')
    encode = lambda g: '/'.join(''.join(map(str, row)) for row in g)
    prompt = '7×7方格由上到下各列為：' + encode(initial) + '。依序：' + '；'.join(instructions)
    prompt += '。每一步位置都依當時方格，左上為第1列第1欄。答最後7列，以/分隔，只用0與1。'
    return prompt, encode(grid), {'initial': initial, 'operations': ops}


def tracking(rng, index):
    seq = rng.sample(list('ABCDEFGHIJKL'), 12); initial = seq[:]
    shift = rng.randint(2, 5); delete = rng.randrange(12); insert = rng.randrange(11)
    operations = [('left', shift), ('reverse_range', 1, 10), ('swap_letters', 'A', 'H'),
                  ('if_before', 'C', 'F'), ('delete', delete), ('insert', insert, 'M'),
                  ('reverse',), ('if_first',), ('reverse_range', 5, 12)]
    states = []
    for op in operations:
        if op[0] == 'left': seq = seq[op[1]:] + seq[:op[1]]
        elif op[0] == 'reverse_range': seq[op[1]:op[2]] = reversed(seq[op[1]:op[2]])
        elif op[0] == 'swap_letters':
            a, b = seq.index(op[1]), seq.index(op[2]); seq[a], seq[b] = seq[b], seq[a]
        elif op[0] == 'if_before':
            if seq.index('C') < seq.index('F'): seq[0], seq[11] = seq[11], seq[0]
            else: seq = seq[4:] + seq[:4]
        elif op[0] == 'delete': seq.pop(op[1])
        elif op[0] == 'insert': seq.insert(op[1], op[2])
        elif op[0] == 'reverse': seq.reverse()
        elif seq[0] in 'ABCDEF': seq[2:9] = reversed(seq[2:9])
        else: seq[1], seq[9] = seq[9], seq[1]
        states.append(seq[:])
    prompt = '初始序列' + ','.join(initial)
    prompt += f'。依序①左循環移{shift}項；②反轉第2至10項的區段（含端點）；③交換字母A與H的位置；'
    prompt += '④若當時C在F前面，交換第1與12項，否則左循環移4項；'
    prompt += f'⑤刪除第{delete+1}項；⑥在當時第{insert+1}項前插入M；⑦反轉全部；'
    prompt += '⑧若當時第一項是A至F之一，反轉第3至9項區段，否則交換第2與10項；⑨反轉第6至12項區段。'
    prompt += '位置都從1起，條件以執行當步前的序列判斷；左循環為左端項移至右端且順序不變。答最後序列，以英文逗號分隔。'
    return prompt, ','.join(seq), {'initial': initial, 'operations': operations, 'states': states}


def constraints(rng, index):
    permutations = list(itertools.permutations('ABCDEFGH')); secret = rng.choice(permutations)
    pool = [(op, a, b, n) for a, b in itertools.permutations('ABCDEFGH', 2)
            for op, n in [('before', 0), ('not_adjacent', 0), ('distance', 1), ('distance', 2), ('distance', 3), ('distance', 4)]]
    pool += [('not_position', a, 'A', n) for a in 'ABCDEFGH' for n in range(8)]
    rng.shuffle(pool); clauses = []; solutions = permutations
    for clause in pool:
        if not order_valid(secret, clause): continue
        reduced = [p for p in solutions if order_valid(p, clause)]
        if len(reduced) == len(solutions): continue
        clauses.append(clause); solutions = reduced
        if len(solutions) == 1: break
    if len(solutions) != 1: raise ValueError('挑戰排列不唯一。')
    prompt = 'A至H各出現一次，排在1至8位。全部條件：' + '；'.join(order_words(c) for c in clauses)
    prompt += '。求唯一完整排列，從第1到第8位，以英文逗號分隔。'
    return prompt, ','.join(secret), {'clauses': clauses, 'solution_count': 1}


def induction(rng, index):
    modulus = rng.choice((11, 13, 17))
    coefficients = [rng.randint(-2, 2) for _ in range(3)] + [rng.randrange(modulus)]
    a, b, c, d = coefficients
    candidates = list(itertools.product(range(-2, 3), range(-2, 3), range(-2, 3), range(modulus)))
    examples = []
    while len(candidates) > 1:
        x, y = rng.randint(1, 20), rng.randint(1, 20)
        z = (a*x*y + b*x + c*y + d) % modulus
        examples.append([x, y, z])
        candidates = [(aa, bb, cc, dd) for aa, bb, cc, dd in candidates if (aa*x*y + bb*x + cc*y + dd) % modulus == z]
        if len(examples) > 40: raise ValueError('挑戰歸納不唯一。')
    x, y = rng.randint(21, 50), rng.randint(21, 50)
    prompt = f'機器固定z=(a×x×y+b×x+c×y+d)除以{modulus}的非負餘數。a、b、c各為-2至2的整數，d為0至{modulus-1}整數。觀測'
    prompt += '；'.join(f'({xx},{yy})→{zz}' for xx, yy, zz in examples)
    prompt += f'。指定範圍內規則唯一。輸入({x},{y})時z是多少？只答整數。'
    return prompt, str((a*x*y + b*x + c*y + d) % modulus), {'modulus': modulus, 'coefficients': coefficients, 'examples': examples, 'target': [x, y]}


def make_challenge_cases(seed):
    rng = random.Random(seed); cases = []
    for category, create in [('logic', logic), ('quantitative', quantitative), ('spatial', spatial),
                             ('tracking', tracking), ('constraints', constraints), ('induction', induction)]:
        for index in range(6):
            prompt, answer, proof = create(rng, index)
            case = {'id': 'challenge_' + category + '_' + str(index+1), 'category': category,
                    'prompt': prompt, 'answer': answer, 'proof': proof}
            if category == 'spatial': case['grid_size'] = 7
            cases.append(case)
    return cases
