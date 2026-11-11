"""User-visible goals, bounded autonomous work, and resumable progress."""
import json
import os
import queue
import sqlite3
import threading
from .ui_workers import start_worker
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import autonomy
from . import continuous
from . import artifacts
from . import work_plan
from . import goal_drafts
from .goal_conversation import describe as describe_conversation
from .ui_layout import ScrollableForm, fit_window
from .companion_profile import describe as describe_profile


class AutonomousWindow:
    def __init__(self, parent, store, sid, engine, *, from_chat=False, initial_objective='', discuss_report=None):
        self.store, self.sid, self.engine = store, sid, engine
        self.discuss_report = discuss_report
        self.busy = False
        self.active_goal = None
        self.goal_draft_result = None
        self.draft_origin = None
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        with store.connect() as db:
            title = db.execute('SELECT title FROM sessions WHERE id=?', (sid,)).fetchone()
        self.window.title('自主目標 · ' + (title['title'][:60] if title else 'AI 夥伴'))
        fit_window(self.window, 900, 780)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='給我一個目標，我會自行選擇查找、讀取、文件差異比較、JSON欄位查讀／條件篩選統計、數值／日期計算與整理步驟，保存行動紀錄與成果。',
                                  padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='本階段使用目前對話的已匯入文件與有效記憶，工具結果會傳給模型。每輪有步數上限，可停止與接續；成果產生後仍由你確認。',
                                  padding=12).pack(fill='x')
        objective_frame = ttk.Frame(body)
        objective_frame.pack(fill='x', padx=12)
        self.objective = tk.Text(objective_frame, height=5, wrap='word')
        objective_scroll = ttk.Scrollbar(objective_frame, command=self.objective.yview)
        self.objective.configure(yscrollcommand=objective_scroll.set)
        objective_scroll.pack(side='right', fill='y')
        self.objective.pack(side='left', fill='both', expand=True)
        if initial_objective:
            self.objective.insert('1.0', initial_objective)
        self.draft_button = ttk.Button(body, text='從本對話整理目標草稿', command=self.draft_from_chat)
        self.draft_button.pack(anchor='w', padx=12, pady=4)
        self.draft_preview = ttk.Button(body, text='查看整理草稿與原話', command=self.preview_draft, state='disabled')
        self.draft_preview.pack(anchor='w', padx=12, pady=4)
        self.layout.wrapped_label(body, text='整理會把目前輸入與本對話有限背景送給模型，填入可編輯的目標、限制與交付文字。只整理草稿；核對後再保存並開始，CSV／文件／帶入背景選項仍由你選擇。未保存草稿只留在本視窗。', padding=12).pack(fill='x')
        self.from_chat = tk.BooleanVar(value=from_chat)
        self.history_search = tk.BooleanVar(value=False)
        self.chat_option = ttk.Checkbutton(body, text='新目標帶入本對話背景（開場、近期討論、更正與已保存目標）', variable=self.from_chat, command=self.toggle_chat)
        self.chat_option.pack(anchor='w', padx=12, pady=6)
        self.layout.wrapped_label(body, text='勾選後，保存目標時會固定當時的背景；可在下方查看，再開始。背景會交給模型，不會自動更新，也不增加檔案或CSV權限。', padding=12).pack(fill='x')
        self.history_option = ttk.Checkbutton(body, text='另存本對話歷史供此目標查找', variable=self.history_search, command=self.toggle_history)
        self.history_option.pack(anchor='w', padx=12, pady=6)
        self.layout.wrapped_label(body, text='選用：固定保存最近最多1000則、每則前2000字、總100萬字；排除本機記憶命令。模型只收到查找命中的片段，不是整份歷史。超出範圍會標示，後續新訊息不自動加入。', padding=12).pack(fill='x')
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
        self.layout.wrapped_label(body, text='延續工作：先選取已交付成果，或已有步驟且已停止的工作，再填寫下一階段目標。將帶入成果或有限進度摘要、問題與當時補充；可接續已滿32步的工作。新階段不會自行開始，來源與需要的引用會重新核對。', padding=12).pack(fill='x')
        self.follow = ttk.Button(body, text='帶入所選成果／進度，建立上方的新目標', command=self.next_stage, state='disabled')
        self.follow.pack(anchor='w', padx=12, pady=4)
        self.previous = ttk.Button(body, text='查看上一階段', command=self.previous_stage, state='disabled')
        self.previous.pack(anchor='w', padx=12, pady=4)
        self.discuss = ttk.Button(body, text='將所選成果帶入聊天草稿（不送出）', command=self.discuss_selected, state='disabled')
        self.discuss.pack(anchor='w', padx=12, pady=4)
        self.layout.wrapped_label(body, text='選取已保存成果後，可帶回原對話討論。保留聊天中未送出的問題，附上歷史成果與當時補充；只存本機草稿，核對後自行送出，不會驗收或接續工作。', padding=12).pack(fill='x')
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
        continuous_controls = ttk.Frame(body, padding=12)
        continuous_controls.pack(fill='x')
        ttk.Label(continuous_controls, text='持續執行：新增最多').pack(side='left')
        self.continuous_steps = ttk.Spinbox(continuous_controls, from_=1, to=32, width=4)
        self.continuous_steps.set('24')
        self.continuous_steps.pack(side='left')
        ttk.Label(continuous_controls, text='步，最多').pack(side='left')
        self.continuous_minutes = ttk.Spinbox(continuous_controls, from_=1, to=120, width=4)
        self.continuous_minutes.set('30')
        self.continuous_minutes.pack(side='left')
        ttk.Label(continuous_controls, text='分鐘').pack(side='left')
        self.continue_start = ttk.Button(continuous_controls, text='開始持續執行', command=lambda: self.begin(continuous_mode=True))
        self.continue_start.pack(side='left', padx=8)
        self.layout.wrapped_label(body, text='持續執行只處理所選目標，不需逐輪按接續。完成成果、需要補充、來源改變、連續三次工具錯誤或達上限即停止；目標累計仍最多32步。關閉程式會要求停止，重開不會自行啟動。', padding=12).pack(fill='x')
        self.reply = ttk.Button(body, text='回答目前問題', command=self.add_reply, state='disabled')
        self.reply.pack(anchor='w', padx=12, pady=6)
        self.guidance = ttk.Button(body, text='補充／更正此目標', command=self.add_guidance, state='disabled')
        self.guidance.pack(anchor='w', padx=12, pady=6)
        self.notice = tk.StringVar(value='選擇目標後開始；進度保存在本機。')
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.output = tk.Text(body, height=14, wrap='word', state='disabled')
        self.output.pack(fill='x', padx=12, pady=8)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_timer = self.window.after(150, self.poll)
        self.refresh()

    def draft_guard(self):
        if self.draft_origin and self.objective.get('1.0', 'end').strip() == self.draft_origin['objective_text'].strip():
            return self.draft_origin
        return None  # An explicit manual rewrite uses the ordinary goal editor.

    def toggle_chat(self):
        if not self.from_chat.get():
            self.history_search.set(False)

    def toggle_history(self):
        if self.history_search.get():
            self.from_chat.set(True)

    def draft_from_chat(self):
        if self.busy:
            return
        original = self.objective.get('1.0', 'end-1c')
        try:
            source = goal_drafts.capture(self.store, self.sid, original.strip())
        except (ValueError, OSError) as exc:
            messagebox.showerror('無法整理', str(exc), parent=self.window)
            return
        self.busy = True
        self.active_goal = None
        self.cancel.clear()
        self.csv_input.configure(state='disabled')
        for button in (self.start, self.continue_start, self.continuous_steps, self.continuous_minutes, self.budget, self.create, self.reply, self.guidance, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option, self.history_option, self.draft_button, self.discuss):
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.notice.set('正在整理目標草稿；不會保存或開始工作，可停止。')
        def work():
            try:
                result = goal_drafts.generate(source, self.engine, self.cancel)
                self.events.put(('draft_done', (result, original)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        start_worker(work, lambda error: self.events.put(('error', error)))

    def receive_draft(self, result, original):
        self.goal_draft_result = result
        self.draft_preview.configure(state='normal')
        if self.cancel.is_set():
            self.notice.set('已停止，原目標草稿未變更。')
            return
        if self.objective.get('1.0', 'end-1c') != original:
            self.notice.set('整理期間你已修改目標，保留目前文字；可查看整理草稿與原話，或重新整理。')
            return
        try:
            with self.store.connect() as db:
                db.execute('BEGIN')
                goal_drafts.ensure_current(db, self.sid, result)
        except ValueError as exc:
            self.notice.set(str(exc))
            return
        if not result['objective_text']:
            self.notice.set('尚無明確工作目標；請查看待釐清問題，原輸入保留。')
            return
        self.objective.delete('1.0', 'end')
        self.objective.insert('1.0', result['objective_text'])
        self.draft_origin = result
        self.notice.set('已填入目標草稿，請核對原話及修改文字，再選擇所需選項、保存並開始。尚未建立目標。')

    def preview_draft(self):
        result = self.goal_draft_result
        if result is None:
            return
        window = tk.Toplevel(self.window)
        window.title('目標草稿與來源（未自動保存）')
        fit_window(window, 800, 650)
        frame = ttk.Frame(window); frame.pack(fill='both', expand=True)
        field = tk.Text(frame, wrap='word', padx=12, pady=12)
        scroll = ttk.Scrollbar(frame, command=field.yview)
        field.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); field.pack(fill='both', expand=True)
        lines = ['模型草稿，需核對語意；原話存在不等於已確認決策。',
                 result['objective_text'] or '尚無可辨認的目標。',
                 '待釐清：\n' + '\n'.join(result['proposal']['questions'])]
        for item in result['proposal']['items']:
            lines += [{'goal': '目標', 'constraint': '限制', 'deliverable': '交付'}[item['kind']] + '：' + item['text']]
            lines += [ref['key'] + ' 原話：' + ref['quote'] for ref in item['sources']]
        lines += ['本次有限來源快照：', goal_drafts.describe_source(result['source']),
                  '整理時的目標輸入：' + result['source']['current_request']]
        field.insert('1.0', '\n\n'.join(lines)); field.configure(state='disabled')

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
            gid = autonomy.create_goal(self.store, self.sid, self.objective.get('1.0', 'end'), file_delivery=self.file_delivery.get(), csv_document_id=self.selected_csv_id(), from_chat=self.from_chat.get(), goal_draft=self.draft_guard(), history_search=self.history_search.get())
            self.refresh(gid)
        except Exception as exc:
            messagebox.showerror('無法保存目標', str(exc), parent=self.window)

    def next_stage(self):
        if self.busy:
            return
        try:
            gid = autonomy.create_goal(self.store, self.sid, self.objective.get('1.0', 'end'), parent_id=self.selected_id(), file_delivery=self.file_delivery.get(), csv_document_id=self.selected_csv_id(), from_chat=self.from_chat.get(), goal_draft=self.draft_guard(), history_search=self.history_search.get())
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
        for button in (self.start, self.continue_start, self.continuous_steps, self.continuous_minutes, self.budget, self.create, self.reply, self.guidance, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option, self.history_option, self.draft_button, self.discuss):
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.notice.set('正在核對／重建已保存的歷史內容，不會覆寫不同內容的同名檔案。')
        def work():
            try:
                result = artifacts.rebuild(self.store, self.sid, gid, self.cancel)
                self.events.put(('files_done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        start_worker(work, lambda error: self.events.put(('error', error)))

    def select(self, event=None):
        try:
            goal = autonomy.get_goal(self.store, self.sid, self.selected_id())
        except ValueError:
            return
        self.start.configure(state='disabled' if self.busy or goal['status'] in ('report_ready', 'needs_input') else 'normal')
        self.continue_start.configure(state=self.start['state'])
        self.reply.configure(state='normal' if not self.busy and goal['status'] == 'needs_input' else 'disabled')
        can_recheck_report = goal['status'] == 'report_ready' and not goal['file_delivery']
        reference_report = can_recheck_report and autonomy.decision_table.reference_report(goal['steps'], goal['result'])
        version_report = can_recheck_report and autonomy.decision_table.document_version_report(goal['steps'], goal['result'])
        supplement_report = can_recheck_report and autonomy.decision_table.document_supplement_report(goal['steps'], goal['result'])
        self.guidance.configure(state='normal' if not self.busy and (
            reference_report or version_report or supplement_report
            or goal['status'] in ('pending','paused','failed','needs_input')) else 'disabled')
        self.follow.configure(state='normal' if not self.busy and autonomy.goal_stages.eligible(goal['status'], len(goal['steps'])) else 'disabled')
        self.previous.configure(state='normal' if goal['previous_stage'] else 'disabled')
        self.discuss.configure(state='normal' if self.discuss_report and not self.busy and goal['status'] == 'report_ready' else 'disabled')
        self.files.configure(state='normal' if goal['artifacts'] else 'disabled')
        self.rebuild.configure(state='normal' if goal['artifacts'] and not self.busy else 'disabled')
        self.csv_results.configure(state='normal' if goal['csv_plans'] else 'disabled')
        lines = ['目標：' + goal['objective'], '狀態：' + autonomy.LABELS.get(goal['status'], goal['status']),
                 goal['notice'], '參考來源：' + ', '.join(f"D{d['document_id']} {d['name']}" for d in goal['sources'])]
        if goal['result']:
            lines += ['成果（待你確認）：', goal['result'],
                      '匯入所缺版本規則後，可按「補充／更正此目標」指定補件原行。' if supplement_report
                      else '如有本批正式版本更正，可按「補充／更正此目標」重核成果。' if version_report
                      else '如有新的明確欄位更正，可按「補充／更正此目標」依序重核各分支。' if reference_report
                      else '此目標已交付成果；後續工作請保存新目標。']
        if goal['work_plan']:
            lines += [work_plan.describe(goal['work_plan'])]
        if scope := goal.get('decision_rule_scope'):
            for key, label in (('ranges', '宣告的決策規則範圍'), ('missing_ranges', '尚未完整查讀的規則行')):
                tokens = [f"D{row['document_id']} L{row['start_line']}–L{row['end_line']}" for row in scope[key]]
                lines += [label + ' · ' + ('、'.join(tokens) or '無')]
        if scope := goal.get('quantity_adoption_scope'):
            lines += ['宣告的數量採用欄位 · ' + scope['field']]
            for key, label in (('ranges', '宣告的數量採用範圍'), ('missing_ranges', '尚未完整查讀的採用行')):
                tokens = [f"D{row['document_id']} L{row['start_line']}–L{row['end_line']}" for row in scope[key]]
                lines += [label + ' · ' + ('、'.join(tokens) or '無')]
        lines += ['交付形式：' + ('本機文件' if goal['file_delivery'] else '視窗文字成果')]
        if goal['continuous_runs']:
            lines += ['最近10次持續執行紀錄（本機保存；執行中標記不代表程序仍存活）：']
            for run in goal['continuous_runs']:
                count = (run['end_step'] if run['end_step'] is not None else len(goal['steps'])) - run['start_step']
                lines += [f"{run['started_at']} UTC · 設定最多{run['max_steps']}步／{run['max_seconds'] // 60}分鐘 · 已記錄{count}步 · {continuous.REASONS.get(run['reason'], run['reason'])}"]
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
        if goal.get('partial_result'):
            label = '更正前的暫表（歷史）' if goal.get('reference_review') else '待補期間的成果（尚待定稿）'
            lines += [label, goal['partial_result']]
        if goal.get('reference_review'):
            lines += [autonomy.decision_table.describe_waiting_review(goal['reference_review'])]
        if goal.get('answered_reference_review'):
            lines += [autonomy.decision_table.describe_answered_review(goal['answered_reference_review'])]
        if goal['question']:
            lines += ['需要你的補充：' + goal['question']]
        if goal.get('withdrawn_questions'):
            lines += ['已依更正撤下的問題（原步驟保留）']
            lines += [f"步驟 {row['ask_sequence']} · {row['guidance_citation']} · {row['question']}"
                      for row in goal['withdrawn_questions']]
        if goal.get('reply_sources'):
            lines += ['使用者回覆來源（本目標）']
            lines += [f"{source['citation']} · {source['question']}\n{source['answer']}"
                      for source in goal['reply_sources']]
        if goal.get('guidance_sources'):
            lines += ['主動更正來源（本目標）']
            lines += [f"{source['citation']} · {source['content']}" for source in goal['guidance_sources']]
        if goal['notes']:
            lines += ['本階段的問題與補充紀錄：', goal['notes']]
        if goal.get('conversation_history') is not None:
            history = goal['conversation_history']
            lines += [f"可搜尋歷史：保存{history['captured_message_count']}／{history['source_message_count']}則，{history['captured_characters']}字；時間{history['captured_at']}。",
                      '部分訊息或內容已省略。' if history['messages_limited'] or history['content_truncated'] else '來源訊息在此副本界線內完整保存。',
                      '開始工作後可查找此固定副本；不包含後來的新訊息，也不授權其他對話。']
        if goal['conversation_context'] is not None:
            lines += [describe_conversation(goal['conversation_context'])]
        previous = goal['previous_stage']
        if previous:
            lines += ['沿用的上一階段：' + previous['parent_goal_id'], '原目標：' + previous['objective'],
                      '歷史成果（未代表已驗收）：', previous['report'],
                      '當時的問題與補充紀錄：', previous['user_notes'] or '無',
                      '上一階段與本階段的文件版本：' + ('相同，引用仍需重新讀取' if previous['sources_match_current'] else '不同，需重新核對')]
            if previous.get('source_comparison'):
                lines += [autonomy.goal_stages.describe_source_comparison(previous['source_comparison'])]
            if 'checkpoint' in previous:
                saved = previous['checkpoint']
                if saved.get('work_plan'):
                    lines += ['上一階段的歷史工作清單：', work_plan.describe(saved['work_plan'])]
                lines += [f"上一階段尚未交付，保留{saved['step_count']}步的有限進度：", saved['notice'],
                          '當時未回答的問題：' + (saved['question'] or '無'), saved['omissions']]
                for item in saved['activity']:
                    purpose = item['purpose'].get('value', item['purpose'].get('json_preview', ''))
                    lines += [f"歷史步驟 {item['sequence']} · {item['tool']} · {purpose}",
                              '有限參數：' + json.dumps(item['request'], ensure_ascii=False),
                              '歷史結果摘要：' + json.dumps(item['observation_metadata'], ensure_ascii=False)]
                lines += ['舊文件與CSV計畫僅在上一階段保留；可按「查看上一階段」檢查。']
        for step in goal['steps']:
            observation = dict(step['observation'])
            profile = observation.pop('companion_profile', None)
            lines += [f"步驟 {step['sequence']} · {step['action']['tool']} · {step['action']['note']}",
                      json.dumps(observation, ensure_ascii=False, indent=2)]
            if profile:
                lines += ['此步使用的夥伴設定：\n' + describe_profile(profile)]
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('1.0', '\n\n'.join(lines))
        self.output.configure(state='disabled')

    def discuss_selected(self):
        if self.busy or self.discuss_report is None:
            return
        try:
            self.discuss_report(self.sid, self.selected_id())
            self.notice.set('已帶入原對話的聊天草稿，尚未送出；請到主視窗核對內容與問題。')
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.notice.set(str(exc))

    def add_guidance(self):
        if self.busy:return
        from .goal_guidance_ui import GoalGuidanceWindow
        gid=self.selected_id()
        return GoalGuidanceWindow(self.window,self.store,self.sid,gid,lambda:self.refresh(gid))

    def add_reply(self):
        if self.busy:
            return
        from .goal_reply_ui import GoalReplyWindow
        return GoalReplyWindow(self.window, self.store, self.sid, self.selected_id(),
                               prepare_resume=self.reply_budget, saved=self.reply_saved)

    def reply_budget(self):
        if self.busy:
            raise ValueError('原視窗仍在工作，請等候結束或先只保存回答。')
        budget = int(self.budget.get())
        if not 1 <= budget <= 12:
            raise ValueError('原視窗的一般本輪步數需為1到12。')
        return budget

    def reply_saved(self, gid, budget):
        self.refresh(gid)
        if budget is not None:
            started = self.begin(goal_id=gid, step_budget=budget)
            if not started:
                # The saved answer stays committed. Keep the busy UI until its
                # queued launch failure is consumed, preventing a new run race.
                raise RuntimeError('接續未啟動；已保存的回答保留，請在原視窗查看原因。')
            return True
        return False

    def begin(self, *, continuous_mode=False, goal_id=None, step_budget=None):
        if self.busy:
            return
        try:
            gid = goal_id or self.selected_id()
            if continuous_mode:
                budget, seconds = int(self.continuous_steps.get()), int(self.continuous_minutes.get()) * 60
                continuous.validate_limits(budget, seconds)
            else:
                budget = int(self.budget.get()) if step_budget is None else step_budget
                if not 1 <= budget <= 12:
                    raise ValueError('每輪步數需為1到12。')
        except ValueError as exc:
            messagebox.showinfo('請確認目標', str(exc), parent=self.window)
            return
        self.busy = True
        self.active_goal = gid
        self.cancel.clear()
        self.csv_input.configure(state='disabled')
        for button in (self.start, self.continue_start, self.continuous_steps, self.continuous_minutes, self.budget, self.create, self.reply, self.guidance, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option, self.history_option, self.draft_button, self.discuss):
            button.configure(state='disabled')
        self.stop.configure(state='normal')
        self.notice.set('正在自主選擇下一步；可隨時停止。')
        def work():
            try:
                runner = continuous.run_continuous if continuous_mode else autonomy.run_goal
                options = {'max_seconds': seconds} if continuous_mode else {}
                result = runner(self.store, self.sid, gid, self.engine, max_steps=budget, **options,
                    cancel=self.cancel, progress=lambda event: self.events.put(('progress', event)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        return start_worker(work, lambda error: self.events.put(('error', '自主工作／接續未啟動：' + error)))

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
                    for button in (self.start, self.continue_start, self.continuous_steps, self.continuous_minutes, self.budget, self.create, self.reply, self.guidance, self.follow, self.rebuild, self.file_option, self.csv_reload, self.chat_option, self.history_option, self.draft_button, self.discuss):
                        button.configure(state='normal')
                    self.stop.configure(state='disabled')
                    if kind == 'draft_done':
                        self.receive_draft(*value)
                        self.active_goal = None
                        self.select()
                        continue
                    self.notice.set(value if kind == 'error' else '已核對／重建保存文件；未重新驗證歷史內容。' if kind == 'files_done' else autonomy.LABELS.get(value['status'], value['status']))
                    self.refresh(self.active_goal)
                    self.active_goal = None
        except queue.Empty:
            pass
        self.poll_timer = self.window.after(150, self.poll)

    def close(self):
        self.cancel.set()
        if self.busy:
            self.notice.set('正在停止自主流程，已保存步驟會保留。')
            self.window.after(200, self.close)
        else:
            self.window.after_cancel(self.poll_timer)
            self.window.destroy()
