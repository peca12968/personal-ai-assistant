"""Frozen synthetic continual-learning experiment; no private stores or ML imports."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PHASES = ('baseline', 'stage-a', 'stage-b', 'reload-b')
SETTINGS = {
    'seed': 20261104, 'rank': 16, 'alpha': 32, 'dropout': 0.05,
    'learning_rate': 0.0002, 'accumulation': 4, 'steps_a': 80,
    'steps_b': 128, 'max_sequence_tokens': 192, 'max_new_tokens': 24,
    'target_modules': ['q_proj', 'k_proj', 'v_proj', 'o_proj',
                       'gate_proj', 'up_proj', 'down_proj'],
    'system': '請直接回答問題，只輸出所要求的答案，不附說明。',
}
EXPECTED_VERSIONS = {'torch': '2.11.0+cu128', 'transformers': '4.57.6', 'peft': '0.18.1',
                     'accelerate': '1.12.0', 'safetensors': '0.7.0'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def sources():
    return {name: file_hash(HERE / name) for name in
            ('protocol.py', 'experiment.py', 'runtime.py', 'model.json', 'requirements.txt', 'fetch_model.py')}


def make_suite():
    # All mappings are invented test data. No assistant/user conversation is read.
    keys = ('Veklum', 'Norvash', 'Pelmik', 'Zundrel', 'Kavsol', 'Temvok', 'Rilnek', 'Borsan')
    values = {
        'a': ('blue', 'orange', 'green', 'purple', 'white', 'red', 'yellow', 'black'),
        'b': ('pear', 'mango', 'apple', 'lemon', 'grape', 'peach', 'melon', 'plum'),
    }
    train, evaluation = [], []
    for group, answers in values.items():
        field = '標記顏色' if group == 'a' else '配給水果'
        templates = (
            '虛構站點{key}的{field}是什麼？只答英文。',
            '請給我{key}站點的{field}英文名稱。',
            '{key}的{field}？僅輸出一個英文詞。',
            '在站點登記資料中，{key}所對應的{field}為何？用英文回答。',
        )
        withheld = (
            '查詢代號{key}：它登記了哪個{field}？請僅回覆英文名稱。',
            '我要填寫{key}的資料卡；{field}這欄應填哪個英文單字？',
        )
        for index, (key, answer) in enumerate(zip(keys, answers)):
            for variant, template in enumerate(templates):
                train.append({'id': f'{group}-{index}-train-{variant}', 'group': group,
                              'prompt': template.format(key=key, field=field), 'answer': answer})
            for variant, template in enumerate(withheld):
                evaluation.append({'id': f'{group}-{index}-heldout-{variant}', 'group': group,
                                   'prompt': template.format(key=key, field=field), 'answer': answer})
    # General-skill anchors are training data, not the held-out controls below.
    # Added in v2 after v1 showed a transient traditional/simplified drift.
    translations = (('dog', '狗'), ('bird', '鳥'), ('fish', '魚'), ('horse', '馬'),
                    ('door', '門'), ('cloud', '雲'), ('dragon', '龍'), ('book', '書'),
                    ('car', '車'), ('leaf', '葉'), ('lamp', '燈'), ('learn', '學習'),
                    ('computer', '電腦'), ('quiet', '安靜'), ('efficient', '有效率'), ('answer', '回答'))
    anchors = []
    for english, chinese in translations:
        anchors.extend([(f'請將{english}翻成繁體中文，只輸出翻譯。', chinese),
                        (f'英文「{english}」用繁體中文怎麼寫？只答譯詞。', chinese)])
    for left, right, operation in ((14, 27, '+'), (18, 16, '+'), (25, 17, '+'), (41, 8, '+'),
                                    (6, 8, '*'), (12, 4, '*'), (7, 5, '*'), (9, 4, '*')):
        answer = left + right if operation == '+' else left * right
        anchors.append((f'計算{left}{operation}{right}，只輸出數字結果。', str(answer)))
    anchors += [('把WORLD改為小寫，只答結果。', 'world'),
                ('把PYTHON改為小寫，只答結果。', 'python'),
                ('把f,d,e依字母順序排列，以逗號分隔且不加空白。', 'd,e,f'),
                ('把z,x,y依字母順序排列，以逗號分隔且不加空白。', 'x,y,z'),
                ('小華先喝水，再讀書；他先做什麼？只答動作。', '喝水'),
                ('所有三角形都是圖形，乙是三角形，所以乙是圖形嗎？只答是或否。', '是'),
                ('輸出JSON物件，鍵為ready，值為true；不要空白或程式碼框。', '{"ready":true}'),
                ('輸出JSON物件，鍵為done，值為false；不要空白或程式碼框。', '{"done":false}')]
    train += [{'id': f'anchor-{i}', 'group': 'anchor', 'prompt': prompt, 'answer': answer}
              for i, (prompt, answer) in enumerate(anchors)]
    guards = (
        ('17+26等於多少？只答數字。', '43'),
        ('9乘以7等於多少？只答數字。', '63'),
        ('將英文cat翻成繁體中文，只輸出翻譯。', '貓'),
        ('把英文單字HELLO改成小寫，只輸出結果。', 'hello'),
        ('將字母序列c,a,b按字母順序排列，用逗號隔開，不要空白。', 'a,b,c'),
        ('句子「小明先吃飯，再散步」中，先做哪件事？只答「吃飯」或「散步」。', '吃飯'),
        ('若所有方形都是圖形，而甲是方形，甲是否為圖形？只答「是」或「否」。', '是'),
        ('只輸出一個JSON物件，鍵為ok，值為布林值true，不要空白或程式碼框。', '{"ok":true}'),
    )
    evaluation += [{'id': f'guard-{i}', 'group': 'guard', 'prompt': prompt, 'answer': answer}
                   for i, (prompt, answer) in enumerate(guards)]
    return {'schema': 'synthetic_weight_learning_v2', 'training': train, 'evaluation': evaluation,
            'limitations': 'Known synthetic facts and eight narrow controls; not IQ, AGI, consciousness or general lifelong learning.'}


def training_rows(suite, phase):
    if phase not in ('stage-a', 'stage-b'):
        raise ValueError('Only training phases have training rows')
    # Stage B uses balanced replay of A; no held-out question is passed to training.
    return [dict(row) for row in suite['training'] if phase == 'stage-b' or row['group'] in ('a', 'anchor')]


def scores(rows):
    return {group: {'correct': sum(row['correct'] for row in rows if row['group'] == group),
                    'total': sum(row['group'] == group for row in rows)}
            for group in ('a', 'b', 'guard')}


def assess(reports):
    for phase in PHASES:
        if reports[phase]['status'] != 'complete':
            raise ValueError('Cannot assess an unfinished phase')
    baseline, a, b, reloaded = [reports[p]['evaluation'] for p in PHASES]
    expected = [r['id'] for r in make_suite()['evaluation']]
    for rows in (baseline, a, b, reloaded):
        if [r['id'] for r in rows] != expected:
            raise ValueError('Evaluation rows missing, reordered or duplicated')
        for row, case in zip(rows, make_suite()['evaluation']):
            if (any(row[key] != case[key] for key in ('group', 'prompt', 'answer'))
                    or type(row['correct']) is not bool
                    or row['correct'] != (row['output'].strip().casefold() == case['answer'].casefold())):
                raise ValueError('Evaluation row does not match frozen case or exact-match grade')
    sa, sb, s0 = scores(a), scores(b), scores(baseline)
    lost = [old['id'] for old, new in zip(baseline, b)
            if old['group'] == 'guard' and old['correct'] and not new['correct']]
    lost_a = [old['id'] for old, new in zip(baseline, a)
              if old['group'] == 'guard' and old['correct'] and not new['correct']]
    forgotten = [old['id'] for old, new in zip(a, b)
                 if old['group'] == 'a' and old['correct'] and not new['correct']]
    checks = {
        'a_learned': sa['a']['correct'] >= 14 and sa['a']['correct'] > s0['a']['correct'],
        'b_learned': sb['b']['correct'] >= 14 and sb['b']['correct'] > s0['b']['correct'],
        'a_retained': sb['a']['correct'] >= 14 and not forgotten,
        'baseline_guards_retained': not lost,
        'stage_a_baseline_guards_retained': not lost_a,
        'fresh_process_same_answers': [r['output'] for r in b] == [r['output'] for r in reloaded],
        'a_weights_changed': reports['stage-a']['weights_before'] != reports['stage-a']['weights_after'],
        'b_weights_changed': reports['stage-b']['weights_before'] != reports['stage-b']['weights_after'],
        'b_started_from_a': reports['stage-b']['weights_before'] == reports['stage-a']['weights_after'],
        'reload_weights_match': reports['reload-b']['weights_after'] == reports['stage-b']['weights_after'],
    }
    return {'schema': 'synthetic_weight_learning_assessment_v2', 'checks': checks,
            'passed': all(checks.values()), 'scores': {p: scores(reports[p]['evaluation']) for p in PHASES},
            'forgotten_a_ids': forgotten, 'lost_guard_ids': lost,
            'stage_a_lost_guard_ids': lost_a,
            'automatic_activation': False, 'consciousness': 'not_measured',
            'scope': make_suite()['limitations']}
