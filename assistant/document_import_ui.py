"""Root-owned document import progress; workers never touch Tk widgets."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .data_window import DataWindow
from .document_imports import execute,freeze
from .ui_workers import start_worker


class DocumentImportWindow(DataWindow):
    shutdown_controls=('stop_btn',)
    shutdown_text='正在停止匯入，等候目前檔案收尾後關閉；已匯入文件會保留。'

    def __init__(self,parent,store,sid,items,on_complete=None):
        items=freeze(items)
        if getattr(parent._root(),'_assistant_closing',False):raise ValueError('程式正在關閉，未開始匯入。')
        self.store,self.sid,self.items=store,sid,items
        self.parent,self.on_complete=parent,on_complete
        self.cancel=threading.Event();self.events=queue.Queue();self.busy=True;self.result=None
        self.own_window(parent)
        self.window.title('文件匯入進度')
        self.window.geometry('760x460');self.window.minsize(600,360)
        ttk.Label(self.window,text='正在加入選取文件。可繼續使用主視窗；停止不撤銷已完成的匯入。\n本機讀取不呼叫模型；未完成前，請依各檔結果判斷可用來源。',padding=12,wraplength=680).pack(fill='x')
        frame=ttk.Frame(self.window,padding=(12,0));frame.pack(fill='both',expand=True)
        self.listing=ttk.Treeview(frame,columns=('name','status'),show='headings',height=6)
        self.listing.heading('name',text='文件');self.listing.heading('status',text='狀態')
        self.listing.column('name',width=430);self.listing.column('status',width=190)
        scroll=ttk.Scrollbar(frame,command=self.listing.yview);scroll.pack(side='right',fill='y')
        self.listing.configure(yscrollcommand=scroll.set);self.listing.pack(fill='both',expand=True)
        for i,item in enumerate(items):self.listing.insert('', 'end',iid=str(i),values=(item['path'].name,'尚未處理'))
        self.detail=tk.Text(self.window,height=4,wrap='word',state='disabled');self.detail.pack(fill='x',padx=12,pady=8)
        controls=ttk.Frame(self.window,padding=(12,0));controls.pack(fill='x')
        self.stop_btn=ttk.Button(controls,text='停止匯入',command=self.stop);self.stop_btn.pack(side='left')
        ttk.Button(controls,text='關閉',command=self.close).pack(side='right')
        self.notice=tk.StringVar(value=f'共{len(items)}個文件，正在準備匯入。')
        ttk.Label(self.window,textvariable=self.notice,wraplength=680,padding=12).pack(fill='x')
        self.parent_binding=parent.bind('<Destroy>',self.parent_destroyed,add='+')
        def work():
            try:
                result=execute(store,sid,items,self.cancel,lambda kind,value:self.events.put((kind,value)))
            except Exception as exc:
                self.events.put(('failed',str(exc)))
            else:self.events.put(('finished',result))
        start_worker(work,lambda error:self.events.put(('failed',error)),daemon=True)
        self.schedule_poll()

    def stop(self):
        if self.busy:
            self.cancel.set();self.stop_btn.configure(state='disabled')
            self.notice.set('已要求停止，等候目前檔案收尾；已匯入的文件不會撤銷。')

    def parent_destroyed(self,event):
        if event.widget==self.parent and self.busy:self.close()

    def show_result(self,index,row):
        labels={'imported':f'已匯入 D{row.get("document_id", "")}','failed':'失敗','cancelled':'已停止，未匯入','unprocessed':'尚未處理'}
        self.listing.set(str(index),'status',labels[row['status']])
        if row.get('error'):
            self.detail.configure(state='normal');self.detail.insert('end',row['name']+'：'+row['error']+'\n');self.detail.configure(state='disabled')

    def poll(self):
        if not self.begin_poll():return
        # At most two progress events per selected file plus one terminal event.
        for _ in range(17):
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='reading':self.listing.set(str(value),'status','正在讀取…')
            elif kind=='result':self.show_result(*value)
            else:
                self.busy=False;self.stop_btn.configure(state='disabled')
                if kind=='finished':
                    self.result=value
                    counts={status:sum(row['status']==status for row in value['results']) for status in ('imported','failed','cancelled','unprocessed')}
                    for i,row in enumerate(value['results']):
                        if row['status']=='unprocessed':self.show_result(i,row)
                    self.notice.set(f'已匯入{counts["imported"]}／{len(self.items)}，失敗{counts["failed"]}，停止或尚未處理{counts["cancelled"]+counts["unprocessed"]}。'+
                                    ('已收到停止要求；已完成的匯入保留。' if value['stop_requested'] else '本次處理已結束。'))
                else:self.notice.set('匯入未完成：'+str(value))
                if self.on_complete and not getattr(self.root,'_assistant_closing',False) and self.parent.winfo_exists():
                    try:self.on_complete()
                    except Exception as exc:self.notice.set(self.notice.get()+' 列表更新未完成，請重開參考文件：'+str(exc)[:300])
                break
        self.schedule_poll()

    def _destroyed(self,event):
        if event.widget==self.window and not self.closed and getattr(self,'parent_binding',None):
            if self.parent.winfo_exists():self.parent.unbind('<Destroy>',self.parent_binding)
            self.parent_binding=None
        super()._destroyed(event)
