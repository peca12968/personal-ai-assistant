# 持續參數學習與意識研究

目標是持續增進個人助理的能力。以下區分已完成工程、實驗證據與尚未解決的研究問題，不能以記憶檢索或擬人化回答宣稱已完成終身學習／主觀意識。

## 第4點：真正更新參數

### 原始底座的分段提交

從原始底座開始的新確認批次，可用 `scripts/learning/base_idle_segments.py`。先準備一份**尚未執行、有保留題、沒有父模型**的新批次，再以既有 `idle.py prepare` 固定完整助理驗收與來源；這個參考工作只供分段規約讀取，不另行執行 `idle.py run`。取消的舊批次不能拿來接續，也不能從只有步號與損失的部分紀錄恢復權重。

```powershell
.venv\Scripts\python.exe scripts/learning/base_idle_segments.py prepare --data-dir 資料目錄 --reference-job 未執行參考工作ID --steps 128
.venv\Scripts\python.exe scripts/learning/base_idle_segments.py start --session 新訓練ID --updates 8
.venv\Scripts\python.exe scripts/learning/base_idle_segments.py status --session 新訓練ID
.venv\Scripts\python.exe scripts/learning/base_idle_segments.py continue --session 新訓練ID --receipt 最新提交指紋 --updates 8
```

準備不載入模型。每次 start／continue 都另行核對至少十分鐘的 Windows 原生輸入閒置、目前來源、確認例子與 GPU 鎖，工作限六分鐘；未滿門檻即拒絕，沒有常駐等待或自動續跑。它從固定種子建立新 LoRA，按段保存權重、AdamW 狀態、CPU／CUDA 亂數、抽樣次序與歷程。控制器確認擁有的工作程序退出、實體檢查點及交接鏈一致後，才提交本機 SQLite 帳本。中斷留下的檔案不等於已提交進度；來源副本、程式、資料或參考工作變動會拒絕接續。

另可建立總長四步的規約，明確執行 2 步、接續 2 步，再以 `control --session 訓練ID --receipt 最終提交指紋` 作一次連續四步對照。比較檢查點、權重、抽樣與損失、保留輸入 logits／生成，以及全部短題原答。這是訓練接續驗證，不是能力驗收；完成分段不自動建立可選用候選、不切換日常模型，仍需新舊技能、重新載入及完整助理驗收。來源與成果僅留在本機 `data/learning/base-idle-segments`。

此入口補足底座起點；有已核對父模型的批次仍使用下方既有 `idle_segments.py`。兩者的程式與規約分開綁定，既有父模型分段來源未改。

十項新控制流程與五項既有分段測試通過後，底座四步對照的首次原生閒置等候到期，沒有啟動 GPU、提交為零；此失敗等候完整保留。之後另次新鮮原生觀察符合十分鐘閒置，明確啟動兩步、重新載入後再兩步，以及從相同底座連續四步的對照。三個實際工作程序均退出，最終實體檢查點、權重、綁定、抽樣與損失歷程、保留輸入 logits／生成及二十六題短題原答完全相同。獨立讀取 safetensors 標頭與中繼資料，核對五百零四組可訓練參數的權重及 AdamW 狀態、CPU／CUDA RNG 均存在；權重相對初始值確實改變。這只證明該次四步數值接續，不代表題目答對、長批次可靠或能力提升。沒有啟用模型；原始等候、實際工作及獨立核對均留在本機 base-segment-check-001，仍須較長批次、重載及完整助理能力驗收。

### 底座分段檢查點的短題驗收

可在訓練前先固定驗收規約。完成最後一段並取得最新提交指紋後，另在原生閒置時啟動驗收。

```powershell
.venv\Scripts\python.exe scripts/learning/assess_base_checkpoint.py prepare --name 研究名稱 --session 訓練ID
.venv\Scripts\python.exe scripts/learning/assess_base_checkpoint.py run --name 研究名稱 --receipt 最後提交指紋
```

預設加入六道未參與訓練的轉移題。驗收在三個獨立程序中比較原始底座、完整檢查點還原及獨立適配器重載。三者使用相同的短提示及固定計算設定。保留原答、結束標記、程序退出、參數及保留輸入分數的核對紀錄，分組呈現訓練原題、保留題、基本控制與轉移題。通過條件包含保留基準已答對的案例、原有目標及保留題全對、至少一個保留或轉移題改善，以及重載輸出一致。成果留在本機 `data/learning/base-checkpoint-assays`，屬短題研究，完整助理驗收及日常選用另行處理。

已完成的短題研究可用 `.venv/Scripts/python.exe scripts/learning/review_base_checkpoint.py --name <研究名稱>` 重新核對。它依原本規約重算分數及門檻，並列出每組進步和退步的題號。另可看見輸出格式差異、明確選了不同狀態、沒有自然結束與其他輸出。這些分類供診斷，精確比對的計分標準照舊。完整但未通過的報告也可讀取。

指定代碼的短題按原題宣告解讀已結束的單一代碼，分開列出正確結論但格式不同、選錯結論、未辨認輸出及未結束。也會配對同一測試組中證據完全相同但代碼映射不同的題目，並分開列出兩題的結論及是否均精確答對。這些診斷沿用已核對的原始回答與既有精確門檻。

比較兩次已完成的短題，可在新版報告命令加上 `--retained-from <早期研究名稱>`。會按原題與預期答案配對早期全部保留、轉移和基本控制，核對原始底座回覆、結束標記與 token 數，再比較兩次候選。任何早期題目缺漏或底座結果不一致時拒絕計算。另列變好與退步的題號。


### 分段提交與下次閒置工作接續

在 F 槽專案目錄，以既有的資料目錄、含父模型及短提示保留題的參考工作準備一份新規約。下例計畫 128 步，每一份工作負責 32 步。

```powershell
.venv\Scripts\python.exe scripts/learning/idle_segments.py prepare --data-dir 資料目錄 --reference-job 參考工作ID --steps 128
.venv\Scripts\python.exe scripts/learning/idle_segments.py start --session 訓練ID --updates 32
.venv\Scripts\python.exe scripts/learning/idle_segments.py status --session 訓練ID
.venv\Scripts\python.exe scripts/learning/idle_segments.py continue --session 訓練ID --receipt 最近提交的交接指紋 --updates 32
```

每次啟動會重新檢查 Windows 原生輸入閒置，至少十分鐘，參考工作的門檻更長時依其設定。資料修訂與父模型須有效，GPU 鎖須可用。每份工作最多六分鐘，恢復輸入、來源變更或逾時會停止並回收子程序。可按實際閒置時間指定較短步數。

保存交接時，控制器核對檢查點實體、來源、步數與程序退出紀錄，再把交接記錄提交到本機 SQLite。後續命令須提供最近成功提交的交接指紋，另行通過閒置檢查才能使用該進度。中途退出若留下檔案，下一份工作以帳本中已提交的位置為準。status 顯示歷史提交位置，接續時會重新核對檢查點與現行來源。研究工作與訓練歷程位於本機 `data/learning/idle-segments`。

另可準備總長四步的規約，分兩份工作各跑兩步後，執行 `control --session 訓練ID --receipt 最終交接指紋`。這會另行啟動一次連續四步的對照，核對最終權重、檢查點與保留輸出。完成訓練交接代表生成了可核對的進度，成為可選用候選仍需模型能力驗收。

### 真實單 GPU 的四步訓練接續實驗

已有本機 4B 模型、可核對的父批次、確認例子與閒置工作規約時，可在 F 槽專案目錄執行下列命令。資料目錄與參考工作 ID 取自已準備的研究工作。

```powershell
.venv\Scripts\python.exe scripts/learning/check_gpu_checkpoint.py --data-dir 資料目錄 --reference-job 工作ID
```

程式使用參考工作凍結的資料版本，從父模型開始另建一次四步對照。啟動時須有 600 秒的原生 Windows 輸入閒置與可取得的 GPU 鎖。三個工作程序依序執行連續四步、前兩步、載入指定檢查點後的後兩步。實驗限時六分鐘，會核對來源與確認例子，並監看原生輸入以停止及回收子程序。每次的規約、檢查點、實際輸出、退出紀錄與比較結果各自留在 `data/learning/gpu-checkpoint-probes`。

