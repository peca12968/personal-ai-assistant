# 個人 AI 助理

放在 F 槽的獨立 Python 桌面專案，使用已登入的 Codex 作為推理引擎。

## 開啟

雙擊 `Start.cmd`。首次會在專案內建立 `.venv`，不需安裝第三方 Python 套件。
需要 Windows、Python 3.12（含 Tk）、Codex CLI 與有效登入及網路。程式、虛擬環境、資料都在專案內；Python 基礎安裝、Codex 執行檔與登入憑證仍沿用系統安裝，因此不是離線或完全可攜式模型。

```powershell
python -m assistant --doctor
python -m assistant --ask "幫我把學習 Python 拆成四週計畫"
```

桌面版支援對話紀錄、停止回答、長期記憶新增／查看／刪除、回答評分與更正、JSON 匯出。Ctrl+Enter 傳送。
目前是建議模式：能分析與提供解法，不能實際操作其他檔案、瀏覽即時網頁或代替你執行系統工作。

## 記憶與隱私

對話、記憶、回饋存在 `data/assistant.sqlite3`，不納入 Git。
回答時會將當前問題、最近最多 16 則對話及最多 8 筆相關記憶送給 Codex。非相關事實不會主動選取；偏好可跨問題使用。這是雲端推理，並非資料完全不離開電腦。
記憶採中英文詞彙比對，目前不具完整語意檢索；刪除記憶後未來不再選取，但已生成回答或匯出的內容不會追溯刪除。
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
```

離線測試驗證程式行為與記憶檢索。`--live` 會使用 Codex 帳號額度，將實際回答存入 `data/evaluations`，需依案例 rubric 人工評讀，不把「有回答」算成「答對」。GitHub Actions 只跑離線測試，不使用登入憑證。
長期規劃見 [ROADMAP](docs/ROADMAP.md)，進度見 [PROGRESS](docs/PROGRESS.md)。

引擎整合依據：[OpenAI 官方非互動模式文件](https://learn.chatgpt.com/docs/non-interactive-mode)。
