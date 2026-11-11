"""Cross-session local review and explicit navigation to a chosen goal."""
import tkinter as tk
from tkinter import messagebox, ttk

from . import goal_review
from .autonomy import LABELS
from .ui_layout import ScrollableForm, fit_window


class GoalReviewWindow:
    def __init__(self, parent, store, open_goal, on_change=lambda: None):
        self.store, self.open_goal, self.on_change = store, open_goal, on_change
        self.offset = 0
        self.rows = {}
        self.window = tk.Toplevel(parent)
        self.window.title('接續與回顧 · 所有對話的自主目標')
        fit_window(self.window, 900, 720)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='看看哪些事情可以繼續。這裡整理同一份本機資料庫的自主目標；選一項查看紀錄，再決定下一步。', padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='收起只是暫時不在回顧顯示，不會停止工作、刪除資料或確認成果。有新進度或匯入文件變更時會再次出現。', padding=12).pack(fill='x')
        controls = ttk.Frame(body, padding=12)
        controls.pack(fill='x')
        self.include_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text='也顯示已收起項目', variable=self.include_hidden,
                        command=self.reset_page).pack(side='left')
        ttk.Button(controls, text='重新整理', command=self.refresh).pack(side='left', padx=8)
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        table = ttk.Frame(body)
        table.pack(fill='x', padx=12)
        self.listing = ttk.Treeview(table, columns=('status', 'session', 'objective'), show='headings', height=8)
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.listing.yview)
        self.listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        for key, title, width in [('status', '狀態', 140), ('session', '對話', 180), ('objective', '目標', 440)]:
            self.listing.heading(key, text=title)
            self.listing.column(key, width=width, minwidth=80, stretch=True)
        self.listing.pack(side='left', fill='both', expand=True)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        self.listing.bind('<Double-1>', lambda event: self.open_selected())
        paging = ttk.Frame(body, padding=12)
        paging.pack(fill='x')
        self.prev = ttk.Button(paging, text='上一頁', command=lambda: self.page(-1))
        self.prev.pack(side='left')
        self.next = ttk.Button(paging, text='下一頁', command=lambda: self.page(1))
        self.next.pack(side='left', padx=8)
        self.output = tk.Text(body, height=10, wrap='word', state='disabled')
        self.output.pack(fill='x', padx=12, pady=8)
        self.open = ttk.Button(body, text='查看所選目標與下一步', command=self.open_selected, state='disabled')
        self.open.pack(anchor='w', padx=12, pady=4)
        self.hide = ttk.Button(body, text='暫時收起', command=self.toggle_hidden, state='disabled')
        self.hide.pack(anchor='w', padx=12, pady=4)
        self.refresh()

    def reset_page(self):
        self.offset = 0
        self.refresh()

    def page(self, direction):
        self.offset = max(0, self.offset + direction * 100)
        self.refresh()

    def refresh(self):
        selected = (self.listing.selection() or [None])[0]
        result = goal_review.snapshot(self.store, include_hidden=self.include_hidden.get(), offset=self.offset)
        if not result['items'] and self.offset:
            self.offset = max(0, (max(1, result['listed_count']) - 1) // 100 * 100)
            result = goal_review.snapshot(self.store, include_hidden=self.include_hidden.get(), offset=self.offset)
        self.rows = {row['goal_id']: row for row in result['items']}
        for item in self.listing.get_children():
            self.listing.delete(item)
        for gid, row in self.rows.items():
            status = LABELS.get(row['recorded_status'], row['recorded_status'])
            self.listing.insert('', 'end', iid=gid, values=(('已收起 · ' if row['hidden'] else '') + status,
                row['session_title'], row['objective'][:100]))
        self.notice.set(f"待回顧 {result['visible_count']} 項 · 已收起 {result['hidden_count']} 項 · 第 {self.offset // 100 + 1} 頁\n本機清單，未啟動工作或呼叫模型；按重新整理取得目前紀錄。")
        self.prev.configure(state='normal' if self.offset else 'disabled')
        self.next.configure(state='normal' if result['has_more'] else 'disabled')
        if selected in self.rows:
            self.listing.selection_set(selected)
        self.select()
        self.on_change()

    def selected(self):
        return self.rows.get((self.listing.selection() or [None])[0])

    def select(self, event=None):
        row = self.selected()
        self.open.configure(state='normal' if row else 'disabled')
        self.hide.configure(state='normal' if row else 'disabled', text='重新顯示在回顧' if row and row['hidden'] else '暫時收起')
        lines = ['選取一個目標，查看可以採取的下一步。']
        if row:
            lines = ['對話：' + row['session_title'], '目標：' + row['objective'] + ('…（已省略，可開啟完整目標）' if row['objective_truncated'] else ''),
                '紀錄狀態：' + LABELS.get(row['recorded_status'], row['recorded_status']),
                '下一步：' + row['explanation'], f"已保存 {row['step_count']} 步",
                '文件來源：' + ('與目標保存的來源版本一致' if row['sources_match'] else '已變更；既有成果仍屬於原版本'),
                '交付形式：' + ('本機文件' if row['file_delivery'] else '視窗文字成果')]
            if row['question']:
                lines += ['當時等待的補充：' + row['question']]
            if row['saved_artifact_count']:
                lines += [f"已保存文件／草稿：{row['saved_artifact_count']} 份；目前檔案狀態需在目標內核對。"]
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('1.0', '\n\n'.join(lines))
        self.output.configure(state='disabled')

    def open_selected(self):
        row = self.selected()
        if row:
            self.open_goal(row['session_id'], row['goal_id'])

    def toggle_hidden(self):
        row = self.selected()
        if row:
            try:
                goal_review.set_hidden(self.store, row['session_id'], row['goal_id'],
                    fingerprint=row['fingerprint'], hidden=not row['hidden'])
            except ValueError as exc:
                messagebox.showinfo('請重新查看目標', str(exc), parent=self.window)
            self.refresh()