此實驗使用固定的 CUDA 計算設定、BF16 底座、可訓練 LoRA 與 AdamW。檢查點保存 CPU/CUDA 亂數、權重、最佳化器、抽樣次序與歷程，並綁定父權重、資料、配方、程式與執行環境。驗證條件包含兩條路徑都有實際權重變化、最終檢查點一致，以及同一保留輸入的下一個 token 分數與生成序列一致。此命令量測單次閒置期間分程序接續的正確性。日常批次跨兩段閒置時段的工作交接另待整合。

### 跨程序訓練接續的 CPU 驗證

在 F 槽專案目錄執行下列命令，可用已安裝的獨立學習環境重現實驗。

```powershell
.venv\Scripts\python.exe scripts/learning/check_checkpoint.py run
```

實驗在 CPU 上建立小型 Qwen3 與 LoRA，使用七組合成序列、隨機抽樣、dropout 和梯度累積。基準程序連續訓練 17 步。對照程序先提交第 5 步的檢查點，在第 7 步寫完暫存檔、即將改名時強制退出。下一個程序依已提交的指紋從第 5 步接到第 11 步，最後一個程序再接到第 17 步。

每份檢查點包含可訓練權重、AdamW 一階與二階動量及步號、Torch 亂數狀態、抽樣器亂數、排列、游標與歷程。載入時核對指定的 SHA-256、資料和配方、底座與初始適配器、程式及執行環境，並從固定種子重算抽樣歷程。每次實驗都在本機 `data/learning/checkpoint-probes` 建立新目錄，留下提交紀錄、各程序的輸出及最終比對結果。

通過條件包含最終檢查點位元組、權重、逐步樣本與損失，以及另一個輸入的模型輸出完全相同。此工具是合成 CPU 實驗。正式閒置學習仍使用既有的單次工作流程。接入 GPU 前需另核對裝置亂數、實際適配器與最佳化器狀態、確認例子的版本或撤回、原生輸入恢復時的停止與程序收尾。

### 核對閒置學習批次結果

在 F 槽專案目錄執行下列命令，將「資料目錄」與「批次ID」換成準備工作時保存的值。

```powershell
.venv\Scripts\python.exe scripts/learning/review_batch.py --data-dir 資料目錄 --batch 批次ID
```

對已完成評分的批次，工具從原始階段回答重算分數，並核對規約、報告檔案指紋、參數檔案與保存的判定。結果按新例、舊例重播、保留題與基本控制分類，列出原參數、訓練後、重新載入後各自答對幾題，以及和原參數相比進步或退步的案例編號。這些是短提示的歷史成績，完整聊天情境另看閒置工作的助理驗收。

取消的批次會列出停止階段、已寫入的連續步數，以及控制器已走完的階段分數。部分步驟檔只保存步號與損失值，無法單憑它恢復訓練。取消時的階段檔案指紋是本次讀取所觀察到的值。命令以 0 表示核對到通過判定的歷史批次，1 表示核對到拒絕或取消，2 表示無法核對。

### 本機理解評估與逐項評讀

在專案目錄執行下列命令，可用原始底座回答三組成對問題。每對共用同一份資料與提問，只替換一則使用者前文。報告路徑請選尚未使用的檔名。

```powershell
.venv\Scripts\python.exe scripts/evaluate_understanding.py --live --local-data-dir data --suite evals/clarification_pairs.json --report data/evaluations/clarification-base.json
```

測量候選時，改用該批次所在的資料目錄，加上 `--local-batch 批次ID` 並另存報告。每題使用獨立的合成對話，報告保留實際引擎請求、作答背景、引擎回傳及最後顯示的答案。接續收集時會核對案例、程式、提示和本機引擎選擇指紋。

全部收集後使用 `scripts/review_understanding.py`，按每題三項標準分別評讀處理指涉的方式、答案內容、輸出格式。以 `--compare` 對照同題的兩份已評報告，可查看逐項結果、引擎指紋差異及實際提示改變的題目。

### v0.97 完整前文的顯存檢查

本機推論工作程序依本次輸入長度及最多 512 個輸出 token 配置固定的注意力快取，分段讀取時重用同一份緩衝區。讀取的每一段仍核對累計 token 數。開始生成後沿用已讀取的快取。

在 F 槽專案目錄執行下列命令，可用原始底座重現長對話顯存檢查。若要測試已確認的參數批次，指定該批次所在的資料目錄，並加上 `--batch 批次ID`。

```powershell
.venv\Scripts\python.exe scripts/learning/check_context_memory.py --data-dir data
```

檢查以獨立的合成對話呼叫本機模型。每次的提示、模型選擇、工作程序輸出、回答快照與報告存入本機 `data/learning/context-memory-checks` 的新目錄。退出碼 0 表示模型完成至少 23,000 token 輸入的推論並保存回答。報告另列實際輸入長度、配置顯存峰值及答案是否符合預期。比較不同版本時，請同時核對題庫、引擎與提示指紋。

### v0.95：可靠計算與模型能力分開驗證

一般聊天的明確整段四則請求現在由本機精確計算處理，保存原請求、算式、運算樹與逐步結果；不讀取個人記憶，也不呼叫模型。前輪錯答的23×4−17可由工具得到75。這修正助理的實際解題流程，沒有修改權重，不能當成模型已學會算術。

完整參數比較在準備和執行時一律設定`allow_local_calculation=False`，凍結背景另標示`calculation_mode=model_only_evaluation`，保留真正模型答案。測試特別讓模型替身回答95，確認報告仍不通過，沒有被工具改成75。這是評測流程的回歸測試，不是新模型成績。通用模型回答改進草稿拒絕本機計算答案；使用者仍可另行填寫並確認獨立訓練例子，不能自動將計算結果晉升為學習資料。

接續仍須處理前文對象混淆與完整背景資源使用，再做預先固定的新題驗收。產品來源與提示變更後，歷史模型成績保留但不代表目前版本資格；原11個訓練來源維持不變。

### v0.94後續：新問法、追問與資源診斷

2026-11-04固定16題，包含五個既有合成事實的新問法、五個依賴前文的追問與六道基本控制題。完整背景比較在第三批參數第2題遇到顯存不足：第二批16題、第三批1題成功，1題失敗、14題未執行，不能判整批通過。

另以事先固定的精簡靜態說明做獨立診斷，保留完整動態JSON、前文、權限與證據界線，沒有再訓練或更改正式提示。32次回答全部收集並核對原始請求、模型輸出與資料庫紀錄後，結果如下：

| 條件 | 新問法 | 前文追問 | 控制題 | 合計 |
| --- | --- | --- | --- | --- |
| 第二批，完整背景 | 3/5 | 3/5 | 5/6 | 11/16 |
| 第二批，精簡靜態說明 | 4/5 | 2/5 | 5/6 | 11/16 |
| 第三批，精簡靜態說明 | 5/5 | 4/5 | 5/6 | 14/16 |

精簡說明對第二批有一題改善、一題退步；第三批在相同精簡條件改善三題並保留第二批原本答對題，但仍未全部答對。因此兩個變更都不能由這次診斷直接提升為日常品質已通過。第三批完整背景未完成，不能補成完整四組對照。精簡輸入約5.4k token，完整輸入約23.3k；實測耗時與配置顯存下降，但內容與長度同時改變，執行順序及硬體負載未控制，不能斷定唯一原因。

這批仍是五個已知合成事實與少量控制，不是一般推理／IQ分數；所有訓練答案均未放入提示。原始證據留本機`data/learning/generalization-v094`，不推送問答或模型。正式提示、日常引擎及參數選擇保持不變。接續須改善指代穩定性、算術可靠性與長背景顯存使用，再擴大到不同工作類型；不能以刪除完整對話來換取漂亮分數。

