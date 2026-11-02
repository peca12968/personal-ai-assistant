"""User-configured daily local reminders, never a background work grant."""
import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk

from . import reminders
from .ui_layout import ScrollableForm, fit_window


class ReminderWindow:
    def __init__(self, parent, store, changed=lambda: None):
        self.store, self.changed = store, changed
        self.window = tk.Toplevel(parent)
        self.window.title('主動回顧提醒')
        fit_window(self.window, 700, 500)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='設定每天何時提醒你回顧未完成工作或查看已保存成果。提醒顯示在主畫面，列出目前可採取的下一步；不插入聊天、不呼叫模型、不自動執行工作。', padding=12).pack(fill='x')
        self.enabled = tk.BooleanVar()
        self.enable_button = ttk.Checkbutton(body, text='啟用每日回顧提醒', variable=self.enabled)
        self.enable_button.pack(anchor='w', padx=12, pady=8)
        ttk.Label(body, text='電腦本地時間（HH:MM，24小時制）：').pack(anchor='w', padx=12)
        self.time = ttk.Entry(body, width=12);self.time.pack(anchor='w', padx=12, pady=8)
        self.layout.wrapped_label(body, text='預設關閉，只在這個應用開啟時檢查，每30秒一次。時間過後才開啟會提醒當天一次，不補發過去日期。聊天、記憶整理或本視窗啟動的自主工作忙碌時延後。已收起及紀錄仍為執行中的目標不列入；沒有可回顧項目就不提醒。', padding=12).pack(fill='x')
        self.layout.wrapped_label(body, text='可按「15分鐘後提醒」或「今日關閉」。重新開啟仍保留稍後提醒與當日紀錄；多個視窗共用設定。修改時間或停用再啟用不會重複當日已發出的提醒；停用會清除目前提醒。電腦睡眠或程式關閉時不會通知，也沒有背景工作或自我改碼排程。', padding=12).pack(fill='x')
        controls = ttk.Frame(body, padding=12);controls.pack(fill='x')
        self.save_button = ttk.Button(controls, text='儲存提醒設定', command=self.save);self.save_button.pack(side='left')
        self.reload_button = ttk.Button(controls, text='重新載入', command=self.reload);self.reload_button.pack(side='left', padx=8)
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.reload()

    def reload(self):
        try:
            value = reminders.settings(self.store)
            self.revision = value['revision'];self.enabled.set(value['enabled'])
            self.time.delete(0,'end');self.time.insert(0,value['daily_time'])
            self.notice.set(f"設定版本 {self.revision} · {'已啟用' if value['enabled'] else '未啟用'}")
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('無法載入提醒', str(exc), parent=self.window)

    def save(self):
        try:
            reminders.save_settings(self.store, enabled=self.enabled.get(), daily_time=self.time.get().strip(), expected_revision=self.revision)
            self.reload();self.changed()
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('無法儲存提醒', str(exc), parent=self.window)
