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


## 2026-11-03：超長單行讀取與字元引用

v0.43以本機合成的10586碼位單行文件驗證實際自主流程。改前read回報character_limit，模型誠實詢問重新排版，未能完成；改後同一來源與目標，模型先read再read_line三頁，連續涵蓋全文，整理四項規則、識別碼與待審例外並用實際字元位置引用。兩輪改後收集均完成，最終保留逐工具輸入／輸出、來源雜湊與原檔不變證據。這是新增讀取能力的具體前後對照，不把改前正確承認限制說成模型幻覺。

另外實際驗證普通聊天的片段問答與自主code.txt交付：均正確取得代碼並引用實際片段，明示未全文檢查；檔案交付包含實際存檔讀回。單元測試另覆蓋字元缺口、不同來源版本、Unicode、空行、超界、舊片段、取消、交接、備份及未讀範圍寫入拒絕。位置覆蓋不等於結論正確或使用者驗收。

`evals/product_help.json` 現為九題，新增long_single_line。新題使用既有理解評估工具實際收集及逐項development_agent評讀，沒有獨立人類驗收；首答的格式示例觸發來源提示，調整說明後最終答覆以文字描述引用規則，不假造實際引用。

原始報告及開發評讀保存在忽略的 `data/evaluations/line-reading-v043`，human_grade空白。模型收集完成後僅變更context_ui.py的字元位置顯示；ui-verification.json記錄實際來源差異並核對其他所有模型相關程式與提示未變，另以隱藏Tk文字檢查確認顯示。舊全專案指紋保留原值，不把顯示更新後的版本冒稱完全相同，也不將隱藏視窗檢查當作視覺驗收。


## 2026-11-03：從對話整理目標草稿

v0.44新增 `evals/goal_drafts.json` 四個固定合成情境，直接呼叫實際goal_drafts流程，驗證最新更正與必要數字、尚未決定工作、引文與錯誤助理提案、工具能力缺口。模型草稿須通過結構與逐字來源核對，再另做語意評讀；來源匹配不能證明使用者已確認該意思。

```powershell
python scripts/evaluate_goal_drafts.py
python scripts/evaluate_goal_drafts.py --live --report data/evaluations/goal-drafts-new.json
python scripts/review_understanding.py --report data/evaluations/goal-drafts-new.json --reviews data/evaluations/goal-drafts-reviews.json --reviewer development_agent
```

離線只建立固定來源，不呼叫模型，已接入發布、CI與自我改碼。真實模式要求新報告及狀態目錄，保留同題來源、草稿、指紋及合成資料庫；收集前後核對來源，不能更動中混入結果，不會自動重試失敗。完整收集後使用既有逐項評讀工具，評讀者須明列，human_grade不由開發者冒填。

本輪兩次收集的四題均符合開發標準；沒有將助理建議或引文當成已確認操作，未決工作保留問題，無法直接處理的PDF／寄信需求保留完整範圍並指出缺口。另把最終真實費用草稿明確保存為pending目標，不帶額外聊天背景或CSV／檔案權限；開始後三次計算得到720、900與180，再交付文字成果。這驗證此草稿可獨立接續該任務，不能代表任意任務都可完成。

`evals/product_help.json` 現為十題，新增conversation_goal_draft。舊說明與首輪新說明均保留未全通過的評讀；補清楚三種權限選項不會自動選取後，最終答覆符合四項標準。所有真實報告、原話、實際草稿及流程記錄只在忽略的 `data/evaluations/goal-draft-v044`，與最終來源／提示指紋對照；這是開發評讀，不是使用者或獨立人類驗收。

## 自主問題回答與接續（v0.45）

