"""Evaluate source-scope cues derived from unlabelled user text."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import assess_scope_evidence as scope

polarity, full = scope.polarity, scope.full
short, gpu, batches = scope.short, scope.gpu, scope.batches
read, save, digest = scope.read, scope.save, scope.digest
SCHEMA = 'natural_scope_evidence_v1'
PARENT = ROOT / 'data/learning/natural-scope-evidence'
FIXTURE = ROOT / 'evals/natural_scope_evidence.json'
STATES = ('T', 'F', 'U')
BOUNDARIES = '。！？；\n'
QUOTES = {'「': '」', '『': '』', '“': '”', '（': '）', '(': ')'}
CLOSERS = frozenset(QUOTES.values())
EARLIER = re.compile(r'^(?:上次|上一輪|上一班|上星期|前一班|前次|前回|先前|原先|早班|昨天|昨日)')
CURRENT = re.compile(r'^(?:本輪|本次|當次|這次|這回|眼下|目前|剛才|剛剛|方才|現場回報|最新一次)')
RULE = re.compile(r'完整要求|全部要求|兩項要求|只需要|須同時具備|必要條件')
HEADINGS = dict(rule='規則線索', earlier='較早線索', current='當前線索', uncertain='範圍待辨')
DIRECTIVE = ('對 A、B 各回當前狀態及對應原句。T 是確認成立，F 是確認不成立，U 是未能確認。'
             'source 逐字抄寫同一則問題裡的一整句，省略句尾標點，保留句內標點。'
             '如沒有可核對的當前觀察，填 U，source 填 null。只回 JSON，例如 '
             '{"A":{"state":"T","source":"整句原文"},"B":{"state":"U","source":null}}。'
             '若有分段標題，只把它當作時間線索，仍依原句核對。')


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def kind(outside):
    parts = [part.strip() for part in re.split(r'[，,]', outside)]
    earlier = any(EARLIER.match(part) for part in parts)
    current = any(CURRENT.match(part) for part in parts)
    rule = bool(RULE.search(outside))
    if sum((earlier, current, rule)) != 1:
        return 'uncertain'
    return 'rule' if rule else 'earlier' if earlier else 'current'


def segments(text):
    """Return original character ranges; quotes and parentheticals remain intact."""
    if not isinstance(text, str) or not 1 <= len(text) <= 1600:
        return []
    stack, result, outside = [], [], []
    start = 0
    for index, char in enumerate(text):
        if char in QUOTES:
            if not stack:
                outside.append(' ')
            stack.append(QUOTES[char])
            continue
        if char in CLOSERS:
            if not stack or stack.pop() != char:
                return []
            continue
        if stack:
            continue
        if char in BOUNDARIES:
            end = index + 1
            result.append(dict(start=start, end=end, kind=kind(''.join(outside))))
            outside.clear()
            start = end
        else:
            outside.append(char)
    if stack:
        return []
    if start < len(text):
        result.append(dict(start=start, end=len(text), kind=kind(''.join(outside))))
    if len(result) > 12 or not all(text[row['start']:row['end']].strip() for row in result):
        return []
    return result


def render(text, spans):
    if not spans:
        return text
    return '\n'.join('【' + HEADINGS[row['kind']] + '】' + text[row['start']:row['end']] for row in spans)


def source_text(text, span):
    return text[span['start']:span['end']].strip().rstrip(BOUNDARIES)


def compile_case(row):
    cid, group = row['id'], row['group']
    if group == 'control':
        if set(row) != {'id', 'group', 'question', 'answer'}:
            raise ValueError('一般輸出控制格式不符。')
        return dict(id=cid, group=group, question=row['question'], semantic_question=row['question'],
                    answer=row['answer'], history=[], mapping={})
    if (group not in ('fresh', 'historical') or set(row) != {'id', 'group', 'history', 'text', 'claims', 'gold'}
            or len(row['claims']) != 2 or len(set(row['claims'])) != 2 or len(row['gold']) != 2):
        raise ValueError('每個自然案例須有兩項不同命題。')
    text = row['text']
    spans = segments(text)
    quotes = [source_text(text, span) for span in spans]
    if not spans or len(quotes) != len(set(quotes)):
        raise ValueError('無法唯一保留題目的原文範圍。')
    for phrase in [row['history'], *row['claims'], *quotes]:
        batches.queue.text(phrase, 200, '原句')
    for item in row['gold']:
        if not isinstance(item, list) or len(item) != 2 or item[0] not in STATES:
            raise ValueError('預期狀態不符。')
        if item[1] not in quotes and (item[1] is not None or item[0] != 'U'):
            raise ValueError('預期引句須等於原句或待查的缺漏。')
    claims = '待核對命題 A 是「' + row['claims'][0] + '」，B 是「' + row['claims'][1] + '」。'
    instructions = '\n' + claims + DIRECTIVE
    return dict(id=cid, group=group, question=text + instructions, semantic_question=render(text, spans) + instructions,
                history=[dict(role='user', content=row['history']), dict(role='assistant', content='收到。')],
                mapping={}, text=text, spans=spans, gold=row['gold'],
                answer=polarity.conclusion([item[0] for item in row['gold']]))


def cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('自然範圍題庫格式不符。')
    output = [compile_case(row) for row in data['cases']]
    ids = [row['id'] for row in output]
    if (len(ids) != 8 or len(set(ids)) != 8 or any(not re.fullmatch('[a-z_]{1,32}', cid) for cid in ids)
            or [sum(row['group'] == group for row in output) for group in ('fresh', 'historical', 'control')] != [6, 1, 1]):
        raise ValueError('自然範圍的分組或案例編號不符。')
    return output


def sources():
    hashes = scope.sources()
    for path in (FIXTURE, Path(__file__).resolve()):
        hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def prepare(name, reference):
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources, location=location):
        return full.prepare(name, reference)


def frozen(folder):
    with patch.multiple(full, SCHEMA=SCHEMA, cases=cases, sources=sources):
        return full.frozen(folder)


def checked(folder):
    with patch.object(scope, 'frozen', frozen):
        return scope.checked(folder)


def parse_facts(row):
    raw = row['raw']
    if not row['ended'] or not 1 <= len(raw) <= 2048:
        return None
    try:
        obj = polarity.strict_json(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.keys() != {'A', 'B'}:
        return None
    facts = []
    for slot in ('A', 'B'):
        item = obj[slot]
        if (not isinstance(item, dict) or item.keys() != {'state', 'source'} or item['state'] not in STATES
                or (item['source'] is not None and (not isinstance(item['source'], str) or not 1 <= len(item['source']) <= 200))):
            return None
        facts.append([item['state'], item['source']])
    return facts


def grade(case, row):
    if case['group'] == 'control':
        answer = row['delivered'] if row['ended'] else None
        return dict(conclusion=answer, correct=answer == case['answer'])
    facts = parse_facts(row)
    found = facts or [[None, None], [None, None]]
    pairs = list(zip(found, case['gold'], strict=True))
    conclusion = polarity.conclusion([item[0] for item in facts]) if facts else None
    quotes = {source_text(case['text'], span) for span in case['spans']}
    return dict(facts=facts, conclusion=conclusion, correct=conclusion == case['answer'], valid=facts is not None,
                states=sum(actual[0] == expected[0] for actual, expected in pairs),
                exact=sum(actual == expected for actual, expected in pairs),
                off_text=sum(source is not None and source not in quotes for _, source in facts or ()),
                unsupported=sum(state != 'U' and source is None for state, source in facts or ()))


def summarize(fixture, rows):
    groups, details = {}, {}
    for case, direct, split in zip(fixture, rows['direct'], rows['split'], strict=True):
        cid, group = case['id'], case['group']
        if direct['id'] != cid or split['id'] != cid:
            raise ValueError('自然範圍的案例順序不符。')
        if group == 'control' and any(direct[key] != split[key] for key in
                                     ('raw', 'ended', 'delivered', 'input_tokens', 'wire_sha')):
            raise ValueError('一般輸出控制未重現原提示與原答。')
        details[cid] = dict(expected=case['answer'], gold=case.get('gold'))
        for arm, row in (('direct', direct), ('split', split)):
            outcome = grade(case, row)
            details[cid][arm] = outcome
            totals = groups.setdefault(group, {}).setdefault(arm, {'count': 0})
            totals['count'] += 1
            for name in ('correct', 'valid', 'states', 'exact', 'off_text', 'unsupported'):
                if name in outcome:
                    totals[name] = totals.get(name, 0) + outcome[name]
    return dict(groups=groups, details=details, automatic_activation=False)


def result(folder, plan, run):
    with patch.multiple(scope, SCHEMA=SCHEMA, summarize=summarize):
        return scope.result(folder, plan, run)


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        plan = checked(folder)
        admitted = short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(plan), admitted=admitted)
        save(folder / 'run.json', run)
        deadline = time.monotonic() + polarity.LIMIT_SECONDS

        def check():
            gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('自然範圍對照已達時限。')
            if read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('自然範圍對照的來源或規約已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, digest(run)]
            check()
            gpu.run_owned(command, folder, scope.PHASE, lambda: gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('自然範圍對照的參考來源已變更。')
            report = result(folder, plan, run)
            save(folder / 'review.json', report)
            return report
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    report = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('自然範圍報告與重算證據不符。')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('check')
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--reference', required=True)
    for mode in ('run', 'verify'):
        modes.add_parser(mode).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'check':
        print(len(cases()))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        with patch.object(polarity, 'frozen', frozen):
            polarity.worker(location(args.name), args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
