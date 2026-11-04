"""Desktop entry point for local daily chat. Keep raw benchmark engines available."""
import argparse
from pathlib import Path
import sqlite3
import sys

from assistant.engine import ROOT
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine
from scripts.daily_expenses import DailyAssistant


def run_daily_window(store, engine):
    from tkinter import scrolledtext
    from assistant.gui import App, tk
    from assistant.chat_calculation import describe

    class DailyWindow(App):
        def inspect_context(self):
            context = self.store.answer_context(self.last_answer) if self.last_answer else None
            if context and 'capacity_check' in context:
                title = '名額條件對照'
                focus, check = context['capacity_trial'], context['capacity_check']
                lines = [context['current_request'], '', '待查原文：' + focus['待查原文'], '假設情況']
                lines.extend(focus['對照假設'])
                lines.extend(['', '模型對兩種情況的原答：', check['trial_answer']])
                if 'fallback_answer' in check:
                    lines.extend(['', '重新以原題作答：', check['fallback_answer']])
                lines.extend(['', '交付：' + check['delivered']])
            elif context and 'clearance_check' in context:
                title = '門寬尺寸核對'
                ref, receipt = context['door_measurements'], context['clearance_check']
                if '本輪門寬原文' in ref:
                    proof = ['本輪實測淨寬：' + ref['本輪門寬原文'], ref.get('阻礙原因') or ref['本輪尺寸比較']]
                elif '阻礙原因' in ref:
                    proof = [ref['阻礙原因']]
                else:
                    proof = [ref['假設一'], ref['假設二']]
                lines = [context['current_request'], '', ref['箱邊原文'], ref['門高原文'], ref['原門寬紀錄'],
                         *proof, '', '模型原答：', receipt['raw_answer'], '', '交付：' + receipt['delivered']]
            elif context and 'inventory_check' in context:
                title = '庫存回答核對紀錄'
                check = context['inventory_check']
                if check['schema'] == 'stock_opening_clarification_v1':
                    lines = [context['current_request'], '', '期初數量尚待核實',
                             f"原文依據：{check['qualifier']} {check['reported_opening']} {check['unit']}",
                             '', '模型原答：', check['raw_answer'], '', '交付的追問：' + check['delivered']]
                elif check['schema'] == 'stock_event_clarification_v1':
                    lines = [context['current_request'], '', '尚待核實的異動']
                    for event in check['pending_events']:
                        lines.append(f"第{event['筆次']}筆 {event['異動']} {event['估計用語']} {event['回報數量']} {check['unit']}")
                    lines.extend(['', '模型原答：', check['raw_answer'], '', '交付的追問：' + check['delivered']])
                else:
                    possible = '、'.join(str(n) for n in dict.fromkeys(check['possible_totals']))
                    lines = [context['current_request'], '', '物品：' + context['inventory_reasoning']['物品'],
                             f"已確認的數量：{check['expression']} = {check['confirmed_total']} {check['unit']}",
                             f"可能數量：{possible} {check['unit']}", '', '模型原答：', check['raw_answer'],
                             '', '按本機核對交付：' + check['delivered']]
            elif context and 'option_plan' in context:
                title = '本機方案比較紀錄'
                plan = context['option_plan']
                lines = [context['current_request'], '', '有效限制']
                if context.get('option_source'):
                    lines.insert(1, f"沿用本對話方案回答 #{context['option_source']['answer_id']}")
                if context.get('option_question'):
                    lines.insert(1, '更正尚待確認，沿用上一份有效條件。')
                elif change := plan.get('last_adjustment'):
                    mode = '相對增減' if change['mode'] == 'relative' else '指定新值'
                    lines.insert(1, f"{change['field']}（{mode}）：{change['before']} → {change['after']} {change['unit']}")
                lines.extend(rule['source'] for rule in plan['limits'].values())
                lines += ['', '依序比較（數值較小者優先）：' + '、'.join(plan['ranking']), '逐項核對']
                for option, check in zip(plan['options'], context['calculation']['checks'], strict=True):
                    reason = '不符合：' + '、'.join(plan['limits'][key]['source'] for key in check['rejected']) if check['rejected'] else '符合'
                    lines.append(option['source'] + ' · ' + reason)
                lines += ['', context['calculation']['result']['exact']]
            elif context and 'meeting_plan' in context:
                title = '本機會議時段計算紀錄'
                receipt = context['calculation']
                lines = [context['current_request'], '', '採用的取消條件']
                if context.get('meeting_source'):
                    lines.insert(1, f"沿用本對話會議回答 #{context['meeting_source']['answer_id']} 的日期與條件")
                lines += [c['source'] for c in context['meeting_plan']['cancelled']] or ['無']
                lines += ['', '每人剩餘時段']
                for name, slots in receipt['effective'].items():
                    lines.append(name + '：' + ('、'.join(slots) or '無'))
                lines += ['', '共同空檔：' + ('、'.join(receipt['common']) or '無'),
                          f"所需時間：{receipt['duration']} 分鐘", '結果：' + receipt['result']['exact']]
            elif context and 'expense_ledger' in context:
                title = '本機核銷計算紀錄'
                ledger = context['expense_ledger']
                lines = [context['current_request'], '', '逐項核對']
                if context.get('expense_selection'):
                    lines.insert(1, '尚待選列；下方總額沿用更正前的明細。')
                if context.get('expense_source'):
                    lines.insert(1, f"沿用本對話核銷回答 #{context['expense_source']['answer_id']}")
                for i, row in enumerate(ledger['rows'], 1):
                    decision = '計入' if row['included'] else '排除'
                    lines.append(f"第{i}項 · {decision} · {row['source']} · 最終狀態 {row['status']}")
                if ledger['changes']:
                    lines += ['', '歷次更正'] + [f"{c['source']} · {c['name']}：{c['before']} → {c['after']}" for c in ledger['changes']]
                lines += ['', describe(context['calculation'])]
            else:
                return super().inspect_context()
            panel = tk.Toplevel(self.root)
            panel.title(title)
            panel.geometry('640x460')
            view = scrolledtext.ScrolledText(panel, wrap='word', padx=10, pady=10)
            view.pack(fill='both', expand=True)
            view.insert('1.0', '\n'.join(lines))
            view.configure(state='disabled')

    root = tk.Tk()
    window = DailyWindow(root, store, engine)
    window.assistant = DailyAssistant(store, engine)
    root.mainloop()


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--engine', choices=('local', 'codex'))
    parser.add_argument('--qualified-segment', action='store_true')
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--local-experiment')
    choices.add_argument('--local-batch')
    choices.add_argument('--local-active', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--session')
    parser.add_argument('--file', type=Path, action='append', default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--ask')
    mode.add_argument('--retry-answer', action='store_true')
    args, other = parser.parse_known_args()

    if args.qualified_segment:
        if args.engine or args.local_experiment or args.local_batch or args.local_active:
            parser.error('分段模型與其他模型選項只能選一種。')
        if other:
            from scripts.learning.segment_chat import main as segment_main
            return segment_main()
    elif args.engine != 'local' or other:
        from assistant.__main__ import main as ordinary_main
        return ordinary_main()
    if args.retry_answer and (not args.session or args.file):
        parser.error('--retry-answer 需要 --session，且不能再附加文件。')
    if args.file and not args.ask:
        parser.error('--file 需搭配 --ask。')
    if args.qualified_segment and args.session and not (args.ask or args.retry_answer):
        parser.error('--session 需搭配 --ask 或 --retry-answer。')
    try:
        if args.qualified_segment:
            from scripts.learning.segment_chat import SegmentEngine
            raw = SegmentEngine(args.data_dir)
        else:
            raw = LocalEngine(args.data_dir, experiment=args.local_experiment, batch=args.local_batch, active=args.local_active)
        engine = DailyLocalEngine(raw)
        store = Store(args.data_dir / 'assistant.sqlite3')
        if args.ask or args.retry_answer:
            sid = args.session or store.new_session()
            for path in args.file:
                store.attach_document(sid, path)
            app = DailyAssistant(store, engine)
            reply = app.retry(sid) if args.retry_answer else app.ask(sid, args.ask)
            print(reply['content'])
            print('\nSession:', sid)
        else:
            run_daily_window(store, engine)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(1, '本機助理未完成：' + str(exc) + '\n')


if __name__ == '__main__':
    main()
