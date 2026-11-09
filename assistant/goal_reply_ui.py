"""A reply editor belongs to one reviewed goal, independent of list selection."""
import tkinter as tk
from tkinter import messagebox, ttk

from . import goal_replies
from .autonomy import LABELS, MAX_STEPS
from .ui_layout import ScrollableForm, fit_window


class GoalReplyWindow:
    def __init__(self, parent, store, sid, gid, *, prepare_resume, saved):
        self.store, self.sid, self.gid = store, sid, gid
        self.prepare_resume, self.saved = prepare_resume, saved
        self.current = None
        self.window = tk.Toplevel(parent)
        self.window.title('回答目前問題')
        fit_window(self.window, 820, 680)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='此回答固定送到下列目標與問題。切換原視窗的目標不會改變回答對象。', padding=12).pack(fill='x')
        self.context = tk.Text(body, height=12, wrap='word', state='disabled')
        self.context.pack(fill='x', padx=12)
        self.layout.wrapped_label(body, text='你的回答（最多4000字）：', padding=12).pack(fill='x')
        self.content = tk.Text(body, height=5, wrap='word')
        self.content.pack(fill='x', padx=12)
        self.layout.wrapped_label(body, text='保存補充只記錄回答。「保存補充並接續一輪」使用原視窗的一般本輪步數；不切換持續執行或增加工具權限。未保存文字僅留此視窗，關閉後不會恢復。', padding=12).pack(fill='x')
        controls = ttk.Frame(body, padding=12)
        controls.pack(fill='x')
        self.save_button = ttk.Button(controls, text='保存補充', command=self.save)
        self.save_button.pack(side='left')
        self.resume_button = ttk.Button(controls, text='保存補充並接續一輪', command=lambda: self.save(resume=True))
        self.resume_button.pack(side='left', padx=8)
        self.withdrawal = ttk.Frame(body, padding=12)
        ttk.Label(self.withdrawal, text='選擇使原問題不再適用的更正編號').pack(side='left')
        self.correction = ttk.Combobox(self.withdrawal, state='readonly', width=5)
        self.correction.pack(side='left', padx=8)
        ttk.Button(self.withdrawal, text='依所選更正撤下目前問題', command=self.withdraw).pack(side='left')
        self.reload_button = ttk.Button(body, text='重新載入問題（保留回答文字）', command=self.reload)
        self.reload_button.pack(anchor='w', padx=12)
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.reload()

    def reload(self):
        try:
            self.current = goal_replies.snapshot(self.store, self.sid, self.gid)
            goal = self.current['goal']
            waiting = goal['status'] == 'needs_input' and bool(goal['question'])
            resumable = self.current['sources_match'] and self.current['step_count'] < MAX_STEPS
            lines = ['目標：' + goal['objective'], '狀態：' + LABELS.get(goal['status'], goal['status']),
                     '目前問題：' + (goal['question'] or '無'), '先前問題與補充：\n' + (goal['notes'] or '無')]
            if goal.get('partial_result'):
                lines[2:2] = ['待補期間的成果（尚待定稿）', goal['partial_result']]
            if goal.get('question_citations'):
                refs = goal['question_citations']
                tokens = dict.fromkeys(row['citation'] for row in refs['documents'] + refs['replies'] + refs.get('guidance', []))
                lines.insert(-1, '目前問題的來源位置 ' + '、'.join(tokens))
                lines[-1:-1] = [f"{source['citation']} · {source['content']}"
                               for source in goal.get('guidance_sources', []) if source['citation'] in tokens]
            options = self.current.get('question_withdrawal', {}).get('correction_numbers', [])
            if options:
                lines += ['這次提問後保存的更正。若原問題已不適用，可選擇依據後撤下。']
                lines += [f"{source['citation']} · {source['content']}"
                          for source in goal.get('guidance_sources', []) if source['number'] in options]
                self.correction.configure(values=options)
                self.correction.set('')
                self.withdrawal.pack(fill='x', before=self.reload_button)
            else:
                self.withdrawal.pack_forget()
            self.context.configure(state='normal')
            self.context.delete('1.0', 'end'); self.context.insert('1.0', '\n\n'.join(lines))
            self.context.configure(state='disabled')
            self.save_button.configure(state='normal' if waiting else 'disabled')
            self.resume_button.configure(state='normal' if waiting and resumable else 'disabled')
            self.notice.set('請核對目前問題與回答文字，再保存。' if waiting and resumable else
                            '來源已變更或達步數上限；可保存回答後，帶入所選進度建立下一階段。' if waiting else
                            '目前沒有等待回答的問題；未保存文字仍留在此視窗。')
        except Exception as exc:
            self.current = None
            self.withdrawal.pack_forget()
            self.save_button.configure(state='disabled'); self.resume_button.configure(state='disabled')
            self.notice.set(str(exc))

    def save(self, *, resume=False):
        if self.current is None:
            return
        try:
            budget = self.prepare_resume() if resume else None
            goal_replies.save(self.store, self.sid, self.gid, self.content.get('1.0', 'end'),
                              expected_fingerprint=self.current['fingerprint'])
        except Exception as exc:
            messagebox.showerror('回答尚未保存', str(exc), parent=self.window)
            return
        self.content.delete('1.0', 'end')
        self.reload()
        # Saving is committed before starting. Never imply rollback on start failure.
        try:
            started = self.saved(self.gid, budget)
            self.notice.set('回答已保存，已啟動接續一輪；請在原視窗查看進度或停止。' if resume and started else
                            '回答已保存；尚未接續，可回原視窗按開始／接續。')
        except Exception as exc:
            self.notice.set('回答已保存，但未能啟動接續；可回原視窗查看並開始。' + str(exc))

    def withdraw(self):
        if self.current is None:
            return
        chosen = self.correction.get()
        if not chosen:
            self.notice.set('請先核對並選擇一筆已保存的更正。')
            return
        try:
            goal_replies.withdraw(self.store, self.sid, self.gid, int(chosen),
                                  expected_fingerprint=self.current['fingerprint'])
        except Exception as exc:
            messagebox.showerror('問題尚未撤下', str(exc), parent=self.window)
            return
        self.reload()
        try:
            self.saved(self.gid, None)
            self.notice.set('問題已撤下，原提問與暫表可在目標詳情查看。請回原視窗開始／接續。')
        except Exception as exc:
            self.notice.set('問題已撤下。請回原視窗重新整理後接續。' + str(exc))