`evals/goal_replies.json`保存四個合成情境：補充期間、最新人數更正、未採用的引文選項、仍未決定。`python scripts/evaluate_goal_replies.py`只核對格式與建立來源，不呼叫模型。實際評估使用`--live --report data/evaluations/指定新檔.json`，每題先讓模型提問、明確保存固定的使用者回答，再啟動同一目標；保存本身不呼叫模型。報告保存兩階段、原問題、使用者回答、工具步驟、原始回應與固定來源指紋，另保留合成資料庫。已有報告／狀態目錄不覆寫，失敗不自動重試。全部完成後以`review_understanding.py`記錄逐項開發評讀；`human_grade`仍空白，不能當成獨立驗收或通用理解分數。發布、CI及自我改碼檢查包含離線來源核對。

`evals/product_help.json`新增第十一題，核對固定問題視窗、保存與接續差別、過期回答及未保存文字的實際行為。真實資料與報告只存忽略的本機資料夾。


## 否定風格與巢狀引文（v0.46）

`evals/communication_scope_revisions.json`保存59個已知提示判讀案例，以核心communication函式與固定期望核對，與原48個communication_revisions案例一起回歸。這些案例來自多輪提供／保留驗收及額外稽核，公開後都是已知案例，不是未見過的獨立驗收集。

`evals/communication_scope.json`有六題真實回答情境，包括否定簡短風格、否定逐步解釋、巢狀引文資料、英文帶受詞的否定、明確引號風格及引文內更正不改句外要求。使用理解評估工具的`--suite`與`--live --report`收集固定來源指紋，完成後逐項開發評讀。改前六題已符合標準，不能把輔助提示的修正當作六題回答從錯變對。發布、CI及自我改碼包含離線情境檢查，離線不呼叫模型、不評分回答。原始自改碼、候選與實際回應只留本機忽略目錄。


## JSON結構查讀（v0.47）

`evals/json_documents.json`包含四個合成自主情境：13萬多字且跳脫欄位名的長單行、陣列後段欄位、精確數字／布林／null、解碼後重複鍵。`scripts/evaluate_json_documents.py`離線只建立來源與目標；真實執行需`--live --report data/evaluations/新報告.json`，可用`--ids`選題，每題八步。保存程式、提示、案例與執行腳本雜湊，以及實際匯入來源SHA、工具動作與原始回應；已有報告／狀態不覆寫，失敗不自動重試。收集完成後使用review_understanding.py逐項評讀，區分開發評讀與使用者驗收。原始資料、模型回應與合成資料庫不進Git。

位置依據採既有行／字元引用檢查，不把JSON Pointer或欄位清單當成未讀原文的證明。[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901.txt)定義路徑及跳脫規則；[RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.txt)說明重複物件名稱的互通性問題。本產品選擇拒絕整份含重複鍵的結構讀取，另設定明確大小及結構上限，並不宣稱支援所有可表示的JSON輸入。


## 受控工具失敗後接續（v0.49）

`python scripts/evaluate_tool_recovery.py`只建立三個固定失敗與補充情境，不評分模型。加上`--live --report data/evaluations/新的報告.json`才呼叫模型，最多接續五步，不自動重試；完整原始回應、失敗種子與本機資料庫保留於忽略目錄。初始失敗由腳本注入，必須與模型自行規劃、犯錯和修正的端到端評估分開。

案例核對除以零後保留原分子並依補充重算、同名文件衝突後原樣另存並保留原檔、尚未指定新除數時僅釐清缺少的值。前後比較須固定案例、執行腳本、初始動作與目標，並核對程式／提示指紋及實際文件位元組；收集完成後使用既有review_understanding.py逐項評讀，保留開發者與使用者驗收的區別。failed_action是未成功的歷史動作，不是新授權、結果或完成證據；舊資料未保留的參數不能靠評估補造。


## 公曆月份規則與實際日期目標（v0.50）

`python scripts/evaluate_date_months.py`只驗證四題格式並建立待開始的本機目標；`--live --report data/evaluations/新的日期報告.json`才收集實際模型的工具動作與成果，每題八步、不自動重試。固定比較原起日逐月計算、明確月底、反向及世紀閏年、未決定短月規則的澄清；保存程式／提示／案例／執行腳本指紋，收集結束後使用review_understanding.py逐項評讀。