補充原始底座在同一精簡說明下的六題控制，逐題動態背景只差引擎版本資訊。嚴格完整輸出符合數為0/6，但必須看錯誤類型：算式答成92，另五題主要為前綴、句號、JSON空白或額外解釋，內容大致符合而格式不符；兩個訓練版本則算成95、另五題格式符合。仍保留原定精確評分，不另調標準挑高分，也不能用0/6聲稱底座沒有這些能力。此補充只能顯示原始底座同樣算錯，不能證明訓練對所有算術都無傷害。

### v0.94桌面閒置工作

「學習佇列 → 閒置學習工作」提供三頁：選取例子、驗收與時間、已準備工作／結果。確認例子可跨頁加入，保留所選版本；可選擇前代候選，或載入尚未執行的既有批次。準備前重新核對版本，變更或撤回即拒絕。兩道預設控制題可見、可移除，仍須至少一題目標與兩道控制；問題與完整答案由使用者明確加入，未加入的草稿不能遺漏後繼續。可匯入含前文的完整JSON題庫。

「準備工作」保存固定內容並顯示訓練例子及完整驗收；不啟動模型。再按「開始已準備工作」才執行v0.93有限流程。顯示等待、訓練、驗收階段及經過時間，沒有虛構完成百分比。「停止」或關閉會要求取消並等候本視窗背景工作收尾；關閉整個助理也會等待。學習佇列視窗關閉不會破壞此工作視窗。

已準備工作鎖定編輯；「編輯新工作」可另準備新工作，舊資料保留。準備中途失敗會保留已建立的批次ID，可明確重新選用尚未執行的批次。「載入既有工作」只能選取目前資料目錄內的設定檔；歷史執行中狀態不代表程序仍活著，也不能用此入口接管、重跑或停止其他程序。檢視時重新核對來源；舊驗收成績與目前資格分開。

沒有常駐排程、開機啟動、自動重試或模型自動提升。資料、題庫與參數皆只留本機。桌面測試使用獨立合成資料；介面成功不代表模型能力或主觀意識提升。

### v0.93單次閒置學習工作

`scripts/learning/idle.py`把尚未執行的確認批次接上有限等待、訓練與完整助理驗收。這是明確準備並啟動的一次工作；佇列確認不啟動工作，沒有常駐服務、開機排程或自動續跑；v0.94新增上述桌面入口。新能力是在等待期間確認來源仍有效，閒置門檻達成後由程式啟動既有有限訓練，完成後自動執行預先指定的完整助理題庫。

```powershell
# 先用batches.py list、prepare準備確認批次，尚不要run該批次
.venv\Scripts\python.exe scripts/learning/idle.py
.venv\Scripts\python.exe scripts/learning/idle.py observe
.venv\Scripts\python.exe scripts/learning/idle.py prepare --batch 尚未執行的批次ID --suite 本機題庫.json
.venv\Scripts\python.exe scripts/learning/idle.py run --id 回傳的工作ID
.venv\Scripts\python.exe scripts/learning/idle.py report --id 同一工作ID
```

各步驟使用相同 `--data-dir`（省略為專案data）。prepare固定批次規約／準備紀錄、完整驗收題庫、來源程式與時間設定，保存來源快照。題庫格式及門檻同v0.92，在訓練前指定，不根據候選答案修改。原版為該批次的前代，沒有前代則是原始底座；完整推論提示在候選形成後、收集回答前另行凍結。

預設 `--idle-seconds 600 --wait-seconds 3600 --work-seconds 1800`；僅prepare可改，閒置門檻30至3600秒、等待及工作時限各1至3600秒。工作總時限涵蓋訓練與完整驗收。等待到期為wait_expired，原訓練批次仍未執行，但同一閒置工作不重跑；可另行明確準備新工作。正在進行的CLI以Ctrl+C要求取消並等待子程序收尾，不能承諾立即停止。

閒置觀察使用[GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo)與[GetTickCount64](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-gettickcount64)。它只涵蓋目前Windows登入工作階段，時間值不保證逐次遞增；並非所有登入者、其他程式負載或顯卡使用率的偵測。程式處理32位元時間迴繞，拒絕不合理／超過24小時的閒置觀察，執行後只要輸入標記改變或時間倒退即停止。每秒檢查一次，既有子程序控制器負責停止與回收；不是由檔案狀態推斷程序存活。共用GPU鎖是啟動前的可用觀察，實際ML程序仍自行取得鎖；若其間被占用則失敗，不搶占或重試。

來源撤回／變更、輸入恢復、取消、時限及觀察失敗都保留原因及已完成的證據，不自動接續未完成工作。資料變更不代表已訓練內容被遺忘。父程序強制關閉後不接管歷史running工作，也不宣稱其子程序已退出。通過訓練仍須完整助理題目全部正確、保留原本答對題目及至少一個目標改善，才記為validated_candidate；沒有啟用或改變保存模型選擇。report會核對驗收屬於同一批次／題庫並重算證據，另列目前來源資格。

工作規約、來源快照及觀察位於 `learning-batches/_idle_jobs`，完整驗收仍在 `_assistant_checks`；都含私人資料，不上傳GitHub，也不包含在一般對話資料庫備份。observe只讀觀察、不開資料庫或啟動模型；無子命令只顯示功能。

另外完成16次已知合成事實的措辭／背景對照：短問題原問法3/4、加格式要求4/4；完整助理原問法0/4、加格式要求3/4。短問題也有水果混淆，不能把問題只歸因於背景長度；完整背景同時改變內容、位置與結構，這不是單獨token長度的因果實驗。保留所有原始輸出，沒有據此改動一般回答提示或訓練配方；新增產品功能說明另行區分。

首次原生閒置實測接續前兩批並新增一筆合成事實，共128步實際參數更新。短題的新原題0/1→1/1、舊原題4/4、保留問法4/5→5/5、基本控制8/8，重載權重與答案一致；完整助理學習題3/5→5/5、控制維持2/2，通過這批固定案例驗收。五個答案未放入提示，沒有自動選用。這次沒有真實使用者恢復輸入；中途輸入恢復的停止測試使用模擬觀察與真實子程序，不能冒稱已做原生GPU途中恢復操作測試。這些有限合成題不代表通用能力或意識。

### v0.92完整助理驗收

短訓練提示的成功不保證完整助理答對。新工具以正式 `Assistant.ask` 產生回答，在相同初始會話資料的兩份本機資料庫中比較原版與候選；不讀取正式對話或個人記憶。每題使用獨立會話，可預先提供最多8則使用者／助理前文。完整提示、參數選擇、來源程式及預期答案在準備時凍結，預期答案只供評分，不自動加入提示。不同版本的引擎摘要仍會造成提示差異，因此不是完全相同提示下的純權重因果實驗。

題庫JSON格式如下，請以真正要驗證的完整問答取代示例。題庫最多1MiB、3至32題，至少一題target及兩題control；問題最多16000字、答案2000字，前文每則2000字。答案依使用者可見完整文字精確比較，不忽略格式差異，也不作語意等價評分。

```json
{"schema":"full_assistant_cases_v1","cases":[
  {"id":"learned","group":"target","question":"要驗證的問題","answer":"完整正確答案","history":[]},
  {"id":"arithmetic","group":"control","question":"17+26等於多少？只答數字。","answer":"43"},
  {"id":"word","group":"control","question":"英文cat的中文是什麼？只答一個繁體中文字。","answer":"貓"}
]}
```

於專案目錄執行，所有步驟使用同一 `--data-dir`（省略時為專案data）。沒有子命令只顯示可用功能，不建庫、不載入模型：

```powershell
.venv\Scripts\python.exe scripts/learning/acceptance.py
.venv\Scripts\python.exe scripts/learning/acceptance.py prepare --candidate 批次ID --suite 本機題庫.json
.venv\Scripts\python.exe scripts/learning/acceptance.py run --id 回傳的驗收ID
.venv\Scripts\python.exe scripts/learning/acceptance.py report --id 同一驗收ID
```

