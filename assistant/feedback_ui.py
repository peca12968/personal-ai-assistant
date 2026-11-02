"""Review local feedback and explicitly author communication/context acceptance."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from .feedback_improvement import check_source, example_from_source, feedback_source, make_draft, make_followup_draft
from .acceptance import strict_json, validate_suite
from .ui_layout import ScrollableForm, fit_window


class FeedbackWindow:
    def __init__(self, parent, store):
        self.store, self.source = store, None
        self.window = tk.Toplevel(parent)
        self.window.title('回饋與改進例子')
        fit_window(self.window, 880, 900)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='選取回饋，核對問題與前文，再指定回答長度或接續前文的期望。此處不會自動改碼。',
                  wraplength=800, padding=10).pack(fill='x')
        self.listing = ttk.Treeview(body, columns=('id', 'rating', 'correction'), show='headings', height=5)
        for key, label, width in [('id', '回答編號', 90), ('rating', '評價', 100), ('correction', '更正', 600)]:
            self.listing.heading(key, text=label)
            self.listing.column(key, width=width, stretch=key == 'correction')
        self.listing.pack(fill='x', padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        ttk.Button(body, text='重新整理', command=self.refresh).pack(anchor='e', padx=12)
        self.preview = tk.Text(body, height=6, wrap='word', state='disabled')
        self.preview.pack(fill='x', padx=12)
        form = ttk.Frame(body, padding=12)
        form.pack(fill='x')
        self.fields = {}
        for key, label, height in [('question', '可編輯的驗收問題', 2),
                                   ('history', '可編輯的前文 JSON（role / content；可移除私人資訊）', 3),
                                   ('problem', '可編輯的改善描述', 2)]:
            ttk.Label(form, text=label).pack(anchor='w')
            field = tk.Text(form, height=height, wrap='word')
            field.pack(fill='x', pady=(2, 6))
            self.fields[key] = field
        choice = ttk.Frame(form)
        choice.pack(fill='x')
        ttk.Label(choice, text='改善類型：').pack(side='left')
        self.kind = ttk.Combobox(choice, values=('回答長度辨識', '接續前文檢索'), state='readonly', width=18)
        self.kind.set('回答長度辨識')
        self.kind.pack(side='left')
        self.kind.bind('<<ComboboxSelected>>', self.change_kind)
        self.next = ttk.Button(choice, text='帶入自我改進', command=self.prepare, state='disabled')
        self.next.pack(side='right')
        self.length_row = row = ttk.Frame(form)
        row.pack(fill='x')
        ttk.Label(row, text='你期望辨識的回答長度：').pack(side='left')
        self.expected = ttk.Combobox(row, values=('簡短', '詳細', '混合', '未指定'), state='readonly', width=10)
        self.expected.pack(side='left')
        self.followup_frame = ttk.Frame(form)
        self.followup_mode = tk.StringVar(value='')
        ttk.Radiobutton(self.followup_frame, text='不沿用前題作檢索', variable=self.followup_mode, value='none').pack(anchor='w')
        ttk.Radiobutton(self.followup_frame, text='沿用下方選取的使用者前文（最多四則）', variable=self.followup_mode, value='selected').pack(anchor='w')
        ttk.Button(self.followup_frame, text='從編輯後前文載入可選訊息', command=self.load_turns).pack(anchor='w')
        self.turns = tk.Listbox(self.followup_frame, selectmode='multiple', exportselection=False, height=4)
        self.turns.pack(fill='x')
        self.loaded_history = None
        self.turn_indices = []
        self.notice = tk.StringVar(value='驗證程式的長度提示或前文檢索，不直接評分整篇回答。請明確選擇期望；不會由現有錯誤推算正確答案。')
        self.layout.wrapped_label(body, textvariable=self.notice, wraplength=820, padding=12).pack(fill='x')
        self.refresh()

    def change_kind(self, event=None):
        self.length_row.pack_forget()
        self.followup_frame.pack_forget()
        (self.followup_frame if self.kind.get() == '接續前文檢索' else self.length_row).pack(fill='x')

    def load_turns(self):
        self.loaded_history = None
        self.turn_indices = []
        self.turns.delete(0, 'end')
        try:
            history = strict_json(self.fields['history'].get('1.0', 'end'))
            validate_suite({'version':1, 'cases':[{'id':'preview', 'kind':'followup',
                'question':self.fields['question'].get('1.0', 'end').strip(), 'history':history, 'expect':None}]})
            for i, row in enumerate(history):
                if row['role'] == 'user':
                    self.turn_indices.append(i)
                    self.turns.insert('end', f'前文第 {i+1} 則：' + row['content'].replace('\n', ' ')[:150])
            self.loaded_history = history
            self.notice.set('請選取期望沿用的使用者前文，或明確選擇不沿用。每則最多一千字；下一頁可查看完整驗收內容。')
        except (ValueError, KeyError) as exc:
            self.notice.set(str(exc))

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
        self.followup_mode.set('')
        self.loaded_history = None
        self.turn_indices = []
        self.turns.delete(0, 'end')
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
            history = strict_json(self.fields['history'].get('1.0', 'end'))
            question = self.fields['question'].get('1.0', 'end').strip()
            problem = self.fields['problem'].get('1.0', 'end').strip()
            if self.kind.get() == '接續前文檢索':
                if self.followup_mode.get() not in ('none', 'selected'):
                    raise ValueError('請明確選擇沿用前文，或不沿用前題。')
                selected_turns = None
                if self.followup_mode.get() == 'selected':
                    if history != self.loaded_history:
                        raise ValueError('前文尚未載入或已變更，請重新載入可選訊息並選取。')
                    selected_turns = [self.turn_indices[int(i)] for i in self.turns.curselection()]
                draft = make_followup_draft(source, question, history, problem, selected_turns)
            else:
                choices = {'簡短': 'concise', '詳細': 'detailed', '混合': 'mixed', '未指定': 'unspecified'}
                if self.expected.get() not in choices:
                    raise ValueError('請先選擇你期望的回答長度辨識。')
                draft = make_draft(source, question, history, problem, choices[self.expected.get()])
            from .evolution_ui import EvolutionWindow
            EvolutionWindow(self.window, initial_draft=draft,
                            source_check=lambda: check_source(self.store, draft['source_message_id'], draft['source_fingerprint']))
        except (ValueError, KeyError) as exc:
            messagebox.showerror('無法帶入', str(exc), parent=self.window)
