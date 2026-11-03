import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .documents import EXTENSIONS
from .document_metadata import metadata


class DocumentWindow:
    def __init__(self, parent, store, sid):
        self.store, self.sid = store, sid
        self.window = tk.Toplevel(parent)
        self.window.title("參考文件")
        self.window.geometry("840x580")
        self.window.minsize(650, 450)
        ttk.Label(self.window, text="選取文字、DOCX或文字型PDF供目前對話分析。僅抽取文字，不做OCR；PDF保留頁次標記，引用為抽取行號。提問時，選出的片段會傳給 Codex；原檔不會被修改。",
                  padding=12, wraplength=730).pack(fill="x")
        table = ttk.Frame(self.window, padding=(12, 0))
        table.pack(fill="both", expand=True)
        self.listing = ttk.Treeview(table, columns=("id", "name", "size", "imported"), show="headings", selectmode="browse", height=7)
        for key, title, width in (("id", "來源", 60), ("name", "名稱", 370), ("size", "大小", 100), ("imported", "匯入時間（UTC）", 180)):
            self.listing.heading(key, text=title)
            self.listing.column(key, width=width, minwidth=50, stretch=key == "name")
        scroll = ttk.Scrollbar(table, command=self.listing.yview)
        scroll.pack(side="right", fill="y")
        self.listing.configure(yscrollcommand=scroll.set)
        self.listing.pack(fill="both", expand=True)
        self.listing.bind("<<TreeviewSelect>>", self.select)
        controls = ttk.Frame(self.window, padding=12)
        controls.pack(fill="x")
        self.add_btn=ttk.Button(controls, text="加入文件", command=self.add)
        self.add_btn.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="搜尋資料夾", command=self.search_folder).pack(side="left", padx=(0, 8))
        self.reload_btn=ttk.Button(controls, text="重新匯入選取文件", command=self.reload)
        self.reload_btn.pack(side="left", padx=(0, 8))
        self.remove_btn=ttk.Button(controls, text="移除選取參考", command=self.remove)
        self.remove_btn.pack(side="left")
        self.detail = tk.Text(self.window, height=7, wrap="word", padx=12, pady=8, state="disabled", font=("Microsoft JhengHei UI", 10))
        self.detail.pack(fill="x", padx=12)
        self.notice = tk.StringVar(value="每段對話最多 8 個文件，單檔最多 2 MiB、合計 8 MiB。可在提問指定範圍，例如「查看 D1 第33到35行」；請使用列表中的實際來源編號。")
        ttk.Label(self.window, textvariable=self.notice, wraplength=730, padding=12).pack(fill="x")
        self.refresh()

    def refresh(self):
        self.rows = {str(row["id"]): row for row in self.store.documents(self.sid)}
        for item in self.listing.get_children():
            self.listing.delete(item)
        for key, row in self.rows.items():
            size = f'{row["size_bytes"]} B' if row["size_bytes"] < 1024 else f'{row["size_bytes"] / 1024:.1f} KiB'
            self.listing.insert("", "end", iid=key, values=("D" + key, row["name"], size, row["imported_at"]))

    def selected(self):
        selection = self.listing.selection()
        return self.rows.get(selection[0]) if selection else None

    def select(self, event=None):
        row = self.selected()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if row:
            self.detail.insert("1.0", f'來源 D{row["id"]}：{row["name"]}\n本機原檔：{row["source_path"]}\n內容指紋：{row["sha256"]}\n\n使用匯入時副本；原檔變更後需重新匯入。移除只影響未來選取，過往回答與參考快照仍保留。')
            if metadata(row): self.detail.insert('end', '\n\n'+metadata(row)['extraction_limits'])
        self.detail.configure(state="disabled")

    def unavailable(self):
        return getattr(self.window._root(), "_assistant_closing", False) or not self.window.winfo_exists()

    def import_paths(self, paths):
        if self.unavailable() or (getattr(self,'importer',None) and self.importer.active()):
            return
        from .document_import_ui import DocumentImportWindow
        try:self.importer=DocumentImportWindow(self.window,self.store,self.sid,[{'path':path} for path in paths],self.import_finished)
        except ValueError as exc:
            messagebox.showerror('未開始匯入',str(exc),parent=self.window);return
        self.notice.set('請查看「文件匯入進度」。成功加入後才會出現在本列表；不自動監看或呼叫模型。')
        for button in (self.add_btn,self.reload_btn,self.remove_btn):button.configure(state='disabled')
        return self.importer

    def import_finished(self):
        for button in (self.add_btn,self.reload_btn,self.remove_btn):button.configure(state='normal')
        self.refresh()

    def importing(self):
        return getattr(self,'importer',None) is not None and self.importer.active()

    def add(self):
        if self.unavailable() or self.importing():
            return
        paths = filedialog.askopenfilenames(parent=self.window, title="選取要分析的文字、DOCX或PDF",
                    filetypes=[("文字、程式、DOCX與PDF", " ".join("*" + ext for ext in sorted(EXTENSIONS))), ("所有檔案", "*.*")])
        if paths:
            self.import_paths(paths)

    def search_folder(self):
        if self.unavailable():
            return
        from .search_ui import SearchWindow
        SearchWindow(self.window, self.store, self.sid, self.refresh)

    def reload(self):
        if self.unavailable() or self.importing():
            return
        row = self.selected()
        if row:
            self.import_paths([row["source_path"]])

    def remove(self):
        if self.unavailable() or self.importing():
            return
        row = self.selected()
        if row:
            self.store.detach_document(self.sid, row["id"])
            self.refresh()
            self.select()
            self.notice.set("已移除這段對話的文件參考，原檔保持不變；過往快照仍保留。")