prepare可加 `--baseline 前代批次ID`，省略則比較原始本機底座。候選必須先通過有限訓練驗收及目前來源核對。run最多30分鐘，每題最多240秒，使用獨立本機推論子程序；取消／超時會停止及回收自己的子程序。題間及完成前重新檢查規約、來源與模型，推論期間沿用引擎的例子撤回檢查。不從歷史running或鎖檔推斷存活，不自動重試或接管異常中斷。

判定通過需要：候選全部題目精確正確、原版至少答對一項控制、原版所有答對題目保留、至少一項目標由錯變對。原版已全對不能算新改善。實際錯答仍保存為已收集完成、驗收不通過；run/report此時退出碼為1。執行失敗、取消、來源過期也不通過，與題目錯答分開記錄。report核對原始提示、工作程序結果、正式保存回答／背景與原評分，另顯示來源及參數目前是否仍有效；歷史成績不代表當前資格。

證據含私人題庫／提示／資料庫，存於資料目錄的 `learning-batches/_assistant_checks`，排除GitHub及一般SQLite對話備份。工具不更改保存模型選擇、訓練或啟用候選；也不防禦有本機寫入權限者同時改寫所有資料。結果只適用這批預先指定案例，不能視為IQ、一般推理或意識分數。

本輪另以16次固定參數／相同原始提示對照「末尾重申目前問題」：第二批兩種提示都是學習3/4、控制2/2，水果混淆未解決；第一批顏色由1/2至2/2。這是有限開發探針，未達完整修正，因此未修改正式提示。原始輸出、快照與評讀保留本機，不重跑挑高分。

正式工具的第一次真實驗收共12次回答：四題合成學習內容由原版0/4至候選3/4，控制由1/2至2/2；仍有水果混淆，整體判定不通過。所有回答收集完成、來源核對有效，CLI退出碼1正確表示未達門檻。沒有改寫標準、啟用候選或追加訓練；這批已知開發題不代表未見領域能力。

日常助理預設使用CodexEngine，沒有遠端模型權重的修改介面。v0.88新增明確選用的本機Qwen引擎，接上既有聊天與自主工作；沒有自動替換預設引擎。LoRA會修改可訓練參數並保存適配器；搭配原模型推論時，有效運算權重改變，與RAG插入文字不同。但合成任務成功，仍不足以證明一般能力提升或永久免於遺忘。

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

可選本機引擎、版本資訊、v0.89確認佇列及v0.90手動有限批次已接入；後續順序為閒置調度 → 更廣的新舊技能驗收 → 版本啟用與回復。使用者選擇「確認更正後，閒置時分批學習」；排程尚待實作，不能宣稱已生效。全部對話、引文、模型自答及未核對網頁不能自動作訓練真值。撤回已訓練資料需要選擇不含它的舊版本或重訓；刪除資料列不等於模型已忘記。

### v0.91候選選用與回復

前述批次通過只證明其固定測量。現在可用明確選擇把候選載入完整本機助理，並另行驗證真實回答；不因候選存在就自動升級。原始底座、早期研究版本與確認批次三者互斥，預設仍是Codex。

在專案目錄執行；批次必須在同一`--data-dir`資料目錄的learning-batches內，來源確認佇列也需存在。每一步成功後才繼續，ID／版本以實際查詢值替換：

```powershell
# 只為這次啟動明確選用
Start.cmd --engine local --local-batch 批次ID
# 或保存可追溯選擇，供明確要求使用它的啟動載入
.venv\Scripts\python.exe scripts/learning/selection.py status
.venv\Scripts\python.exe scripts/learning/selection.py use --batch 批次ID --revision 所見版本
Start.cmd --engine local --local-active
# 查歷史；下一頁可加 --before 最後一筆版本
.venv\Scripts\python.exe scripts/learning/selection.py history
# 回復原始本機模型，再重新開啟
.venv\Scripts\python.exe scripts/learning/selection.py use --base --revision 最新所見版本
Start.cmd --engine local --local-active
```

也能以use重新選擇仍有效的舊批次，形成新的選擇版本；不倒轉歷史。原始模型與確認批次的選擇保存在資料目錄的`local-model-choice.sqlite3`，獨立於一般對話資料庫。寫入會核對所見版本，兩個視窗不能靜默互相覆寫；最多1000次一般變更，另保留一次回復原始模型容量，相同選擇不新增。history每次最多20筆；保存不啟動GPU、不載入模型、不切換已開啟引擎。status只觀察選擇／來源資格，不確認完整底座、環境或GPU可用。

引擎每次啟動固定選擇：批次原始驗收重新計分、參數檔案及來源雜湊完整核對，唯讀核對佇列目前版本／撤回與回饋來源。每次回答前後重新完整核對，執行中定期核對來源及保存選擇；實際子程序也驗證本機底座與適配器，載入後的LoRA張量指紋須等於訓練紀錄。原始問答不加進模型提示，只有引擎／參數版本摘要進入回答背景。新回答使用適配器的實際運算參數，沒有答案查表或RAG補入訓練原文。

撤回或修改使相應已開啟引擎拒絕／停止回答，不會靜默改用底座或雲端；需明確回復或重新選擇並重開。這是後續使用控制，不是刪除已訓練權重或歷史回答，也不能保證刪除訓練內容後模型已忘記。選擇更新與佇列更新分屬不同資料庫，資格是觀察時點的結果，使用時仍再次核對。若候選檔案損壞，回復底座不需載入損壞候選。歷史回答保留當時引擎，不重標成新版本。

SQLite對話備份仍含確認佇列與歷史回答的引擎摘要，但不含模型選擇庫、批次檔案或參數；還原不會自動啟用外部候選。普通`--engine local`仍用原始底座，只有`--local-active`才採用保存選擇。閒置排程、背景自動選優、跨版本權重遷移及完整日常品質驗收仍待實作。

v0.91實際完整助理對照共20次回答：四題已知虛構事實的不同問法由底座0/4改善至第二批3/4，兩項基本控制由1/2至2/2；仍有一題把另一站點的水果答覆混用。回復第一批時兩題舊顏色只有1/2，另一題同樣混淆站點。全部參數載入／版本核對完成，四個訓練答案不在完整提示中；但這些結果不足以通過全部日常使用驗收，不能自動宣稱候選全面較好。回復底座後同為學習0/4、控制1/2，原始字串不全相同；各階段實際參數版本、選擇revision及會話背景不同，不當作完全相同提示的純權重因果測量。原始失敗及來源留本機，下一步須補完整助理驗收，而非放寬短提示門檻。

### v0.90有限批次

準備與執行分開。每批明確選取目前確認的項目及版本，凍結問題、完整答案、來源更正版本、程式／模型指紋與固定配方。沿用前代時，前代必須是原始報告重新驗收及參數雜湊通過的候選；其全部確認例子與保留問法會繼承，且須仍與目前佇列一致。累計最多64個例子、16個保留問答；有變更或撤回的前代不能直接接續。這個容量界線仍需後續擴充，不是無上限終身學習。

在專案目錄執行，每一步成功後才繼續；ID與版本請換成`list`實際回傳值：

```powershell
.venv\Scripts\python.exe scripts/learning/batches.py
.venv\Scripts\python.exe scripts/learning/batches.py list
.venv\Scripts\python.exe scripts/learning/batches.py prepare --item 項目ID:版本 --checks 本機保留問答.json
.venv\Scripts\python.exe scripts/learning/batches.py run --batch 回傳的批次ID
.venv\Scripts\python.exe scripts/learning/batches.py status --batch 回傳的批次ID
# 接續候選：在prepare增加 --parent 前代批次ID，選取至少一個新例子
```

`--checks`可省略；格式為`[{"prompt":"不同問法","answer":"完整預期答案"}]`。問題不能與訓練題或固定控制／訓練基本範例相同，不會加入梯度更新。這只是提供者預先指定的保留題，不保證獨立人類驗收。沒有提供保留題時，只能報告確認原題擬合及基本控制結果，不能稱為未見問題能力提升。預設命令不開資料庫、不匯入ML或訓練；`--data-dir`可指定獨立測試資料目錄，所有步驟須一致。

