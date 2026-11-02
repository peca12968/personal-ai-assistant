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
