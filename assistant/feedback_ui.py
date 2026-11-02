"""Review local feedback and explicitly author a communication acceptance example."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from .feedback_improvement import check_source, example_from_source, feedback_source, make_draft


class FeedbackWindow:
    def __init__(self, parent, store):
        self.store, self.source = store, None
        self.window = tk.Toplevel(parent)
        self.window.title('回饋與改進例子')
        self.window.geometry('880x780')
        ttk.Label(self.window, text='選取回饋，核對問題與前文，再指定溝通提示應如何辨識。此處不會自動改碼。',
                  wraplength=800, padding=10).pack(fill='x')
        self.listing = ttk.Treeview(self.window, columns=('id', 'rating', 'correction'), show='headings', height=5)
        for key, label, width in [('id', '回答編號', 90), ('rating', '評價', 100), ('correction', '更正', 600)]:
            self.listing.heading(key, text=label)
            self.listing.column(key, width=width, stretch=key == 'correction')
        self.listing.pack(fill='x', padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        ttk.Button(self.window, text='重新整理', command=self.refresh).pack(anchor='e', padx=12)
        self.preview = tk.Text(self.window, height=6, wrap='word', state='disabled')
        self.preview.pack(fill='both', expand=True, padx=12)
        form = ttk.Frame(self.window, padding=12)
        form.pack(fill='x')
        self.fields = {}
        for key, label, height in [('question', '可編輯的驗收問題', 2),
                                   ('history', '可編輯的前文 JSON（role / content；可移除私人資訊）', 3),
                                   ('problem', '可編輯的改善描述', 2)]:
            ttk.Label(form, text=label).pack(anchor='w')
            field = tk.Text(form, height=height, wrap='word')
            field.pack(fill='x', pady=(2, 6))
            self.fields[key] = field
        row = ttk.Frame(form)
        row.pack(fill='x')
        ttk.Label(row, text='你期望辨識的回答長度：').pack(side='left')
        self.expected = ttk.Combobox(row, values=('簡短', '詳細', '混合', '未指定'), state='readonly', width=10)
        self.expected.pack(side='left')
        self.next = ttk.Button(row, text='帶入自我改進', command=self.prepare, state='disabled')
        self.next.pack(side='right')
        self.notice = tk.StringVar(value='此入口只驗證長度提示，不直接評分模型整篇回答。事實錯誤或工具失敗請另建合適案例。')
        ttk.Label(self.window, textvariable=self.notice, wraplength=820, padding=12).pack(fill='x')
        self.refresh()

    def refresh(self):
        self.source = None
        self.next.configure(state='disabled')
        for key in self.listing.get_children():
            self.listing.delete(key)
        for row in self.store.feedback_summary():
            self.listing.insert('', 'end', iid=str(row['message_id']), values=(row['message_id'],
                '有幫助' if row['rating'] == 1 else '需要改進', row['correction'][:200]))

    def select(self, event=None):
        selected = self.listing.selection()
        self.source = None
        self.next.configure(state='disabled')
        self.expected.set('')  # Never infer the user's expected grade from the failing implementation.
        for field in self.fields.values():
            field.delete('1.0', 'end')
        if not selected:
            return
        try:
            source = feedback_source(self.store, int(selected[0]))
            context = source['context'] or {}
            self.preview.configure(state='normal')
            self.preview.delete('1.0', 'end')
            self.preview.insert('1.0', '原回答：\n' + source['answer'] + '\n\n原更正：\n' + source['correction'] +
                '\n\n當時溝通提示（不是答案品質分數）：\n' + json.dumps(context.get('communication', {}), ensure_ascii=False))
            self.preview.configure(state='disabled')
            example = example_from_source(source)
            self.source = source
            self.fields['question'].insert('1.0', example['question'])
            self.fields['history'].insert('1.0', json.dumps(example['history'], ensure_ascii=False, indent=2))
            self.fields['problem'].insert('1.0', source['correction'])
            self.next.configure(state='normal')
            self.notice.set('下一頁可再核對；按「開始改進並發布」才會將編輯後的描述、問題與前文傳給模型，並執行一輪改碼。原回答不會自動傳送。')
        except (ValueError, KeyError) as exc:
            self.notice.set(str(exc))

    def prepare(self):
        if self.source is None:
            return
        try:
            source = check_source(self.store, self.source['message_id'], self.source['fingerprint'])
            choices = {'簡短': 'concise', '詳細': 'detailed', '混合': 'mixed', '未指定': 'unspecified'}
            if self.expected.get() not in choices:
                raise ValueError('請先選擇你期望的回答長度辨識。')
            from .acceptance import strict_json
            history = strict_json(self.fields['history'].get('1.0', 'end'))
            draft = make_draft(source, self.fields['question'].get('1.0', 'end').strip(), history,
                               self.fields['problem'].get('1.0', 'end').strip(), choices[self.expected.get()])
            from .evolution_ui import EvolutionWindow
            EvolutionWindow(self.window, initial_draft=draft,
                            source_check=lambda: check_source(self.store, draft['source_message_id'], draft['source_fingerprint']))
        except (ValueError, KeyError) as exc:
            messagebox.showerror('無法帶入', str(exc), parent=self.window)
