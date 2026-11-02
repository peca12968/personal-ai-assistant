import tkinter as tk
from datetime import date
from tkinter import messagebox, ttk
from . import memory_edits
from .memory_merge_ui import MemoryMergeWindow

LABELS = {"偏好": "preference", "事實": "fact", "經驗": "lesson"}


class MemoryWindow:
    def __init__(self, parent, store):
        self.store, self.selected_id = store, None
        self.original = None
        self.window = tk.Toplevel(parent)
        self.window.title("長期記憶")
        self.window.geometry("900x610")
        self.window.minsize(740, 530)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        header = ttk.Frame(self.window, padding=12)
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text="更正現有資訊請直接編輯；停用或過期的記憶不會加入新回答。", wraplength=540).pack(side='left')
        from .memory_alias_ui import MemoryAliasesWindow
        self.alias_button = ttk.Button(header, text='記憶搜尋稱呼', command=lambda: MemoryAliasesWindow(self.window, self.store))
        self.alias_button.pack(side='right', padx=(8, 0))
        table = ttk.Frame(self.window, padding=(12, 0))
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.listing = ttk.Treeview(table, columns=("id", "kind", "status", "expires", "content"),
                                   show="headings", selectmode="browse", height=6)
        for key, title, width in (("id", "編號", 70), ("kind", "類型", 70), ("status", "狀態", 70),
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
        self.save_button = ttk.Button(buttons, text="儲存", command=self.save)
        self.save_button.pack(side="left", padx=(0, 8))
        self.review_button = ttk.Button(buttons, text='核對最新記憶', command=self.review)
        self.review_button.pack(side='left', padx=(0, 8))
        self.delete_button = ttk.Button(buttons, text="刪除選取記憶", command=self.delete)
        self.delete_button.pack(side="left")
        ttk.Button(buttons, text="查看整理來源", command=self.provenance).pack(side="left", padx=8)
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
            self.listing.insert("", "end", iid=key, values=('#' + key, reverse[row["kind"]], status,
                                row["expires_on"] or "不限", row["content"].replace("\n", " ")))

    def new(self):
        self.selected_id = None
        self.original = None
        self.listing.selection_remove(*self.listing.selection())
        self.kind.set("事實")
        self.expires.set("")
        self.active.set(True)
        self.title.set("新增記憶")
        self.content.delete("1.0", "end")
        self.content.focus_set()

    def select(self, event=None, *, force=False):
        selection = self.listing.selection()
        if not selection:
            return
        row = self.rows[selection[0]]
        if not force and self.selected_id == row['id']:
            return  # Repeated queued selection events must not erase an unsaved draft.
        self.selected_id = row["id"]
        self.original = dict(row)
        self.load_draft(row)
        self.title.set(f'編輯記憶 #{row["id"]}')

    def load_draft(self, row):
        self.kind.set(next(key for key, value in LABELS.items() if value == row["kind"]))
        self.expires.set(row["expires_on"] or "")
        self.active.set(bool(row["active"]))
        self.content.delete("1.0", "end")
        self.content.insert("1.0", row["content"])

    def draft(self):
        return {'content': self.content.get('1.0', 'end').strip(), 'kind': LABELS[self.kind.get()],
                'expires_on': self.expires.get().strip() or None, 'active': int(self.active.get())}

    def apply_review(self, original, current, draft, merged):
        if self.selected_id != current['id'] or self.original != original or self.draft() != draft:
            raise ValueError('原視窗已改選記憶或修改草稿，核對結果未套用；請重新核對。')
        self.original = dict(current)
        self.load_draft(merged)
        self.notice.set('核對結果已放入草稿，尚未儲存；按儲存時會再檢查是否有新的修改。')

    def review(self):
        if self.selected_id is None:
            return
        current = next((row for row in self.store.memories() if row['id'] == self.selected_id), None)
        if current is None:
            messagebox.showerror('無法核對', '這筆記憶已刪除；你的草稿仍保留，沒有重新新增。', parent=self.window)
            return
        self.merge_view = MemoryMergeWindow(self.window, self.original, current, self.draft(), self.apply_review)

    def save(self):
        content = self.content.get("1.0", "end").strip()
        kind = LABELS[self.kind.get()]
        expires = self.expires.get().strip() or None
        preserved = []
        try:
            if self.selected_id is None:
                self.selected_id = self.store.remember(content, kind, expires, active=self.active.get())
            else:
                receipt = self.store.edit_memory(self.selected_id, content, kind, expires, self.active.get(), expected=self.original)
                preserved = receipt['preserved_fields']
        except memory_edits.MemoryConflict as exc:
            self.notice.set(str(exc))
            self.merge_view = MemoryMergeWindow(self.window, exc.original, exc.current, exc.draft, self.apply_review)
            return
        except ValueError as exc:
            messagebox.showerror("無法儲存", str(exc), parent=self.window)
            return
        key = str(self.selected_id)
        self.refresh()
        self.listing.selection_set(key)
        self.listing.see(key)
        self.select(force=True)
        self.notice.set('已儲存。' + ('已保留其他地方較新的' + '、'.join(memory_edits.LABELS[k] for k in preserved) + '。' if preserved else '') + '變更會在下次提問時生效；不會改寫既有回答。')

    def delete(self):
        if self.selected_id is None:
            return
        try:
            self.store.forget(self.selected_id, expected=self.original)
        except ValueError as exc:
            messagebox.showerror('未刪除記憶', str(exc), parent=self.window)
            return
        self.new()
        self.refresh()
        self.notice.set("記憶已刪除，未來回答不再選取；既有對話與匯出內容仍保留。")

    def provenance(self):
        if self.selected_id is None:
            return
        import json
        from .memory_drafts import provenance
        from .ui_layout import fit_window
        value = provenance(self.store, self.selected_id)
        window = tk.Toplevel(self.window)
        window.title('記憶保存時的整理來源')
        fit_window(window, 800, 600)
        text = tk.Text(window, wrap='word')
        text.pack(fill='both', expand=True)
        text.insert('1.0', '以下是當時接受的草稿與原話；記憶後續編輯不會回寫歷史來源。\n\n' +
            (json.dumps(value, ensure_ascii=False, indent=2) if value else '此記憶沒有從對話整理的來源紀錄；可能由手動或記憶指令新增。'))
        text.configure(state='disabled')
