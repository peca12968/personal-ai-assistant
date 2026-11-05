import tkinter as tk
import json
import sqlite3
from tkinter import messagebox, ttk
from .document_ranges import range_status_text
from .companion_profile import describe as describe_profile


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
    calculated = context.get('response_mode') == 'local_calculation'
    transcribed = context.get('response_mode') == 'local_transcription'
    evidence = context.get('response_mode') == 'local_evidence'
    from . import document_calculation
    document_math = context.get('response_mode') == document_calculation.MODE
    if document_math:
        try:
            math_receipt = document_calculation.read(store, message_id)
            if math_receipt is None:
                raise ValueError('找不到本機文件計算紀錄。')
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            window.destroy()
            messagebox.showerror('本機文件計算紀錄無法核對', str(exc), parent=parent)
            return
    if evidence:
        from . import chat_evidence
        try:
            evidence_receipt = chat_evidence.read(store, message_id)
            if evidence_receipt is None:
                raise ValueError('找不到本機條件核對紀錄。')
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            window.destroy()
            messagebox.showerror('本機條件核對紀錄無法核對', str(exc), parent=parent)
            return
    if transcribed:
        from .chat_transcription import read,describe as describe_transcription
        try:
            transcription_receipt = read(store,message_id)
        except (ValueError,TypeError,KeyError,sqlite3.Error) as exc:
            window.destroy()
            messagebox.showerror('本機轉錄紀錄無法核對',str(exc),parent=parent)
            return
    window.title('本機文件計算紀錄' if document_math else '本機條件核對紀錄' if evidence else '本機逐字轉錄紀錄' if transcribed else '本機精確計算紀錄' if calculated else '本機記憶操作紀錄' if local else "本次參考資料 · 送出時快照")
    window.geometry("820x640")
    window.minsize(600, 420)
    notice = ('這是本機操作完成時的紀錄，沒有呼叫模型。後續編輯不會改寫這份歷史紀錄。' if local or calculated or transcribed or evidence or document_math else
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
    if context.get('image_inputs'):
        sections.append(('本次圖片', '以下是本次實際附件的順序與本機副本指紋；不是辨識正確性的保證。\n\n' +
                         json.dumps(context['image_inputs'], ensure_ascii=False, indent=2)))
    if not local:
        sections.append(('夥伴設定', describe_profile(context.get('companion_profile'))))
        if context.get('engine_selection'):
            sections.append(('推理引擎', '這是此回答選用的引擎與參數版本；成功回覆前已核對相同選用指紋。不是目前仍在執行的程序狀態，也不代表自動訓練或回答正確性。\n\n' +
                             json.dumps(context['engine_selection'], ensure_ascii=False, indent=2)))
    corrections = context.get('earlier_user_corrections', {})
    if context.get('learning_capabilities'):
        sections.append(('學習功能', '當時程式提供的功能說明；沒有讀取或附上學習佇列內容，不是已訓練證據。\n\n' +
                         json.dumps(context['learning_capabilities'], ensure_ascii=False, indent=2)))
    sections.append(('早期更正', '依明確開頭選取的原文，不是自動確認的事實或長期記憶。\n\n' +
                     ('更早的更正未全部提供。\n\n' if corrections.get('selection_limited') else '') +
                     ('\n\n'.join(f'使用者訊息 #{r["message_id"]}' +
                                  ('（中段省略）' if r.get('truncated') else '') + '\n' + r['content']
                                  for r in corrections.get('excerpts', [])) or '本次沒有選取早期更正。')))
    checks = store.answer_checks(message_id)
    citation_text = "\n".join(check["citation"] + ("：範圍存在" if check["range_valid"] else "：無法對照") for check in checks) or "沒有可檢查的文件引用。"
    doc_notice = ('本機計算從下方指定行核對欄位與單位，依文件記載的數值運算。' if document_math else
                  '僅檢查來源編號、行號與字元範圍，未驗證結論的語意或正確性。')
    doc_text = (doc_notice + '\n' + citation_text + "\n\n" +
                range_status_text(documents) + '\n\n' +
                ("只提供部分文件片段，不能據此視為讀完整份文件。\n\n" if documents.get("coverage_limited") else "") +
                "\n\n".join(f'D{row["document_id"]} · {row["name"]} · L{row["start_line"]}-L{row["end_line"]}' +
                             (f' · C{row["start_char"]}-C{row["end_char"]}（該行共{row["line_chars"]}個Unicode碼位）'
                              if {'start_char', 'end_char', 'line_chars'} <= row.keys() else '') +
                             ("（部分行內容）" if row["partial_line"] else "") + "\n" + row["text"]
                             for row in documents.get("excerpts", [])))
    sections.append(("文件來源", doc_text))
    sections.append(('溝通提示', '這些是可能誤判的輔助提示，不能取代完整原文。\n\n' +
                     json.dumps(context.get('communication', {}), ensure_ascii=False, indent=2)))
    sections.append(('改版紀錄', json.dumps(context.get('self_improvement', []), ensure_ascii=False, indent=2)))
    sections.append(('回答流程', '這是提問前的本機流程狀態快照，不是雲端即時狀態。\n\n' +
                     json.dumps({'answer_attempts': context.get('answer_attempts', []), 'retry_of': context.get('retry_of')}, ensure_ascii=False, indent=2)))
    sections.append(('工作結果', '以下是提問時已保存的工作狀態快照，並非聊天模型新執行的操作。\n\n' +
                     (json.dumps(context.get('task_runs'), ensure_ascii=False, indent=2) if context.get('task_runs') else '本次未提供工作紀錄。')))
    if local:
        sections = [('本機操作', '當時指令：\n' + context['current_request'] + '\n\n' +
                     json.dumps(context['memory_action'], ensure_ascii=False, indent=2))]
    if calculated:
        from .chat_calculation import describe
        sections = [('計算紀錄', '當時請求：\n' + context['current_request'] + '\n\n' + describe(context['calculation']))]
    if transcribed:
        sections = [('轉錄紀錄','當時請求：\n'+context['current_request']+'\n\n'+describe_transcription(transcription_receipt))]
    if evidence:
        sections = [('條件核對', '當時請求：\n' + context['current_request'] + '\n\n' + chat_evidence.describe(evidence_receipt))]
    if document_math:
        sections = [('文件計算', '當時請求：\n' + context['current_request'] + '\n\n' + document_calculation.describe(math_receipt)),
                    ('文件來源', doc_text)]
    from .response_delivery import describe, VIEW_TAB
    if delivery_text := describe(store, message_id):
        sections.insert(0, (VIEW_TAB, delivery_text))
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
