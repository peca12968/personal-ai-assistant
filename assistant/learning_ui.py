"""User-confirmed local examples. Saving never invokes a model or starts training."""
import json
import sqlite3
import tkinter as tk
from tkinter import ttk

from . import learning_queue as queue
from .ui_layout import ScrollableForm, fit_window

LABELS = {'ready':'已確認，待訓練', 'needs_review':'來源已變更，需核對', 'withdrawn':'已撤回'}


class LearningWindow:
    def __init__(self, parent, store, source=None):
        self.store, self.current, self.source, self.page_number = store, None, None, 1
        self.window = tk.Toplevel(parent)
        self.window.title('本機學習佇列')
        fit_window(self.window, 880, 900)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='先核對一個不依賴前文的問題與完整正確答案，再按確認加入。內容保存在本機並隨資料備份；保存不代表已學會。可另外用閒置工作CLI明確啟動一次批次；確認佇列不啟動工作。', padding=12).pack(fill='x')
        self.listing = ttk.Treeview(body, columns=('status','revision','prompt'), show='headings', height=5)
        for key,label,width in [('status','目前狀態',190),('revision','版本',50),('prompt','問題預覽',560)]:
            self.listing.heading(key,text=label); self.listing.column(key,width=width,stretch=key=='prompt')
        self.listing.pack(fill='x',padx=12)
        nav = ttk.Frame(body,padding=12); nav.pack(fill='x')
        ttk.Button(nav,text='上一頁',command=lambda:self.move_page(-1)).pack(side='left')
        ttk.Button(nav,text='下一頁',command=lambda:self.move_page(1)).pack(side='left',padx=4)
        self.page_label = ttk.Label(nav); self.page_label.pack(side='left',padx=8)
        ttk.Button(nav,text='重新整理列表',command=self.refresh).pack(side='right')
        controls = ttk.Frame(body,padding=(12,0)); controls.pack(fill='x')
        ttk.Button(controls,text='載入選取項目（取代草稿）',command=self.load_selected).pack(side='left')
        ttk.Button(controls,text='新建獨立例子（清空草稿）',command=self.new).pack(side='left',padx=6)
        ttk.Button(controls,text='查看選取項目的歷史',command=self.show_history).pack(side='left')
        self.saved = tk.Text(body,height=6,wrap='word',state='disabled'); self.saved.pack(fill='x',padx=12,pady=8)
        self.source_view = tk.Text(body,height=5,wrap='word',state='disabled'); self.source_view.pack(fill='x',padx=12)
        self.fields = {}
        for key,label in [('prompt','獨立完整問題（最多2000字；必要背景請自行整理進來）'),('answer','你確認的完整正確答案（最多2000字；不要只填「不對」等評論）')]:
            ttk.Label(body,text=label).pack(anchor='w',padx=12,pady=(8,2))
            self.fields[key] = tk.Text(body,height=5,wrap='word')
            self.fields[key].pack(fill='x',padx=12)
        buttons = ttk.Frame(body,padding=12); buttons.pack(fill='x')
        self.confirm_button = ttk.Button(buttons,text='確認上述內容，加入／更新學習佇列',command=self.confirm)
        self.confirm_button.pack(side='left')
        ttk.Button(buttons,text='重新核對（保留草稿）',command=self.recheck).pack(side='left',padx=6)
        self.withdraw_button = ttk.Button(buttons,text='撤回目前項目',command=self.withdraw,state='disabled')
        self.withdraw_button.pack(side='left')
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body,textvariable=self.notice,padding=12).pack(fill='x')
        self.layout.wrapped_label(body,text='撤回會保留版本歷史，排除後續學習；不代表刪除備份或讓已訓練參數忘記內容。每個問題只能有一筆確認項目，最多500個項目／5000筆版本。這些例子不會自動加入一般聊天或上傳GitHub。',padding=12).pack(fill='x')
        self.new(source)
        self.refresh()

    @staticmethod
    def put(widget, value):
        widget.configure(state='normal'); widget.delete('1.0','end'); widget.insert('1.0',value); widget.configure(state='disabled')

    def display_source(self):
        if self.source:
            value = f"來源回答 #{self.source['message_id']} · 更正版本 {self.source['feedback_revision']}\n原問題：{self.source['question']}\n原更正意見：{self.source['correction']}\n更正意見不會自動填成訓練答案。"
        else:
            value = '獨立例子：問題與答案由你明確填寫，沒有自動讀取對話或記憶。'
        self.put(self.source_view,value)

    def display_saved(self):
        if self.current:
            r = self.current
            self.put(self.saved,f"已保存版本 {r['revision']} · {LABELS[r['readiness']]}\n問題：{r['prompt']}\n正確答案：{r['answer']}")
        else:
            self.put(self.saved,'新例子，尚未加入學習佇列。')
        self.withdraw_button.configure(state='normal' if self.current and self.current['status']=='confirmed' else 'disabled')
        self.display_source()

    def new(self, source=None):
        self.current = None
        self.source = json.loads(queue.encoded(queue.validate_source(source)))
        for field in self.fields.values(): field.delete('1.0','end')
        if self.source: self.fields['prompt'].insert('1.0',self.source['question'])
        self.display_saved()
        self.notice.set('請核對問題並填寫完整答案，按確認才會保存；沒有呼叫模型。')

    def refresh(self):
        try:
            result = queue.page(self.store,self.page_number)
            if self.page_number > result['pages']:
                self.page_number = result['pages']; result = queue.page(self.store,self.page_number)
            for key in self.listing.get_children(): self.listing.delete(key)
            for row in result['items']:
                self.listing.insert('', 'end', iid=row['id'], values=(LABELS[row['readiness']],row['revision'],row['prompt'].replace('\n',' ')[:100]))
            self.pages = result['pages']
            self.page_label.configure(text=f"第{result['page']}/{result['pages']}頁，共{result['total']}項")
        except (ValueError, sqlite3.Error) as exc:
            self.notice.set(str(exc))

    def move_page(self, delta):
        self.page_number = max(1,min(getattr(self,'pages',1),self.page_number+delta)); self.refresh()

    def load_selected(self):
        selected = self.listing.selection()
        if not selected: return
        try:
            self.current = queue.get(self.store,selected[0]); self.source = self.current['source']
            for key,field in self.fields.items():
                field.delete('1.0','end'); field.insert('1.0',self.current[key])
            self.display_saved()
            self.notice.set('已載入已保存內容。若來源已變更，請按重新核對、閱讀新更正並修改答案，再明確確認。')
        except (ValueError, sqlite3.Error) as exc:
            self.notice.set(str(exc))

    def recheck(self):
        try:
            if self.current:
                self.current = queue.get(self.store,self.current['id'])
            if self.source:
                self.source = queue.source_from_feedback(self.store,self.source['message_id'])
            self.display_saved()
            self.notice.set('已載入目前保存版本及最新來源供比較，編輯草稿保持原樣；核對完成後才能再按確認。')
        except (ValueError, sqlite3.Error) as exc:
            self.display_saved()
            self.notice.set(str(exc)+' 可撤回舊項目；新建獨立例子需要重新填寫與確認。')

    def confirm(self):
        try:
            value = queue.confirm(self.store,self.fields['prompt'].get('1.0','end-1c'),self.fields['answer'].get('1.0','end-1c'),
                source=self.source,item_id=self.current['id'] if self.current else None,
                expected_revision=self.current['revision'] if self.current else None)
            self.current = value; self.source = value['source']
            self.display_saved(); self.refresh()
            self.notice.set(f"已保存確認版本 {value['revision']}，待訓練；目前沒有啟動學習或變更模型參數。")
        except (ValueError, sqlite3.Error) as exc:
            self.notice.set(str(exc))

    def withdraw(self):
        if not self.current: return
        try:
            self.current = queue.withdraw(self.store,self.current['id'],self.current['revision'])
            self.display_saved(); self.refresh()
            self.notice.set('已撤回；版本歷史保留，不會用於後續新批次。已訓練參數與備份不會因此自動刪除。')
        except (ValueError, sqlite3.Error) as exc:
            self.notice.set(str(exc))

    def show_history(self):
        selected = self.listing.selection()
        if not selected: return
        item_id = selected[0]
        window = tk.Toplevel(self.window); window.title('學習項目版本歷史'); fit_window(window,800,650)
        frame = ttk.Frame(window); frame.pack(fill='both',expand=True)
        value = tk.Text(frame,wrap='word',state='disabled')
        scroll = ttk.Scrollbar(frame,command=value.yview); scroll.pack(side='right',fill='y')
        value.configure(yscrollcommand=scroll.set); value.pack(fill='both',expand=True)
        controls = ttk.Frame(window); controls.pack(fill='x')
        state = {'page':1,'pages':1}
        label = ttk.Label(controls); label.pack(side='left')
        def load(delta=0):
            state['page'] = max(1,min(state['pages'],state['page']+delta))
            try:
                result = queue.history(self.store,item_id,state['page']); state['pages'] = result['pages']
                self.put(value,'歷史保存內容，不是目前有效狀態或學習完成證明。\n\n'+'\n\n'.join(
                    f"版本 {r['revision']} · {'確認' if r['status']=='confirmed' else '撤回'} · {r['recorded_at']} UTC\n問題：{r['prompt']}\n答案：{r['answer']}\n來源：" + (f"回答#{r['source']['message_id']}，更正版本{r['source']['feedback_revision']}：{r['source']['correction']}" if r['source'] else '獨立填寫') for r in result['items']))
                label.configure(text=f"第{state['page']}/{state['pages']}頁")
            except (ValueError,sqlite3.Error) as exc: self.put(value,str(exc))
        ttk.Button(controls,text='上一頁',command=lambda:load(-1)).pack(side='left',padx=6)
        ttk.Button(controls,text='下一頁',command=lambda:load(1)).pack(side='left')
        load()
