# 個人 AI 助理

放在 F 槽的獨立 Python 桌面專案，使用已登入的 Codex 作為推理引擎。

## 開啟

雙擊 `Start.cmd`。首次會在專案內建立 `.venv`，不需安裝第三方 Python 套件。
需要 Windows、Python 3.12（含 Tk）、Codex CLI 與有效登入及網路。程式、虛擬環境、資料都在專案內；Python 基礎安裝、Codex 執行檔與登入憑證仍沿用系統安裝，因此不是離線或完全可攜式模型。

```powershell
python -m assistant --doctor
python -m assistant --ask "幫我把學習 Python 拆成四週計畫"
```

桌面版支援對話紀錄、停止回答、長期記憶新增／編輯／停用／到期日／刪除、回答評分與更正、JSON 匯出。Ctrl+Enter 傳送。
目前是建議模式：能分析與提供解法，不能實際操作其他檔案、瀏覽即時網頁或代替你執行系統工作。

## 記憶與隱私

對話、記憶、回饋存在 `data/assistant.sqlite3`，不納入 Git。
回答時會將當前問題、最近最多 16 則對話及最多 8 筆相關記憶送給 Codex。非相關事實不會主動選取；偏好可跨問題使用。這是雲端推理，並非資料完全不離開電腦。
記憶採中英文詞彙比對、全形正規化與 13 類常見中英文／繁簡詞對應，目前不具完整語意檢索或通用繁簡轉換。相關資訊依詞彙稀有度與長度排序，一般偏好最多額外加入兩筆。
管理記憶時，可直接修改舊內容以避免新舊版本並存；取消「啟用」會保留記錄但不供回答使用。到期日以本機日期判斷，當日仍有效、翌日起不選取。沒有日期則永久有效。
停用、過期或刪除後，未來不再選取該筆記憶；已生成回答、近期對話或匯出內容不會追溯刪除。若兩筆有效記憶互相矛盾，助理會被要求指出衝突，仍可能需要你手動編輯確認。
首次升級記憶結構前會在 `data/backups/` 建立 SQLite 一致性備份，保留原有記憶、對話與回饋。備份不會上傳 GitHub。
更正只有在你選擇保存時才變成長期記憶。回饋不會自動訓練模型權重。

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
python scripts/evaluate.py --live
python scripts/evaluate.py --live --suite memory --limit 3
```

離線測試驗證程式行為與 30 個檢索案例。這些是針對已知詞彙設計的開發回歸集，不是獨立測試集或整體理解分數。`--live` 會使用 Codex 帳號額度，將實際回答存入 `data/evaluations`，需依案例 rubric 人工評讀，不把「有回答」算成「答對」。`--suite memory` 會實際經過記憶檢索與有效性篩選，驗證更正、到期與矛盾情境。GitHub Actions 只跑離線測試，不使用登入憑證。
長期規劃見 [ROADMAP](docs/ROADMAP.md)，進度見 [PROGRESS](docs/PROGRESS.md)。

引擎整合依據：[OpenAI 官方非互動模式文件](https://learn.chatgpt.com/docs/non-interactive-mode)。
