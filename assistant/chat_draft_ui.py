"""Per-session editor recovery with explicit resolution of concurrent changes."""
import sqlite3
import weakref
import tkinter as tk
from tkinter import ttk

from . import chat_drafts as drafts
from .ui_layout import fit_window

ERRORS = (ValueError, OSError, sqlite3.Error)


class DraftEditor:
    def __init__(self, app, parent):
        # Tk objects must be released on their owning thread. Do not keep the
        # App <-> editor cycle alive after the window destroys its callbacks.
        self.app = weakref.proxy(app)
        self.base = drafts.get(app.store, app.sid)
        self.sending = None
        self.conflict = False
        self.failed_text = ''
        self.timer = None
        self.notice = tk.StringVar(value='聊天草稿只存本機，不會自動送出。')
        bar = ttk.Frame(parent)
        bar.grid(row=4,column=0,sticky='ew')
        bar.columnconfigure(0,weight=1)
        ttk.Label(bar,textvariable=self.notice,wraplength=480).grid(row=0,column=0,sticky='w')
        self.review_button = ttk.Button(bar,text='核對草稿',command=self.review)
        self.review_button.grid(row=0,column=1,sticky='e')
        self.replace(self.base['content'])
        app.input.bind('<<Modified>>',self.modified)
        self.timer = app.root.after(1000,self.tick)

    def raw(self):
        return self.app.input.get('1.0','end-1c')

    def replace(self,text):
        self.app.input.delete('1.0','end')
        self.app.input.insert('1.0',text)
        self.app.input.edit_modified(False)

    def modified(self,event=None):
        if self.app.input.edit_modified():
            self.app.input.edit_modified(False)
            if not self.conflict:
                self.notice.set('聊天草稿有未保存修改…')

    def reconcile(self,final=False):
        if self.sending is None:
            return True
        token = self.sending
        if drafts.submitted(self.app.store,self.app.sid,token):
            # Consumption and the message commit together. A subsequent writer
            # must still pass the revision check, including another window.
            self.base = {'revision':token['revision']+1,'content':''}
            self.sending = None
            return True
        if not final:
            return False
        self.sending = None
        self.base = {k:token[k] for k in ('revision','content')}
        if not self.raw():
            self.replace(token['content'])
        elif self.raw() != token['content']:
            self.failed_text = token['content']
            self.conflict = True
            self.notice.set('問題尚未入庫，且你已輸入下一段；請核對草稿，兩段文字均保留供選擇。')
        return not self.conflict

    def flush(self):
        try:
            if not self.reconcile() or self.conflict:
                return False
            raw = self.raw()
            if raw != self.base['content']:
                self.base = drafts.save(self.app.store,self.app.sid,raw,self.base['revision'])
            elif drafts.get(self.app.store,self.app.sid) != self.base:
                raise ValueError('本機草稿已由其他視窗變更；請核對草稿，目前輸入保留。')
            self.notice.set('聊天草稿已存本機 · 尚未送出' if raw else '輸入框為空 · 沒有待送出的草稿')
            return True
        except ERRORS as exc:
            self.notice.set(str(exc))
            return False

    def tick(self):
        self.timer = None
        self.flush()
        self.timer = self.app.root.after(1000,self.tick)

    def switch(self,sid):
        if sid == self.app.sid:
            return True
        if not self.flush():
            return False
        try:
            target = drafts.get(self.app.store,sid)
        except ERRORS as exc:
            self.notice.set(str(exc)); return False
        self.app.sid = sid
        self.base = target
        self.replace(target['content'])
        self.failed_text = ''
        self.notice.set('已恢復這段對話的本機草稿；尚未送出。')
        return True

    def prepare_send(self):
        if not self.flush():
            return None
        self.sending = drafts.submission(self.base)
        return self.sending

    def finish(self):
        try:
            self.reconcile(final=True)
            self.flush()
        except ERRORS as exc:
            self.notice.set(str(exc))

    def stop_timer(self):
        if self.timer is not None:
            self.app.root.after_cancel(self.timer)
            self.timer = None

    def resolve(self, observed, editor_at_open, choice):
        if self.app.busy or self.sending is not None:
            raise ValueError('請先等候本次送出流程結束，再核對草稿。')
        if self.raw() != editor_at_open:
            raise ValueError('核對期間輸入又有修改，請重新開啟核對；目前文字保留。')
        if choice == 'local':
            content = editor_at_open
        elif choice == 'saved':
            content = observed['content']
        elif choice == 'failed' and self.failed_text:
            content = self.failed_text
        else:
            raise ValueError('請明確選取要保留的草稿。')
        # Even accepting the saved version writes a checked revision, so the
        # reviewed source cannot silently change before replacing the editor.
        saved = drafts.save(self.app.store,self.app.sid,content,observed['revision'])
        self.base = saved
        self.replace(content)
        self.failed_text = ''
        self.conflict = False
        self.notice.set('核對後的聊天草稿已存本機；尚未送出。')

    def review(self):
        if self.app.busy or self.sending is not None:
            self.notice.set('請先等候本次送出流程結束，再核對草稿。'); return
        try:
            observed = drafts.get(self.app.store,self.app.sid)
        except ERRORS as exc:
            self.notice.set(str(exc)); return
        sid, local, failed = self.app.sid, self.raw(), self.failed_text
        window = tk.Toplevel(self.app.root)
        window.title('核對未送出的聊天草稿')
        fit_window(window,800,650)
        ttk.Label(window,text='選擇要保留的版本；其他文字可先複製。這不會送出問題。',wraplength=740).pack(fill='x',padx=12,pady=8)
        panels=[('目前輸入',local),('本機已保存',observed['content'])]
        if failed: panels.append(('先前尚未入庫的問題',failed))
        for label, content in panels:
            ttk.Label(window,text=label).pack(anchor='w',padx=12)
            frame=ttk.Frame(window);frame.pack(fill='both',expand=True,padx=12)
            field=tk.Text(frame,height=5,wrap='word');field.pack(side='left',fill='both',expand=True)
            scroll=ttk.Scrollbar(frame,command=field.yview);scroll.pack(side='right',fill='y')
            field.configure(yscrollcommand=scroll.set)
            field.insert('1.0',content);field.configure(state='disabled')
        notice=tk.StringVar()
        ttk.Label(window,textvariable=notice,wraplength=740).pack(fill='x',padx=12)
        buttons=ttk.Frame(window);buttons.pack(fill='x',padx=12,pady=8)
        def choose(choice):
            try:
                if self.app.sid != sid or self.failed_text != failed:
                    raise ValueError('對話或待核對內容已變，請重新開啟核對。')
                self.resolve(observed,local,choice)
                window.destroy()
            except ERRORS as exc:
                notice.set(str(exc))
        for choice,label in [('local','保留目前輸入'),('saved','採用已保存草稿')] + ([('failed','採用先前未入庫問題')] if failed else []):
            ttk.Button(buttons,text=label,command=lambda c=choice:choose(c)).pack(side='left',padx=4)