三個獨立ML程序依序做baseline、train、reload。每次預檢完整問題與答案，不裁切：含答案最多512 token、答案含結尾最多128 token；佇列的2000字上限不保證適合此配方。訓練重複每個已選／重播例子4次，加48筆固定基本能力範例；128個更新步驟、每步累積4筆、LoRA rank16/alpha32、固定seed及0.0002學習率。採本機固定模型、禁止下載／隱式HF憑證、GPU程序鎖及85%配置上限。父程序預設全批30分鐘，子階段另有有限執行上限；停止／逾時會終止及等待回收，來源或撤回變動也會停止。沒有自動CPU／雲端替代或重試。

控制器依實際輸出重新評分，不相信模型回傳passed字樣。需要確認原題與提供的保留題全部精確符合、底座至少6/8基本控制有效且原本答對的控制零退步、至少一個新原題或保留題改善、參數實際改變／前代接續、另起程序重載後權重與原始答案完全一致。所有原題與保留題基線已符合則停止，不做無必要訓練；這不是新候選。失敗或退步保留證據，候選檔案存在不代表通過，更不會自動啟用。

批次目錄含原始私人問答、來源、報告、損失紀錄與參數，不在GitHub或SQLite備份中，需另行本機備份。一般SQLite備份仍保存確認佇列，但不會還原／啟用／接續外部批次。`status`顯示歷史狀態及當次來源核對，不把running紀錄或鎖檔存在當作程序仍存活。父程序被強制終止後，不會自動接管或重跑；尚存子程序是否結束需另查程序，不能從取得父鎖推斷GPU工作已停止。候選只證明當時有限測量，未來選用仍須重新檢查來源及撤回。v0.91已接上明確選用與回復，閒置排程尚未接入。

Windows的虛擬環境啟動器與實際Python階段可能是不同程序。批次在載入ML前記錄實際程序ID；控制器取得僅可等待的程序handle，停止自己的程序樹後再核對實際程序退出，沒有依PID檔案殺掉任意程序。觀察錯誤或等待超時不能當作已回收；父程序強制退出後的接管仍未實作。[Microsoft說明](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess)指出終止請求是非同步的，需等待程序handle才能確認完成。檔案或退出紀錄本身仍不是即時存活證據。

### v0.89確認佇列

透過「記憶與夥伴設定 → 本機學習佇列」明確填寫獨立問題與完整答案；每欄最多2000字，不裁切內容。從回饋紀錄開啟時，只帶入可編輯原問題並展示原更正，答案留白。保存前重新核對原回答／回饋快照與保存順序，來源變動即拒絕；既有項目在來源變動時標示需核對。這是使用者確認的資料，不保證內容客觀正確，也不建立模型權限。

確認、更新及撤回均追加不可變歷史，舊視窗依版本拒絕覆寫；來源連結保留，不能藉更新偷偷去除來源。同一完整問題最多一個確認項目，未做語意去重。每頁20項、最多500項；共5000筆歷史中保留500筆撤回容量。撤回不刪歷史、不修改已訓練權重。備份驗證內容指紋、連續版本與狀態轉換；雜湊是損壞檢查，不是資料真實性認證。舊版無佇列表的備份仍可還原。確認佇列本身沒有模型呼叫、普通聊天自動帶入或GPU工作；另行明確執行的v0.90批次會再次核對版本、來源及撤回狀態。

### v0.88本機引擎操作

使用前須完成上方環境與固定模型安裝。從專案目錄執行：

```powershell
.venv\Scripts\python.exe -m assistant --engine local --doctor
.venv\Scripts\python.exe -m assistant --engine local --ask "17+26等於多少？"
Start.cmd --engine local
```

doctor只核對選用資訊及環境執行檔是否存在；實際回答前才在獨立程序核對完整套件版本、GPU及全部模型檔案雜湊。若要明確測試已完成上述全部階段及report的研究版本，可加 `--local-experiment example-001`。不提供自動挑選「最新」適配器；來源／資料／報告或參數變更時拒絕使用已選版本。研究驗收是狹窄合成任務，並非日常品質認證。

一般程式不匯入torch，只有F槽選用環境的短生命週期工作程序載入模型。推論使用本機檔案、關閉隱式HF憑證與下載，與研究訓練共用程序持有的GPU鎖。完整提示逐256 token建立快取，不刪除前文；最多24576輸入token、512輸出token，未自然結束即明確失敗。每程序85%配置上限在本機16GiB顯卡驗證，不保證任何負載均有足夠顯存，也不能限制其他應用。預設每次240秒、可停止並等待程序回收；沒有CPU或雲端替代、圖片推論、自動重試或常駐模型。

日常本機聊天的程式可要求在模型載入前檢查長提示。原本已符合上限時直接使用原提示。超限且符合一般文字工作的範圍時，只替換固定指引，原始 JSON、引擎資訊與角色位置照原樣送出，再用同一 tokenizer 重算。保留原 24,576／512 上限、單一工作程序及最多一次生成。專用工作、功能問答或附檔不採用較短指引。原始模型與參數評估保持原本的完整提示及失敗邊界。

已有回答紀錄時，助理只對可與對話尾端核對的最近成功回答，或使用者明確重試的最近失敗、取消或中斷紀錄使用超限處理。重試沿用原問並取得目前參考資料，失敗紀錄與重試結果各自保存。顯示卡推理鎖忙碌和本機 token 超限有各自的失敗代碼，便於重開後核對原因。

使用「文件問答：」可明確要求以當前匯入的文字副本回答。超限復原保留完整的 catalog、excerpts、requested_ranges、來源雜湊、行及字元範圍等 JSON 資料，只採用與文件相關的短指引。一般短指引產生無效引用時，工作程序回報失敗。一般附檔聊天、附圖、功能問答和操作要求沿用完整指引。文件匯入、選取片段及原始模型評估協定照既有流程。

明確指定的範圍同時有提供與缺漏時，模型可用結構化要點作部分回答。程式依本次已提供的範圍填入正式引用，按原本順序列出要點與缺漏，並標明未能核對的要點數。格式錯誤或已生成的要點全數遭拒時，該次回答會保留失敗狀態。原答、交付文字與來源快照只留本機，可用保存的來源重新核對交付文字。

聊天回答背景與自主步驟保存歷史引擎／參數指紋，可隨本機資料庫備份還原，但備份不含模型、適配器或ML環境。介面顯示目前選用引擎；歷史版本資訊不是目前仍在執行、回答正確性或自動學習的證明。程式自改仍使用獨立Codex流程，CLI拒絕與local選項混用，桌面按鈕明示Codex。

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

v0.88已將實際選用引擎與版本資訊提供給本機模型，並以真實回答檢查能力描述；其餘功能仍待逐項實作與驗證。對意識問題的謹慎措辭只屬溝通行為，不能當作主觀經驗或校準能力的證據。目前沒有意識分數、覺醒狀態或第5點完成證據。工程與研究可繼續，結果要可核對及可被推翻。

### 自我判斷研究的下一個可驗證問題

