"""Review conflicting fields locally; applying choices edits the draft only."""
import tkinter as tk
from tkinter import ttk, messagebox
from . import memory_edits
from .ui_layout import ScrollableForm, fit_window


def display(key, value):
    if key == 'active':
        return '啟用' if value else '停用'
    if key == 'kind':
        return {'fact': '事實', 'preference': '偏好', 'lesson': '經驗'}[value]
    return value or ('不限' if key == 'expires_on' else '（空白）')


class MemoryMergeWindow:
    def __init__(self, parent, original, current, draft, apply):
        self.original, self.current, self.draft = dict(original), dict(current), dict(draft)
        self.comparison = memory_edits.compare(self.original, self.current, self.draft)
        self.apply_callback = apply
        self.window = tk.Toplevel(parent)
        self.window.title(f'核對記憶 #{current["id"]}')
        fit_window(self.window, 820, 700)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='比較開啟編輯時、目前已保存及你的草稿。不同欄位會合併；同一欄位的不同修改請選擇。套用只更新草稿，回原視窗按儲存才會寫入；儲存時仍會檢查新的變更。', padding=12).pack(fill='x')
        self.choices = {}
        self.previews = []
        for key in memory_edits.FIELDS:
            frame = ttk.LabelFrame(body, text=memory_edits.LABELS[key] + ('（有衝突）' if key in self.comparison['conflicts'] else ''), padding=10)
            frame.pack(fill='x', padx=12, pady=6)
            value = '\n\n'.join(f'{label}：\n{display(key, row[key])}' for label, row in (
                ('開啟編輯時', self.original), ('目前已保存', self.current), ('你的草稿', self.draft)))
            text_frame = ttk.Frame(frame)
            text_frame.pack(fill='x')
            preview = tk.Text(text_frame, height=8 if key == 'content' else 6, wrap='word')
            preview.pack(side='left', fill='both', expand=True)
            scroll = ttk.Scrollbar(text_frame, orient='vertical', command=preview.yview)
            scroll.pack(side='right', fill='y')
            preview.configure(yscrollcommand=scroll.set)
            preview.insert('1.0', value)
            preview.configure(state='disabled')
            self.previews.append(preview)
            if key in self.comparison['conflicts']:
                variable = tk.StringVar(value='current')
                self.choices[key] = variable
                ttk.Radiobutton(frame, text='保留目前已保存的版本', variable=variable, value='current').pack(anchor='w')
                ttk.Radiobutton(frame, text='採用我的草稿', variable=variable, value='draft').pack(anchor='w')
            else:
                summary = display(key, self.comparison['merged'][key])
                self.layout.wrapped_label(frame, text='合併後：' + summary[:160] +
                    ('…（預覽省略，完整內容見上方對應版本）' if len(summary) > 160 else '')).pack(fill='x')
        self.apply_button = ttk.Button(body, text='將核對結果套用到草稿', command=self.apply)
        self.apply_button.pack(anchor='w', padx=12, pady=12)

    def apply(self):
        merged = memory_edits.resolve(self.comparison, self.current, self.draft,
                                      {key: value.get() for key, value in self.choices.items()})
        try:
            self.apply_callback(self.original, self.current, self.draft, merged)
        except ValueError as exc:
            messagebox.showerror('無法套用核對結果', str(exc), parent=self.window)
            return
        self.window.destroy()
