# 需求理解評估流程

`evals/understanding.json` 目前包含 31 個合成案例，涵蓋限制遵守、必要澄清、最新更正、多輪／長對話、事實可靠性與推理；可靠性六題，其餘各五題。每題有三項評讀標準。這是開發評估集，不含使用者私人對話，也不是獨立測試集。

## 執行與接續

```powershell
python scripts/evaluate_understanding.py
python scripts/evaluate_understanding.py --live --report data/evaluations/understanding-run.json
```

第一個命令只驗證案例格式與上下文建構，不呼叫模型，不產生理解分數。第二個命令透過產品的 `Assistant.ask` 實際流程收集答案，使用 Codex 帳號額度；報告保存每題上下文、答案、提示指紋與程式／案例集指紋。

若程序確定已結束，可使用相同來源與案例選取接續未完成題目：

```powershell
python scripts/evaluate_understanding.py --live --report data/evaluations/understanding-run.json --resume
```

先確認原程序已停止，不要同時執行兩個程序寫同一報告。已有檔案不能默默覆寫；提示、程式來源或案例選取變動會拒絕接續。`--ids` 可選部分案例，重試應使用新的報告路徑；不要把失敗後重跑的結果冒充首次結果。

## 逐題評讀

收集完成後，閱讀每題實際答案與其 `criteria`，另建評讀 JSON。例如：

```json
[
  {
    "id": "clear_request",
    "criteria_pass": [true, true, true],
    "note": "回答 420 元，遵守先折扣再加運費，未詢問額外問題。"
  }
]
```

依標準原有順序逐一評判，每項皆通過才計為該題通過。未符合任一項應填 `false` 並記錄原因；證據不足也不應填通過。工具驗證格式，不會代替評讀者判定內容正確性。

```powershell
python scripts/review_understanding.py --report data/evaluations/understanding-run.json --reviews data/evaluations/reviews.json --reviewer user
python scripts/review_understanding.py --report data/evaluations/understanding-run.json
```

評讀者類型為 `user`、`development_agent` 或 `independent_reviewer`，需符合實際情況。評讀歷史會追加保存，摘要採用最近一次；原始答案不改寫。未評讀題目列為 pending，未產生答案另列 not_answered。既有 `human_grade` 欄位保持空值，不把開發代理評讀包裝成人工驗收。

唯一自動答案檢查是特定案例的三行項目格式；格式正確不代表內容正確。其餘題目沒有以關鍵字命中替代評讀。

## 固定案例比較

```powershell
python scripts/review_understanding.py --report data/evaluations/before.json --compare data/evaluations/after.json
```

只接受同一案例集與相同選取清單，列出每題前後通過／失敗變化；沒有兩邊實際回答與評讀的題目仍列為待完成。提示與程式版本可不同，指紋保留差異。模型使用登入 Codex 的預設服務，未固定遠端模型版本；單次執行會受取樣、服務變動與評讀者差異影響，不據此推論因果或整體能力提升。

目前尚無使用者真實失敗形成的獨立保留測試集；未來新增匿名案例時，需保留原本案例的歷史結果，不只挑選容易通過的題目。

## 2026-11-02 首次基準與規則修訂

v0.6 首次收集 30 題真實答案，依原先三項標準逐題評讀結果為 29 通過、1 未滿足全部標準。評讀者是開發代理，並非使用者或獨立評審。原始報告位於忽略目錄 `data/evaluations/understanding-v6-baseline.json`，原案例與提示另留 `understanding-suite-v1.json`、`system-v6-baseline.txt`，沒有改寫舊答案或追溯改分。

未通過題目是 `omitted_middle`：答案誠實說明中段省略、無法確認復原代號；未額外要求補上內容，因而未滿足原第三項標準。然而使用者明確要求「若沒有看到就直說」，原評分規則要求額外追問並不合理。因此新版保留誠實性標準，取消強制追問。這是評估規則修正，不是推理能力提升，也不能把舊分數直接重算成新版通過。

在 `missing_document` 的實際答案中，另發現原標準未涵蓋的產品問題：助理建議提供 PDF 並表示收到後可定位，但應用目前只支援文字文件。v0.6.1 補上檔案格式與 OCR 能力邊界，要求先轉成支援文字格式；需要原頁碼時必須保留頁碼標記。新增 `unsupported_pdf` 案例，檢查不虛報直接 PDF／OCR 能力，也不把行號當頁碼。

新舊套件因此由 30 題變為 31 題，且其中一題標準修改；兩份報告不符合固定案例直接比較的條件。後續結果需各自報告，不宣稱是 29/30 到某個新比例的同條件提升。

v0.6.1 已完整執行修正版 31 題，開發代理逐題閱讀答案與三項標準，31 題均符合本輪標準。報告為 `data/evaluations/understanding-v61.json`，每題保留評讀理由與評讀者類型。這是單次開發案例結果，不代表使用者驗收或一般任務成功率。

同一個 `missing_document` 問題的新版回答，改為要求貼上條文或轉成 TXT／Markdown，並保留頁碼標記；新增的 `unsupported_pdf` 也明示無直接 PDF 解析與 OCR，且不把行號當頁碼。這提供了特定能力說明問題的修正證據，未證明整體模型推理能力改變。


## 2026-11-03：完整基準與產品情境誤套修正

v0.17 先收集目前完整三十六題真實回答，開發代理逐題閱讀原有三項標準，三十六題均符合本輪標準；報告 `data/evaluations/understanding-v017-full.json` 保留每題理由與 development_agent 類型，human_grade 仍空白。這是本次單次基準，不是舊版31題的同條件比較，也不是使用者或獨立驗收。