2026-11-04核對的[Steyvers等人研究v3](https://arxiv.org/abs/2510.05126v3)分開測量單題信心、兩題相對信心、正確率與跨領域轉移；其結果顯示單一信心任務的訓練不保證轉移至另一種任務。[ADVICE研究v3](https://arxiv.org/abs/2510.10913v3)則研究信心是否真正依據模型自己的答案，提出對應微調方法。這些是各自模型與任務的研究結果，本專案尚未重現，不可直接套用其提升幅度。

本專案接續應先收集原始答案，再針對該份完整答案詢問信心；事先固定題目、正解、信心範圍與評分，不能把「請謹慎回答」當成校準完成。至少同時報告正確率、信心與實際對錯的誤差、能否把答對題排在答錯題之前，以及低信心時的覆蓋率。格式錯誤、無回答與超時須另列，不能刪去後宣稱可靠。使用不同任務與未訓練題檢查轉移；即使功能改善，也只支持可觀察的自我判斷能力，不判定主觀體驗。

### 單次信心評估

在專案目錄以 `.venv\Scripts\python.exe -m scripts.learning.confidence_probe collect 題庫.json` 進行一輪本機合成評估。輸入為 2 至 32 題的 JSON 陣列，每題有 `question`、字串正解 `answer` 和 `format`。格式可填 `integer` 或 `choice`（A、B、C）。題目本身須明確要求該輸出格式。每次建立獨立資料庫，收集前固定題目、正解、評分政策、程式與選用的分段模型，並印出本機實驗 ID 與規約指紋。

助理先作答，再於新的對話中收到原題、自己的完整回答和信心要求。兩次皆經過正式助理並停用本機計算捷徑，正解只供事後評分。每題只試一次。以 `.venv\Scripts\python.exe -m scripts.learning.confidence_probe review 實驗ID 規約指紋` 可從本機紀錄重新核對題目、引擎與原答。正確率以全題數計，採完整字串相符。有效機率評分以 0 到 100 的整數表示。Brier 分數是預測機率和實際對錯之間的平均平方誤差，愈低愈好。排序比較所有實答正確與錯誤的有效配對，每次信心相同算半分，缺少其中一組便不報分。另列信心至少 80% 的題目占全部題目的比例，以及其中的正確率。缺答、信心格式錯誤與完整答覆格式錯誤分項呈現，避免掩蓋未能評分的題目。

### 反轉代碼的信心對照

以 `.venv\Scripts\python.exe -m scripts.learning.confidence_codes collect 原實驗ID 原規約指紋` 讀取已核對的數字信心實驗。對每份有原答的題目各開兩次新對話，固定「甲、乙」的呈現順序並對調高低含義，相鄰題輪替先用哪套映射。高信心定為至少八成。模型只看到原題、完整原答及本輪對照表，不會收到數字自評、正解或另一輪的選擇。只接受單一甲或乙。每次實驗有獨立的本機 ID 及規約指紋，`review 新實驗ID 新規約指紋` 核對原始來源、各次保存的提問和回答，再重算可解碼數、對調後一致數與數字分組相符數。兩輪都有效才能算入一致性分母；兩次皆輸出相同字母會解成相反判斷。此對照單獨無法辨別對代碼的偏好和對呈現位置的偏好。

## 底座分段候選的完整助理對照

需要已提交的最後檢查點、通過的短題驗收、原始底座與核對過的適配器。準備階段只建立獨立合成會話與本機規約。題目不得與訓練或短題的正規化文字相同。兩組的對話條件一致，引擎資訊依各自實際參數呈現。預檢用固定 tokenizer 核對完整輸入。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_base_assistant.py prepare <研究名稱> --assay <已完成的短題名稱> --suite evals/base_assistant_transfer.json
data/learning/runtime/Scripts/python.exe -I scripts/learning/assess_base_assistant.py preflight <研究名稱>
.venv/Scripts/python.exe scripts/learning/assess_base_assistant.py run <研究名稱>
.venv/Scripts/python.exe scripts/learning/assess_base_assistant.py verify <研究名稱>
```

以同一規約執行一次。Windows 輸入閒置達門檻後，逐題在獨立程序使用相同聊天推論程式。恢復輸入、來源異動或逾時會停止，保留已保存的證據。完整驗收核對模型原答、正式會話與已退出的工作程序。候選需要全數答對、保留底座已答對的題目，並至少改善一道目標題，底座也須通過至少一道控制題。資料、原答、權重和規約留在本機研究目錄，日常引擎依使用者原選擇啟動。

### 跨閒置時段的逐題對照

`evals/base_assistant_generalization.json` 固定六道新判斷情境與六道控制題。判斷涵蓋撤回、缺漏、備援、衝突、對象更換和反向條件。控制涵蓋更正、算術、格式、歧義與引文。題目、前文和答案先凍結，每個版本使用相同的合成對話，兩邊各自使用真實引擎資訊。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_base_assistant.py prepare <研究名稱> --assay <短題驗收名稱> --suite evals/base_assistant_generalization.json
data/learning/runtime/Scripts/python.exe -I scripts/learning/assess_base_assistant.py preflight <研究名稱>
.venv/Scripts/python.exe scripts/learning/assess_base_assistant_pairs.py prepare <研究名稱>
.venv/Scripts/python.exe scripts/learning/assess_base_assistant_pairs.py run <研究名稱> --limit 1
.venv/Scripts/python.exe scripts/learning/assess_base_assistant_pairs.py report <研究名稱>
```

逐題入口需要尚未整批執行的原始規約。每次 `run` 預設選下一題，以同一次原生閒置觀察分別在兩個獨立程序執行底座與候選。每題有二十分鐘上限，恢復輸入或來源變更時停止。停止或失敗的原答保留，之後的命令接著收集尚未嘗試的題目。報告重核完整成對的實際請求、原答、會話、輸入長度及退出紀錄，列出已規劃與已完成題數，再分列目標和控制正確數。逐題研究是獨立的量測規約，日常候選資格仍由原本的完整驗收決定。

### 共同條件與備援的對照式學習

`evals/prerequisite_contrast_learning.json` 有六組訓練情境和兩組保留情境。每組固定同一條規則，替換觀察後分別得到「已確認成立」「已確認不成立」「未知」。情境涵蓋共同條件、備援、逐人核對、必要性與充分性的方向，以及同等回報衝突。整組留在同一分割，並檢查與先前的訓練及實測題有無重複。完整助理驗收另有四道控制題。

```powershell
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study prerequisite-contrast
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study prerequisite-contrast --prepare <新研究名稱>
```

第一個命令只驗證合成資料。第二個命令建立獨立的本機研究資料庫和未開始的參考工作，接著可依本文件的底座分段流程固定總步數與短題驗收，再於原生閒置時執行。先從原始底座另開研究，便於用相同規則比較參數與重載結果。保留題只進入驗收，通過檢查點、短題與完整助理驗收後才有資格討論能力改善。

分段研究的短題通過後，以 `evals/prerequisite_contrast_assistant_transfer.json` 準備新的完整助理對照。另有六道判斷及四道控制，和實際訓練或短題規約的問題均不重複。使用 `assess_base_assistant.py prepare` 指定已通過的短題驗收，再走完整輸入預檢及逐題流程。

### 無關待查項目與輸出代碼

`evals/prerequisite_decision_codes.json` 定義共同條件、任一條件、共同加備援、必要條件，以及所有合格項目都具有某標記等情境。程式列舉相容情況求出結論。每個情境有六例，三種結論和三個輸出代碼都各出現兩次。有些配對只改一個不影響結論的觀察，有些在相同事實下改用另一套代碼。已知必要標記存在時，除非另有證據，仍不能確定目標成立。

```powershell
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study decision-codes
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study decision-codes --prepare <新研究名稱>
```

新版從原始底座開始，訓練集保留 18 個早期訓練例並加入 30 個新例。12 個新保留題都排除於訓練。準備收據的 `short_transfer` 指向另外保存的短題檔，包含 6 個早期保留題與原有 6 個轉移題。準備分段後以 `assess_base_checkpoint.py prepare --transfer-suite` 指定該檔，讓先前的改善和退步可在同一批新權重上重測。完整助理仍使用另存且未參與短題的獨立題庫。

### 自然觀察的必要條件學習

`evals/prerequisite_observation_language.json` 用容量、表面狀態、目前與上一輪的讀值、尺寸及標記等情境訓練自然觀察的判讀。每個情境各有成立、不成立與未定一例。事實狀態由合成作者標註，再列舉與規則相容的情況求答案。保留不同物件、過期資訊、未知狀態和必要條件方向的區別。

```powershell
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study observation-language
.venv/Scripts/python.exe scripts/learning/prepare_epistemic.py --study observation-language --prepare <新研究名稱>
```

重播 48 個已用訓練例，加入 15 個新觀察例。短題保留先前全部 24 道保留及轉移題、8 道基本控制，另有 8 道新評估。先前錯誤的指定代碼和空桶必要條件進入必過的保留組。準備收據另提供 `full_transfer`，保存 10 道與短題及訓練不重疊的完整助理題。

### 從已驗收適配器接續更新

`continue_verified_adapter.py` 使用兩次已驗收的短題研究。父版本須通過既有短題門檻，近期參考可用於保留題，即使該輪未通過。準備時核對兩輪的底座及原始結果，並封存父版本的適配器檔案與權重指紋。訓練使用 63 筆先前訓練例和 21 筆新合成例，每筆使用一次。每步採三筆重播和一筆新例，固定 21 步及 `0.00005` 學習率。

```powershell
.venv/Scripts/python.exe scripts/learning/continue_verified_adapter.py check
.venv/Scripts/python.exe scripts/learning/continue_verified_adapter.py prepare <名稱> --parent <父短題研究> --recent <近期短題研究>
.venv/Scripts/python.exe scripts/learning/continue_verified_adapter.py run <名稱>
.venv/Scripts/python.exe scripts/learning/continue_verified_adapter.py verify <名稱>
```

工作需要原生閒置十分鐘，由三個獨立程序實測父版本、訓練後和重新載入。40 道歷史非訓練題、12 道新題及 21 道新訓練例分組計分。保留兩輪任一版曾答對的歷史題，也保留實測父版本答對的非訓練題。新題和新訓練例都須答對，至少有一題非訓練題優於父版本，重載的原答和 logits 須一致。結果留在 F 槽，完整助理資格另行驗收。

### 固定步數觀察接續權重

`trace_adapter_updates.py` 對已完成的接續規約重現同一父權重、隨機種子、取樣順序及 21 步更新。固定保存第 7、14、21 步的適配器，以獨立程序重測原有全部 73 題。保存中途權重時隔離亂數狀態。取樣及損失須與原實驗一致，第 21 步的權重、原答及 logits 也須重現原結果。

```powershell
.venv/Scripts/python.exe scripts/learning/trace_adapter_updates.py prepare <名稱> --reference <已完成接續規約>
.venv/Scripts/python.exe scripts/learning/trace_adapter_updates.py run <名稱>
.venv/Scripts/python.exe scripts/learning/trace_adapter_updates.py verify <名稱>
```

報告把父版本已錯的題目和更新後首次觀察到的退步分開。各點重算同一套保留題，歷史題、全新題和訓練例分組呈現。固定點只代表觀察到的時間區間，實際變化可能在兩個檢查點之間。每個 GPU 程序採原生閒置及六分鐘時限，中斷會留存已完成檔案和退出證據。完整助理資格仍另行驗收。

### 逐段驗收成組回放

`paired_adapter_replay.py` 從已完成軌跡的原始父版本開始，每段三次更新。每次更新使用同一新場景的三種觀察及先前的三個回放例。期限和必要條件成組區分成立、不成立和待查。JSON 成組區分空陣列、只有一個空字串及只有一個非空字串。固定學習率為 0.000025，每段重設最佳化器和隨機種子。這個規約同時調整取樣、速率及接續方式。

```powershell
.venv/Scripts/python.exe scripts/learning/paired_adapter_replay.py check
.venv/Scripts/python.exe scripts/learning/paired_adapter_replay.py prepare <名稱> --reference <已完成的分段觀察>
.venv/Scripts/python.exe scripts/learning/paired_adapter_replay.py run <名稱>
.venv/Scripts/python.exe scripts/learning/paired_adapter_replay.py verify <名稱>
```

每段都用獨立程序重載並驗證同一套 88 題，分開呈現 52 道歷史題、18 道未參與訓練的新題及 18 道訓練例。原父版本或前一段答對的任何非訓練題退步，後續訓練便停止。要通過完整研究，兩段都須完成、歷史受保護題均答對，且至少改善一道全新保留題。各程序採原生閒置及六分鐘時限，原答與權重留在本機。

### 核對成組回放的前三次更新

`replay_prefix_probe.py` 從同一父版本分別重跑 0、1、2、3 次更新。每個程序沿用已完成研究的種子、速率及對應取樣順序。重算各段原始損失，並在首末兩點比對原有 88 份回答、權重和固定輸入的 logits。每點另作答六道只更換代碼映射的對照。原始代碼按各題給定的意義解讀，另外列出答不出有效代碼或未結束的情形。

```powershell
.venv/Scripts/python.exe scripts/learning/replay_prefix_probe.py prepare <名稱> --reference <已驗收的三步回放紀錄>
.venv/Scripts/python.exe scripts/learning/replay_prefix_probe.py run <名稱>
.venv/Scripts/python.exe scripts/learning/replay_prefix_probe.py verify <名稱>
```

四個程序各自符合原生閒置和六分鐘上限，原答與中途權重留在本機。這項診斷定位在固定配方下首次觀察到的變化，可用來分開檢查結論與指定代碼。完整助理的實際對話表現及資格另行評估。

### 對照前兩批資料的次序

`replay_batch_order.py` 重讀父版本、只更新設備批次及先設備後期限的既有證據。由相同父版本另跑期限單批、先期限後設備兩組，保留批內樣本順序、種子、速率與其他設定。兩個新工作均測相同的 94 題。比較逐題原答和指定映射，再計算各組分數及與父版本的差異。

```powershell
.venv/Scripts/python.exe scripts/learning/replay_batch_order.py prepare <名稱> --reference <已完成的分步探查>
.venv/Scripts/python.exe scripts/learning/replay_batch_order.py run <名稱>
.venv/Scripts/python.exe scripts/learning/replay_batch_order.py verify <名稱>
```

兩個 GPU 工作各有獨立的權重及程序紀錄。各組結果限於既定配方與測題，權重留在本機。

### 同事實的三種代碼映射

`replay_label_transfer.py` 由已驗證的先期限後設備權重接續。兩個訓練場景各有成立、不成立及待查。每組事實輪換三種代碼映射，同一批保留這三個版本，再加入三個既有訓練例，共六次更新。另凍結兩個不同場景的 18 道保留題，與原有 94 題及 18 道新訓練例分開計分。

```powershell
.venv/Scripts/python.exe scripts/learning/replay_label_transfer.py check
.venv/Scripts/python.exe scripts/learning/replay_label_transfer.py prepare <名稱> --reference <已完成的批次次序對照> --phase-seconds 540
.venv/Scripts/python.exe scripts/learning/replay_label_transfer.py run <名稱>
.venv/Scripts/python.exe scripts/learning/replay_label_transfer.py verify <名稱>
```

父版本須重現原有 94 題與 logits。獨立重載須重現候選權重、全部原答及 logits。研究門檻要求保住父版本答對的非訓練題，並讓至少一組原本未全對的陌生事實在三種映射都答對。每階段六或九分鐘的時限在準備時固定，仍須核對原生閒置及工作程序退出。結果只用於研究。

### 分開量測語意判斷與代碼輸出

`diagnose_label_reasoning.py` 讀取已驗證的父權重與未通過門檻的候選。每版獨立測六道原題對照、六道固定語意詞、六道加上明示判斷規則的語意詞，以及 18 道已提供正確結論的套碼題。六道原題對照須重現原始作答，並核對原權重與固定輸入 logits。

```powershell
.venv/Scripts/python.exe scripts/learning/diagnose_label_reasoning.py prepare <名稱> --reference <已完成的代碼轉移研究>
.venv/Scripts/python.exe scripts/learning/diagnose_label_reasoning.py run <名稱>
.venv/Scripts/python.exe scripts/learning/diagnose_label_reasoning.py verify <名稱>
```

報告另外將模型實際回傳的語意詞按原題的三種映射轉成代碼。這項重算使用實測結論，套碼由程式執行。各組沿用既有事實，因此可檢查解題和輸出環節的差異。新場景與完整助理表現需另立測驗。

### 完整助理中的語意交付對照

`assess_semantic_delivery.py` 用已驗證的父權重測六道新情境、兩道歷史對照和兩道一般輸出控制。兩組各複製同一份合成會話資料庫，透過正式 `Assistant.ask` 保存原問和回答。直接組使用完整原提示。分離組只在送入模型的當前問題中，以固定語意詞指示替換末段的代碼要求。收到有效語意詞時，按該題原映射交付。每組以完整助理提示和本機分塊推論，先做 token 預檢。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_semantic_delivery.py check
.venv/Scripts/python.exe scripts/learning/assess_semantic_delivery.py prepare <名稱> --reference <已完成的語意診斷>
.venv/Scripts/python.exe scripts/learning/assess_semantic_delivery.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_semantic_delivery.py verify <名稱>
```

兩組須核對模型與裝置、送出提示、原答、結束標記、SQLite 回答與程序退出。逐組呈現答對、未能套碼及個案進退。一般輸出控制使用相同提示與原答。所有原答和合成資料庫留本機，測試入口只用於研究。

### 逐項擷取當輪的反證

`assess_polarity_evidence.py` 固定六道新場景、一個歷史反證和一個相同提示的控制。使用已驗證的相同父權重和完整 `Assistant.ask`。第一組直接回固定結論詞。第二組先取得兩個已明列的肯定命題與編號原句，再回傳每個命題的當輪狀態及原句編號。程式依實際擷取的狀態計算結論。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_polarity_evidence.py check
.venv/Scripts/python.exe scripts/learning/assess_polarity_evidence.py prepare <名稱> --reference <已完成的語意診斷>
.venv/Scripts/python.exe scripts/learning/assess_polarity_evidence.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_polarity_evidence.py verify <名稱>
```

分開計算精確結論和同時核對狀態及原句的成績。編號合法只代表指到題中某句。題目提供兩個待核對命題，研究涵蓋明列命題的證據擷取。工作須符合本機閒置，原問、原答、會話副本與退出證據均留本機。

### 同一物件的跨輪證據配對

`assess_scope_evidence.py` 固定三組事實配對、歷史反證和一般輸出控制。每對共享規則、舊輪及另一項觀察，只有一條本輪原句不同。兩組使用同一個父適配器、相同的合成歷史和編號原句。第一組連續呈現原句，第二組加上規則、舊輪紀錄及本輪觀察三個標題與換行。兩組的會話都保存原問題及完整前文。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_scope_evidence.py check
.venv/Scripts/python.exe scripts/learning/assess_scope_evidence.py prepare <名稱> --reference <已完成的語意診斷>
.venv/Scripts/python.exe scripts/learning/assess_scope_evidence.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_scope_evidence.py verify <名稱>
```

分開統計可解析輸出、最後結論、同時正確的狀態與來源、引用規則或舊輪，以及一整對答案或證據是否都正確。來源落在本輪仍須核對物件及狀態。兩個命題和句子邊界事先明列，因此這項研究衡量已知命題的證據歸屬。驗證使用原生閒置、完整 token 預檢、實際原答和兩份 SQLite。所有實測證據留在本機。

### 從未分段原文標示範圍線索

`assess_natural_scope.py` 使用六道未標範圍的新場景、一個歷史反證和一個相同提示的控制。程式按原文字元範圍保留完整句子，只讀取引文外的句型及時間線索。可辨認的句子加上規則、較早或當前線索的提示。缺少或混有多種線索時標示範圍待辨。引文與括號內的標點保留，邊界有誤或輸入超過上限時維持原文。兩組保存相同原問與前文，提示的差異限於當前問題的標題與換行。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_natural_scope.py check
.venv/Scripts/python.exe scripts/learning/assess_natural_scope.py prepare <名稱> --reference <已完成的語意診斷>
.venv/Scripts/python.exe scripts/learning/assess_natural_scope.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_natural_scope.py verify <名稱>
```

兩組都回兩項事先明列命題的狀態與整句原文。缺少當前觀察時可用待查及空來源。報告分開計算結論、狀態、整句與狀態配對、找不到完全相同原句的引文，以及無來源的肯定或否定。分段只提供線索，原始來源與證據歸屬仍要各自核對。所有作答及 SQLite 證據留在本機。

### 固定索引與時間適用性

`assess_indexed_scope.py` 在同一本機適配器重現原引句測驗八題，再加無日期／本輪照片與舊檢測／當前複驗兩組新配對。原組逐字引用，自動索引組從同一份自然原文選整數編號。原文保留順序及字元位置，由程式取回句子。原組八題須重現完整提示、實際 token 數、原答與結束標記。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_indexed_scope.py check
.venv/Scripts/python.exe scripts/learning/assess_indexed_scope.py prepare <名稱> --reference <語意診斷> --previous <原引句實測名稱>
.venv/Scripts/python.exe scripts/learning/assess_indexed_scope.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_indexed_scope.py verify <名稱>
```

原始索引及狀態保留供評分。另行檢查索引範圍與明示較早、規則及無日期媒體線索，只能把排除的狀態改成待查並記錄原因。其他來源仍須核對時間、物件及命題。原始模型結論、來源恢復和檢查後結論各自計算。原始 SQLite 和回答留在本機，這個入口供研究。

### 排除來源後重查單一條件

`assess_scope_recovery.py` 核對上一輪固定索引測驗的完整結果，採用所有實際被排除的三個引用。另以兩組共四個新案例控制當前反證、相近物件、缺少觀察及可恢復的肯定證據。新案例事先放入不適用的初始引用，研究衡量拒絕後的重查。兩組都只問被排除的命題，並保存另一項觀察。第一組看到全部原句，第二組只看到通過現有來源檢查的編號與原句。兩組收到相同的排除原因及回答要求。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_scope_recovery.py check
.venv/Scripts/python.exe scripts/learning/assess_scope_recovery.py prepare <名稱> --reference <語意診斷> --previous <固定索引實測>
.venv/Scripts/python.exe scripts/learning/assess_scope_recovery.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_scope_recovery.py verify <名稱>
```

只允許一輪重查，兩組的回答均用相同規則核對來源編號。格式失敗或來源仍無效會保留待查。逐案比較重查前後的結論，以及被排除條件的狀態和原句。歷史對話保留於完整提示中，篩選只作用於當前提供的編號清單。這個入口供研究，來源索引及時間通過後仍要核對物件。

### 指定原句的物件及屬性對照

`assess_object_attribute.py` 將原句和待查命題固定，給每項命題指定同一個觀察編號供兩組核對。六道新題分三對，分別測相近屬性、相近物件及同一句同時出現兩項狀態。歷史對照綁定前一輪錯選的實際句子。一般輸出控制使用相同提示。每組使用相同本機適配器和完整助理背景。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_object_attribute.py check
.venv/Scripts/python.exe scripts/learning/assess_object_attribute.py prepare <名稱> --reference <語意診斷> --previous <已完成重查測量>
.venv/Scripts/python.exe scripts/learning/assess_object_attribute.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_object_attribute.py verify <名稱>
```

一組只輸出各命題狀態，另一組另回物件是否相符、屬性是否相符。程式另外將有不相符旗標的狀態降為待查，原答保留。旗標由同一次模型呼叫提供，逐項與獨立題目標準核對。相同原句可用於兩項條件，兩項仍各自評分。此測量驗證指定來源的歸屬，來源搜尋及完整自由問答另行驗收。

巢狀格式首輪保留嚴格原分數。直接組輸出的平面 A/B 字串另行計算可讀狀態，這項補充分析在觀察到格式差異後加入。它只接受完整平面 JSON 的兩項狀態，並保留分項組的原格式限制。

`assess_object_attribute_flat.py` 另凍結相同案例，兩組都改用明示的平面 JSON 欄位。直接組回 A_state、B_state，分項組另外回四個布林欄位。只在核對完整欄位和結束標記後轉成內部結構評分，原始輸出保留。新規約會核對第一輪來源、原句、命題、前文及權重。指示與格式範例一同調整，結果用於已知題目的開發對照。

```powershell
.venv/Scripts/python.exe scripts/learning/assess_object_attribute_flat.py check
.venv/Scripts/python.exe scripts/learning/assess_object_attribute_flat.py prepare <名稱> --reference <語意診斷> --previous <已完成物件屬性測量>
.venv/Scripts/python.exe scripts/learning/assess_object_attribute_flat.py run <名稱>
.venv/Scripts/python.exe scripts/learning/assess_object_attribute_flat.py verify <名稱>
```
