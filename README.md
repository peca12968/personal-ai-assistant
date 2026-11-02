# 個人 AI 助理

放在 F 槽的獨立 Python 桌面專案，使用已登入的 Codex 作為推理引擎。

## 開啟

雙擊 `Start.cmd`。首次會在專案內建立 `.venv`，不需安裝第三方 Python 套件。
需要 Windows、Python 3.12（含 Tk）、Codex CLI 與有效登入及網路。程式、虛擬環境、資料都在專案內；Python 基礎安裝、Codex 執行檔與登入憑證仍沿用系統安裝，因此不是離線或完全可攜式模型。

```powershell
python -m assistant --doctor
python -m assistant --ask "幫我把學習 Python 拆成四週計畫"
```

桌面版支援對話紀錄、停止回答、長期記憶新增／編輯／停用／到期日／刪除、對話目標與限制、回答參考快照、回答評分與更正、JSON 匯出。Ctrl+Enter 傳送。
目前是建議模式：能分析與提供解法，不能實際操作其他檔案、瀏覽即時網頁或代替你執行系統工作。

## 記憶與隱私

對話、記憶、回饋存在 `data/assistant.sqlite3`，不納入 Git。
回答時會將當前問題、最近最多 16 則對話、最多 8 筆相關記憶、目前對話的手動目標與限制，以及最多 6 則早期使用者原文摘錄送給 Codex。可辨識的接續問題會參考先前使用者提問檢索記憶；一般新問題不會擴大記憶檢索。偏好可跨問題使用。這是雲端推理，並非資料完全不離開電腦。
記憶採中英文詞彙比對、全形正規化與 13 類常見中英文／繁簡詞對應，目前不具完整語意檢索或通用繁簡轉換。相關資訊依詞彙稀有度與長度排序，一般偏好最多額外加入兩筆。
管理記憶時，可直接修改舊內容以避免新舊版本並存；取消「啟用」會保留記錄但不供回答使用。到期日以本機日期判斷，當日仍有效、翌日起不選取。沒有日期則永久有效。
停用、過期或刪除後，未來不再選取該筆記憶；已生成回答、近期對話或匯出內容不會追溯刪除。若兩筆有效記憶互相矛盾，助理會被要求指出衝突，仍可能需要你手動編輯確認。
首次升級記憶結構前會在 `data/backups/` 建立 SQLite 一致性備份，保留原有記憶、對話與回饋。備份不會上傳 GitHub。
更正只有在你選擇保存時才變成長期記憶。回饋不會自動訓練模型權重。

## 長對話與參考來源

「對話目標與限制」可以保存當前目標、限制、已確認決策，每欄最多 2000 字，只套用於這一段對話；請隨需求變更手動更新。這些內容不會自動成為跨對話的長期記憶。

程式會從最近 200 則較早的使用者訊息及最初提問中，依問題關聯與目標／限制／更正用語選取最多 6 則原文摘錄。這是可追溯的摘錄，不是模型自動生成的完整摘要，也不保證保留所有約定；非常重要的要求請寫進對話目標與限制。

近期對話總長上限 24000 字，每則最多 6000 字；記憶每筆最多 1500 字；早期摘錄每則最多 1000 字。長內容保留頭尾並標記中段省略。這是字元預算，不等同精確 token 計費。

「查看本次參考」可查看該回答送出時的資料快照，包含原文訊息編號。快照不代表模型一定採用每一筆內容，也不包含 Codex 平台的內部提示。快照隨本機資料庫保存，不會上傳 GitHub；後續編輯或刪除記憶不會改寫舊快照。升級前的回答沒有快照。

對話推理程序使用 `project_doc_max_bytes=0` 避免將這個專案的開發指引混入一般回答；仍維持唯讀與停用工具。設定依據：[OpenAI 官方設定參考](https://learn.chatgpt.com/docs/config-file/config-reference)。開發工作本身仍遵守專案 AGENTS.md。

## 更新到 GitHub 私人庫

雙擊 `Publish.cmd`，或執行：

```powershell
python scripts/publish.py --message "改善問題理解與測試"
```

發布流程會先跑測試與檢索評估，確認 GitHub 儲存庫為 PRIVATE、origin 符合專案，檢查來源路徑與常見憑證特徵，再 commit 和 push。未使用強制推送。失敗會停止；網路問題可修復後重跑。掃描只是基本防護，不能保證識別所有敏感資料，請勿將私人內容放進原始碼或測試案例。
每次完成的開發更新透過此流程發布，沒有背景監看器上傳每一次按鍵或未完成檔案。

## 驗證與持續改善

```powershell
python -m unittest discover -s tests -v
python scripts/evaluate.py
python scripts/evaluate_context.py
python scripts/evaluate.py --live
python scripts/evaluate.py --live --suite memory --limit 3
python scripts/evaluate_context.py --live --strategy current --ids followup_project long_goal long_constraint long_correction
```

離線測試驗證程式行為與 30 個檢索案例。這些是針對已知詞彙設計的開發回歸集，不是獨立測試集或整體理解分數。`--live` 會使用 Codex 帳號額度，將實際回答存入 `data/evaluations`，需依案例 rubric 人工評讀，不把「有回答」算成「答對」。`--suite memory` 會實際經過記憶檢索與有效性篩選，驗證更正、到期與矛盾情境。GitHub Actions 只跑離線測試，不使用登入憑證。
長期規劃見 [ROADMAP](docs/ROADMAP.md)，進度見 [PROGRESS](docs/PROGRESS.md)。

引擎整合依據：[OpenAI 官方非互動模式文件](https://learn.chatgpt.com/docs/non-interactive-mode)。
