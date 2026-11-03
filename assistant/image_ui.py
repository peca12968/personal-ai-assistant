"""Explicit per-session image selection; importing never sends a model request."""
import io
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk

from .data_window import DataWindow
from . import image_inputs


class ImageWindow(DataWindow):
    shutdown_controls = ('import_btn', 'disable_btn', 'apply_btn', 'clear_btn')

    def __init__(self, parent, store, sid, selected, apply):
        self.store, self.sid, self.apply_selection = store, sid, apply
        self.own_window(parent)
        self.window.title('聊天圖片 · 選取後才隨訊息傳送')
        self.window.geometry('850x650'); self.window.minsize(650, 480)
        ttk.Label(self.window, padding=12, wraplength=760,
                  text='PNG／JPEG，每張最多4 MiB、1600萬像素，每次最多4張。匯入只存本機副本；套用選取後，圖片會隨本對話每次傳送交給Codex，取消選取即停止。重開程式需重新選取。').pack(fill='x')
        self.listing = ttk.Treeview(self.window, columns=('name','size'), show='headings', selectmode='extended', height=7)
        self.listing.heading('name', text='檔名（依清單順序附圖）');self.listing.column('name',width=460)
        self.listing.heading('size', text='尺寸／格式');self.listing.column('size',width=190)
        self.listing.pack(fill='x',padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.preview)
        self.picture = ttk.Label(self.window, anchor='center');self.picture.pack(fill='both',expand=True,padx=12,pady=8)
        buttons=ttk.Frame(self.window,padding=12);buttons.pack(fill='x')
        self.import_btn=ttk.Button(buttons,text='匯入圖片',command=self.import_files);self.import_btn.pack(side='left')
        self.disable_btn=ttk.Button(buttons,text='停用所選圖片',command=self.disable);self.disable_btn.pack(side='left',padx=8)
        self.clear_btn=ttk.Button(buttons,text='取消附圖',command=lambda:self.commit(()));self.clear_btn.pack(side='right')
        self.apply_btn=ttk.Button(buttons,text='套用選取',command=lambda:self.commit(self.selected()));self.apply_btn.pack(side='right',padx=8)
        self.notice=tk.StringVar(value='停用後不再可選，但原副本保留供歷史回答、重試及本機備份使用。')
        ttk.Label(self.window,textvariable=self.notice,wraplength=760,padding=12).pack(fill='x')
        self.refresh(selected)

    def selected(self):
        chosen=set(self.listing.selection())
        return tuple(row['id'] for row in self.rows if row['id'] in chosen)

    def refresh(self, selected=()):
        self.rows=image_inputs.catalog(self.store,self.sid)
        self.listing.delete(*self.listing.get_children())
        for row in self.rows:
            self.listing.insert('', 'end', iid=row['id'], values=(row['name'],f'{row["width"]}×{row["height"]} · {row["format"]}'))
        self.listing.selection_set([r['id'] for r in self.rows if r['id'] in selected])
        self.preview()

    def preview(self, event=None):
        if self.unavailable():return
        selected=self.selected()
        self.picture.configure(image='',text='選取一張可預覽；多選順序依上方清單。');self.photo=None
        if len(selected)!=1:return
        try:
            from PIL import Image, ImageOps, ImageTk
            _,data=image_inputs.prepare(self.store,self.sid,selected)
            with Image.open(io.BytesIO(data[0])) as source:
                picture=ImageOps.exif_transpose(source)
                picture.thumbnail((740,280))
                self.photo=ImageTk.PhotoImage(picture,master=self.window)
            self.picture.configure(image=self.photo,text='')
        except (ValueError,OSError,sqlite3.Error) as exc:self.notice.set(str(exc))

    def import_files(self):
        if self.unavailable():return
        paths=filedialog.askopenfilenames(parent=self.window,title='匯入聊天圖片',filetypes=[('PNG／JPEG','*.png *.jpg *.jpeg')])
        if not paths or self.unavailable():return
        try:
            ids=image_inputs.attach_paths(self.store,self.sid,paths)
            self.refresh(ids);self.notice.set('已保存本機副本，尚未傳送。按套用選取，再回聊天輸入問題。')
        except (ValueError,OSError,sqlite3.Error) as exc:self.notice.set(str(exc))

    def disable(self):
        if self.unavailable():return
        try:
            selected=set(self.selected())
            image_inputs.deactivate_many(self.store,self.sid,[(row['id'],row['sha256']) for row in self.rows if row['id'] in selected])
            self.refresh();self.notice.set('已停用；歷史副本仍保留。原聊天選取若含停用圖片，送出會拒絕，請重新套用選取。')
        except (ValueError,OSError,sqlite3.Error) as exc:self.notice.set(str(exc))

    def commit(self, ids):
        if self.unavailable():return
        try:
            image_inputs.prepare(self.store,self.sid,ids)
            self.apply_selection(self.sid,tuple(ids))
        except (ValueError,OSError,sqlite3.Error) as exc:
            self.notice.set(str(exc));return
        self.close()
