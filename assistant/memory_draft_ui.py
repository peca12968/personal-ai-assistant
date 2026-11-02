"""Review attributable memory suggestions before explicitly saving each one."""
import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import memory_drafts as drafts
from . import goal_conversation
from .memory_ui import LABELS
from .ui_layout import ScrollableForm, fit_window


class MemoryDraftWindow:
    def __init__(self,parent,store,sid,engine):
        self.store,self.sid,self.engine=store,sid,engine
        self.busy=False
        self.cancel=threading.Event()
        self.events=queue.Queue()
        self.current=None
        self.window=tk.Toplevel(parent)
        self.window.title('從本對話整理記憶')
        fit_window(self.window,900,800)
        self.layout=ScrollableForm(self.window)
        body=self.layout.body
        self.layout.wrapped_label(body,text='整理本對話的有限近期討論、較早更正及回饋，提出最多8項記憶草稿。開始時才把這份來源送給模型；草稿不會自動成為記憶。',padding=12).pack(fill='x')
        self.layout.wrapped_label(body,text='每項都有使用者原話來源；引文核對只證明文字存在，仍需你核對意思。來源固定於擷取當下，不會隨後來聊天更新。保存後可跨對話使用，也可在長期記憶編輯、停用或刪除。',padding=12).pack(fill='x')
        buttons=ttk.Frame(body,padding=12);buttons.pack(fill='x')
        self.start=ttk.Button(buttons,text='擷取本對話並整理草稿',command=self.new)
        self.start.pack(side='left')
        self.retry=ttk.Button(buttons,text='重試所選來源',command=self.retry_selected,state='disabled')
        self.retry.pack(side='left',padx=8)
        self.stop=ttk.Button(buttons,text='停止整理',command=self.cancel.set,state='disabled')
        self.stop.pack(side='left')
        ttk.Label(body,text='本對話最近20份草稿（「整理中」紀錄不是程序存活證明）：').pack(anchor='w',padx=12)
        self.listing=ttk.Treeview(body,columns=('time','status'),show='headings',height=4,selectmode='browse')
        for key,label in [('time','擷取時間（UTC）'),('status','狀態')]:self.listing.heading(key,text=label)
        self.listing.pack(fill='x',padx=12);self.listing.bind('<<TreeviewSelect>>',self.select_draft)
        ttk.Button(body,text='查看完整來源快照',command=self.show_source).pack(anchor='w',padx=12,pady=6)
        self.items=ttk.Treeview(body,columns=('state','content'),show='headings',height=5,selectmode='browse')
        self.items.heading('state',text='保存狀態');self.items.column('state',width=110,stretch=False)
        self.items.heading('content',text='建議內容')
        self.items.pack(fill='x',padx=12);self.items.bind('<<TreeviewSelect>>',self.select_item)
        self.evidence=tk.Text(body,height=8,wrap='word',state='disabled')
        self.evidence.pack(fill='x',padx=12,pady=6)
        ttk.Label(body,text='核對並修改要保存的內容：').pack(anchor='w',padx=12)
        self.content=tk.Text(body,height=3,wrap='word');self.content.pack(fill='x',padx=12)
        controls=ttk.Frame(body,padding=12);controls.pack(fill='x')
        self.kind=ttk.Combobox(controls,values=list(LABELS),state='readonly',width=8);self.kind.set('事實');self.kind.pack(side='left')
        ttk.Label(controls,text='到期日（YYYY-MM-DD，可留白）：').pack(side='left',padx=8)
        self.expires=ttk.Entry(controls,width=12);self.expires.pack(side='left')
        self.save=ttk.Button(body,text='將這一項保存為長期記憶',command=self.accept,state='disabled')
        self.save.pack(anchor='w',padx=12,pady=6)
        self.notice=tk.StringVar(value='先整理，再逐項選擇；未保存的項目不會加入回答。')
        self.layout.wrapped_label(body,textvariable=self.notice,padding=12).pack(fill='x')
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.poll_timer=self.window.after(150,self.poll)
        self.refresh()

    def refresh(self,did=None):
        for iid in self.listing.get_children():self.listing.delete(iid)
        for row in drafts.drafts(self.store,self.sid):
            self.listing.insert('','end',iid=row['id'],values=(row['created_at'],drafts.LABELS[row['status']]))
        if did and self.listing.exists(did):
            self.listing.selection_set(did);self.select_draft()

    def select_draft(self,event=None):
        selected=self.listing.selection()
        if not selected:return
        self.current=drafts.get(self.store,self.sid,selected[0])
        for iid in self.items.get_children():self.items.delete(iid)
        accepted={r['item_index']:r for r in self.current['acceptances']}
        for index,item in enumerate(self.current['items']):
            state=f"已保存 #{accepted[index]['memory_id']}" if index in accepted else '待核對'
            self.items.insert('','end',iid=str(index),values=(state,item['content']))
        self.save.configure(state='disabled')
        self.retry.configure(state='normal' if not self.busy and self.current['status']!='ready' else 'disabled')
        self.notice.set(self.current['error'] or ('沒有找到適合保存的穩定資訊。' if self.current['status']=='ready' and not self.current['items'] else '選取草稿查看原話、編輯後保存。'))

    def select_item(self,event=None):
        selected=self.items.selection()
        if not selected or not self.current:return
        index=int(selected[0]);item=self.current['items'][index]
        receipt=next((r for r in self.current['acceptances'] if r['item_index']==index),None)
        value=receipt or item
        self.content.delete('1.0','end');self.content.insert('1.0',value['content'])
        self.kind.set(next(k for k,v in LABELS.items() if v==value['kind']))
        self.expires.delete(0,'end');self.expires.insert(0,value.get('expires_on') or '')
        source=drafts.catalog(self.current['context'])
        details=['原話來源（請核對完整句意，不只引文）：']
        for ref in item['sources']:details += [ref['key']+'：'+source[ref['key']], '選取引文：'+ref['quote']]
        related=self.store.retrieve(item['content'],limit=3)
        details += ['可能相關的既有記憶（有限檢索，非完整衝突檢查；不會自動覆寫）：']
        details += [f"#{r['id']}：{r['content']}" for r in related] or ['未找到相關項目。']
        if receipt:details += ['此項已有保存紀錄；後續編輯、停用或刪除請到長期記憶。此處不代表記憶現在仍存在或啟用。']
        self.evidence.configure(state='normal');self.evidence.delete('1.0','end');self.evidence.insert('1.0','\n\n'.join(details));self.evidence.configure(state='disabled')
        self.save.configure(state='normal' if not self.busy and receipt is None else 'disabled')

    def show_source(self):
        if not self.current:return
        window=tk.Toplevel(self.window);window.title('本次整理來源（固定快照）');fit_window(window,800,600)
        text=tk.Text(window,wrap='word');text.pack(fill='both',expand=True)
        text.insert('1.0',goal_conversation.describe(self.current['context']));text.configure(state='disabled')

    def new(self):
        if self.busy:return
        try:
            did=drafts.create(self.store,self.sid);self.refresh(did);self.run(did)
        except (ValueError,RuntimeError) as exc:messagebox.showerror('無法整理',str(exc),parent=self.window)

    def retry_selected(self):
        if not self.busy and self.current:self.run(self.current['id'])

    def run(self,did):
        self.busy=True;self.cancel.clear()
        for widget in (self.start,self.retry,self.save):widget.configure(state='disabled')
        self.stop.configure(state='normal');self.notice.set('正在整理草稿；尚未新增任何長期記憶。')
        def worker():
            try:
                drafts.generate(self.store,self.sid,did,self.engine,cancel=self.cancel)
                self.events.put((did,None))
            except Exception as exc:self.events.put((did,str(exc)))
        threading.Thread(target=worker,daemon=False).start()

    def accept(self):
        if self.busy or not self.current or not self.items.selection():return
        try:
            receipt=drafts.accept(self.store,self.sid,self.current['id'],int(self.items.selection()[0]),self.current['proposals_sha256'],
                content=self.content.get('1.0','end').strip(),kind=LABELS[self.kind.get()],expires_on=self.expires.get().strip() or None)
            self.refresh(self.current['id'])
            self.notice.set(f"已保存為記憶 #{receipt['memory_id']}"+('（相同內容已存在，沿用原編號）。' if receipt['reused'] else '；下次相關提問可選用。'))
        except (ValueError,RuntimeError) as exc:messagebox.showerror('無法保存',str(exc),parent=self.window)

    def poll(self):
        try:
            while True:
                did,error=self.events.get_nowait();self.busy=False
                self.start.configure(state='normal');self.stop.configure(state='disabled');self.refresh(did)
                if error:self.notice.set('整理未完成：'+error)
        except queue.Empty:pass
        self.poll_timer=self.window.after(150,self.poll)

    def close(self):
        self.cancel.set()
        if self.busy:self.window.after(200,self.close)
        else:
            self.window.after_cancel(self.poll_timer)
            self.window.destroy()
