import queue
import threading
from .ui_workers import start_worker
from .data_window import DataWindow
import tkinter as tk
from tkinter import filedialog, ttk

from .folder_search import attach_result, search_folder


class SearchWindow(DataWindow):
    shutdown_controls = ('choose_btn', 'search_btn', 'stop_btn', 'import_btn')

    def __init__(self, parent, store, sid, on_import=None):
        self.store, self.sid, self.on_import = store, sid, on_import
        self.import_parent = parent
        self.report = None
        self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.own_window(parent)
        self.window.title('搜尋資料夾 · 僅在本機搜尋')
        self.window.geometry('900x680')
        self.window.minsize(700, 560)
        ttk.Label(self.window, text='選取一個資料夾，搜尋文字內容或檔名。搜尋不會傳給模型；加入對話後，提問會傳送選出的文件片段。',
                  wraplength=820, padding=12).pack(fill='x')
        self.folder = tk.StringVar()
        folder_row = ttk.Frame(self.window, padding=(12, 0))
        folder_row.pack(fill='x')
        self.choose_btn = ttk.Button(folder_row, text='選取資料夾', command=self.choose)
        self.choose_btn.pack(side='left')
        ttk.Entry(folder_row, textvariable=self.folder, state='readonly').pack(side='left', fill='x', expand=True, padx=(8, 0))
        query_row = ttk.Frame(self.window, padding=12)
        query_row.pack(fill='x')
        self.query = ttk.Entry(query_row)
        self.query.pack(side='left', fill='x', expand=True)
        self.query.bind('<Return>', lambda event: self.search())
        self.search_btn = ttk.Button(query_row, text='搜尋', command=self.search)
        self.search_btn.pack(side='left', padx=8)
        self.stop_btn = ttk.Button(query_row, text='停止', command=self.cancel.set, state='disabled')
        self.stop_btn.pack(side='left')
        table = ttk.Frame(self.window, padding=(12, 0))
        table.pack(fill='both', expand=True)
        self.listing = ttk.Treeview(table, columns=('file', 'lines'), show='headings', selectmode='extended', height=7)
        self.listing.heading('file', text='相對路徑（可複選）')
        self.listing.heading('lines', text='預覽行號')
        self.listing.column('file', width=620)
        self.listing.column('lines', width=130, stretch=False)
        scroll = ttk.Scrollbar(table, command=self.listing.yview)
        scroll.pack(side='right', fill='y')
        self.listing.configure(yscrollcommand=scroll.set)
        self.listing.pack(fill='both', expand=True)
        self.listing.bind('<<TreeviewSelect>>', self.preview)
        self.preview_text = tk.Text(self.window, height=9, wrap='word', state='disabled', padx=10, pady=8,
                                    font=('Microsoft JhengHei UI', 10))
        self.preview_text.pack(fill='x', padx=12, pady=10)
        self.import_btn = ttk.Button(self.window, text='將選取文件加入目前對話', command=self.import_selected, state='disabled')
        self.import_btn.pack(anchor='e', padx=12)
        self.notice = tk.StringVar(value='單次最多讀取 500 個文字檔／32 MiB；跳過隱藏項目、連結、常見憑證及產出目錄。搜尋結果不代表完整閱讀。')
        ttk.Label(self.window, textvariable=self.notice, wraplength=820, padding=12).pack(fill='x')
        self.schedule_poll()

    def choose(self):
        if self.unavailable() or self.busy:
            return
        path = filedialog.askdirectory(parent=self.window, title='選取唯讀搜尋範圍')
        if path and not self.unavailable():
            self.folder.set(path)
            self.report = None
            self.clear_results()

    def clear_results(self):
        self.listing.delete(*self.listing.get_children())
        self.import_btn.configure(state='disabled')
        self.preview()

    def search(self):
        if self.unavailable() or self.busy:
            return
        if not self.folder.get():
            self.notice.set('請先選取資料夾。')
            return
        folder, query = self.folder.get(), self.query.get()
        self.report = None
        self.clear_results()
        self.busy = True
        self.cancel.clear()
        for button in (self.choose_btn, self.search_btn):
            button.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.notice.set('正在本機搜尋… 可隨時停止，不會自動加入文件或呼叫模型。')
        def work():
            try:
                self.events.put((True, search_folder(folder, query, cancel=self.cancel)))
            except Exception as exc:
                self.events.put((False, str(exc)))
        start_worker(work, lambda error: self.events.put((False, error)), daemon=True)

    def poll(self):
        if not self.begin_poll():
            return
        try:
            ok, result = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.choose_btn.configure(state='normal')
            self.search_btn.configure(state='normal')
            self.stop_btn.configure(state='disabled')
            if ok:
                self.report = result
                for i, row in enumerate(result['results']):
                    self.listing.insert('', 'end', iid=str(i), values=(row['relative_path'], f'L{row["start_line"]}–L{row["end_line"]}'))
                importing=getattr(self,'importer',None) and self.importer.active()
                self.import_btn.configure(state='normal' if result['results'] and not importing else 'disabled')
                self.notice.set(f'已讀 {result["files_read"]} 個檔案，顯示 {len(result["results"])} 個結果；排除 {result["excluded"]} 項、無法讀取 {result["unreadable"]} 項。' +
                                (' 已停止，結果不完整。' if result['cancelled'] else '') +
                                (' 達搜尋上限，結果不完整。' if result['limits_hit'] else '') +
                                ' 尚未加入對話；每段對話仍最多 8 個文件。')
            else:
                self.notice.set('搜尋未完成：' + result)
        self.schedule_poll()

    def preview(self, event=None):
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        selection = self.listing.selection()
        if self.report and selection:
            row = self.report['results'][int(selection[0])]
            self.preview_text.insert('1.0', row['relative_path'] + ('（部分行內容）' if row['partial_line'] else '') + '\n\n' + row['text'])
            if row.get('extraction_limits'):
                self.preview_text.insert('end', '\n\n抽取範圍：'+row['extraction_limits'])
        self.preview_text.configure(state='disabled')

    def import_selected(self):
        if self.unavailable() or self.busy or (getattr(self,'importer',None) and self.importer.active()):
            return
        if not self.report:
            return
        from pathlib import Path
        from .document_import_ui import DocumentImportWindow
        selected=[self.report['results'][int(item)] for item in self.listing.selection()]
        if not selected:return
        root=Path(self.report['root'])
        items=[{'path':root/row['relative_path'],'scope':root,'expected_sha256':row['sha256']} for row in selected]
        def completed():
            self.import_btn.configure(state='normal' if self.report and self.report['results'] and not self.busy else 'disabled')
            if self.on_import and self.import_parent.winfo_exists():self.on_import()
        try:self.importer=DocumentImportWindow(self.window,self.store,self.sid,items,completed)
        except ValueError as exc:self.notice.set(str(exc));return
        self.notice.set('請查看「文件匯入進度」。匯入時仍會重新核對搜尋範圍與來源指紋。')
        self.import_btn.configure(state='disabled')
        return self.importer