需要區分模型自行推算後查weekday，與工具直接以add_months計算所選規則。原本正確的日期不能因改用新工具就說原答案錯誤；新增能力及更可核對的計算依據，不等同整體推理品質提升。所有實際回應與合成資料庫留在忽略目錄；使用者與獨立評讀仍需分開記錄。


## DOCX抽取與實際問答（v0.52）

`python scripts/evaluate_docx_reading.py`以scripts/docx_fixture.py產生固定合成DOCX，驗證可匯入並建立三個待開始目標；加`--live --report data/evaluations/新的報告.json`才收集實際模型，每題最多八步、不自動重試。報告綁定程式、提示、案例、runner及fixture腳本，另存DOCX原始SHA、抽取副本、工具與回答；完成收集後使用review_understanding.py逐項評讀。

重點為表格與註腳合計560、頁首頁尾／附註資料與未決議註解、無法由文字得知Word頁碼／圖片時的限制說明。引用應對照實際抽取行號，不能把部件順序、註記封裝ID或文字行號當成Word頁碼／顯示編號。原封裝與投影的來源指紋只是版本證據，並非原版面等價；單輪開發者評讀與使用者驗收分開。初輪服務失敗的紀錄必須保留，改程式後的新收集不覆寫舊結果。


## 精確結果接續計算（v0.53）

`python scripts/evaluate_exact_calculation.py`離線只驗證三個固定情境並建立目標；加上`--live --report data/evaluations/新的報告.json`才呼叫真實模型，每題最多八步、不自動重試、不覆寫舊報告。保存程式／提示／案例／腳本指紋、原始動作與完整目標；收集完成後以review_understanding.py逐項評讀。案例涵蓋循環分數接續、長有限小數與大整數抵消。

改前模型能以精確整數拆解繞過輸入限制時，保留正確評讀；另比較被拒絕的直接輸入、成功工具的完整參數與總步數，不能將「改成直接工具計算」冒稱原答案錯誤或整體推理分數提升。小數顯示不等於精確值，模型是否沿用exact須由實際工具輸入核對。原始報告與資料庫只留本機；開發評讀不等同使用者驗收。


## 回饋保存順序與受控時鐘（v0.55）

`python scripts/evaluate_feedback_order.py`離線只建立兩個合成回饋情境；`--live --report data/evaluations/新的報告.json`才收集實際聊天回答。使用真正的保存API及明確保存順序，之後僅將時間欄位設為同秒／倒退以重現時鐘條件；這不是自然發生事件的成功率。保存資料庫、原始回應、實際脈絡、前後案例／腳本／來源／提示指紋，失敗不自動重試，收集完成後用review_understanding.py逐項評讀。

需分辨來源選取與模型推理：改前最新更正被省略或順序被時間顛倒時，不能只把錯誤結果歸因模型。改後核對最新內容確實進入脈絡、保存順序與時間分開，以及最終答案採用正確更正。遷移來源的同秒真實先後未知，舊快照仍保留原格式，不以新序號補造過去；相容性與交易回滾另以實際資料庫及備份測試驗證。原始回應與本機資料庫不上傳GitHub。


## 對話分隔字元與來源保真（v0.56）

`python scripts/evaluate_conversation_text.py`離線只建立兩個合成對話及明確帶入的自主目標；`--live --report data/evaluations/新的報告.json`才呼叫模型，每題八步，不自動重試。固定測試近期條件與被一般討論推遠的明確更正，其中NUL後的數字仍在原字數上限內；前後案例、腳本及原始文字相同。保存實際快照、動作、原始回應與程式／提示指紋，收集完成後以review_understanding.py逐項評讀。

改前模型因來源截斷而詢問缺少資料，需將來源錯誤與模型合理追問分開；改後核對完整條件確實帶入、實際計算及交付結果，不只檢查回答含預期數字。Unicode邊界、UTF-16資料庫、損壞編碼拒絕、草稿引用／來源變更與備份另以實際SQLite回歸驗證。模型案例不是所有輸入或所有語言的理解分數；原始資料與回應只留本機。


## 長助理回覆與近期使用者條件（v0.57）

