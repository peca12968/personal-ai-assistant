import tkinter as tk
from datetime import date
from tkinter import messagebox, ttk

LABELS = {"偏好": "preference", "事實": "fact", "經驗": "lesson"}


class MemoryWindow:
    def __init__(self, parent, store):
        self.store, self.selected_id = store, None
        self.window = tk.Toplevel(parent)
        self.window.title("長期記憶")
        self.window.geometry("900x610")
        self.window.minsize(740, 530)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        ttk.Label(self.window, text="更正現有資訊請直接編輯；停用或過期的記憶不會加入新回答。",
                  padding=12).grid(row=0, column=0, sticky="w")
        table = ttk.Frame(self.window, padding=(12, 0))
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.listing = ttk.Treeview(table, columns=("kind", "status", "expires", "content"),
                                   show="headings", selectmode="browse", height=6)
        for key, title, width in (("kind", "類型", 70), ("status", "狀態", 70),
                                  ("expires", "到期日", 100), ("content", "內容", 540)):
            self.listing.heading(key, text=title)
            self.listing.column(key, width=width, minwidth=60, stretch=key == "content")
        self.listing.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.listing.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listing.configure(yscrollcommand=scroll.set)
        self.listing.bind("<<TreeviewSelect>>", self.select)
        editor = ttk.Frame(self.window, padding=12)
        editor.grid(row=2, column=0, sticky="ew")
        editor.columnconfigure(1, weight=1)
        self.kind = tk.StringVar(value="事實")
        self.expires = tk.StringVar()
        self.active = tk.BooleanVar(value=True)
        self.title = tk.StringVar(value="新增記憶")
        ttk.Label(editor, textvariable=self.title).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(editor, text="類型").grid(row=1, column=0, sticky="w")
        ttk.Combobox(editor, textvariable=self.kind, values=list(LABELS), state="readonly", width=12).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(editor, text="啟用", variable=self.active).grid(row=1, column=2, padx=12)
        ttk.Label(editor, text="到期日（YYYY-MM-DD；留白為不限）").grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Entry(editor, textvariable=self.expires, width=16).grid(row=2, column=3, sticky="e")
        self.content = tk.Text(editor, height=5, wrap="word", font=("Microsoft JhengHei UI", 11), padx=8, pady=6)
        self.content.grid(row=3, column=0, columnspan=4, sticky="ew", pady=8)
        buttons = ttk.Frame(editor)
        buttons.grid(row=4, column=0, columnspan=4, sticky="w")
        ttk.Button(buttons, text="新增", command=self.new).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="儲存", command=self.save).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="刪除選取記憶", command=self.delete).pack(side="left")
        self.notice = tk.StringVar(value="選取上方記憶可編輯；到期日當日仍有效。選用記憶會隨問題傳給 Codex。")
        ttk.Label(self.window, textvariable=self.notice, padding=12, wraplength=710).grid(row=3, column=0, sticky="ew")
        self.refresh()

    def refresh(self):
        self.rows = {str(row["id"]): row for row in self.store.memories()}
        for item in self.listing.get_children():
            self.listing.delete(item)
        reverse = {value: key for key, value in LABELS.items()}
        today = date.today().isoformat()
        for key, row in self.rows.items():
            status = "停用" if not row["active"] else "過期" if row["expires_on"] and row["expires_on"] < today else "啟用"
            self.listing.insert("", "end", iid=key, values=(reverse[row["kind"]], status,
                                row["expires_on"] or "不限", row["content"].replace("\n", " ")))

    def new(self):
        self.selected_id = None
        self.listing.selection_remove(*self.listing.selection())
        self.kind.set("事實")
        self.expires.set("")
        self.active.set(True)
        self.title.set("新增記憶")
        self.content.delete("1.0", "end")
        self.content.focus_set()

    def select(self, event=None):
        selection = self.listing.selection()
        if not selection:
            return
        row = self.rows[selection[0]]
        self.selected_id = row["id"]
        self.kind.set(next(key for key, value in LABELS.items() if value == row["kind"]))
        self.expires.set(row["expires_on"] or "")
        self.active.set(bool(row["active"]))
        self.title.set(f'編輯記憶 #{row["id"]}')
        self.content.delete("1.0", "end")
        self.content.insert("1.0", row["content"])

    def save(self):
        content = self.content.get("1.0", "end").strip()
        kind = LABELS[self.kind.get()]
        expires = self.expires.get().strip() or None
        try:
            if self.selected_id is None:
                self.selected_id = self.store.remember(content, kind, expires, active=self.active.get())
            else:
                self.store.edit_memory(self.selected_id, content, kind, expires, self.active.get())
        except ValueError as exc:
            messagebox.showerror("無法儲存", str(exc), parent=self.window)
            return
        key = str(self.selected_id)
        self.refresh()
        self.listing.selection_set(key)
        self.listing.see(key)
        self.notice.set("已儲存。變更會在下次提問時生效；不會改寫既有回答。")

    def delete(self):
        if self.selected_id is None:
            return
        self.store.forget(self.selected_id)
        self.new()
        self.refresh()
        self.notice.set("記憶已刪除，未來回答不再選取；既有對話與匯出內容仍保留。")
