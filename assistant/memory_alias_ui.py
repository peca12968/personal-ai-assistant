"""Explicit local search vocabulary editing and read-only retrieval preview."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from . import memory_aliases
from .ui_layout import ScrollableForm, fit_window


class MemoryAliasesWindow:
    def __init__(self, parent, store):
        self.store = store
        self.window = tk.Toplevel(parent)
        self.window.title('記憶搜尋稱呼')
        fit_window(self.window, 820, 740)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='將同一主題的中文名、英文名或暱稱放在同一行，以 | 分隔。例如：星橋 | StarBridge | 小星。儲存後，聊天與自主目標的記憶搜尋可使用這些稱呼。', padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='最多8組，每組2至6個稱呼，每個2至64字；不能重複或跨組重疊。這是搜尋線索，不會改寫原始記憶、確認身分或授權工作；只會將實際匹配的一組稱呼連同選用記憶傳給模型。', padding=(12, 0)).pack(fill='x')
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        ttk.Label(body, text='目前已儲存內容（核對用）').pack(anchor='w', padx=12)
        self.saved = tk.Text(body, height=4, wrap='word', state='disabled')
        self.saved.pack(fill='x', padx=12, pady=4)
        ttk.Label(body, text='編輯草稿：每行一組；全部留白再儲存可撤回全部自訂稱呼').pack(anchor='w', padx=12)
        self.editor = tk.Text(body, height=7, wrap='word')
        self.editor.pack(fill='x', padx=12, pady=4)
        buttons = ttk.Frame(body, padding=12)
        buttons.pack(fill='x')
        self.save_button = ttk.Button(buttons, text='儲存搜尋稱呼', command=self.save)
        self.save_button.pack(side='left')
        self.reload_button = ttk.Button(buttons, text='重新核對（保留草稿）', command=self.reload)
        self.reload_button.pack(side='left', padx=8)
        ttk.Button(buttons, text='清空草稿', command=lambda: self.editor.delete('1.0', 'end')).pack(side='left')
        self.query = tk.StringVar()
        ttk.Label(body, text='預覽已儲存設定：輸入問題，本機列出目前會選用的記憶，不呼叫模型').pack(anchor='w', padx=12)
        self.query_entry = ttk.Entry(body, textvariable=self.query)
        self.query_entry.pack(fill='x', padx=12, pady=4)
        self.preview_button = ttk.Button(body, text='預覽目前記憶搜尋', command=self.preview)
        self.preview_button.pack(anchor='w', padx=12)
        self.result = tk.Text(body, height=9, wrap='word', state='disabled')
        self.result.pack(fill='x', padx=12, pady=12)
        self.reload()
        self.editor.insert('1.0', self.format_groups(self.current['groups']))

    @staticmethod
    def format_groups(groups):
        return '\n'.join(' | '.join(group) for group in groups)

    def reload(self):
        self.current = memory_aliases.get(self.store)
        self.saved.configure(state='normal')
        self.saved.delete('1.0', 'end')
        self.saved.insert('1.0', self.format_groups(self.current['groups']) or '尚無自訂稱呼')
        self.saved.configure(state='disabled')
        self.notice.set(f"目前版本 {self.current['revision']}；已載入供核對，編輯草稿保持原樣。")
        self.result.configure(state='normal')
        self.result.delete('1.0', 'end')
        self.result.insert('1.0', '設定已重新載入；請重新預覽。')
        self.result.configure(state='disabled')

    def save(self):
        raw = self.editor.get('1.0', 'end').strip()
        groups = [line.split('|') for line in raw.splitlines() if line.strip()]
        try:
            memory_aliases.save(self.store, groups, expected_revision=self.current['revision'])
        except ValueError as exc:
            messagebox.showerror('無法儲存搜尋稱呼', str(exc), parent=self.window)
            return
        self.reload()
        self.notice.set(f"已儲存版本 {self.current['revision']}；只影響之後的搜尋，原記憶與歷史回答保持。")

    def preview(self):
        query = self.query.get().strip()
        if not query or len(query) > 500:
            messagebox.showerror('無法預覽', '問題需為1至500字。', parent=self.window)
            return
        rows = self.store.retrieve(query)
        output = []
        for row in rows:
            label = f"#{row['id']} [{row['kind']}] {row['content']}"
            if row.get('retrieval_aliases'):
                label += '\n匹配稱呼：' + json.dumps(row['retrieval_aliases'], ensure_ascii=False)
            output.append(label)
        self.result.configure(state='normal')
        self.result.delete('1.0', 'end')
        self.result.insert('1.0', '本次預覽使用目前已儲存設定，未儲存草稿不影響結果。\n\n' + ('\n\n'.join(output) or '沒有選用記憶。'))
        self.result.configure(state='disabled')
