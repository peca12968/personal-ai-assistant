import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .engine import Assistant
from .ui_layout import ScrollableForm

BG, PANEL, FG, MUTED, ACCENT = "#101820", "#192530", "#eef4f7", "#9db0bd", "#52d6ad"


class App:
    def __init__(self, root, store, engine):
        self.root, self.store = root, store
        self.assistant = Assistant(store, engine)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.autonomous_windows = []
        self.last_answer = None
        root.title("個人 AI 助理")
        from .engine import ROOT
        if store.path.parent.resolve() != (ROOT / 'data').resolve():
            root.title('個人 AI 助理 · ' + str(store.path.parent.resolve()))
        root.geometry("1120x780")
        root.minsize(840, 600)
        root.configure(bg=BG)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=("Microsoft JhengHei UI", 10), padding=(12, 7))
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        header = tk.Frame(root, bg=BG, padx=24, pady=18)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="個人 AI 助理", bg=BG, fg=FG,
                 font=("Microsoft JhengHei UI", 22, "bold")).pack(side="left")
        ttk.Button(header, text="工作工具", command=self.work_tools).pack(side='right', padx=(12, 0))
        ttk.Button(header, text='自我改進', command=self.self_improve).pack(side='right', padx=(8, 0))
        ttk.Button(header, text='自主目標', command=self.autonomous_goals).pack(side='right', padx=(8, 0))
        tk.Label(header, text="聊天建議  ·  本機記憶", bg=BG, fg=ACCENT,
                 font=("Microsoft JhengHei UI", 11)).pack(side="right")
        body = tk.Frame(root, bg=BG, padx=18)
        body.grid(row=1, column=0, sticky="nsew")
        side_shell = tk.Frame(body, bg=PANEL, width=220)
        side_shell.pack(side="left", fill="y", padx=(0, 14))
        side_shell.pack_propagate(False)
        style.configure('Sidebar.TFrame', background=PANEL)
        self.sidebar_layout = ScrollableForm(side_shell, event_root=root)
        self.sidebar_layout.configure(style='Sidebar.TFrame')
        self.sidebar_layout.canvas.configure(bg=PANEL)
        sidebar = self.sidebar_layout.body
        sidebar.configure(style='Sidebar.TFrame', padding=12)
        self.new_btn = ttk.Button(sidebar, text="＋ 新對話", command=self.new_session)
        self.new_btn.pack(fill="x", pady=(0, 14))
        self.review_btn = ttk.Button(sidebar, text='接續與回顧', command=self.review_goals)
        self.review_btn.pack(fill='x', pady=(0, 8))
        tk.Label(sidebar, text="對話紀錄", bg=PANEL, fg=MUTED).pack(anchor="w")
        self.session_list = tk.Listbox(sidebar, bg=PANEL, fg=FG, bd=0,
            selectbackground="#315766", highlightthickness=0, height=5, font=("Microsoft JhengHei UI", 10))
        self.session_list.pack(fill="both", expand=True, pady=10)
        self.session_list.bind("<<ListboxSelect>>", self.select_session)
        ttk.Button(sidebar, text="對話目標與限制", command=self.edit_brief).pack(fill="x", pady=4)
        ttk.Button(sidebar, text="參考文件", command=self.manage_documents).pack(fill="x", pady=4)
        memory_menu = ttk.Menubutton(sidebar, text='記憶與夥伴設定')
        menu = tk.Menu(memory_menu, tearoff=False)
        menu.add_command(label='管理長期記憶', command=self.memories)
        menu.add_command(label='夥伴設定', command=self.companion_settings)
        memory_menu.configure(menu=menu)
        memory_menu.pack(fill='x', pady=4)
        ttk.Button(sidebar, text="回饋紀錄", command=self.feedback_report).pack(fill="x", pady=4)
        ttk.Button(sidebar, text="匯出目前對話", command=self.export).pack(fill="x", pady=4)
        self.backup_btn = ttk.Button(sidebar, text="本機備份與還原", command=self.backups)
        self.backup_btn.pack(fill="x", pady=4)
        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        chat_frame = tk.Frame(main, bg=PANEL)
        chat_frame.grid(row=0, column=0, sticky="nsew")
        self.chat = tk.Text(chat_frame, bg=PANEL, fg=FG, insertbackground=FG,
            wrap="word", padx=22, pady=20, relief="flat", state="disabled",
            font=("Microsoft JhengHei UI", 11), spacing3=8, height=12)
        scroll = ttk.Scrollbar(chat_frame, command=self.chat.yview)
        self.chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("user", foreground=ACCENT, font=("Microsoft JhengHei UI", 11, "bold"))
        self.chat.tag_configure("assistant", foreground="#9ac9ff", font=("Microsoft JhengHei UI", 11, "bold"))
        self.chat.tag_configure("note", foreground=MUTED)
        feedback = tk.Frame(main, bg=BG, pady=8)
        feedback.grid(row=1, column=0, sticky="ew")
        self.good_btn = ttk.Button(feedback, text="有幫助", command=lambda: self.rate(1))
        self.good_btn.pack(side="left")
        self.bad_btn = ttk.Button(feedback, text="需要改進／更正", command=lambda: self.rate(-1))
        self.bad_btn.pack(side="left", padx=6)
        self.context_btn = ttk.Button(feedback, text="查看本次參考", command=self.inspect_context)
        self.context_btn.pack(side="left")
        self.input = tk.Text(main, height=4, bg=PANEL, fg=FG, insertbackground=FG,
            wrap="word", relief="flat", padx=12, pady=10, font=("Microsoft JhengHei UI", 11))
        self.input.grid(row=2, column=0, sticky="ew")
        self.input.bind("<Control-Return>", lambda event: self.send())
        actions = tk.Frame(main, bg=BG, pady=10)
        actions.grid(row=3, column=0, sticky="ew")
        tk.Label(actions, text="Ctrl + Enter 傳送  ·  參考內容會傳給 Codex", bg=BG, fg=MUTED).pack(side="left")
        self.send_btn = ttk.Button(actions, text="傳送", command=self.send)
        self.send_btn.pack(side="right")
        self.stop_btn = ttk.Button(actions, text="停止", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="right", padx=6)
        self.retry_btn = ttk.Button(actions, text='重試最後一題', command=self.retry_answer)
        self.retry_btn.pack(side='right', padx=6)
        self.status = tk.StringVar(value="就緒 · 記憶與對話只存本機，不納入 GitHub 程式更新")
        tk.Label(root, textvariable=self.status, bg=BG, fg=MUTED, anchor="w", padx=24, pady=10).grid(row=2, column=0, sticky="ew")
        sessions = store.sessions()
        self.sid = sessions[0]["id"] if sessions else store.new_session()
        self.refresh_sessions()
        self.render()
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def self_improve(self):
        from .evolution_ui import EvolutionWindow
        EvolutionWindow(self.root)

    def autonomous_goals(self, sid=None, gid=None):
        from .autonomy_ui import AutonomousWindow
        sid = sid or self.sid
        self.autonomous_windows = [window for window in self.autonomous_windows if window.window.winfo_exists()]
        existing = next((window for window in self.autonomous_windows if window.sid == sid), None)
        if existing is None:
            existing = AutonomousWindow(self.root, self.store, sid, self.assistant.engine)
            self.autonomous_windows.append(existing)
        if gid:
            existing.refresh(gid)
        existing.window.lift()

    def refresh_review_count(self):
        from .goal_review import snapshot
        count = snapshot(self.store, limit=1)['visible_count']
        self.review_btn.configure(text=f'接續與回顧（{count}）')

    def review_goals(self):
        from .goal_review_ui import GoalReviewWindow
        GoalReviewWindow(self.root, self.store, self.autonomous_goals, self.refresh_review_count)

    def refresh_sessions(self):
        self.session_rows = self.store.sessions()
        self.session_list.delete(0, "end")
        for i, row in enumerate(self.session_rows):
            self.session_list.insert("end", row["title"])
            if row["id"] == self.sid:
                self.session_list.selection_set(i)

    def append(self, role, text):
        self.chat.configure(state="normal")
        label = {"user": "你", "assistant": "助理", "note": "提示"}[role]
        self.chat.insert("end", label + "\n", role)
        self.chat.insert("end", text + "\n\n")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def render(self):
        self.refresh_review_count()
        from .answer_runs import recover, runs, ERROR_LABELS
        recovery_notice = ''
        try:
            recover(self.store, self.sid)
        except (OSError, sqlite3.Error, ValueError):
            recovery_notice = '目前無法核對本機回答流程的工作鎖；沒有將進行中紀錄改成中斷。'
        attempts = {}
        for run in reversed(runs(self.store, self.sid)):
            attempts[run['user_message_id']] = run
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        if recovery_notice:
            self.append('note', recovery_notice)
        rows = self.store.messages(self.sid, 500)
        self.last_answer = None
        if not rows:
            self.append("note", "告訴我你想完成什麼。\n我可以協助釐清需求、規劃步驟、分析問題與檢查解法。\n\n你可以在「管理長期記憶」保存偏好，並對回答留下更正。")
        for row in rows:
            self.append(row["role"], row["content"])
            attempt = attempts.get(row['id'])
            if attempt and attempt['status'] != 'succeeded':
                self.append('note', '回答狀態：' + ('本機流程尚未保存完成狀態。' if attempt['status'] == 'running' else
                    ERROR_LABELS.get(attempt['error_code'], ERROR_LABELS['unexpected_error'])))
            if row["role"] == "assistant":
                self.last_answer = row["id"]
        if rows and rows[-1]['role'] != 'assistant':
            self.last_answer = None

    def new_session(self):
        if self.busy:
            return
        self.sid = self.store.new_session()
        self.refresh_sessions()
        self.render()

    def select_session(self, event=None):
        selection = self.session_list.curselection()
        if selection and not self.busy:
            self.sid = self.session_rows[selection[0]]["id"]
            self.render()

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.send_btn, self.new_btn, self.session_list, self.good_btn, self.bad_btn, self.context_btn, self.retry_btn):
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def retry_answer(self):
        if self.busy:
            return
        from .answer_runs import retry_target
        try:
            retry_target(self.store, self.sid)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showinfo('無法重試', str(exc), parent=self.root)
            return
        self.cancel.clear()
        self.set_busy(True)
        self.status.set('重試最後一題，使用目前有效的記憶與文件；不重複新增問題。')
        sid = self.sid
        def work():
            try:
                self.events.put(('answer', self.assistant.retry(sid, self.cancel)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def send(self):
        if self.busy:
            return "break"
        question = self.input.get("1.0", "end").strip()
        if not question:
            return "break"
        if len(question) > 16000:
            messagebox.showinfo("問題太長", "請將問題縮短至 16000 字內。")
            return "break"
        self.input.delete("1.0", "end")
        self.append("user", question)
        self.cancel.clear()
        self.set_busy(True)
        self.status.set("思考中… 可按「停止」中止本次回答")
        sid = self.sid

        def work():
            try:
                self.events.put(("answer", self.assistant.ask(sid, question, self.cancel)))
            except Exception as exc:
                self.events.put(("error", {'message': str(exc), 'question': question}))
        threading.Thread(target=work, daemon=True).start()
        return "break"

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                self.set_busy(False)
                self.refresh_sessions()
                self.render()
                if kind == "answer":
                    self.last_answer = value["id"]
                    ids = ", ".join(map(str, value["memory_ids"])) or "無"
                    if value.get('response_mode') == 'local_memory':
                        self.status.set('本機記憶操作已回覆 · 未呼叫模型 · 可查看本次操作紀錄')
                    else:
                        self.status.set("回答已存本機 · 記憶：" + ids + f' · 早期摘錄：{len(value["excerpt_ids"])} 則 · 文件：{value["document_count"]} 個')
                else:
                    if isinstance(value, dict):
                        latest = self.store.messages(self.sid, 1)
                        if (not latest or latest[0]['role'] != 'user' or latest[0]['content'] != value['question']) and not self.input.get('1.0', 'end').strip():
                            self.input.insert('1.0', value['question'])
                        value = value['message']
                    self.append("note", value)
                    self.status.set("本次未完成；可重新提問")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def rate(self, rating):
        if not self.last_answer:
            return
        correction = ""
        if rating < 0:
            correction = simpledialog.askstring("更正回答", "哪裡需要改善？正確答案或期望做法是什麼？", parent=self.root)
            if correction is None:
                return
        self.store.rate(self.last_answer, rating, correction)
        if correction and messagebox.askyesno("保存經驗", "也把這段更正保存成長期記憶，供相關問題參考？"):
            self.store.remember(correction, "lesson")
        self.status.set("已記錄回饋；這會用於改進流程，不會自動修改模型權重")

    def memories(self):
        from .memory_ui import MemoryWindow
        MemoryWindow(self.root, self.store)

    def companion_settings(self):
        from .companion_ui import CompanionWindow
        CompanionWindow(self.root, self.store)

    def edit_brief(self):
        from .context_ui import BriefWindow
        BriefWindow(self.root, self.store, self.sid)

    def manage_documents(self):
        from .document_ui import DocumentWindow
        DocumentWindow(self.root, self.store, self.sid)

    def backups(self):
        from .backup_ui import BackupWindow
        BackupWindow(self.root, self.store)

    def work_tools(self):
        from .task_ui import TaskWindow
        TaskWindow(self.root, self.store, self.sid)

    def inspect_context(self):
        from .context_ui import show_context
        show_context(self.root, self.store, self.last_answer)

    def feedback_report(self):
        from .feedback_ui import FeedbackWindow
        FeedbackWindow(self.root, self.store)

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialdir=self.store.path.parent,
                                          filetypes=[("JSON", "*.json")])
        if path:
            self.store.export_session(self.sid, path)
            self.status.set("對話已匯出：" + path)

    def close(self):
        self.cancel.set()
        for window in self.autonomous_windows:
            window.cancel.set()
        if self.busy or any(window.busy for window in self.autonomous_windows):
            self.status.set("正在停止推理引擎…")
            self.root.after(200, self.close)
        else:
            self.root.destroy()


def run(store, engine):
    root = tk.Tk()
    App(root, store, engine)
    root.mainloop()
