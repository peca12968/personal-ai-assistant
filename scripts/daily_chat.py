"""Desktop entry point and local evidence viewer for daily chat."""
import argparse
from pathlib import Path
import sqlite3
import sys

from assistant.engine import CodexEngine, ROOT
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine
from scripts.daily_expenses import DailyAssistant


def run_daily_window(store, engine, *, default_chat=False):
    from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
    from assistant.gui import App, tk
    from assistant.chat_calculation import describe
    from scripts import daily_container_id, daily_counts, daily_deadline, daily_explicit_json, daily_meeting, daily_meeting_export, daily_meeting_lookup, daily_necessary

    class DailyWindow(App):
        def __init__(self, *args):
            super().__init__(*args)
            ttk.Button(self.context_btn.master, text='會議紀錄', command=self.meeting_history).pack(side='left', padx=6)

        def meeting_history(self):
            sid = self.sid
            try:
                rows = daily_meeting.recent_completed(self.store, sid)
            except sqlite3.Error:
                messagebox.showerror('無法讀取會議紀錄', '請重新開啟本機對話後再試。', parent=self.root)
                return
            if not rows:
                messagebox.showinfo('會議紀錄', '這段對話沒有已保存的會議計算。', parent=self.root)
                return
            panel = tk.Toplevel(self.root)
            panel.title('本對話的近期會議回答')
            panel.geometry('560x300')
            frame = ttk.Frame(panel, padding=10)
            frame.pack(fill='both', expand=True)
            box = tk.Listbox(frame, exportselection=False)
            scroll = ttk.Scrollbar(frame, command=box.yview)
            box.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            box.pack(side='left', fill='both', expand=True)
            for row in rows:
                summary = ' '.join(row['content'].split())
                if daily_meeting.valid_topic(row['topic']):
                    summary = f'「{row["topic"]}」  {summary}'
                box.insert('end', f'#{row["id"]}  {summary}')
            box.selection_set(0)

            def inspect(event=None):
                if self.sid != sid:
                    messagebox.showinfo('會議紀錄', '請回到這段對話後再查看。', parent=panel)
                    return
                if selection := box.curselection():
                    self.inspect_context(meeting_answer_id=rows[selection[0]]['id'])

            box.bind('<Double-Button-1>', inspect)
            ttk.Button(panel, text='查看會議參考', command=inspect).pack(pady=(0, 10))

        def inspect_context(self, *, meeting_answer_id=None):
            answer_id = self.last_answer if meeting_answer_id is None else meeting_answer_id
            context = self.store.answer_context(answer_id) if answer_id else None
            if meeting_answer_id is not None and (not context or 'meeting_plan' not in context):
                messagebox.showerror('會議紀錄', '這筆會議回答已無法核對。', parent=self.root)
                return
            meeting_schema = None
            if context and daily_explicit_json.FIELD in context:
                title = '本機 JSON 欄位核對'
                try:
                    receipt = daily_explicit_json.read(self.store, answer_id)
                    if receipt is None:
                        raise ValueError
                    lines = daily_explicit_json.describe(receipt).splitlines()
                except (KeyError, TypeError, ValueError):
                    lines = ['無法核對這次 JSON 回覆與原始欄位。']
            elif context and daily_deadline.FIELD in context:
                title = '本機期限核對'
                try:
                    receipt = daily_deadline.read(self.store, answer_id)
                    if receipt is None:
                        raise ValueError
                    lines = daily_deadline.describe(receipt).splitlines()
                except (KeyError, TypeError, ValueError, sqlite3.Error):
                    lines = ['無法核對這次期限回覆與原始時刻。']
            elif context and daily_container_id.FIELD in context:
                title = '本機編號核對'
                try:
                    receipt = daily_container_id.read(self.store, answer_id)
                    if receipt is None:
                        raise ValueError
                    lines = daily_container_id.describe(receipt).splitlines()
                except (KeyError, TypeError, ValueError):
                    lines = ['無法核對這次編號查找與原始來源。']
            elif context and daily_counts.FIELD in context:
                title = '本機數量核對'
                try:
                    receipt = daily_counts.read(self.store, answer_id)
                    if receipt is None:
                        raise ValueError
                    lines = daily_counts.describe(receipt).splitlines()
                except (KeyError, TypeError, ValueError):
                    lines = ['無法核對這次數量計算紀錄與原始問題。']
            elif context and daily_necessary.FIELD in context:
                title = '本機必要條件核對'
                try:
                    receipt = daily_necessary.read(self.store, self.last_answer)
                    if receipt is None:
                        raise ValueError
                    lines = daily_necessary.describe(receipt).splitlines()
                except (KeyError, TypeError, ValueError):
                    lines = ['無法核對這次必要條件紀錄與原始問題。']
            elif context and 'capacity_check' in context:
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
            elif context and (daily_meeting_lookup.FIELD in context or daily_meeting_lookup.CHOICE_FIELD in context):
                title = '本機會議查詢紀錄'
                field = (daily_meeting_lookup.CHOICE_FIELD if daily_meeting_lookup.CHOICE_FIELD in context
                         else daily_meeting_lookup.FIELD)
                try:
                    verified = daily_meeting_lookup.read(self.store, answer_id, field=field)
                    if verified is None:
                        raise ValueError
                    lines = daily_meeting_lookup.describe(verified).splitlines()
                except (KeyError, TypeError, ValueError, sqlite3.Error):
                    lines = ['無法依查詢當時的會議紀錄重建結果。']
            elif context and 'meeting_plan' in context:
                title = '本機會議時段計算紀錄'
                try:
                    verified = daily_meeting.read(self.store, answer_id)
                    if verified is None:
                        raise ValueError
                    lines = daily_meeting.describe(verified).splitlines()
                    if not daily_meeting.pending_date(verified):
                        lines.insert(0, f'本次會議回答 #{answer_id}')
                        if verified['calculation']['result']['exact'] != '沒有可行時段':
                            meeting_schema = verified['meeting_plan']['schema']
                except (KeyError, TypeError, ValueError, sqlite3.Error):
                    lines = ['無法依原始時段與更正重算這筆會議紀錄。']
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
            if meeting_schema:
                tk.Button(panel, text='匯出行事曆檔案', command=lambda: self.export_meeting(
                    panel, answer_id, meeting_schema)).pack(side='bottom', anchor='e', padx=10, pady=8)
            view.pack(fill='both', expand=True)
            view.insert('1.0', '\n'.join(lines))
            view.configure(state='disabled')

        def export_meeting(self, panel, answer_id, schema):
            title = simpledialog.askstring('行事曆標題', '輸入這場會議的標題。', initialvalue='會議', parent=panel)
            if title is None:
                return
            day = None
            if schema == daily_meeting_export.SAME_DAY_PLAN:
                day = simpledialog.askstring('會議日期', '輸入完整日期，格式為 YYYY-MM-DD。', parent=panel)
                if day is None:
                    return
            zone = None
            if schema not in daily_meeting.ZONED_PLANS:
                zone = simpledialog.askstring('會議時區', '輸入完整 IANA 名稱或 UTC 偏移，例如 Asia/Taipei 或 UTC+08:00。',
                                              parent=panel)
                if zone is None:
                    return
            path = filedialog.asksaveasfilename(parent=panel, title='另存行事曆檔案', defaultextension='.ics',
                                               filetypes=[('iCalendar', '*.ics')], initialfile='meeting.ics',
                                               confirmoverwrite=False)
            if not path:
                return
            try:
                saved = daily_meeting_export.export(self.store, answer_id, title, path, day=day, zone=zone)
            except (OSError, ValueError, sqlite3.Error) as exc:
                messagebox.showerror('匯出未完成', str(exc), parent=panel)
            else:
                messagebox.showinfo('行事曆檔案已建立', str(saved), parent=panel)

    root = tk.Tk()
    window = DailyWindow(root, store, engine)
    window.assistant = DailyAssistant(store, engine, default_chat=default_chat)
    root.mainloop()


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--engine', choices=('local', 'codex'))
    parser.add_argument('--qualified-segment', action='store_true')
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--qualified-base-assay')
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

    use_codex = False
    if args.qualified_base_assay is not None:
        if args.engine or args.qualified_segment or other:
            parser.error('--qualified-base-assay 需單獨選用本機驗收模型。')
    elif args.qualified_segment:
        if args.engine or args.local_experiment or args.local_batch or args.local_active:
            parser.error('分段模型與其他模型選項只能選一種。')
        if other:
            from scripts.learning.segment_chat import main as segment_main
            return segment_main()
    elif args.engine != 'local' or other:
        if (other or args.file or args.local_experiment is not None
                or args.local_batch is not None or args.local_active):
            from assistant.__main__ import main as ordinary_main
            return ordinary_main()
        use_codex = True
    if args.retry_answer and (not args.session or args.file):
        parser.error('--retry-answer 需要 --session，且不能再附加文件。')
    if args.file and not args.ask:
        parser.error('--file 需搭配 --ask。')
    if (args.qualified_segment or args.qualified_base_assay is not None) and args.session and not (args.ask or args.retry_answer):
        parser.error('--session 需搭配 --ask 或 --retry-answer。')
    try:
        if use_codex:
            engine = CodexEngine(args.data_dir)
        else:
            if args.qualified_base_assay is not None:
                from scripts.learning.qualified_base_chat import QualifiedBaseEngine
                raw = QualifiedBaseEngine(args.data_dir, args.qualified_base_assay)
            elif args.qualified_segment:
                from scripts.learning.segment_chat import SegmentEngine
                raw = SegmentEngine(args.data_dir)
            else:
                raw = LocalEngine(args.data_dir, experiment=args.local_experiment, batch=args.local_batch,
                                  active=args.local_active, recover_chat_budget=True)
            engine = DailyLocalEngine(raw)
        store = Store(args.data_dir / 'assistant.sqlite3')
        if args.ask or args.retry_answer:
            sid = args.session or store.new_session()
            for path in args.file:
                store.attach_document(sid, path)
            app = DailyAssistant(store, engine, default_chat=use_codex)
            reply = app.retry(sid) if args.retry_answer else app.ask(sid, args.ask)
            print(reply['content'])
            print('\nSession:', sid)
        else:
            run_daily_window(store, engine, default_chat=use_codex)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        label = '助理未完成' if use_codex else '本機助理未完成'
        parser.exit(1, label + '：' + str(exc) + '\n')


if __name__ == '__main__':
    main()
