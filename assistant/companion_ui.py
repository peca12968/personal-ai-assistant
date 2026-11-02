"""Editable, local companion preferences with stale-editor protection."""
import tkinter as tk
import sqlite3
from tkinter import messagebox, ttk

from .companion_profile import OPTIONS, describe, get_profile, reset_profile, save_profile
from .ui_layout import ScrollableForm, fit_window


class CompanionWindow:
    def __init__(self, parent, store):
        self.store = store
        self.window = tk.Toplevel(parent)
        self.window.title('夥伴設定')
        fit_window(self.window, 720, 760)
        self.layout = ScrollableForm(self.window)
        body = self.layout.body
        self.layout.wrapped_label(body, text='設定你希望的稱呼與平常互動方式。儲存在本機，套用到這份資料庫的所有對話；下次聊天或新一輪自主工作開始時生效。', padding=12).pack(fill='x')
        self.fields = {}
        for key, label in [('assistant_name', '助理的稱呼（最多40字）'), ('user_name', '希望助理怎麼稱呼你（可留空）')]:
            ttk.Label(body, text=label).pack(anchor='w', padx=12, pady=(8, 3))
            entry = ttk.Entry(body)
            entry.pack(fill='x', padx=12)
            self.fields[key] = entry
        for key, label in [('tone', '平常語氣'), ('detail', '回答長度'), ('support', '互動方式')]:
            ttk.Label(body, text=label).pack(anchor='w', padx=12, pady=(8, 3))
            field = ttk.Combobox(body, values=list(OPTIONS[key].values()), state='readonly')
            field.pack(fill='x', padx=12)
            self.fields[key] = field
        ttk.Label(body, text='其他溝通偏好（最多1200字）').pack(anchor='w', padx=12, pady=(8, 3))
        self.notes = tk.Text(body, height=5, wrap='word')
        self.notes.pack(fill='x', padx=12)
        self.layout.wrapped_label(body, text='例如：不用每次喊我的名字；先給重點再補原因。本次明確要求仍優先，這些設定不會自動開啟工作或改碼。設定會隨問題傳給模型；重設不會刪除舊對話、工作紀錄與備份。', padding=12).pack(fill='x')
        controls = ttk.Frame(body, padding=12)
        controls.pack(fill='x')
        self.save_button = ttk.Button(controls, text='儲存設定', command=self.save)
        self.save_button.pack(side='left')
        self.reload_button = ttk.Button(controls, text='重新載入', command=self.reload)
        self.reload_button.pack(side='left', padx=8)
        self.reset_button = ttk.Button(controls, text='恢復預設並儲存', command=self.reset)
        self.reset_button.pack(side='left')
        self.notice = tk.StringVar()
        self.layout.wrapped_label(body, textvariable=self.notice, padding=12).pack(fill='x')
        self.load(get_profile(store))

    def load(self, profile):
        self.revision = profile['revision']
        settings = profile['settings']
        for key, field in self.fields.items():
            if key in OPTIONS:
                field.set(OPTIONS[key][settings[key]])
            else:
                field.delete(0, 'end')
                field.insert(0, settings[key])
        self.notes.delete('1.0', 'end')
        self.notes.insert('1.0', settings['notes'])
        self.notice.set(describe(profile).split('\n')[0])

    def values(self):
        result = {key: field.get() for key, field in self.fields.items()}
        for key, choices in OPTIONS.items():
            result[key] = next(code for code, label in choices.items() if label == result[key])
        result['notes'] = self.notes.get('1.0', 'end-1c')
        return result

    def save(self):
        try:
            self.load(save_profile(self.store, self.values(), expected_revision=self.revision))
            self.notice.set(f'已儲存版本 {self.revision}；下次聊天或新一輪自主工作生效。')
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('無法儲存設定', str(exc), parent=self.window)

    def reset(self):
        try:
            self.load(reset_profile(self.store, expected_revision=self.revision))
            self.notice.set('已恢復預設；舊對話與工作快照仍保留。')
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('無法重設', str(exc), parent=self.window)

    def reload(self):
        try:
            self.load(get_profile(self.store))
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror('無法載入設定', str(exc), parent=self.window)
