"""Review existing goal state before appending a user correction."""
import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk

from . import decision_table, goal_guidance as guidance
from .autonomy import LABELS
from .ui_layout import ScrollableForm, fit_window


class GoalGuidanceWindow:
    def __init__(self, parent, store, sid, gid, changed=lambda:None):
        self.store,self.sid,self.gid,self.changed=store,sid,gid,changed
        self.current=None
        self.window=tk.Toplevel(parent);self.window.title('補充／更正此目標')
        fit_window(self.window,820,700)
        self.layout=ScrollableForm(self.window);body=self.layout.body
        self.layout.wrapped_label(body,text='核對目前目標後追加新的條件或更正。原目標、已完成步驟與舊補充會保留，明確更正供下一次接續使用；保存不會自行開始工作。',padding=12).pack(fill='x')
        self.context=tk.Text(body,height=12,wrap='word',state='disabled');self.context.pack(fill='x',padx=12)
        self.layout.wrapped_label(body,text='新的補充（最多4000字，全部補充累計最多12000字）：',padding=12).pack(fill='x')
        self.content=tk.Text(body,height=5,wrap='word');self.content.pack(fill='x',padx=12)
        self.layout.wrapped_label(body,text='若正在工作，先停止並等候結束。需要回答問題時，使用原視窗的「回答目前問題」。',padding=12).pack(fill='x')
        controls=ttk.Frame(body,padding=12);controls.pack(fill='x')
        self.save_button=ttk.Button(controls,text='追加補充到此目標',command=self.save,state='disabled');self.save_button.pack(side='left')
        self.reload_button=ttk.Button(controls,text='重新載入目標',command=self.reload);self.reload_button.pack(side='left',padx=8)
        self.notice=tk.StringVar()
        self.layout.wrapped_label(body,textvariable=self.notice,padding=12).pack(fill='x')
        self.reload()

    def reload(self):
        try:
            self.current=guidance.snapshot(self.store,self.sid,self.gid)
            goal=self.current['goal'];allowed,reason=guidance.eligibility(self.current)
            lines=['目標：'+goal['objective'],'狀態：'+LABELS.get(goal['status'],goal['status']),
                   f"已記錄步驟：{self.current['step_count']}", '尚待回答：'+(goal['question'] or '無'),
                   '先前補充（歷史原文）：\n'+(goal['notes'] or '無')]
            if 'supplement_documents' in self.current:
                files = self.current['supplement_documents']
                lines.append('目前已匯入文件\n' + '\n'.join(f'D{row["document_id"]} · {row["name"]}' for row in files))
            if conflict := self.current.get('document_conflict_report'):
                if conflict['options']:
                    lines.append('待確認的相衝突原行\n' + '\n'.join(
                        f'{option["citation"]} · {option["rule"]["evidence"]["quote"]}' for option in conflict['options']))
                for amendment in conflict['amendments']:
                    lines.append(f'目前採用 {amendment["citation"]}\n' + '\n'.join(
                        f'{option["citation"]} · {option["rule"]["evidence"]["quote"]}' for option in amendment['options']))
            self.context.configure(state='normal');self.context.delete('1.0','end');self.context.insert('1.0','\n\n'.join(lines));self.context.configure(state='disabled')
            self.save_button.configure(state='normal' if allowed else 'disabled');self.notice.set(reason)
        except (ValueError,RuntimeError,OSError,sqlite3.Error) as exc:
            self.current=None;self.save_button.configure(state='disabled');self.notice.set(str(exc))

    def save(self):
        if self.current is None:return
        try:
            saved = guidance.save(self.store,self.sid,self.gid,self.content.get('1.0','end'),expected_fingerprint=self.current['fingerprint'])
            rechecked = self.current.keys() & {
                'reference_report', 'document_version_report', 'document_supplement', 'document_conflict_report',
                'report_colloquial_question'} or (
                self.current['goal']['status'] == 'needs_input'
                and self.current['goal']['question'] != saved['goal']['question'])
            self.content.delete('1.0','end');self.reload();self.changed()
            if saved['goal'].get('answered_reference_review'):
                self.notice.set(decision_table.describe_answered_review(saved['goal']['answered_reference_review']))
            elif saved['goal'].get('reference_review'):
                self.notice.set(decision_table.describe_waiting_review(saved['goal']['reference_review']))
            else:
                self.notice.set(saved['goal']['notice'] if rechecked else '已追加保存。可回原視窗處理待回答問題或開始／接續。')
        except (ValueError,RuntimeError,OSError,sqlite3.Error) as exc:
            messagebox.showerror('無法保存補充',str(exc),parent=self.window)
