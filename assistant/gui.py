import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .engine import Assistant

BG, PANEL, FG, MUTED, ACCENT = "#101820", "#192530", "#eef4f7", "#9db0bd", "#52d6ad"


class App:
    def __init__(self, root, store, engine):
        self.root, self.store = root, store
        self.assistant = Assistant(store, engine)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.last_answer = None
        root.title("個人 AI 助理")
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
        tk.Label(header, text="建議模式  ·  本機記憶", bg=BG, fg=ACCENT,
                 font=("Microsoft JhengHei UI", 11)).pack(side="right")
        body = tk.Frame(root, bg=BG, padx=18)
        body.grid(row=1, column=0, sticky="nsew")
        sidebar = tk.Frame(body, bg=PANEL, width=220, padx=12, pady=12)
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        sidebar.pack_propagate(False)
        self.new_btn = ttk.Button(sidebar, text="＋ 新對話", command=self.new_session)
        self.new_btn.pack(fill="x", pady=(0, 14))
        tk.Label(sidebar, text="對話紀錄", bg=PANEL, fg=MUTED).pack(anchor="w")
        self.session_list = tk.Listbox(sidebar, bg=PANEL, fg=FG, bd=0,
            selectbackground="#315766", highlightthickness=0, height=5, font=("Microsoft JhengHei UI", 10))
        self.session_list.pack(fill="both", expand=True, pady=10)
        self.session_list.bind("<<ListboxSelect>>", self.select_session)
        ttk.Button(sidebar, text="對話目標與限制", command=self.edit_brief).pack(fill="x", pady=4)
        ttk.Button(sidebar, text="管理長期記憶", command=self.memories).pack(fill="x", pady=4)
        ttk.Button(sidebar, text="回饋紀錄", command=self.feedback_report).pack(fill="x", pady=4)
        ttk.Button(sidebar, text="匯出目前對話", command=self.export).pack(fill="x", pady=4)
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
        tk.Label(actions, text="Ctrl + Enter 傳送  ·  對話與選用記憶會傳給 Codex", bg=BG, fg=MUTED).pack(side="left")
        self.send_btn = ttk.Button(actions, text="傳送", command=self.send)
        self.send_btn.pack(side="right")
        self.stop_btn = ttk.Button(actions, text="停止", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="right", padx=6)
        self.status = tk.StringVar(value="就緒 · 記憶與對話只存本機，不納入 GitHub 程式更新")
        tk.Label(root, textvariable=self.status, bg=BG, fg=MUTED, anchor="w", padx=24, pady=10).grid(row=2, column=0, sticky="ew")
        sessions = store.sessions()
        self.sid = sessions[0]["id"] if sessions else store.new_session()
        self.refresh_sessions()
        self.render()
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

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
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        rows = self.store.messages(self.sid, 500)
        self.last_answer = None
        if not rows:
            self.append("note", "告訴我你想完成什麼。\n我可以協助釐清需求、規劃步驟、分析問題與檢查解法。\n\n你可以在「管理長期記憶」保存偏好，並對回答留下更正。")
        for row in rows:
            self.append(row["role"], row["content"])
            if row["role"] == "assistant":
                self.last_answer = row["id"]

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
        for widget in (self.send_btn, self.new_btn, self.session_list, self.good_btn, self.bad_btn, self.context_btn):
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

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
                self.events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()
        return "break"

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                self.set_busy(False)
                self.refresh_sessions()
                if kind == "answer":
                    self.last_answer = value["id"]
                    self.append("assistant", value["content"])
                    ids = ", ".join(map(str, value["memory_ids"])) or "無"
                    self.status.set("回答已存本機 · 參考記憶：" + ids + f' · 早期摘錄：{len(value["excerpt_ids"])} 則')
                else:
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

    def edit_brief(self):
        from .context_ui import BriefWindow
        BriefWindow(self.root, self.store, self.sid)

    def inspect_context(self):
        from .context_ui import show_context
        show_context(self.root, self.store, self.last_answer)

    def feedback_report(self):
        rows = self.store.feedback_summary()
        window = tk.Toplevel(self.root)
        window.title("回饋紀錄")
        text = tk.Text(window, wrap="word", padx=16, pady=16, font=("Microsoft JhengHei UI", 11))
        text.pack(fill="both", expand=True)
        good = sum(r["rating"] == 1 for r in rows)
        text.insert("end", f"有幫助：{good}　需要改進：{len(rows) - good}\n\n")
        for row in rows:
            text.insert("end", f'回答 #{row["message_id"]} · {row["updated_at"]}\n更正：{row["correction"] or "無"}\n原回答：{row["answer"]}\n\n')
        text.configure(state="disabled")

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialdir=self.store.path.parent,
                                          filetypes=[("JSON", "*.json")])
        if path:
            self.store.export_session(self.sid, path)
            self.status.set("對話已匯出：" + path)

    def close(self):
        self.cancel.set()
        if self.busy:
            self.status.set("正在停止推理引擎…")
            self.root.after(200, self.close)
        else:
            self.root.destroy()


def run(store, engine):
    root = tk.Tk()
    App(root, store, engine)
    root.mainloop()
