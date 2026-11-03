"""Explicit local report browsing; no grading, model calls or report writes."""
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .data_window import DataWindow
from .evaluation_view import PAGE_SIZE, case_detail, clip, load_snapshot, row_state, summary_text
from .ui_layout import ScrollableForm, fit_window
from .ui_workers import start_worker


class EvaluationWindow(DataWindow):
    shutdown_text = '正在停止評估報告載入，等候讀取返回後關閉。'

    def __init__(self, parent, initial_dir):
        self.initial_dir = Path(initial_dir)
        self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.paths = [None, None, None]
        self.snapshot = None
        self.page = 0
        self.own_window(parent)
        self.window.title('查看評估報告 · 本機唯讀')
        fit_window(self.window, 1020, 800)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        body.configure(padding=14)
        self.layout.wrapped_label(body, text='查看既有報告與原評讀。沒有重新測試、改分或傳送答案；結果不是IQ分數。').pack(fill='x', pady=(0, 8))
        self.path_labels = []
        names = ['報告A（必選）', '報告B（比較時選取）', '原案例集（選用）']
        self.shutdown_controls = ('load_btn', 'previous_btn', 'next_btn')
        for i, name in enumerate(names):
            frame = ttk.Frame(body);frame.pack(fill='x', pady=3)
            choose = ttk.Button(frame, text='選取' + name, command=lambda index=i: self.choose(index))
            choose.pack(side='left')
            setattr(self, 'choose_' + str(i), choose)
            self.shutdown_controls += ('choose_' + str(i),)
            if i:
                clear = ttk.Button(frame, text='清除', command=lambda index=i: self.clear(index))
                clear.pack(side='right')
                setattr(self, 'clear_' + str(i), clear)
                self.shutdown_controls += ('clear_' + str(i),)
            label = tk.StringVar(self.window, value='尚未選取')
            ttk.Entry(frame, textvariable=label, state='readonly', width=45).pack(side='left', fill='x', expand=True, padx=8)
            self.path_labels.append(label)
        actions = ttk.Frame(body);actions.pack(fill='x', pady=8)
        self.load_btn = ttk.Button(actions, text='核對並載入／重新載入', command=self.begin)
        self.load_btn.pack(side='left')
        self.loaded_label = tk.StringVar(self.window, value='目前尚未顯示任何報告。')
        self.layout.wrapped_label(body, textvariable=self.loaded_label).pack(fill='x')
        self.notice = tk.StringVar(self.window, value='選取的是本機JSON報告；原案例集需與當時版本相同。畫面為載入副本，不會自動更新。')
        self.layout.wrapped_label(body, textvariable=self.notice).pack(fill='x', pady=8)
        self.summary = self.text_box(body, 8)
        table = ttk.Frame(body);table.pack(fill='x', pady=8)
        self.listing = ttk.Treeview(table, columns=('id','category','a','b','reviewer'), show='headings', height=7, selectmode='browse')
        for key, title, width in [('id','案例',220),('category','分類',130),('a','報告A評讀',100),('b','報告B評讀',100),('reviewer','評讀者 A → B',220)]:
            self.listing.heading(key,text=title);self.listing.column(key,width=width,minwidth=60)
        bar = ttk.Scrollbar(table,command=self.listing.yview)
        bar.pack(side='right',fill='y');self.listing.configure(yscrollcommand=bar.set)
        horizontal = ttk.Scrollbar(table,orient='horizontal',command=self.listing.xview)
        horizontal.pack(side='bottom',fill='x');self.listing.configure(xscrollcommand=horizontal.set)
        self.listing.pack(fill='x')
        self.listing.bind('<<TreeviewSelect>>',self.select)
        paging = ttk.Frame(body);paging.pack(fill='x')
        self.previous_btn = ttk.Button(paging,text='上一頁',command=lambda:self.change_page(-1),state='disabled');self.previous_btn.pack(side='left')
        self.next_btn = ttk.Button(paging,text='下一頁',command=lambda:self.change_page(1),state='disabled');self.next_btn.pack(side='left',padx=8)
        self.page_label = tk.StringVar(self.window,value='尚無案例')
        ttk.Label(paging,textvariable=self.page_label).pack(side='left')
        notebook = ttk.Notebook(body);notebook.pack(fill='both',expand=True,pady=8)
        self.details = []
        for name in ('報告A原文與評讀','報告B原文與評讀'):
            frame=ttk.Frame(notebook);notebook.add(frame,text=name)
            self.details.append(self.text_box(frame,16))
        self.schedule_poll()

    @staticmethod
    def text_box(parent, height):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        box=tk.Text(frame,height=height,wrap='word',state='disabled',font=('Microsoft JhengHei UI',10),padx=8,pady=8)
        bar=ttk.Scrollbar(frame,command=box.yview);bar.pack(side='right',fill='y')
        box.configure(yscrollcommand=bar.set);box.pack(fill='both',expand=True)
        return box

    @staticmethod
    def put(box, text):
        box.configure(state='normal');box.delete('1.0','end');box.insert('1.0',text);box.configure(state='disabled')

    def choose(self, index):
        if self.unavailable() or self.busy:
            return
        path=filedialog.askopenfilename(parent=self.window,title='選取既有評估JSON',initialdir=self.initial_dir,filetypes=[('JSON','*.json')])
        if path and not self.unavailable():
            self.paths[index]=path;self.path_labels[index].set(path)
            self.notice.set('選取已變更，請按「核對並載入」；畫面仍是上次載入的副本。')

    def clear(self, index):
        if self.unavailable() or self.busy or index not in (1,2):
            return
        self.paths[index]=None;self.path_labels[index].set('尚未選取')
        self.notice.set('選取已變更，請重新載入；畫面仍是上次載入的副本。')

    def begin(self):
        if self.unavailable() or self.busy:
            return
        if not self.paths[0]:
            self.notice.set('請先選取報告A。');return
        selected=tuple(self.paths)
        self.busy=True;self.cancel.clear();self.disable_controls()
        self.notice.set('正在本機核對；畫面暫保留上次副本，尚未載入新結果。')
        # Worker closures must not own Tk objects: the UI may consume the
        # terminal event and dispose the window before this thread returns.
        events, cancel = self.events, self.cancel
        def work():
            try:events.put((True,load_snapshot(selected,cancel)))
            except Exception as exc:events.put((False,str(exc)))
        start_worker(work,lambda error:events.put((False,error)))

    def poll(self):
        if not self.begin_poll():
            return
        try:ok,result=self.events.get_nowait()
        except queue.Empty:pass
        else:
            self.busy=False
            if not self.unavailable():
                if ok:
                    self.snapshot=result;self.page=0
                    sources='；'.join('報告'+'AB'[i]+'：'+item['path'] for i,item in enumerate(result['files']))
                    if result['suite_file']:sources+='；原案例集：'+result['suite_file']['path']
                    self.loaded_label.set('目前顯示：'+sources+'\n載入時間（UTC）：'+result['loaded_at'])
                    self.put(self.summary,summary_text(result));self.render_page()
                    self.notice.set('已核對並載入本機副本。沒有改分、模型呼叫或上傳；檔案變更後請重新載入。')
                else:
                    self.notice.set('載入未成功；'+('仍顯示上次副本。' if self.snapshot else '尚無可顯示結果。')+' '+clip(result,1500))
                self.restore_controls()
        self.schedule_poll()

    def restore_controls(self):
        for name in self.shutdown_controls:
            getattr(self,name).configure(state='normal')
        count=len(self.snapshot['case_ids']) if self.snapshot else 0
        self.previous_btn.configure(state='normal' if self.page>0 else 'disabled')
        self.next_btn.configure(state='normal' if (self.page+1)*PAGE_SIZE<count else 'disabled')

    def render_page(self):
        self.listing.delete(*self.listing.get_children())
        for box in self.details:self.put(box,'選取一題查看原文與最近評讀。')
        ids=self.snapshot['case_ids'];start=self.page*PAGE_SIZE;end=min(start+PAGE_SIZE,len(ids))
        for i in range(start,end):
            id=ids[i];a=self.snapshot['indexed'][0].get(id)
            b=self.snapshot['indexed'][1].get(id) if len(self.snapshot['indexed'])>1 else None
            sa,ra=row_state(a);sb,rb=row_state(b) if len(self.snapshot['indexed'])>1 else ('—','—')
            category=(a or b or {}).get('category','尚未收集')
            self.listing.insert('', 'end', iid=str(i),values=(clip(id,160),clip(category,100),sa,sb,ra+' → '+rb))
        self.page_label.set(f'案例 {start+1}–{end}／{len(ids)}；每頁最多{PAGE_SIZE}題')
        if end>start:self.listing.selection_set(str(start));self.select()
        self.restore_controls()

    def change_page(self, delta):
        if self.unavailable() or self.busy or not self.snapshot:
            return
        next_page=self.page+delta
        if 0<=next_page and next_page*PAGE_SIZE<len(self.snapshot['case_ids']):
            self.page=next_page;self.render_page()

    def select(self, event=None):
        if self.closed or not self.snapshot:
            return
        selection=self.listing.selection()
        if selection:
            id=self.snapshot['case_ids'][int(selection[0])]
            for i,box in enumerate(self.details):self.put(box,case_detail(self.snapshot,i,id))
