"""User-started, single-pass self-improvement window."""
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .evolution import EDITABLE, improve
from .acceptance import load_suite


class EvolutionWindow:
    def __init__(self, parent, initial_draft=None, source_check=None):
        self.source_check = source_check
        self.window = tk.Toplevel(parent)
        self.window.title('自我改進 · 程式與溝通')
        self.window.geometry('800x780')
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
        self.imported_suite = None
        frame = ttk.LabelFrame(self.window, text='驗收例子（選填；未提供時僅驗證既有功能）', padding=8)
        frame.pack(fill='x', padx=16, pady=8)
        ttk.Label(frame, text='輸入一句話，指定溝通模組應如何辨識：').pack(anchor='w')
        self.example = ttk.Entry(frame)
        self.example.pack(fill='x', pady=4)
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Label(row, text='期望辨識為：').pack(side='left')
        self.expected = ttk.Combobox(row, values=('簡短', '詳細', '混合', '未指定'), state='readonly', width=10)
        self.expected.set('詳細')
        self.expected.pack(side='left')
        ttk.Button(row, text='匯入多案例 JSON', command=self.import_cases).pack(side='left', padx=8)
        ttk.Button(row, text='清除匯入', command=self.clear_cases).pack(side='left')
        self.case_notice = tk.StringVar(value='例子會與改善方向一併傳給模型；請使用不含私人資料的例子。')
        ttk.Label(frame, textvariable=self.case_notice, wraplength=720).pack(anchor='w', pady=4)
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
        if initial_draft:
            self.goal.insert('1.0', initial_draft['goal'])
            for name, value in self.targets.items():
                value.set(name in initial_draft['selected'])
            from .acceptance import validate_suite
            self.imported_suite = validate_suite(initial_draft['acceptance'], initial_draft['selected'])
            self.example.configure(state='disabled')
            self.case_notice.set('已帶入你指定的回饋案例；預期值由你選定。可查看完整案例，再決定是否開始。')
        ttk.Button(frame, text='查看完整驗收案例', command=self.show_cases).pack(anchor='w')

    def show_cases(self):
        window = tk.Toplevel(self.window)
        window.title('完整驗收案例 · 開始後會傳給模型')
        text = tk.Text(window, wrap='word', width=90, height=24)
        text.pack(fill='both', expand=True)
        text.insert('1.0', json.dumps(self.imported_suite, ensure_ascii=False, indent=2) if self.imported_suite else '未匯入案例；單一例子請查看主視窗。')
        text.configure(state='disabled')

    def import_cases(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.window, filetypes=[('驗收案例', '*.json')])
        if not path:
            return
        try:
            self.imported_suite = load_suite(path)
        except (ValueError, OSError) as exc:
            messagebox.showerror('無法匯入', str(exc), parent=self.window)
            return
        self.example.delete(0, 'end')
        self.example.configure(state='disabled')
        self.case_notice.set(f'已匯入 {len(self.imported_suite["cases"])} 個案例；失敗案例必須改善，且全部案例需通過。')

    def clear_cases(self):
        if not self.busy:
            self.imported_suite = None
            self.example.configure(state='normal')
            self.case_notice.set('未匯入；可填單一溝通例子，或留白僅檢查既有功能。')

    def append(self, text):
        self.output.configure(state='normal')
        self.output.insert('end', text + '\n')
        self.output.see('end')
        self.output.configure(state='disabled')

    def begin(self):
        if self.busy:
            return
        try:
            if self.source_check:
                self.source_check()
        except ValueError as exc:
            messagebox.showerror('回饋已變更', str(exc), parent=self.window)
            return
        goal = self.goal.get('1.0', 'end').strip()
        selected = [name for name, value in self.targets.items() if value.get()]
        suite = self.imported_suite
        example = self.example.get().strip()
        if suite is None and example:
            choices = {'簡短': 'concise', '詳細': 'detailed', '混合': 'mixed', '未指定': 'unspecified'}
            suite = {'version': 1, 'cases': [{'id': 'user_example', 'kind': 'communication',
                     'question': example, 'history': [], 'expect': {'length_hint': choices[self.expected.get()]}}]}
        if not goal or not selected:
            messagebox.showinfo('請填改善方向', '需要改善方向與至少一個可修改模組。', parent=self.window)
            return
        self.busy = True
        self.cancel.clear()
        self.start.configure(state='disabled')
        self.stop.configure(state='normal')

        def work():
            try:
                result = improve(goal, selected, cancel=self.cancel, acceptance=suite,
                                 progress=lambda state: self.events.put(('progress', state)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        labels = {'preparing': '準備程式副本', 'generating': 'AI 正在產生程式變更',
                  'baseline_testing': '先量測改版前的驗收結果', 'acceptance_testing': '核對改前與改後的案例結果',
                  'no_change_needed': '指定案例目前已全數通過；沒有改碼或發布，請補上能重現問題的案例',
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
