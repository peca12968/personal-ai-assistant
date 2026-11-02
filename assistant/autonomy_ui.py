"""User-visible goals, bounded autonomous work, and resumable progress."""
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import autonomy
from . import artifacts
from .goal_conversation import describe as describe_conversation
from .ui_layout import ScrollableForm, fit_window
from .companion_profile import describe as describe_profile


class AutonomousWindow:
    def __init__(self, parent, store, sid, engine, *, from_chat=False, initial_objective=''):
        self.store, self.sid, self.engine = store, sid, engine
        self.busy = False
        self.active_goal = None
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        with store.connect() as db:
            title = db.execute('SELECT title FROM sessions WHERE id=?', (sid,)).fetchone()
        self.window.title('自主目標 · ' + (title['title'][:60] if title else 'AI 夥伴'))
        fit_window(self.window, 900, 780)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='給我一個目標，我會自行選擇查找、讀取、計算與整理步驟，保存行動紀錄與成果。',
                                  padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='本階段使用目前對話的已匯入文件與有效記憶，工具結果會傳給模型。每輪有步數上限，可停止與接續；成果產生後仍由你確認。',
                                  padding=12).pack(fill='x')
        self.objective = tk.Text(body, height=3, wrap='word')
        self.objective.pack(fill='x', padx=12)
        if initial_objective:
            self.objective.insert('1.0', initial_objective)
        self.from_chat = tk.BooleanVar(value=from_chat)
        self.chat_option = ttk.Checkbutton(body, text='新目標帶入本對話背景（近期討論、較早更正與已保存目標）', variable=self.from_chat)
        self.chat_option.pack(anchor='w', padx=12, pady=6)
        self.layout.wrapped_label(body, text='勾選後，保存目標時會固定當時的背景；可在下方查看，再開始。背景會交給模型，不會自動更新，也不增加檔案或CSV權限。', padding=12).pack(fill='x')
        self.file_delivery = tk.BooleanVar(value=False)
        self.file_option = ttk.Checkbutton(body, text='新目標需要交付文件（本機Markdown／TXT／JSON，不覆寫既有內容）', variable=self.file_delivery)
        self.file_option.pack(anchor='w', padx=12, pady=6)
        self.layout.wrapped_label(body, text='新目標的CSV分析來源（選取後，開始時可自行規劃並執行篩選、分組、加總與平均；使用已匯入副本）：', padding=12).pack(fill='x')
        self.csv_input = ttk.Combobox(body, state='readonly')
        self.csv_input.pack(fill='x', padx=12)
        self.csv_reload = ttk.Button(body, text='重新載入已匯入CSV清單', command=self.reload_csv_sources)
        self.csv_reload.pack(anchor='w', padx=12, pady=4)
        self.csv_choices = [None]
        self.reload_csv_sources()
        self.create = ttk.Button(body, text='保存新目標（使用目前參考文件）', command=self.new_goal)
        self.create.pack(anchor='w', padx=12, pady=8)
        self.listing = ttk.Treeview(body, columns=('status', 'objective'), show='headings', height=5)
        self.listing.heading('status', text='狀態')
        self.listing.column('status', width=130, stretch=False)
        self.listing.heading('objective', text='目標')
        self.listing.column('objective', width=600)
        self.listing.pack(fill='x', padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        self.layout.wrapped_label(body, text='延續已交付的工作：先選取成果，在上方填寫下一階段目標，再按下方按鈕。所選成果與當時補充會作為歷史背景傳給模型，不代表成果已驗收；來源以目前參考文件重新核對。', padding=12).pack(fill='x')
        self.follow = ttk.Button(body, text='帶入所選成果，建立上方的新目標', command=self.next_stage, state='disabled')
        self.follow.pack(anchor='w', padx=12, pady=4)
        self.previous = ttk.Button(body, text='查看上一階段', command=self.previous_stage, state='disabled')
        self.previous.pack(anchor='w', padx=12, pady=4)
        self.files = ttk.Button(body, text='開啟成果資料夾', command=self.open_files, state='disabled')
        self.files.pack(anchor='w', padx=12, pady=4)
        self.rebuild = ttk.Button(body, text='核對／重建已保存文件', command=self.rebuild_files, state='disabled')
        self.rebuild.pack(anchor='w', padx=12, pady=4)
        self.csv_results = ttk.Button(body, text='查看CSV計畫／結果與匯出', command=self.open_csv_results, state='disabled')
        self.csv_results.pack(anchor='w', padx=12, pady=4)
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

    def selected_csv_id(self):
        index = self.csv_input.current()
        return self.csv_choices[index] if 0 <= index < len(self.csv_choices) else None

    def reload_csv_sources(self):
        selected = self.selected_csv_id()
        docs = [d for d in self.store.documents(self.sid) if Path(d['name']).suffix.lower() == '.csv']
        self.csv_choices = [None] + [d['id'] for d in docs]
        self.csv_input['values'] = ['不啟用CSV分析'] + [f"D{d['id']} · {d['name']}" for d in docs]
        self.csv_input.current(self.csv_choices.index(selected) if selected in self.csv_choices else 0)

    def open_csv_results(self):
        from .autonomy_csv_ui import CsvResultsWindow
        CsvResultsWindow(self.window, self.store, self.sid, self.selected_id())

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
            gid = autonomy.create_goal(self.store, self.sid, self.objective.get('1.0', 'end'), file_delivery=self.file_delivery.get(), csv_document_id=self.selected_csv_id(), from_chat=self.from_chat.get())
            self.refresh(gid)
        except Exception as exc:
            messagebox.showerror('無法保存目標', str(exc), parent=self.window)

    def next_stage(self):
        if self.busy:
            return
        try:
            gid = autonomy.create_goal(self.store, self.sid, self.objective.get('1.0', 'end'), parent_id=self.selected_id(), file_delivery=self.file_delivery.get(), csv_document_id=self.selected_csv_id(), from_chat=self.from_chat.get())
            self.refresh(gid)
            self.notice.set('下一階段已保存；可先查看帶入背景，再開始自主工作。')
        except Exception as exc:
            messagebox.showerror('無法建立下一階段', str(exc), parent=self.window)

    def previous_stage(self):
        goal = autonomy.get_goal(self.store, self.sid, self.selected_id())
        if goal['previous_stage']:
            self.refresh(goal['previous_stage']['parent_goal_id'])

    def open_files(self):
        try:
            os.startfile(artifacts.directory(self.store, self.selected_id()))
        except (ValueError, OSError) as exc:
            messagebox.showinfo('尚無可開啟的成果資料夾', str(exc), parent=self.window)

    def rebuild_files(self):
        if self.busy:
            return
        try:
            gid = self.selected_id()
        except ValueError:
            return
        self.busy, self.active_goal = True, gid
        self.cancel.clear()
        self.csv_input.configure(state='disabled')
        for button in (self.start, self.create, self.reply, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option):
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.notice.set('正在核對／重建已保存的歷史內容，不會覆寫不同內容的同名檔案。')
        def work():
            try:
                result = artifacts.rebuild(self.store, self.sid, gid, self.cancel)
                self.events.put(('files_done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def select(self, event=None):
        try:
            goal = autonomy.get_goal(self.store, self.sid, self.selected_id())
        except ValueError:
            return
        self.start.configure(state='disabled' if self.busy or goal['status'] in ('report_ready', 'needs_input') else 'normal')
        self.reply.configure(state='normal' if not self.busy and goal['status'] == 'needs_input' else 'disabled')
        self.follow.configure(state='normal' if not self.busy and goal['status'] == 'report_ready' else 'disabled')
        self.previous.configure(state='normal' if goal['previous_stage'] else 'disabled')
        self.files.configure(state='normal' if goal['artifacts'] else 'disabled')
        self.rebuild.configure(state='normal' if goal['artifacts'] and not self.busy else 'disabled')
        self.csv_results.configure(state='normal' if goal['csv_plans'] else 'disabled')
        lines = ['目標：' + goal['objective'], '狀態：' + autonomy.LABELS.get(goal['status'], goal['status']),
                 goal['notice'], '參考來源：' + ', '.join(f"D{d['document_id']} {d['name']}" for d in goal['sources'])]
        lines += ['交付形式：' + ('本機文件' if goal['file_delivery'] else '視窗文字成果')]
        if goal['csv_source']:
            csv = goal['csv_source']
            lines += [f"自主CSV分析：D{csv['document_id']} {csv['name']} · {csv['rows']}筆資料；使用匯入副本。",
                      '已保存CSV計畫：' + str(len(goal['csv_plans'])) + '份（完整規格與結果請用CSV結果入口查看）']
        if goal['file_delivery']:
            lines += ['成果位置：' + str(self.store.path.resolve().parent / 'autonomy-artifacts' / goal['id'])]
        if goal['artifacts']:
            labels = {'prepared': '已保存內容，尚未核對檔案', 'verified': '曾核對一致', 'uncertain': '待處理'}
            lines += ['已保存文件（可能包含未交付草稿；狀態不是即時檔案檢查）：']
            for item in goal['artifacts']:
                lines += [f"{item['name']} · {item['size_bytes']} bytes · {labels.get(item['recorded_status'], item['recorded_status'])} · 最近核對：{item['verified_at'] or '無'}" +
                          ('\n' + item['error'] if item['error'] else '')]
        if goal['question']:
            lines += ['需要你的補充：' + goal['question']]
        if goal['notes']:
            lines += ['本階段的問題與補充紀錄：', goal['notes']]
        if goal['conversation_context'] is not None:
            lines += [describe_conversation(goal['conversation_context'])]
        previous = goal['previous_stage']
        if previous:
            lines += ['沿用的上一階段：' + previous['parent_goal_id'], '原目標：' + previous['objective'],
                      '歷史成果（未代表已驗收）：', previous['report'],
                      '當時的問題與補充紀錄：', previous['user_notes'] or '無',
                      '上一階段與本階段的文件版本：' + ('相同，引用仍需重新讀取' if previous['sources_match_current'] else '不同，需重新核對')]
        for step in goal['steps']:
            observation = dict(step['observation'])
            profile = observation.pop('companion_profile', None)
            lines += [f"步驟 {step['sequence']} · {step['action']['tool']} · {step['action']['note']}",
                      json.dumps(observation, ensure_ascii=False, indent=2)]
            if profile:
                lines += ['此步使用的夥伴設定：\n' + describe_profile(profile)]
        if goal['result']:
            lines += ['成果（待你確認）：', goal['result']]
            lines += ['此目標已交付成果；後續工作請保存新目標。']
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
        self.csv_input.configure(state='disabled')
        for button in (self.start, self.create, self.reply, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option):
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
                    self.csv_input.configure(state='readonly')
                    for button in (self.start, self.create, self.reply, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option):
                        button.configure(state='normal')
                    self.stop.configure(state='disabled')
                    self.notice.set(value if kind == 'error' else '已核對／重建保存文件；未重新驗證歷史內容。' if kind == 'files_done' else autonomy.LABELS.get(value['status'], value['status']))
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
