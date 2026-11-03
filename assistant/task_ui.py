import json
import queue
import threading
from .ui_workers import start_worker
from .data_window import DataWindow
import tkinter as tk
from tkinter import filedialog, ttk

from .tasks import execute_task, export_task, load_csv, plan_csv
from .workflows import propose_workflow
from .engine import CodexEngine

LABELS = {'planned': '待執行', 'running': '尚無完成結果（執行中／中斷）',
          'succeeded': '已驗證完成', 'failed': '失敗', 'cancelled': '已取消'}


class TaskWindow(DataWindow):
    shutdown_controls = ('choose_btn', 'plan_btn', 'propose_btn', 'execute_btn', 'export_btn', 'stop_btn')

    def __init__(self, parent, store, sid):
        self.store, self.sid = store, sid
        self.path = None
        self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.own_window(parent)
        self.window.title('工作工具 · CSV 計畫與驗證')
        self.window.geometry('920x800')
        self.window.minsize(820, 600)
        ttk.Label(self.window, text='選取 CSV，手動分組加總，或用文字規劃篩選、加總與平均。文字規劃只將需求與欄名傳給模型；計算在本機，先看計畫再執行。',
                  padding=12, wraplength=850).pack(fill='x')
        inputs = ttk.Frame(self.window, padding=(12, 0))
        inputs.pack(fill='x')
        self.choose_btn = ttk.Button(inputs, text='選取 CSV', command=self.choose)
        self.choose_btn.pack(side='left')
        ttk.Label(inputs, text='分組欄').pack(side='left', padx=(12, 4))
        self.group = ttk.Combobox(inputs, state='readonly', width=20)
        self.group.pack(side='left')
        ttk.Label(inputs, text='加總欄').pack(side='left', padx=(12, 4))
        self.value = ttk.Combobox(inputs, state='readonly', width=20)
        self.value.pack(side='left')
        self.plan_btn = ttk.Button(inputs, text='建立計畫', command=self.plan)
        self.plan_btn.pack(side='left', padx=8)
        self.file_label = tk.StringVar(value='尚未選取來源。')
        ttk.Label(self.window, textvariable=self.file_label, wraplength=850, padding=12).pack(fill='x')
        natural = ttk.Frame(self.window, padding=(12, 0, 12, 10))
        natural.pack(fill='x')
        ttk.Label(natural, text='工作需求').pack(side='left')
        self.request = ttk.Entry(natural)
        self.request.pack(side='left', fill='x', expand=True, padx=8)
        self.propose_btn = ttk.Button(natural, text='用文字規劃', command=self.propose)
        self.propose_btn.pack(side='right')
        listing_frame = ttk.Frame(self.window, padding=(12, 0))
        listing_frame.pack(fill='x')
        self.listing = ttk.Treeview(listing_frame, columns=('id', 'status', 'created'), show='headings', selectmode='browse', height=5)
        for name, title, width in [('id', '任務', 150), ('status', '狀態', 300), ('created', '建立時間（UTC）', 220)]:
            self.listing.heading(name, text=title)
            self.listing.column(name, width=width)
        scroll = ttk.Scrollbar(listing_frame, command=self.listing.yview)
        scroll.pack(side='right', fill='y')
        self.listing.configure(yscrollcommand=scroll.set)
        self.listing.pack(fill='x')
        self.listing.bind('<<TreeviewSelect>>', self.show)
        detail_frame = ttk.Frame(self.window, padding=12)
        detail_frame.pack(fill='both', expand=True)
        self.detail = tk.Text(detail_frame, wrap='word', state='disabled', font=('Microsoft JhengHei UI', 10))
        detail_scroll = ttk.Scrollbar(detail_frame, command=self.detail.yview)
        detail_scroll.pack(side='right', fill='y')
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(fill='both', expand=True)
        controls = ttk.Frame(self.window, padding=(12, 0))
        controls.pack(fill='x')
        self.execute_btn = ttk.Button(controls, text='執行選取計畫', command=self.execute, state='disabled')
        self.execute_btn.pack(side='left')
        self.stop_btn = ttk.Button(controls, text='停止', command=self.cancel.set, state='disabled')
        self.stop_btn.pack(side='left', padx=8)
        self.export_btn = ttk.Button(controls, text='匯出已驗證報告', command=self.export, state='disabled')
        self.export_btn.pack(side='right')
        self.notice = tk.StringVar(value='來源上限 2 MiB、50000 筆、5000 組。數值不接受空白、千分位或貨幣符號；報告為 JSON，不會覆寫既有檔案。')
        ttk.Label(self.window, textvariable=self.notice, wraplength=850, padding=12).pack(fill='x')
        self.refresh()
        self.schedule_poll()

    def choose(self):
        if self.unavailable() or self.busy:
            return
        path = filedialog.askopenfilename(parent=self.window, title='選取要彙總的 CSV', filetypes=[('CSV', '*.csv')])
        if path and not self.unavailable():
            try:
                _, headers, rows = load_csv(path)
                self.path = path
                self.group['values'] = headers
                self.value['values'] = headers
                self.group.set(headers[0])
                self.value.set(headers[-1])
                self.file_label.set(path + f' · {len(rows)} 筆資料')
            except (OSError, ValueError) as exc:
                self.notice.set(str(exc))

    def refresh(self, select=None):
        self.listing.delete(*self.listing.get_children())
        for row in self.store.tasks(self.sid):
            self.listing.insert('', 'end', iid=row['id'], values=(row['id'][:12], LABELS[row['status']], row['created_at']))
        if select:
            self.listing.selection_set(select)
        self.show()

    def selected(self):
        rows = self.listing.selection()
        return rows[0] if rows else None

    def show(self, event=None):
        id = self.selected()
        task = self.store.task(self.sid, id) if id else None
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        if task:
            plan = task['plan']
            text = f'狀態：{LABELS[task["status"]]}\n來源：{plan["source_path"]}\n分組欄：{plan["group_column"]}　加總欄：{plan["value_column"]}\n來源筆數：{plan["rows"]}\n來源指紋：{plan["source_sha256"]}\n\n'
            text += '\n'.join(f'{i}. {step}' for i, step in enumerate(plan['steps'], 1)) + '\n\n' + plan['rules']
            if plan.get('request'):
                text += '\n\n原始需求（請逐項比對計畫是否正確）：\n' + plan['request']
            text += '\n\n前五筆預覽：\n' + json.dumps(plan['preview_rows'], ensure_ascii=False, indent=2)
            if task['error']:
                text += '\n\n未完成原因：' + task['error']
            if task['result']:
                result_text = json.dumps(task['result'], ensure_ascii=False, indent=2)
                text += '\n\n已保存結果（完整報告可匯出）：\n' + result_text[:30000]
                if len(result_text) > 30000:
                    text += '\n[…預覽已省略，請匯出完整報告…]'
            self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')
        self.execute_btn.configure(state='normal' if task and task['status'] == 'planned' and not self.busy and not self.unavailable() else 'disabled')
        self.export_btn.configure(state='normal' if task and task['status'] == 'succeeded' and not self.busy and not self.unavailable() else 'disabled')

    def plan(self):
        if self.unavailable() or self.busy:
            return
        if not self.path:
            self.notice.set('請先選取 CSV。')
            return
        try:
            task = plan_csv(self.store, self.sid, self.path, self.group.get(), self.value.get())
            self.refresh(task['id'])
            self.notice.set('計畫已保存，尚未執行。請核對來源、欄位及規則後按「執行選取計畫」。')
        except (ValueError, OSError) as exc:
            self.notice.set(str(exc))

    def execute(self):
        if self.unavailable() or self.busy:
            return
        id = self.selected()
        if not id:
            return
        self.busy = True
        self.cancel.clear()
        self.choose_btn.configure(state='disabled')
        self.plan_btn.configure(state='disabled')
        self.propose_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.show()
        self.notice.set('正在執行並驗證，尚未完成。')
        def work():
            try:
                self.events.put((id, execute_task(self.store, self.sid, id, self.cancel), None))
            except Exception as exc:
                self.events.put((id, None, str(exc)))
        start_worker(work, lambda error: self.events.put((id, None, error)))

    def propose(self):
        if self.unavailable() or self.busy:
            return
        request = self.request.get().strip()
        if not self.path or not request:
            self.notice.set('請選取 CSV 並輸入工作需求，例如：只保留已付款，按類別加總金額，由大到小列前三組。')
            return
        path = self.path
        self.busy = True
        self.cancel.clear()
        for button in (self.choose_btn, self.plan_btn, self.propose_btn):
            button.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.show()
        self.notice.set('正在規劃；僅傳送需求與欄名，尚未執行。')
        def work():
            try:
                response = propose_workflow(self.store, self.sid, path, request, CodexEngine(self.store.path.parent), self.cancel)
                if response['status'] == 'planned':
                    task = response['task']
                    self.events.put((task['id'], task, None))
                else:
                    self.events.put((None, None, response.get('question') or response['reason']))
            except Exception as exc:
                self.events.put((None, None, str(exc)))
        start_worker(work, lambda error: self.events.put((None, None, error)))

    def poll(self):
        if not self.begin_poll():
            return
        try:
            id, task, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.choose_btn.configure(state='normal')
            self.plan_btn.configure(state='normal')
            self.propose_btn.configure(state='normal')
            self.stop_btn.configure(state='disabled')
            self.refresh(id)
            self.notice.set(error or ('計畫已保存但尚未執行；請比對原始需求與每一步，再按執行。' if task['status'] == 'planned' else
                '已驗證完成，結果已存本機；可另存 JSON 報告。' if task['status'] == 'succeeded' else task['error']))
        self.schedule_poll()

    def export(self):
        if self.unavailable() or self.busy:
            return
        id = self.selected()
        if not id:
            return
        path = filedialog.asksaveasfilename(parent=self.window, title='另存新報告', defaultextension='.json',
                    initialdir=self.store.path.parent, initialfile='task-' + id[:12] + '.json', filetypes=[('JSON', '*.json')])
        if path and not self.unavailable():
            try:
                result = export_task(self.store, self.sid, id, path)
                self.notice.set('已匯出並驗證：' + result['path'])
            except (OSError, ValueError) as exc:
                self.notice.set('未匯出：' + str(exc))
