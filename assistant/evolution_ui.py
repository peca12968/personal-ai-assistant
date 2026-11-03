"""User-started, single-pass self-improvement window."""
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .evolution import EDITABLE, improve
from .acceptance import load_suite
from .ui_layout import ScrollableForm, fit_window


class EvolutionWindow:
    def __init__(self, parent, initial_draft=None, source_check=None):
        self.source_check = source_check
        # A feedback dialog may close while this worker is still publishing.
        # Keep the window owned by the application, not that temporary dialog.
        self.root = parent._root()
        self.window = tk.Toplevel(self.root)
        self.window.title('自我改進 · 程式與溝通')
        fit_window(self.window, 800, 780)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.layout.wrapped_label(body, text='描述一個具體改善方向，例如：更準確理解簡短回答與更正。',
                  wraplength=700).pack(anchor='w', padx=16, pady=12)
        self.goal = tk.Text(body, height=5, wrap='word')
        self.goal.pack(fill='x', padx=16)
        self.targets = {}
        for name in EDITABLE:
            value = tk.BooleanVar(value=name in (EDITABLE[0], EDITABLE[3]))
            self.targets[name] = value
            ttk.Checkbutton(body, text=name, variable=value).pack(anchor='w', padx=16)
        self.imported_suite = None
        self.holdout_suite = None
        frame = ttk.LabelFrame(body, text='驗收例子（選填；未提供時僅驗證既有功能）', padding=8)
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
        self.layout.wrapped_label(frame, textvariable=self.case_notice, wraplength=720).pack(anchor='w', pady=4)
        holdout_frame = ttk.LabelFrame(body,text='保留驗收（選填；不加入提案模型提示）',padding=8)
        holdout_frame.pack(fill='x',padx=16,pady=8)
        self.layout.wrapped_label(holdout_frame,text='另準備不同輸入的案例，改前與改後都會檢查；候選必須全部通過。需搭配上方驗收案例或單一例子。這不保證候選程式無法讀取本機資料，測試副本不是作業系統沙箱。').pack(fill='x')
        self.holdout_notice = tk.StringVar(value='尚未匯入保留案例。')
        self.layout.wrapped_label(holdout_frame,textvariable=self.holdout_notice).pack(fill='x',pady=6)
        holdout_controls=ttk.Frame(holdout_frame);holdout_controls.pack(fill='x')
        self.holdout_import=ttk.Button(holdout_controls,text='匯入保留案例 JSON',command=self.import_holdout);self.holdout_import.pack(side='left')
        self.holdout_clear=ttk.Button(holdout_controls,text='清除保留案例',command=self.clear_holdout);self.holdout_clear.pack(side='left',padx=8)
        self.holdout_show=ttk.Button(holdout_frame,text='查看完整保留案例',command=lambda:self.show_cases(held_out=True));self.holdout_show.pack(anchor='w',pady=4)
        self.layout.wrapped_label(body, wraplength=700, text=(
            '每次按下開始，只執行一輪：AI 改碼 → 副本測試 → 套用 → 私人 GitHub。'
            '測試會以目前帳號執行候選 Python 程式；副本是版本隔離，不是作業系統沙箱。'
            '發布成功後重新開啟助理，讓所有變更生效。')).pack(fill='x', padx=16, pady=10)
        buttons = ttk.Frame(body)
        buttons.pack(fill='x', padx=16)
        self.start = ttk.Button(buttons, text='開始改進並發布', command=self.begin)
        self.start.pack(side='left')
        self.stop = ttk.Button(buttons, text='停止（套用前）', command=self.cancel.set, state='disabled')
        self.stop.pack(side='left', padx=8)
        self.output = tk.Text(body, height=8, wrap='word', state='disabled')
        self.output.pack(fill='x', padx=16, pady=12)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_timer = self.window.after(150, self.poll)
        if initial_draft:
            self.goal.insert('1.0', initial_draft['goal'])
            for name, value in self.targets.items():
                value.set(name in initial_draft['selected'])
            from .acceptance import validate_suite
            self.imported_suite = validate_suite(initial_draft['acceptance'], initial_draft['selected'])
            self.example.configure(state='disabled')
            self.case_notice.set('已帶入你指定的回饋案例；預期值由你選定。可查看完整案例，再決定是否開始。')
        ttk.Button(frame, text='查看完整驗收案例', command=self.show_cases).pack(anchor='w')
        if not hasattr(self.root, '_assistant_evolution_windows'):
            self.root._assistant_evolution_windows = []
        self.root._assistant_evolution_windows.append(self)
        if getattr(self.root, '_assistant_closing', False):
            self.request_shutdown()

    def show_cases(self, *, held_out=False):
        window = tk.Toplevel(self.window)
        window.title('完整保留案例 · 不加入提案模型提示' if held_out else '完整驗收案例 · 開始後會傳給模型')
        fit_window(window, 800, 600)
        text = tk.Text(window, wrap='word', width=90, height=24)
        scrollbar = ttk.Scrollbar(window, orient='vertical', command=text.yview)
        scrollbar.pack(side='right', fill='y')
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(fill='both', expand=True)
        suite = self.holdout_suite if held_out else self.imported_suite
        text.insert('1.0', json.dumps(suite, ensure_ascii=False, indent=2) if suite else '未匯入保留案例。' if held_out else '未匯入案例；單一例子請查看主視窗。')
        text.configure(state='disabled')

    def import_holdout(self):
        if self.busy:return
        path=filedialog.askopenfilename(parent=self.window,filetypes=[('保留驗收案例','*.json')])
        if not path:return
        try:
            value=load_suite(path)
            self.holdout_suite=value
            self.holdout_notice.set(f"已匯入{len(value['cases'])}例；不加入本輪提案提示，改後需全數通過。")
        except (ValueError,OSError) as exc:
            messagebox.showerror('無法匯入保留案例',str(exc),parent=self.window)

    def clear_holdout(self):
        if not self.busy:
            self.holdout_suite=None;self.holdout_notice.set('尚未匯入保留案例。')

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
        if self.busy or getattr(self.root, '_assistant_closing', False):
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
        # Detach both input sets before the worker starts. Later UI edits cannot
        # change which cases this already-started run uses.
        from .acceptance import validate_suite
        try:
            suite=validate_suite(suite,selected) if suite is not None else None
            withheld=validate_suite(self.holdout_suite) if self.holdout_suite is not None else None
            if withheld is not None and suite is None:
                raise ValueError('保留驗收需搭配提供給模型的驗收案例或單一例子。')
        except ValueError as exc:
            messagebox.showerror('請確認驗收案例',str(exc),parent=self.window)
            return
        self.busy = True
        self.cancel.clear()
        self.start.configure(state='disabled')
        self.stop.configure(state='normal')

        def work():
            try:
                result = improve(goal, selected, cancel=self.cancel, acceptance=suite, holdout=withheld,
                                 progress=lambda state: self.events.put(('progress', state)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        try:
            threading.Thread(target=work, daemon=False).start()
        except Exception as exc:
            self.events.put(('error', '無法啟動自我改進：' + str(exc)))

    def poll(self):
        labels = {'preparing': '準備程式副本', 'generating': 'AI 正在產生程式變更',
                  'baseline_testing': '先量測改版前的驗收結果', 'acceptance_testing': '核對改前與改後的案例結果',
                  'holdout_baseline_testing': '量測原版的保留案例（不加入提案提示）',
                  'holdout_testing': '檢查候選的保留案例；未全數通過就不套用',
                  'needs_reproduction': '提供的案例原已通過，但保留案例有失敗；未改碼，請補上能重現問題的提供案例',
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
                    self.start.configure(state='disabled' if getattr(self.root, '_assistant_closing', False) else 'normal')
                    self.stop.configure(state='disabled')
        except queue.Empty:
            pass
        self.poll_timer = self.window.after(150, self.poll)

    def request_shutdown(self):
        self.cancel.set()
        self.start.configure(state='disabled')

    def close(self):
        if self.busy:
            self.cancel.set()
            self.append('已要求停止；若已開始套用／發布，請等候完成再關閉。')
        else:
            self.window.after_cancel(self.poll_timer)
            self.root._assistant_evolution_windows.remove(self)
            self.window.destroy()
