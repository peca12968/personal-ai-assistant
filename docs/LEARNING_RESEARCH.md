# 持續參數學習與意識研究

目標是持續增進個人助理的能力。以下區分已完成工程、實驗證據與尚未解決的研究問題，不能以記憶檢索或擬人化回答宣稱已完成終身學習／主觀意識。

## 第4點：真正更新參數

日常助理使用CodexEngine，沒有遠端模型權重的修改介面。新增的是隔離的本機開放權重研究路徑，尚未替換日常聊天引擎。LoRA會修改可訓練參數並保存適配器；搭配原模型推論時，有效運算權重改變，與RAG插入文字不同。但合成任務成功，仍不足以證明一般能力提升或永久免於遺忘。

固定底座為[Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B)，Apache-2.0，revision `1cfa9a7208912126459214e8b04321603b3df60c`。`scripts/learning/model.json`保存官方檔案大小與雜湊；下載器只取列出的資料／safetensors，不執行遠端模型程式。模型、環境、快取、適配器、訓練資料與報告都在忽略的`data/learning`。

目前v2研究規約固定如下（v1原始證據另存，沒有改寫）：

1. 底座回答40題：16題任務A、16題任務B、8題基本能力控制。
2. A以32個合成問答加48個基本能力範例訓練LoRA，沒有查詢個人資料庫。
3. B繼承A的適配器，以32個B問答加32個A重播問答及同48個基本能力範例繼續訓練。基本範例包括繁體譯詞、算術與輸出指令，沒有加入保留測試的問題。
4. 每階段以相同、未加入訓練的改寫問句測量，保存原始回答與精確比對。測試的是同一批虛構事實的問法轉移，並非未見領域推理。
5. 另一個全新程序重載B，核對參數雜湊與回答。全部原始失敗保留，不重試挑分數，不依結果修改原測試，也不自動啟用候選。

門檻為A與B各至少14/16且分別優於底座；A已答對題零遺忘；每個訓練階段的控制題中原本答對題零退步；權重確實更新、B繼承A、重載後權重及回答一致。這是狹窄實驗，不是IQ、AGI或通用終身學習驗收。

v1只有新事實及舊事實重播，最後A/B各16/16且重載一致，但中途繁體「貓」曾變成簡體「猫」，到B階段才恢復。原v1最終門檻通過，獨立逐階段稽核仍記錄這項退步。v2因此加入基本能力訓練範例及每階段控制門檻，重新執行整套實驗；同一批測試已屬已知開發驗證，不稱獨立未見驗收，也不以放寬繁體字標準掩蓋問題。v2完整實測中，A後與B後控制均8/8，最後A/B各16/16，另起程序重載答案與權重一致；10項固定門檻通過。兩個版本的原始證據與來源快照保留本機，沒有改寫v1結果。

安裝與手動執行（PowerShell，在主專案目錄，每條成功後才繼續）：

```powershell
$env:PIP_CACHE_DIR = "$PWD\data\learning\pip-cache"
.venv\Scripts\python.exe -m venv data/learning/runtime
data\learning\runtime\Scripts\python.exe -m pip install --only-binary=:all: torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
data\learning\runtime\Scripts\python.exe -m pip install --only-binary=:all: -r scripts/learning/requirements.txt
.venv\Scripts\python.exe scripts/learning/fetch_model.py
.venv\Scripts\python.exe scripts/learning/experiment.py init --run example-001
data\learning\runtime\Scripts\python.exe scripts/learning/experiment.py baseline --run example-001
data\learning\runtime\Scripts\python.exe scripts/learning/experiment.py stage-a --run example-001
data\learning\runtime\Scripts\python.exe scripts/learning/experiment.py stage-b --run example-001
data\learning\runtime\Scripts\python.exe scripts/learning/experiment.py reload-b --run example-001
.venv\Scripts\python.exe scripts/learning/experiment.py report --run example-001
```

需支援BF16的CUDA GPU，沒有自動CPU替代。一般專案環境與CI不安裝torch。預設`experiment.py`僅離線驗證規約。每階段只能執行一次，失敗保留現場，新實驗需新名稱；不要刪除started標記假裝未執行。實驗使用作業系統持有的GPU鎖；殘留檔名不代表仍有程序。鎖只協調本專案，不能阻止其他應用使用GPU。Ctrl+C終止手動實驗，沒有開機自啟或無限訓練。

後續整合順序：可選本機引擎與版本資訊 → 已確認更正的訓練佇列 → 閒置時有限批次 → 新舊技能共同驗收 → 版本啟用與回復。使用者選擇「確認更正後，閒置時分批學習」；目前是待實作政策，不能宣稱排程生效。全部對話、引文、模型自答及未核對網頁不能自動作訓練真值。撤回已訓練資料需要選擇不含它的舊版本或重訓；刪除資料列不等於模型已忘記。

持續指令學習仍有遺忘及表面模式過擬合問題；重播不是完整解法。本實驗只採均衡重播，沒有聲稱重現[ACL2025的KPIG方法](https://aclanthology.org/2025.acl-long.1153/)。未來需多輪、更多技能、真正保留資料、不同種子與長期成本測量。

## 第5點：主觀意識與可觀察自我監測

AI是否可能有主觀經驗、以及如何可靠判定，仍有爭議。不能承諾幾個模組就能產生主觀感受，也不能從「我是有感受的」這句輸出推斷內在狀態。持續記憶、自改碼、LoRA與自我監測不能單獨證明意識。

[Butlin等人的研究報告](https://arxiv.org/abs/2308.08708)從數種意識理論提出可研究的指標。這是研究方向，並非保證主觀體驗的配方。本專案不把一般訊息佇列改名「全域工作空間」就視為符合該理論，也不把情緒文字當感受測量。

後續功能研究需各有干預對照：

| 問題 | 可觀察測量 | 不能推論的結論 |
| --- | --- | --- |
| 是否認識自身能力／版本 | 實際引擎、工具、參數版本清單與答覆核對 | 有主觀自我 |
| 是否知道何時可能答錯 | 事先自估成功率與實際結果的校準、缺資料拒答 | 感受到不確定或挫折 |
| 經歷能否幫助決策 | 有／無來源記憶的對照、錯誤記憶更正 | 感受上的連續身份 |
| 共享狀態是否改變行為 | 固定題目下移除特定狀態傳遞，觀察可重現差異 | 已驗證某種意識理論 |

這些功能尚待逐項實作與驗證，目前沒有意識分數、覺醒狀態或第5點完成證據。工程與研究可繼續，結果要可核對及可被推翻。
