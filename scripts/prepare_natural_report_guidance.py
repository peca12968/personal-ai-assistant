"""Prepare synthetic report states for the natural-guidance comparison."""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, decision_table, goal_guidance, goal_replies
from assistant.backup import create_backup
from assistant.store import Store

FIELDS = ('收件班別', '核對班別')
ROUTES = {('早班', '早班'): '集合席', ('晚班', '早班'): '西席',
          ('早班', '晚班'): '東席', ('晚班', '晚班'): '交接席'}
SEEDS = (('workshop', '芒灣 W6', '工坊零件交接'), ('archives', '石荔 A7', '圖冊回庫'),
         ('supplies', '竹湧 S9', '器材借出'))


def action(tool, **args):
    return json.dumps({'tool': tool, 'args': args, 'note': '建立固定測量狀態'}, ensure_ascii=False)


def prepare_seed(work, destination, name, scope, task):
    store = Store(work / 'assistant.sqlite3')
    sid = store.new_session()
    table = {'conditions': list(FIELDS), 'domains': {field: ['早班', '晚班'] for field in FIELDS},
             'excluded': [], 'known': dict.fromkeys(FIELDS, '早班'),
             'rows': [{'when': list(when), 'result': result} for when, result in ROUTES.items()]}
    changed, alternate = ('晚班', '早班'), ('早班', '晚班')

    def attach(expected_id, filename, lines):
        path = work / filename
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        if store.attach_document(sid, path) != expected_id:
            raise ValueError('合成文件編號與固定案例不一致。')

    def rule(version, when, result, ending='其他配對沿用原表。'):
        left, right = when
        return (f'本輪「{scope}」的「{version}」版規則。若「收件班別」為「{left}」、「核對班別」為「{right}」，'
                f'原「{ROUTES[when]}」改為「{result}」。{ending}')

    attach(1, 'baseline.txt', [f'{scope} 的收件班別、核對班別原本都是早班。'])
    attach(2, 'routes.txt', [f'收件{left}、核對{right}去{result}。' for (left, right), result in ROUTES.items()])
    attach(3, 'v1.txt', [rule('v1', changed, '臨時席'), rule('v1', alternate, '臨時席')])
    attach(4, 'adoptions.txt', [f'本輪「{scope}」正式採用版本「{version}」。' for version in ('v1', 'v2')])
    objective = (f'本次為「{scope}」。處理{task}，依核對規則交付確定去向及來源。'
                 '若正式更新後只剩欄名會影響去向，只問該欄名；規則有缺或衝突時保留條件式結果。')
    gid = autonomy.create_goal(store, sid, objective)

    def run(*actions):
        pending = iter(actions)
        return autonomy.run_goal(store, sid, gid, SimpleNamespace(answer=lambda *_: next(pending)),
                                 max_steps=len(actions))

    def reply(text):
        observed = goal_replies.snapshot(store, sid, gid)
        goal_replies.save(store, sid, gid, text, expected_fingerprint=observed['fingerprint'])
        return f'[補充#{observed["step_count"]}]'

    def guide(text):
        observed = goal_guidance.snapshot(store, sid, gid)
        return goal_guidance.save(store, sid, gid, text, expected_fingerprint=observed['fingerprint'])

    run(action('read_ranges', ranges=[{'document_id': d, 'start_line': 1, 'end_line': end}
                                      for d, end in ((1, 1), (2, 4))]),
        action('check_decision', **table), action('ask', question='現場有什麼更動？'))
    source = reply('正式改成晚班。')
    run(action('check_decision', field_reference=source),
        action('read_ranges', ranges=[{'document_id': d, 'start_line': 1, 'end_line': 2} for d in (3, 4)]),
        action('check_decision', scope=scope, document_rules=['[D3:L1]', '[D3:L2]', '[D4:L1]', '[D4:L2]']),
        action('ask', question=f'本次「{scope}」正式採用哪個版本？請回覆「v1」或「v2」。', document_version=True))
    adopted = reply('v1')
    initial = run(action('check_decision', version_confirmation=adopted),
                  action('finish', report='v1 兩種情況都去臨時席。'))
    if initial['status'] != 'report_ready':
        raise ValueError('合成狀態未完成第一份報告。')
    guide(f'本輪「{scope}」正式採用版本「v2」。')
    run(action('finish', report='已採用 v2，規則缺漏，去向待查。'))

    partial = name == 'supplies'
    ending = '其他配對待補。' if partial else '其他配對沿用原表。'
    lines = [rule('v2', changed, '青席', ending), rule('v2', changed, '金席', ending)]
    if not partial:
        lines.append(rule('v2', alternate, '青席'))
    attach(5, 'supplement.txt', lines)
    guide(f'補件 [D5:L1-L{len(lines)}]')
    run(action('finish', report='v2 在收件晚班、核對早班時有青席與金席兩筆規則，去向待查。'))

    selected = 2 if name == 'archives' else 1
    guide(f'正式採用 [D5:L{selected}]。')
    if name == 'archives':
        state = autonomy.get_goal(store, sid, gid)
        question = decision_table.reference_question_for_steps(state['steps'])
        run(action('ask', question=question))
        confirmed = reply('收件班別')
        run(action('check_decision', field_reference=confirmed, resolve=True))
        prior = '已確認收件班別改為晚班，依 v2 去金席。'
    elif partial:
        prior = '收件晚班、核對早班去青席。另一配對缺 v2 規則。'
    else:
        prior = '兩種可能情況在 v2 都去青席，第一筆欄名仍待查。'
    if run(action('finish', report=prior))['status'] != 'report_ready':
        raise ValueError('合成狀態未完成先前報告。')
    create_backup(store.path, destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    for name, scope, task in SEEDS:
        with TemporaryDirectory(dir=args.output) as temporary:
            prepare_seed(Path(temporary), args.output / (name + '.paibackup'), name, scope, task)


if __name__ == '__main__':
    main()

