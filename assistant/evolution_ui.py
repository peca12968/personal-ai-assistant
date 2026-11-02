"""User-started, single-pass self-improvement window."""
import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .evolution import EDITABLE, improve


class EvolutionWindow:
    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title('自我改進 · 程式與溝通')
        self.window.geometry('760x650')
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        ttk.Label(self.window, text='描述一個具體改善方向，例如：更準確理解簡短回答與更正。',
                  wraplength=700).pack(anchor='w', padx=16, pady=12)
        self.goal = tk.Text(self.window, height=5, wrap='word')
        self.goal.pack(fill='x', padx=16)
        self.targets = {}
        for name in EDITABLE:
            value = tk.BooleanVar(value=name in (EDITABLE[0], EDITABLE[3]))
            self.targets[name] = value
            ttk.Checkbutton(self.window, text=name, variable=value).pack(anchor='w', padx=16)
        ttk.Label(self.window, wraplength=700, text=(
            '每次按下開始，只執行一輪：AI 改碼 → 副本測試 → 套用 → 私人 GitHub。'
            '測試會以目前帳號執行候選 Python 程式；副本是版本隔離，不是作業系統沙箱。'
            '發布成功後重新開啟助理，讓所有變更生效。')).pack(fill='x', padx=16, pady=10)
        buttons = ttk.Frame(self.window)
        buttons.pack(fill='x', padx=16)
        self.start = ttk.Button(buttons, text='開始改進並發布', command=self.begin)
        self.start.pack(side='left')
        self.stop = ttk.Button(buttons, text='停止（套用前）', command=self.cancel.set, state='disabled')
        self.stop.pack(side='left', padx=8)
        self.output = tk.Text(self.window, wrap='word', state='disabled')
        self.output.pack(fill='both', expand=True, padx=16, pady=12)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.after(150, self.poll)

    def append(self, text):
        self.output.configure(state='normal')
        self.output.insert('end', text + '\n')
        self.output.see('end')
        self.output.configure(state='disabled')

    def begin(self):
        if self.busy:
            return
        goal = self.goal.get('1.0', 'end').strip()
        selected = [name for name, value in self.targets.items() if value.get()]
        if not goal or not selected:
            messagebox.showinfo('請填改善方向', '需要改善方向與至少一個可修改模組。', parent=self.window)
            return
        self.busy = True
        self.cancel.clear()
        self.start.configure(state='disabled')
        self.stop.configure(state='normal')

        def work():
            try:
                result = improve(goal, selected, cancel=self.cancel,
                                 progress=lambda state: self.events.put(('progress', state)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        labels = {'preparing': '準備程式副本', 'generating': 'AI 正在產生程式變更',
                  'testing': '檢查候選程式', 'applying': '測試通過，套用變更',
                  'publishing': '發布中，請等候完成', 'published': '已發布，重新開啟助理生效',
                  'not_applied': '未套用，原版本保留',
                  'applied_unpublished': '已套用但發布未確認；請查看紀錄並重試 Publish.cmd'}
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.append(labels.get(value, value))
                else:
                    self.append(json.dumps(value, ensure_ascii=False, indent=2) if kind == 'done' else value)
                    self.busy = False
                    self.start.configure(state='normal')
                    self.stop.configure(state='disabled')
        except queue.Empty:
            pass
        self.window.after(150, self.poll)

    def close(self):
        if self.busy:
            self.cancel.set()
            self.append('已要求停止；若已開始套用／發布，請等候完成再關閉。')
        else:
            self.window.destroy()