`python scripts/evaluate_conversation_priority.py`離線只建立兩個合成對話及自主目標；`--live --report data/evaluations/新的報告.json`才執行實際模型，每題最多八步、不自動重試。固定16則訊息：八則使用者與八則長助理回覆，檢查原採購條件與後來明確定案是否仍進入近期背景。前後使用相同案例與腳本，保存實際快照、工具參數及原始回應、來源／提示指紋；完整收集後才逐項評讀。

改前未收到數字而發問是合理缺漏處理，應歸因來源選取，不能宣稱模型憑空遺忘。改後須核對工具確實計算1040／1050，以及採用使用者最後決定而非助理12人提案。這是固定情境的開發評讀，不是獨立理解分數。短對話方案指代、使用者本身超額、近期窗界線、跨對話與本機指令排除、草稿引用、來源變更及備份以實際SQLite測試另外驗證；原始紀錄只留本機。


## 聊天草稿恢復說明（v0.58）

產品說明第十八個情境chat_draft_recovery，使用既有understanding runner的`--suite evals/product_help.json --ids chat_draft_recovery --live --report data/evaluations/新的報告.json`收集改前／改後回答。評讀保存範圍、衝突操作、問題是否入庫與草稿清除的區分、字數與突然中斷限制；不能從模型聲稱可恢復就判定實作成功。

功能證據另以實際臨時SQLite及隱藏Tk檢查切換／重開、定期保存／正常關閉、過期版本與核對中變更、草稿清除和問題入庫回滾、模型失敗後重試、完整本機命令、下一題輸入、備份與舊格式相容。保存草稿不呼叫模型，也不執行草稿中的指令。模型評讀只是功能說明正確性，不是理解分數或視覺驗收；原始對話與合成資料庫不納入GitHub。


## JSON整個陣列統計（v0.59）

`python scripts/evaluate_json_statistics.py`離線只建立三份合成來源與目標；`--live --report data/evaluations/新的報告.json`才執行模型，每題相同八步上限、不自動重試。保存來源位元組、案例／腳本／程式／提示指紋及完整實際動作，收集終止後才以review_understanding.py逐項評讀。案例為300筆巢狀欄位、超過浮點安全整數的小數抵消及JSON指數、未決定處理方式的null。

300筆獨立精確參考為總和13002/5（2600.4）、平均2167/250（8.668）、最小-40、最大291/5（58.2）；五筆精度案例為總和301/1000、平均301/5000，極值正負9007199254740993。核對實際工具是否處理完整陣列，不能只看答案數字。改前已正確的小案例保留通過；八步內尚未交付只代表此次預算內未完成，不能宣稱舊版本永遠無法接續。缺測應詢問，不把部分計算說成全部。

解析與算術邊界、無效型別、取消、匯入副本／備份、未讀行號拒絕另有確定性測試。產品說明第十九題沿用understanding runner前後評讀，不能以說明正確代替工具正確或使用者驗收。原始回應與合成資料庫只留忽略的本機目錄。


## 資料工作關閉說明（v0.60）

第二十個產品說明情境data_work_shutdown使用既有understanding runner：`--suite evals/product_help.json --ids data_work_shutdown --live --report data/evaluations/新的報告.json`。固定同題核對主視窗／單獨工作視窗關閉、CSV與搜尋取消及備份／還原收尾的差別、搜尋來源視窗關閉、禁止新工作與無自動重試／重啟，以及正常關閉和強制終止的差別。收集終止後才以development_agent逐項評讀；模型回答不是實作驗收。

實作證據另由隱藏Tk及臨時SQLite驗證主程式等待每個終止佇列事件、真實CSV取消狀態、搜尋取消及來源視窗獨立性、真實備份／還原完成、關閉後不啟動還原程式、原對話匯入、直接呼叫與檔案選擇返回時的關閉檢查、啟動失敗及計時器清理。保留既有聊天草稿保存與自改碼發布收尾測試；沒有宣稱桌面視覺驗收或強制終止安全性。原始回答、失敗日誌與合成資料只留忽略的本機data。
