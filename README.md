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
目前是建議模式：能分析明確選取的文字文件與提供解法，不能修改原檔、瀏覽即時網頁或代替你執行系統工作。

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

## 分析參考文件

點「參考文件」加入目前對話需要的檔案，再提問。支援 `.txt`、`.md`、`.csv`、`.tsv`、`.log`、`.json`、`.yaml`、`.yml`、`.toml` 與文字程式檔（`.py`、`.js`、`.ts`、`.html`、`.css`）。需為 UTF-8 或含 BOM 的 UTF-16；目前不解析 PDF、Word 或圖片。資料夾可先以以下搜尋功能找出文件，再選取加入。

每段對話最多 8 個文件、每檔 2 MiB、合計 8 MiB。程式只讀取你選定的檔案，將匯入副本保存在本機資料庫；原檔變更後需按「重新匯入選取文件」。移除參考只影響未來提問，原檔及過往回答快照仍保留。

每次提問依詞彙選取最多 8 個片段、合計最多 14000 字；盡量先提供每個文件的一個片段，再分配剩餘空間。長文件可能只提供部分內容，不能把沒找到的資訊當作不存在。除了前述對話與記憶，文件名稱、內容指紋、行數及選出的片段也會傳給 Codex，原始絕對路徑不會加入文件上下文。

回答可用 `[D1:L2-L4]` 標示來源。「查看本次參考 → 文件來源」顯示當時片段及行號檢查結果；行號存在不代表結論正確，長行也可能只提供其中一段。模型未提供引用時，系統不會自動補造來源。文件內容作為資料，不會授權程式執行其中的命令。

```powershell
python -m assistant --ask "比對這兩份設定的差異" --file "F:\Notes\app.txt" --file "F:\Notes\proxy.txt"
```

## 搜尋指定資料夾

「參考文件 → 搜尋資料夾」可以選定一個唯讀範圍，輸入關鍵字後查看相對路徑與原文行號。結果可複選後加入目前對話，再回到聊天區提問。搜尋本身不呼叫模型、不上傳檔案、不建立持久索引，也不授權助理自動搜尋其他位置。搜尋後原檔若變更，匯入會停止並要求重新搜尋，避免預覽與匯入內容不一致。

命令列可直接查找而不啟動推理引擎：

```powershell
python -m assistant --search-folder "F:\Notes" --query "備份保留"
```

以詞彙比對內容與檔名，沿用有限的中英文詞對應，並非完整語意搜尋。每個文件顯示一個最佳片段；搜尋上限為 500 個文字檔、32 MiB 讀取預算、5000 個目錄項目、12 層子目錄、50 個結果及 15 秒處理時間。限制到達、無法讀取與排除數量會明示；停止後可能保留部分結果。時間／停止檢查發生在檔案處理之間，不能中斷作業系統中已阻塞的檔案 I/O。

預設跳過以點開頭的隱藏項目、`node_modules`、`venv`、`env`、`__pycache__`、`dist`、`build`、`data`、`backups`，以及 `auth.json`、`credentials.json`、`config.local.json` 與常見套件鎖檔。不讀符號連結、接合點、其他重新解析點或硬連結；不解析 `.gitignore`。排除規則不是完整的秘密內容偵測，請選定適當範圍並查看預覽。

Windows 讀檔前會核對已開啟控制代碼的實際路徑，檢查實作依據 [Microsoft GetFinalPathNameByHandleW 文件](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)。不支援實際路徑查詢時拒絕讀取。搜尋沒有改變既有每段對話 8 檔／8 MiB 的匯入上限；找不到結果不代表文件或資訊不存在。

## 本機備份與還原

點「本機備份與還原 → 建立本機備份」，會在目前資料目錄的 `backups` 中建立 `.paibackup`。備份透過 SQLite 一致性快照保存已提交的資料，包含對話、記憶、回饋、目標、文件副本及回答快照；不包含程式、原始文件、Codex 登入或模型。正在生成且尚未儲存的回答不在該次快照內。

「從備份還原到新資料夾」會要求選取備份與存放位置，檢查雜湊、資料庫完整性、關聯及格式後，建立新的 `assistant-restored-日期-識別碼` 資料夾。完成後按「開啟還原後的助理」使用該資料；新視窗標題會顯示資料位置。原視窗與 `Start.cmd` 仍使用原資料，兩份資料不會自動合併。原始文件的路徑可能已失效，但匯入副本仍可分析；重新匯入需原檔仍存在。

```powershell
python -m assistant --backup
python -m assistant --inspect-backup "F:\Backups\assistant.paibackup"
python -m assistant --restore "F:\Backups\assistant.paibackup" --data-dir "F:\AssistantRecovered"
python -m assistant --data-dir "F:\AssistantRecovered"
```

命令列還原目的資料夾必須不存在；不會覆寫任何現有資料目錄。備份／檢查／還原不需登入或呼叫模型。支援目前完整資料庫格式，單一資料庫上限 512 MiB；舊版請先以相容新版開啟完成結構升級，再建立備份。未支援未來格式或舊的遷移用裸 SQLite 備份。

備份未加密，應視為私人資料；Git 忽略並拒絕發布 `.paibackup`。請自行將重要備份複製至其他磁碟，留在 F 槽的備份不能防止 F 槽故障。沒有排程備份、雲端備份或自動刪除舊備份。雜湊可發現損壞，不提供來源身分認證。驗證／還原過程會使用作業系統的本機暫存目錄。

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
python scripts/evaluate_documents.py
python scripts/evaluate_search.py
python scripts/evaluate_understanding.py
python scripts/evaluate_search.py --live --ids port_compare policy_conflict late_recovery --report data/evaluations/search-live.json
python scripts/evaluate_documents.py --live
python scripts/evaluate.py --live
python scripts/evaluate.py --live --suite memory --limit 3
python scripts/evaluate_context.py --live --strategy current --ids followup_project long_goal long_constraint long_correction
```

離線測試驗證程式行為與 30 個檢索案例。這些是針對已知詞彙設計的開發回歸集，不是獨立測試集或整體理解分數。`--live` 會使用 Codex 帳號額度，將實際回答存入 `data/evaluations`，需依案例 rubric 人工評讀，不把「有回答」算成「答對」。`--suite memory` 會實際經過記憶檢索與有效性篩選，驗證更正、到期與矛盾情境。GitHub Actions 只跑離線測試，不使用登入憑證。
目前另含 10 題上下文、20 題文件證據選取及 10 題資料夾搜尋回歸；20 題真實文件回答的評讀結果與限制見 [文件評估](docs/DOCUMENT-EVALUATION.md)。搜尋案例以排序結果選取文件後才測問答；三個真實案例的結果見開發進度，同樣需要實際閱讀答案，不能只看檢索命中。
另有 31 題需求理解評估，涵蓋限制、更正、澄清、多輪脈絡與事實可靠性。預設只驗證案例；真實回答需另外執行並逐題評讀，支援版本指紋、接續與固定案例比較，詳見 [需求理解評估流程](docs/UNDERSTANDING-EVALUATION.md)。
長期規劃見 [ROADMAP](docs/ROADMAP.md)，進度見 [PROGRESS](docs/PROGRESS.md)。

引擎整合依據：[OpenAI 官方非互動模式文件](https://learn.chatgpt.com/docs/non-interactive-mode)。
