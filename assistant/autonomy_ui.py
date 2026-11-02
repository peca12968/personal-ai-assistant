"""User-visible goals, bounded autonomous work, and resumable progress."""
import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import autonomy
from .ui_layout import ScrollableForm, fit_window


class AutonomousWindow:
    def __init__(self, parent, store, sid, engine):
        self.store, self.sid, self.engine = store, sid, engine
        self.busy = False
        self.active_goal = None
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.title('自主目標 · AI 夥伴')
        fit_window(self.window, 900, 780)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='給我一個目標，我會自行選擇查找、讀取、計算與整理步驟，保存行動紀錄與成果。',
                                  padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='本階段使用目前對話的已匯入文件與有效記憶，工具結果會傳給模型。每輪有步數上限，可停止與接續；成果產生後仍由你確認。',
                                  padding=12).pack(fill='x')
        self.objective = tk.Text(body, height=3, wrap='word')
        self.objective.pack(fill='x', padx=12)
        self.create = ttk.Button(body, text='保存新目標（使用目前參考文件）', command=self.new_goal)
        self.create.pack(anchor='w', padx=12, pady=8)
        self.listing = ttk.Treeview(body, columns=('status', 'objective'), show='headings', height=5)
        self.listing.heading('status', text='狀態')
        self.listing.column('status', width=130, stretch=False)
        self.listing.heading('objective', text='目標')
        self.listing.column('objective', width=600)
        self.listing.pack(fill='x', padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        controls = ttk.Frame(body, padding=12)
        controls.pack(fill='x')
        ttk.Label(controls, text='本輪最多步數：').pack(side='left')
        self.budget = ttk.Spinbox(controls, from_=1, to=12, width=4)
        self.budget.set('6')
        self.budget.pack(side='left')
        self.start = ttk.Button(controls, text='開始／接續自主工作', command=self.begin)
        self.start.pack(side='left', padx=8)
        self.stop = ttk.Button(controls, text='停止並保存進度', command=self.cancel.set, state='disabled')
        self.stop.pack(side='left')
        ttk.Label(body, text='需要補充時，在這裡回答：').pack(anchor='w', padx=12)
        self.answer = tk.Text(body, height=2, wrap='word')
        self.answer.pack(fill='x', padx=12)
        self.reply = ttk.Button(body, text='保存補充', command=self.add_reply)
        self.reply.pack(anchor='w', padx=12, pady=6)
        self.notice = tk.StringVar(value='選擇目標後開始；進度保存在本機。')
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.output = tk.Text(body, height=14, wrap='word', state='disabled')
        self.output.pack(fill='x', padx=12, pady=8)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.after(150, self.poll)
        self.refresh()

    def selected_id(self):
        selection = self.listing.selection()
        if not selection:
            raise ValueError('請先選擇一個目標。')
        return selection[0]

    def refresh(self, gid=None):
        selected = gid or (self.listing.selection() or [None])[0]
        for item in self.listing.get_children():
            self.listing.delete(item)
        for row in autonomy.goals(self.store, self.sid):
            self.listing.insert('', 'end', iid=row['id'], values=(autonomy.LABELS.get(row['status'], row['status']), row['objective'][:100]))
        if selected and self.listing.exists(selected):
            self.listing.selection_set(selected)
            self.select()

    def new_goal(self):
        if self.busy:
            return
        try:
            gid = autonomy.create_goal(self.store, self.sid, self.objective.get('1.0', 'end'))
            self.refresh(gid)
        except Exception as exc:
            messagebox.showerror('無法保存目標', str(exc), parent=self.window)

    def select(self, event=None):
        try:
            goal = autonomy.get_goal(self.store, self.sid, self.selected_id())
        except ValueError:
            return
        lines = ['目標：' + goal['objective'], '狀態：' + autonomy.LABELS.get(goal['status'], goal['status']),
                 goal['notice'], '參考來源：' + ', '.join(f"D{d['document_id']} {d['name']}" for d in goal['sources'])]
        if goal['question']:
            lines += ['需要你的補充：' + goal['question']]
        for step in goal['steps']:
            lines += [f"步驟 {step['sequence']} · {step['action']['tool']} · {step['action']['note']}",
                      json.dumps(step['observation'], ensure_ascii=False, indent=2)]
        if goal['result']:
            lines += ['成果（待你確認）：', goal['result']]
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('1.0', '\n\n'.join(lines))
        self.output.configure(state='disabled')

    def add_reply(self):
        if self.busy:
            return
        try:
            autonomy.reply(self.store, self.sid, self.selected_id(), self.answer.get('1.0', 'end'))
            self.answer.delete('1.0', 'end')
            self.refresh()
        except Exception as exc:
            messagebox.showerror('無法保存補充', str(exc), parent=self.window)

    def begin(self):
        if self.busy:
            return
        try:
            gid, budget = self.selected_id(), int(self.budget.get())
            if not 1 <= budget <= 12:
                raise ValueError('每輪步數需為1到12。')
        except ValueError as exc:
            messagebox.showinfo('請確認目標', str(exc), parent=self.window)
            return
        self.busy = True
        self.active_goal = gid
        self.cancel.clear()
        for button in (self.start, self.create, self.reply):
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.notice.set('正在自主選擇下一步；可隨時停止。')
        def work():
            try:
                result = autonomy.run_goal(self.store, self.sid, gid, self.engine, max_steps=budget,
                    cancel=self.cancel, progress=lambda event: self.events.put(('progress', event)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.notice.set(f"步驟 {value['step']}：{value['note']}")
                    self.refresh(self.active_goal)
                else:
                    self.busy = False
                    for button in (self.start, self.create, self.reply):
                        button.configure(state='normal')
                    self.stop.configure(state='disabled')
                    self.notice.set(value if kind == 'error' else autonomy.LABELS.get(value['status'], value['status']))
                    self.refresh(self.active_goal)
                    self.active_goal = None
        except queue.Empty:
            pass
        self.window.after(150, self.poll)

    def close(self):
        self.cancel.set()
        if self.busy:
            self.notice.set('正在停止自主流程，已保存步驟會保留。')
            self.window.after(200, self.close)
        else:
            self.window.destroy()
