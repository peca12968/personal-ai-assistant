import queue
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .backup import create_backup, restore_backup, stamp
from .engine import ROOT


class BackupWindow:
    def __init__(self, parent, store):
        self.store = store
        self.restored = None
        self.running = False
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.title('本機備份與還原')
        self.window.geometry('730x510')
        self.window.minsize(620, 440)
        ttk.Label(self.window, text='保存完整記憶與對話，還原時保留目前資料', padding=14,
                  font=('Microsoft JhengHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(self.window, text='包含對話、記憶、回饋、文件副本、回答快照及工作紀錄／報告。\n不包含程式、Codex 登入、原始文件或外部匯出檔；備份未加密，請放在你信任的位置。',
                  wraplength=670, padding=(14, 0, 14, 12)).pack(fill='x')
        ttk.Label(self.window, text='目前資料：' + str(store.path), wraplength=670, padding=14).pack(fill='x')
        controls = ttk.Frame(self.window, padding=14)
        controls.pack(fill='x')
        self.backup_button = ttk.Button(controls, text='建立本機備份', command=self.backup)
        self.backup_button.pack(side='left', padx=(0, 8))
        self.restore_button = ttk.Button(controls, text='從備份還原到新資料夾', command=self.restore)
        self.restore_button.pack(side='left')
        self.notice = tk.StringVar(value='備份預設存放在目前資料旁的 backups。還原會先檢查格式與完整性，不會覆寫原資料。')
        ttk.Label(self.window, textvariable=self.notice, wraplength=670, padding=14).pack(fill='both', expand=True)
        self.open_button = ttk.Button(self.window, text='開啟還原後的助理', state='disabled', command=self.open_restored)
        self.open_button.pack(anchor='e', padx=14, pady=14)
        self.window.after(100, self.poll)

    def work(self, operation):
        self.running = True
        for button in (self.backup_button, self.restore_button, self.open_button):
            button.configure(state='disabled')
        self.notice.set('處理中… 正在建立或核對資料。')
        def run():
            try:
                self.events.put((True, operation()))
            except Exception as exc:
                self.events.put((False, str(exc)))
        # Finish a requested snapshot even if this dialog closes.
        threading.Thread(target=run, daemon=False).start()

    def backup(self):
        if not self.running:
            self.work(lambda: create_backup(self.store.path))

    def restore(self):
        if self.running:
            return
        backup = filedialog.askopenfilename(parent=self.window, title='選取助理備份',
                    initialdir=self.store.path.parent / 'backups', filetypes=[('助理備份', '*.paibackup')])
        if not backup:
            return
        parent = filedialog.askdirectory(parent=self.window, title='選取還原資料夾的存放位置')
        if parent:
            destination = Path(parent) / ('assistant-restored-' + stamp())
            self.work(lambda: restore_backup(backup, destination))

    def poll(self):
        try:
            ok, result = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.running = False
            self.backup_button.configure(state='normal')
            self.restore_button.configure(state='normal')
            if ok:
                counts = result['counts']
                summary = f'對話 {counts["sessions"]} 段、訊息 {counts["messages"]} 則、記憶 {counts["memories"]} 筆、文件 {counts["documents"]} 個、工作 {counts.get("task_runs", 0)} 筆。'
                if 'data_dir' in result:
                    self.restored = result['data_dir']
                    self.notice.set('已驗證並還原至：\n' + self.restored + '\n\n' + summary + '\n按下方按鈕可開啟還原資料；原視窗仍使用原資料。')
                else:
                    self.notice.set('備份已完成：\n' + result['path'] + '\n\n' + summary + '\n可將此備份另存到其他磁碟，以應對磁碟故障。')
            else:
                self.notice.set('未完成：' + result)
            self.open_button.configure(state='normal' if self.restored else 'disabled')
        self.window.after(100, self.poll)

    def open_restored(self):
        if self.restored and not self.running:
            try:
                subprocess.Popen([sys.executable, '-m', 'assistant', '--data-dir', self.restored], cwd=ROOT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except OSError as exc:
                self.notice.set('還原資料已保存，但無法開啟視窗：' + str(exc))
                return
            self.notice.set('已啟動還原資料視窗：\n' + self.restored + '\n原視窗仍使用原資料，可在標題或備份視窗確認資料位置。')