閱讀 `ambiguous_move` 時另發現原三項標準未涵蓋的問題：雖保留兩份資料並避免覆寫，卻將未說明用途的資料夾帶入助理程式、對話記憶與 Start.cmd 情境。保留原標準通過結果並另記觀察，不追溯改分。新固定補充集 `evals/communication_context.json` 六題包括一般資料夾、照片、未指定程式、確實的助理重啟、用途更正與只想傾聽。

補充集改前六題實際回答中五題通過、一般資料夾一題未符合全部標準。將助理專屬功能與啟動說明限於已確認談論本助理的情境後，以完全相同的六題重新收集並逐題評讀：原失敗轉為通過，五個原通過維持。原始檔案為 `communication-context-v017-before.json` 與 `communication-context-v018-after.json`，其 suite 指紋與選取相同，使用既有 compare 核對。遠端模型取樣與開發評讀差異未受控制，不能從單次6題推論一般能力提升。

補充集使用相同評估器，可指定：

```powershell
python scripts/evaluate_understanding.py --suite evals/communication_context.json
python scripts/evaluate_understanding.py --live --suite evals/communication_context.json --report data/evaluations/communication-new-run.json
```

第一條僅驗證案例及上下文可建立，不代表語意品質通過。發布、GitHub CI 與自我改進回歸均納入此離線命令。完整36題真實基準是在提示修正前收集；本輪修正後只對固定補充6題重新取得真實答案，不把兩者混算為新版42題全數通過。

本輪也修正評估完整性：render_prompt 同時讀取 system.txt 與 communication.txt，原指紋只有前者。新報告另保存 communication_prompt_sha256，恢復執行時必須完全匹配；每題生成後再次檢查，避免最後一題生成途中改版仍被寫入舊版本報告。兩個故障注入／檔案修改測試確認舊答案不被覆寫，變更後的答案不被記在舊指紋下。舊報告仍可閱讀，但不接受缺少新指紋的報告接續新版執行。


## 2026-11-03：現行產品操作說明

v0.41新增 `evals/product_help.json` 八個固定情境，涵蓋未完成／步數已滿的階段接續、執行中工作與新階段選項、手動記憶編輯的不同欄位合併、同欄衝突、刪後草稿、工作清單與驗收差別，以及不把本助理控制項套用到其他軟體。

程式已支援未完成工作帶入與記憶逐欄合併，但原提示仍只介紹已交付階段，亦缺少新記憶編輯規則。改前八題的開發評讀為三題通過、五題未符合全部標準；首輪更新後五題通過，仍有三題把下一階段按鈕的立即保存時機或CSV／檔案選項順序說錯。對照實際控制器後補清楚建立前選好選項、按按鈕即保存、之後直接開始，並區分普通保存新目標。最終同題八題均符合本輪三項標準，原三個通過案例維持；案例與選取完全相同，由compare工具核對。

三輪原始報告及逐項評讀保存在忽略的 `data/evaluations/product-help-v041`，評讀者是development_agent，human_grade空白。這是針對已知產品說明缺陷的單次配對觀察，模型服務取樣與評讀差異未受控制，不是獨立驗收、普遍答題成功率或模型權重改善。

```powershell
python scripts/evaluate_understanding.py --suite evals/product_help.json
python scripts/evaluate_understanding.py --live --suite evals/product_help.json --report data/evaluations/product-help-new.json
```

第一條只核對案例與上下文可建立，已加入發布、CI及自我改碼的保護檢查；不呼叫模型，也不判定答案正確。第二條收集實際回答，完整收集後再使用review_understanding.py逐題評讀。產品流程變更時需一併核對提示中的按鈕名稱、保存時機、前置選項及能力範圍；文字更新不代表已替使用者執行操作。


## 2026-11-03：引文、否定與真正換題的脈絡界線

v0.42新增 `evals/topic_context.json` 六個固定合成情境：引文標題、inline code、否定換題、較早明確更正、真正換題及引用不沿用前文指令。改前直接搜尋關鍵字，導致五題未取得已保存的資料而回答未知或要求重貼；真正換題的一題原本通過。這是上下文選取缺陷，不把模型根據欠缺輸入回答未知算成捏造。

透過既有自我改碼流程提出兩版context.py：第一版提供案例14/14但保留案例11/12，未套用。將首輪26例與另查出的彎引號重現轉為第二輪已知27例，另用八例保留驗收；第二版分別27/27、8/8，通過保護檢查後由控制器發布。保留驗收只是不加入該輪提案提示，不能稱為OS隔離或從未見過；現全部35例已納入私人程式庫作為已知回歸，不能再作未見測試宣稱。

維護端將回答回饋接上同一context_reset入口後，以相同六題收集實際改後回答。開發評讀由1/6改善為6/6，五個失敗改善、一個原通過保留；逐項符合已存設定、先前更正與真正換題要求。兩份報告使用相同案例指紋／選取，每題記錄程式及提示指紋，完整收集後才評讀。這是單次已知案例對照，仍受模型取樣與開發評讀影響，並非獨立人類驗收或通用能力分數。

原始資料、實際答案、兩輪提案與評讀只在忽略的 `data/evaluations/topic-context-v042` 與 `data/evolution`；human_grade空白。35例回歸由單元測試涵蓋，六題離線建立檢查接入發布、CI與自我改碼，但離線結果不表示模型答案正確。

```powershell
python scripts/evaluate_understanding.py --suite evals/topic_context.json
python scripts/evaluate_understanding.py --live --suite evals/topic_context.json --report data/evaluations/topic-context-new.json
```

context_reset仍是有界文字提示，沒有通用語意解析。換題只影響前文接續查找與較早摘錄／更正／回饋選取，不刪除原始訊息或清空近期歷史，也不禁止當前問題直接查到相關記憶。
