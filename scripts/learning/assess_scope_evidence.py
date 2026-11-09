"""Compare inline and separated source scopes on paired multi-turn evidence cases."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import local_models
from scripts.learning import assess_polarity_evidence as polarity

full = polarity.full
prior, short, gpu, batches = polarity.prior, polarity.short, polarity.gpu, polarity.batches
read, save, digest = polarity.read, polarity.save, polarity.digest
PHASE = polarity.PHASE
SCHEMA = 'scope_evidence_pairs_v1'
PARENT = ROOT / 'data/learning/scope-evidence-pairs'
FIXTURE = ROOT / 'evals/scope_evidence_pairs.json'
DIRECTIVE = ('對 A 和 B 各回一個當輪狀態及支持它的原句編號。T 表示已確認成立，F 表示已確認不成立，'
             'U 表示本輪未核對。只用當輪對應物件的觀察。只回 JSON，例如 '
             '{"A":{"state":"T","source":5},"B":{"state":"U","source":4}}。例子的狀態與編號僅示意。')


def location(name):
    return PARENT / polarity.diagnosis.location(name).name


def compile_case(row):
    cid = row['id']
    if cid == 'literal_control':
        if set(row) != {'id', 'pair', 'question', 'answer'} or row['pair'] is not None:
            raise ValueError('一般輸出控制的格式不符。')
        return dict(id=cid, group='control', pair=None, question=row['question'],
                    semantic_question=row['question'], answer=row['answer'], history=[], mapping={})
    if (set(row) != {'id', 'pair', 'rule', 'claims', 'prior', 'current', 'gold'}
            or len(row['claims']) != 2 or len(set(row['claims'])) != 2 or len(row['prior']) != 2
            or not 2 <= len(row['current']) <= 3 or len(row['gold']) != 2):
        raise ValueError('題目須有兩項命題、兩句舊輪紀錄與二至三句本輪紀錄。')
    records = [row['rule'], *row['prior'], *row['current']]
    if len(set(records)) != len(records):
        raise ValueError('同一題的規則或觀察句重複。')
    for clause in [*records, *row['claims']]:
        batches.queue.text(clause, 110, '原句')
    if any(not isinstance(item, list) or len(item) != 2 or item[0] not in ('T', 'F', 'U')
           or type(item[1]) is not int or not 4 <= item[1] <= len(records) for item in row['gold']):
        raise ValueError('預期狀態或來源不符。')
    if row['gold'][0][1] == row['gold'][1][1]:
        raise ValueError('兩項命題須各有自己的原句。')
    numbered = [f'[{index}] {clause}。' for index, clause in enumerate(records, 1)]
    claims = '待核對的命題 A 為「' + row['claims'][0] + '」，B 為「' + row['claims'][1] + '」。'
    question = ''.join(numbered) + claims + DIRECTIVE
    grouped = ('規則\n' + numbered[0] + '\n舊輪紀錄\n' + '\n'.join(numbered[1:3])
               + '\n本輪觀察\n' + '\n'.join(numbered[3:]) + '\n' + claims + DIRECTIVE)
    history = [dict(role='user', content='工作規則與舊輪紀錄。' + '。'.join(records[:3]) + '。'),
               dict(role='assistant', content='已記下規則和這次提供的紀錄。')]
    return dict(id=cid, group='fresh' if row['pair'] else 'historical', pair=row['pair'], question=question,
                semantic_question=grouped, history=history, mapping={}, records=records, gold=row['gold'],
                answer=polarity.conclusion([item[0] for item in row['gold']]))


def cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('跨輪證據題庫格式不符。')
    rows = data['cases']
    output = [compile_case(row) for row in rows]
    ids = [row['id'] for row in output]
    if (len(ids) != 8 or len(set(ids)) != 8 or any(not re.fullmatch('[a-z_]{1,32}', cid) for cid in ids)
            or [sum(row['group'] == group for row in output)
                for group in ('fresh', 'historical', 'control')] != [6, 1, 1]):
        raise ValueError('跨輪證據題組數量或編號不符。')
    for index, pair in enumerate(('rail', 'tray', 'gate')):
        first, second = rows[2 * index:2 * index + 2]
        if (first['pair'] != pair or second['pair'] != pair
                or any(first[key] != second[key] for key in ('rule', 'claims', 'prior'))
                or [a + 4 for a, (left, right) in enumerate(zip(first['current'], second['current'], strict=True))
                    if left != right] != [first['gold'][0][1]]
                or first['gold'][0][1] != second['gold'][0][1] or first['gold'][0][0] == second['gold'][0][0]
                or first['gold'][1] != second['gold'][1] or output[2 * index]['answer'] == output[2 * index + 1]['answer']):
            raise ValueError('每組只能換一條當前觀察，且結論必須跟著改變。')
    return output


def sources():
    hashes = polarity.sources()
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
    plan = frozen(folder)
    reference, selection, description = full.reference(plan['reference'])
    if (digest(reference) != plan['reference_protocol'] or selection != plan['selection']
            or description != plan['description'] or plan['model'] != reference['model']
            or plan['files'] != reference['models']['parent']['files']):
        raise ValueError('跨輪證據的參考權重或規約已變更。')
    return plan


def parse_facts(row, count):
    text = row['raw']
    if not row['ended'] or not 1 <= len(text) <= 2048:
        return None
    try:
        value = polarity.strict_json(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.keys() != {'A', 'B'}:
        return None
    facts = []
    for key in ('A', 'B'):
        item = value[key]
        if (not isinstance(item, dict) or item.keys() != {'state', 'source'}
                or item['state'] not in ('T', 'F', 'U')
                or type(item['source']) is not int or not 1 <= item['source'] <= count):
            return None
        facts.append([item['state'], item['source']])
    return facts


def grade(case, row):
    if case['group'] == 'control':
        output = row['delivered'] if row['ended'] else None
        return dict(facts=None, exact_facts=0, prior_citations=0, rule_citations=0, valid=False,
                    conclusion=output, correct=output == case['answer'], current_only=False)
    facts = parse_facts(row, len(case['records']))
    conclusion = polarity.conclusion([fact[0] for fact in facts]) if facts else None
    ids = [fact[1] for fact in facts or ()]
    return dict(facts=facts, exact_facts=sum(a == b for a, b in zip(facts or [None, None], case['gold'], strict=True)),
                prior_citations=sum(i in (2, 3) for i in ids), rule_citations=ids.count(1),
                valid=facts is not None, conclusion=conclusion, correct=conclusion == case['answer'],
                current_only=bool(ids) and min(ids) >= 4)


def summarize(fixture, rows):
    groups, contrasts, details = {}, {}, {}
    for case, direct, split in zip(fixture, rows['direct'], rows['split'], strict=True):
        cid, group = case['id'], case['group']
        if direct['id'] != cid or split['id'] != cid:
            raise ValueError('配對推論案例順序不符。')
        if group == 'control' and any(direct[key] != split[key] for key in
                                     ('raw', 'ended', 'delivered', 'input_tokens', 'wire_sha')):
            raise ValueError('一般輸出控制未重現原提示與原答。')
        details[cid] = dict(expected=case['answer'], gold=case.get('gold'))
        for arm, row in (('direct', direct), ('split', split)):
            outcome = grade(case, row)
            details[cid][arm] = outcome
            totals = groups.setdefault(group, {}).setdefault(arm, dict(
                count=0, correct=0, valid=0, current_correct=0, exact_facts=0, rule_citations=0, prior_citations=0))
            totals['count'] += 1
            for metric in ('correct', 'valid', 'exact_facts', 'rule_citations', 'prior_citations'):
                totals[metric] += outcome[metric]
            totals['current_correct'] += outcome['correct'] and outcome['current_only']
            if case['pair']:
                contrasts.setdefault(case['pair'], {}).setdefault(arm, []).append(cid)
    paired = {pair: {arm: dict(correct=all(details[cid][arm]['correct'] for cid in ids),
                              exact=all(details[cid][arm]['exact_facts'] == 2 for cid in ids))
                     for arm, ids in arms.items()} for pair, arms in contrasts.items()}
    return dict(groups=groups, pairs=paired, details=details, automatic_activation=False)


def result(folder, plan, run):
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('跨輪證據的規約或閒置觀察不符。')
    record, raw_sha, exit_sha = prior.phase_record(folder, PHASE, digest(run))
    if record['weights'] != plan['selection']['weights'] or record['environment'] != prior.EXPECTED_VERSIONS:
        raise ValueError('跨輪證據的權重或環境不符。')
    counts = read(folder / 'preflight.json')
    for arm in full.PHASES:
        database = folder / (arm + '.sqlite3')
        if record['db_sha'][arm] != batches.file_hash(database):
            raise ValueError('跨輪證據的會話副本已變更。')
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for case, row in zip(plan['cases'], record['rows'][arm], strict=True):
                cid = case['id']
                transcript = db.execute('SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id',
                                        (plan['sessions'][cid],)).fetchall()
                saved = db.execute('SELECT payload FROM answer_contexts WHERE message_id=?', (row['message_id'],)).fetchone()
                succeeded = db.execute('SELECT 1 FROM answer_runs WHERE id=? AND user_message_id=? '
                                       'AND answer_message_id=? AND status=?',
                                       (row['run_id'], row['user_id'], row['message_id'], 'succeeded')).fetchone()
                if (row['id'] != cid or row['wire_sha'] != plan['prompts'][cid][arm]
                        or row['app_prompt_sha'] != plan['prompts'][cid]['direct']
                        or row['input_tokens'] != counts[cid][arm] or not 1 <= row['input_tokens'] <= local_models.MAX_INPUT_TOKENS
                        or type(row['ended']) is not bool or row['mapped'] is not False
                        or row['delivered'] != row['raw'].strip() or saved is None
                        or json.loads(saved[0]) != plan['contexts'][cid] or succeeded is None
                        or [item[1:] for item in transcript[:-2]] != [(h['role'], h['content']) for h in case['history']]
                        or transcript[-2:] != [(row['user_id'], 'user', case['question']),
                                               (row['message_id'], 'assistant', row['delivered'])]):
                    raise ValueError('跨輪證據的提示、原答或已完成會話不符。')
    return dict(**summarize(plan['cases'], record['rows']), scope=SCHEMA, report_sha=raw_sha, exit_sha=exit_sha)


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
                raise TimeoutError('跨輪證據對照已達時限。')
            if read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('跨輪證據的來源或規約已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, digest(run)]
            check()
            gpu.run_owned(command, folder, PHASE, lambda: gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('跨輪證據的參考來源已變更。')
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
        raise ValueError('跨輪證據報告與重算證據不符。')
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
        # Reuse the fixed paired inference worker with this study's protocol validator.
        with patch.object(polarity, 'frozen', frozen):
            polarity.worker(location(args.name), args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
