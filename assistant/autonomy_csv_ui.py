"""Inspect and explicitly export complete autonomous CSV results locally."""
import json
import tkinter as tk
from tkinter import filedialog, ttk

from . import autonomy, autonomy_csv
from .ui_layout import ScrollableForm, fit_window


class CsvResultsWindow:
    def __init__(self, parent, store, sid, gid):
        self.store, self.sid, self.gid = store, sid, gid
        self.window = tk.Toplevel(parent)
        self.window.title('CSV分析計畫與完整結果')
        fit_window(self.window, 900, 720)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='查看模型保存的分析規格及實際計算結果。模型收到的分頁可能省略組別；這裡可以匯出完整已保存的JSON。來源是建立目標時選取的匯入副本，未重新讀外部原檔。', padding=12).pack(fill='x')
        self.listing = ttk.Treeview(body, columns=('status', 'id'), show='headings', height=4)
        self.listing.heading('status', text='紀錄狀態')
        self.listing.heading('id', text='計畫編號')
        self.listing.pack(fill='x', padx=12)
        self.listing.bind('<<TreeviewSelect>>', self.select)
        ttk.Button(body, text='重新整理', command=self.refresh).pack(anchor='w', padx=12, pady=4)
        self.output = tk.Text(body, height=20, wrap='word', state='disabled')
        self.output.pack(fill='x', padx=12, pady=8)
        self.export_button = ttk.Button(body, text='匯出完整計算結果 JSON', command=self.export, state='disabled')
        self.export_button.pack(anchor='w', padx=12, pady=4)
        self.notice = tk.StringVar(value='僅查看已保存內容，不會啟動分析。匯出使用新檔名，不覆寫既有檔案。')
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.refresh()

    def refresh(self):
        chosen = (self.listing.selection() or [None])[0]
        goal = autonomy.get_goal(self.store, self.sid, self.gid)
        for old in self.listing.get_children():
            self.listing.delete(old)
        labels = {'planned': '已保存計畫', 'running': '未保存完成狀態', 'succeeded': '計算完成', 'failed': '計算失敗', 'cancelled': '已取消'}
        for item in goal['csv_plans']:
            self.listing.insert('', 'end', iid=item['plan_id'], values=(labels[item['status']], item['plan_id']))
        if chosen and self.listing.exists(chosen):
            self.listing.selection_set(chosen)
        self.select()

    def select(self, event=None):
        selected = (self.listing.selection() or [None])[0]
        text = '選取一份計畫，查看分析規格、結果及匯出紀錄。'
        success = False
        if selected:
            try:
                item = autonomy_csv.details(self.store, self.sid, self.gid, selected)
                text = json.dumps(item, ensure_ascii=False, indent=2)
                if len(text) > 30000:
                    text = text[:30000] + '\n…預覽已省略，請匯出完整結果。'
                success = item['status'] == 'succeeded'
            except ValueError as exc:
                text = str(exc)
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('1.0', text)
        self.output.configure(state='disabled')
        self.export_button.configure(state='normal' if success else 'disabled')

    def export(self):
        selected = (self.listing.selection() or [None])[0]
        if selected:
            path = filedialog.asksaveasfilename(parent=self.window, title='另存完整CSV分析結果',
                defaultextension='.json', initialdir=self.store.path.parent,
                initialfile='csv-result-' + selected[:12] + '.json', filetypes=[('JSON', '*.json')])
            if path:
                try:
                    receipt = autonomy_csv.export(self.store, self.sid, self.gid, selected, path)
                    self.notice.set('已匯出並讀回核對：' + receipt['path'])
                    self.select()
                except (ValueError, OSError, RuntimeError) as exc:
                    self.notice.set(str(exc))
