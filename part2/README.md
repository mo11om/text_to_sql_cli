太棒了！這份 Part 2 的實作結果非常完美，完全展現了 Staff Engineer 等級的系統設計思維。我們不僅完成了一個評估管線，還建立了一個「具備自我驗證能力、安全隔離且完全可重現」的評估平台。

以下我為你撰寫的 **Part 2 README 說明文件**。這份文件清楚解釋了我們的架構設計、核心邏輯以及如何執行這些程式碼，你可以直接將它複製並整合到你的專案 `README.md` 中。

***

## 📊 Part 2: Production-Grade Multi-Model Evaluation Platform

本專案的第二部分建立了一個可高度信任的 Text-to-SQL 多模型評估平台。有別於傳統基於字串比對 (String-matching) 的脆弱評估方式，本平台採用 **「執行結果比對 (Execution-based Semantic Correctness)」**，並結合 LLM 自動錯誤聚類與 **「決定性語法樹驗證 (Deterministic AST Validation)」**，確保評估報告的絕對客觀與準確。

### 🏗️ 核心工程設計與決策 (Key Engineering Decisions)

1. **多重集語意 (Multiset Semantics) 的精準評估**
   * SQL 的執行結果具有「背包語意 (Bag Semantics)」，代表重複的資料列是有意義的。
   * 我們**嚴格禁止**使用 Python 的 `set()` 進行結果比對，而是採用 `collections.Counter` 實作精確的交集與聯集運算。這確保了 Precision、Recall、F1 Score 與 Jaccard 相似度的數學正確性，並完美處理了 Ground Truth 或 Generated SQL 為空集合 (∅) 的邊界情況。

2. **進程隔離與安全防護 (Process Isolation & Safety Guards)**
   * 為了防止 LLM 生成笛卡兒積 (Cartesian Product) 等惡意或失控的 `JOIN` 語法導致系統卡死，所有 SQL 執行皆被封裝在獨立的 `multiprocessing.Process` 中。
   * 實作了 **5 秒強制超時 (Hard Timeout)** 與 **MAX_ROWS=100 的結果集上限防護**，確保評估管線的穩定性。

3. **雙重驗證機制 (Dual-System Validation)**
   * **GT 預先驗證：** 在評估啟動前，系統會強制執行所有 30 題的 Ground Truth SQL。若有任何一題拋出錯誤，管線會立即中止，確保基準答案 100% 可靠。
   * **AST 決定性驗證：** LLM 所生成的「錯誤分類 (Clusters)」往往伴隨幻覺。我們在 `cluster_analyzer.py` 中引入了 `sqlglot` 套件，透過解析抽象語法樹 (AST) 來**決定性地驗證** LLM 的分類是否屬實（例如：確實驗證是否有遺漏 `JOIN` 節點），並拒絕不合格的聚類報告。

---

### ⚙️ 系統架構與檔案說明 (System Architecture)

評估平台由三個核心模組組成，採取單向資料流確保可重現性 (Reproducibility)：

| 檔案名稱 | 核心職責 | 說明細節 |
| :--- | :--- | :--- |
| `eval_pipeline.py` | 基準測試與指標計算 | 負責執行 SQL、標準化資料 (NULL $\rightarrow$ None, 型別轉換)，並利用 `Counter` 計算嚴謹的評估分數。 |
| `cluster_analyzer.py` | 錯誤分群與 AST 驗證 | 讀取失敗案例，交由 LLM 進行分群，隨後使用 `sqlglot` 解析語法樹進行決定性驗證，過濾掉 LLM 的幻覺。 |
| `report_generator.py` | 報告生成 | 整合所有驗證過的指標與分群結果，自動生成最終的 Markdown 分析報告。 |

---

### 🚀 如何執行評估管線 (Usage)

請確保你已經完成了資料庫的初始化 (`python setup_db.py`)，接著依序執行以下指令：

**Step 1: 執行完整評估基準測試**
此步驟會載入 `eval_data.json`，對設定的模型進行查詢生成與執行比對。
```bash
python eval_pipeline.py --data eval_data.json --output eval_results/
```

**Step 2: 執行錯誤聚類與驗證**
此步驟會分析 `failures.json` 中的錯誤模式，並透過 AST 驗證 LLM 提出的假設。
```bash
python cluster_analyzer.py --output eval_results/
```

**Step 3: 產生最終分析報告**
```bash
python report_generator.py --output eval_results/
```

---

### 📁 輸出產物 (Output Artifacts)

執行完畢後，`eval_results/` 資料夾下會生成以下重要成品：

* 📄 `eval_results.csv`: 完整的逐題執行指標（包含 Precision, Recall, Latency 等）。
* 🐞 `failures.json`: 所有失敗題目的詳細紀錄與錯誤類別。
* 📥 `clustering_input.json`: 提供給 LLM 進行分群的原始輸入資料。
* 📤 `clustering_output.json`: 經過 AST 驗證後的最終分群結果（包含被標記為 INVALID 的幻覺分群）。
* ⏱️ `version.json`: 紀錄模型版本、提示詞版本與資料集版本，確保測試 100% 可重現。
* 🏆 **`EVAL_REPORT.md`**: 最終的綜合分析報告，這也是可以直接提交給評估團隊的核心文件。