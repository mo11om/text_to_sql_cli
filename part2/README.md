## 📊 Part 2: Production-Grade Multi-Model Evaluation Platform

本專案的第二部分建立了一個可高度信任的 Text-to-SQL 多模型評估平台。有別於傳統基於字串比對 (String-matching) 或基於計數器的多重集比對的脆弱評估方式，本平台全面升級採用 **「Pandas DataFrame 執行結果比對 (Execution-Based Accuracy)」**，並結合 LLM 自動錯誤聚類與 **「決定性語法樹驗證 (Deterministic AST Validation)」**，確保評估報告的絕對客觀與準確。

### 🏗️ 核心工程設計與決策 (Key Engineering Decisions)

1. **Pandas 驅動的執行準確度 (Execution-Based Accuracy - EX Score)**
   * 我們徹底移除了易受欄位命名與別名 (Alias) 影響的舊有字串/計數器比對邏輯。
   * **正規化對齊 (Normalization)**：系統會將 Ground Truth 與 LLM 產生的 SQL 送入本地 SQLite 執行。透過 Pandas DataFrames，系統會動態剝除所有欄位名稱，統一替換為數字索引，並使用 `df.equals()` 方法進行深度比對。
   * **決定性計分**：只要資料的形狀與值完全相同，即使 LLM 發明了不同於 Ground Truth 的欄位別名 (例如 `T1.name` vs `student.name`)，系統依然會給出客觀的 `EX Score: 1.0` 滿分。

2. **進程隔離與安全防護 (Process Isolation & Safety Guards)**
   * 為了防止 LLM 生成笛卡兒積 (Cartesian Product) 等惡意或失控的 `JOIN` 語法導致系統卡死，所有 SQL 執行皆被封裝在獨立的 `multiprocessing.Process` 中。
   * 實作了 **5 秒強制超時 (Hard Timeout)** 與 **MAX_ROWS=100 的結果集上限防護**，確保評估管線的穩定性。

3. **雙重驗證機制 (Dual-System Validation)**
   * **GT 預先驗證：** 在評估啟動前，系統會透過 Pandas 快取機制預先執行所有題目的 Ground Truth SQL。若有任何一題拋出錯誤，管線會立即中止，確保基準答案 100% 可靠。
   * **AST 決定性驗證：** LLM 所生成的「錯誤分類 (Clusters)」往往伴隨幻覺。我們在 `cluster_analyzer.py` 中引入了 `sqlglot` 套件，透過解析抽象語法樹 (AST) 來**決定性地驗證** LLM 的分類是否屬實（例如：確實驗證是否有遺漏 `JOIN` 節點），並拒絕不合格的聚類報告。

---

### ⚙️ 系統架構與檔案說明 (System Architecture)

評估平台由三個核心模組組成，採取單向資料流確保可重現性 (Reproducibility)：

| 檔案名稱 | 核心職責 | 說明細節 |
| :--- | :--- | :--- |
| `eval_pipeline.py` | 基準測試與指標計算 | 負責執行 SQL，將結果轉為 Pandas DataFrame 進行無視欄位別名的深度多重集比對，產出 `ex_score`。 |
| `cluster_analyzer.py` | 錯誤分群與 AST 驗證 | 讀取失敗案例，交由 LLM 進行分群，隨後使用 `sqlglot` 解析語法樹進行決定性驗證，過濾掉 LLM 的幻覺。 |
| `report_generator.py` | 報告生成 | 整合所有驗證過的指標與分群結果，自動生成最終的 Markdown 分析報告。 |

---

### 🚀 如何執行評估管線 (Usage)

> **[!] 重要提示**：所有指令請在專案根目錄 (`text_to_sql/`) 下執行，並使用 Python 的 `-m` 模組語法。

請確保你已經完成了資料庫的初始化 (`python -m part1.setup_db`)，接著依序執行以下指令：

**Step 1: 執行完整評估基準測試**
此步驟會載入 `eval_data.json`，對設定的模型進行查詢生成與執行比對。
```bash
python -m part2.eval_pipeline --data part2/eval_data.json --output part2/eval_results/
```

**Step 2: 執行錯誤聚類與驗證**
此步驟會分析 `failures.json` 中的錯誤模式，並透過 AST 驗證 LLM 提出的假設。
```bash
python -m part2.cluster_analyzer --output part2/eval_results/
```

**Step 3: 產生最終分析報告**
```bash
python -m part2.report_generator --output part2/eval_results/
```

---

### 📁 輸出產物 (Output Artifacts)

執行完畢後，`part2/eval_results/` 資料夾下會生成以下重要成品：

* 📄 `eval_results.csv`: 完整的逐題執行指標（包含 `ex_score`, Latency 等）。
* 🐞 `failures.json`: 所有執行失敗或 `ex_score < 1.0` 題目的詳細紀錄與錯誤類別。
* 📥 `clustering_input.json`: 提供給 LLM 進行分群的原始輸入資料。
* 📤 `clustering_output.json`: 經過 AST 驗證後的最終分群結果（包含被標記為 INVALID 的幻覺分群）。
* 🏆 **`EVAL_REPORT.md`**: 最終的綜合分析報告，這也是可以直接提交給評估團隊的核心文件。