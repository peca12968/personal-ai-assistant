import tkinter as tk
import json
from tkinter import messagebox, ttk


class BriefWindow:
    def __init__(self, parent, store, sid):
        self.store, self.sid = store, sid
        self.window = tk.Toplevel(parent)
        self.window.title("對話目標與限制")
        self.window.geometry("720x620")
        self.window.minsize(600, 540)
        self.window.columnconfigure(0, weight=1)
        ttk.Label(self.window, text="僅套用到目前對話。手動保存的內容會加入後續提問；每欄最多 2000 字。",
                  padding=12, wraplength=570).grid(row=0, column=0, sticky="ew")
        brief = store.session_brief(sid)
        self.fields = {}
        for index, (key, title) in enumerate((("objective", "想完成的目標"),
                                             ("constraints_text", "不能違反的限制"),
                                             ("decisions", "已確認的決策／目前進度"))):
            frame = ttk.LabelFrame(self.window, text=title, padding=8)
            frame.grid(row=index + 1, column=0, sticky="nsew", padx=12, pady=5)
            self.window.rowconfigure(index + 1, weight=1)
            field = tk.Text(frame, height=4, wrap="word", font=("Microsoft JhengHei UI", 11))
            field.pack(fill="both", expand=True)
            field.insert("1.0", brief.get(key, ""))
            self.fields[key] = field
        ttk.Button(self.window, text="儲存", command=self.save).grid(row=4, column=0, sticky="e", padx=12, pady=10)
        self.notice = tk.StringVar(value="這是你確認的資訊，不會自動把模型推測當成事實。")
        ttk.Label(self.window, textvariable=self.notice, padding=12).grid(row=5, column=0, sticky="w")

    def save(self):
        try:
            self.store.save_session_brief(self.sid, **{key: field.get("1.0", "end").strip()
                                                       for key, field in self.fields.items()})
        except ValueError as exc:
            messagebox.showerror("無法儲存", str(exc), parent=self.window)
            return
        self.notice.set("已儲存，下次提問時生效。請隨需求變更更新這些欄位。")


def show_context(parent, store, message_id):
    context = store.answer_context(message_id) if message_id else None
    if context is None:
        messagebox.showinfo("本次參考", "尚無可查看的參考紀錄。升級前的回答未保存快照。", parent=parent)
        return
    window = tk.Toplevel(parent)
    local = context.get('response_mode') == 'local_memory'
    window.title('本機記憶操作紀錄' if local else "本次參考資料 · 送出時快照")
    window.geometry("820x640")
    window.minsize(600, 420)
    notice = ('這是本機操作完成時的紀錄，沒有呼叫模型。後續編輯不會改寫這份歷史紀錄。' if local else
              "顯示此回答送出時的參考資料，不代表模型實際採用了每一筆。之後編輯記憶不會改寫此快照。")
    ttk.Label(window, text=notice,
              padding=12, wraplength=730).pack(fill="x")
    notebook = ttk.Notebook(window)
    notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    brief = context.get("session_brief", {})
    sections = [
        ("問題與目標", "當時問題：\n" + context["current_request"] + "\n\n目標：\n" + brief.get("objective", "未設定") +
         "\n\n限制：\n" + brief.get("constraints_text", "未設定") + "\n\n決策：\n" + brief.get("decisions", "未設定")),
        ("長期記憶", "\n\n".join(f'記憶 #{r["id"]} · {r["kind"]}\n{r["content"]}' for r in context["memories"]) or "本次沒有選取長期記憶。"),
        ("早期摘錄", "\n\n".join(f'使用者訊息 #{r["message_id"]}\n{r["content"]}' for r in context["earlier_user_excerpts"]) or "本次沒有早期摘錄。"),
        ("最近對話", "\n\n".join(f'{"你" if r["role"] == "user" else "助理"} · #{r["message_id"]}\n{r["content"]}' for r in context["recent_conversation"]) or "本次沒有前文。"),
    ]
    documents = context.get("documents", {})
    checks = store.answer_checks(message_id)
    citation_text = "\n".join(check["citation"] + ("：範圍存在" if check["range_valid"] else "：無法對照") for check in checks) or "沒有可檢查的文件引用。"
    doc_text = ("僅檢查來源編號與行號，未驗證結論的語意或正確性。\n" + citation_text + "\n\n" +
                ("只提供部分文件片段，不能據此視為讀完整份文件。\n\n" if documents.get("coverage_limited") else "") +
                "\n\n".join(f'D{row["document_id"]} · {row["name"]} · L{row["start_line"]}-L{row["end_line"]}' +
                             ("（部分行內容）" if row["partial_line"] else "") + "\n" + row["text"]
                             for row in documents.get("excerpts", [])))
    sections.append(("文件來源", doc_text))
    sections.append(('溝通提示', '這些是可能誤判的輔助提示，不能取代完整原文。\n\n' +
                     json.dumps(context.get('communication', {}), ensure_ascii=False, indent=2)))
    sections.append(('改版紀錄', json.dumps(context.get('self_improvement', []), ensure_ascii=False, indent=2)))
    sections.append(('工作結果', '以下是提問時已保存的工作狀態快照，並非聊天模型新執行的操作。\n\n' +
                     (json.dumps(context.get('task_runs'), ensure_ascii=False, indent=2) if context.get('task_runs') else '本次未提供工作紀錄。')))
    if local:
        sections = [('本機操作', '當時指令：\n' + context['current_request'] + '\n\n' +
                     json.dumps(context['memory_action'], ensure_ascii=False, indent=2))]
    for title, content in sections:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        text = tk.Text(frame, wrap="word", padx=12, pady=12, font=("Microsoft JhengHei UI", 11))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
