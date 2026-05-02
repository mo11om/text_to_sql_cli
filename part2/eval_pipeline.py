"""
eval_pipeline.py
────────────────
生產等級 Text-to-SQL 評估管線。

核心原則：
- 分數的唯一真理是「執行結果 (Execution Results)」
- 使用 multiset 語意比較（Counter，禁用 set()）
- 行程隔離執行 (multiprocessing.Process) + 5 秒硬超時
- 地端真值 (Ground Truth) 預先驗證：任何 GT SQL 執行失敗即停止管線
- 完整版本記錄確保可重現性

使用方式：
    python eval_pipeline.py --data eval_data.json --output eval_results/
"""

import csv
import json
import multiprocessing
import os

import time
import sqlite3
import pandas as pd
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional, Tuple, Dict

from dotenv import load_dotenv

# ── 載入環境變數 ────────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ── 常數設定 ────────────────────────────────────────────────────────────────────
MAX_ROWS = 1000         # 結果行數上限，超過即標記為 Result Overflow
EXEC_TIMEOUT = 5        # 查詢執行硬超時（秒）
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "college_2.db"))


# ═══════════════════════════════════════════════════════════════════════════════
# § 資料模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EvalResult:
    """單筆查詢的完整評估結果"""
    id: int                              # 題目編號
    category: str                        # 題目分類（Basic / JOIN / Aggregation / Complex / Adversarial）
    nl: str                              # 自然語言查詢
    gt_sql: str                          # Ground Truth SQL
    gen_sql: Optional[str] = None        # LLM 生成的 SQL
    gen_status: str = ""                 # LLM 回傳狀態（SUCCESS / OUT_OF_SCOPE / ...）
    confidence: float = 0.0              # LLM 信心分數
    ex_score: float = 0.0                # Execution-Based Accuracy (1.0 or 0.0)
    error_type: Optional[str] = None    # 錯誤類型（Execution Error / Result Overflow / ...）
    error_detail: Optional[str] = None  # 錯誤明細
    retry_count: int = 0                # 重試次數
    success_after_retry: bool = False   # 是否在重試後成功
    llm_latency_ms: float = 0.0        # LLM 呼叫延遲（毫秒）
    execution_latency_ms: float = 0.0  # SQL 執行延遲（毫秒）
    total_latency_ms: float = 0.0      # 總延遲（毫秒）


@dataclass
class PipelineVersion:
    """管線執行版本資訊，確保可重現性"""
    model_name: str
    model_version: str
    prompt_version: str = "v5"
    schema_version: str = "college_2_v1"
    dataset_version: str = "eval_data_v1"
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# § 安全執行引擎與 Pandas 驗證 (Execution & Pandas Validation)
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_query_worker(db_path: str, sql: str, queue: multiprocessing.Queue) -> None:
    """
    在獨立行程中執行 SQL，將結果讀入 Pandas DataFrame。
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        start_time = time.time()
        df = pd.read_sql_query(sql, conn)
        exec_time_ms = (time.time() - start_time) * 1000
        
        if len(df) > MAX_ROWS:
            queue.put(("ERROR", "Result Overflow", f"結果包含 {len(df)} 行，超過上限 {MAX_ROWS}"))
        else:
            queue.put(("SUCCESS", df, exec_time_ms))
            
    except sqlite3.OperationalError as e:
        queue.put(("ERROR", "Syntax Error", str(e)))
    except sqlite3.DatabaseError as e:
        queue.put(("ERROR", "Database Error", str(e)))
    except Exception as e:
        queue.put(("ERROR", type(e).__name__, str(e)))
    finally:
        if conn:
            conn.close()

def _run_with_timeout(db_path: str, sql: str, timeout_sec: int) -> Tuple[str, Any, float]:
    """執行 SQL 並加上硬超時機制"""
    queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_execute_query_worker, 
        args=(db_path, sql, queue),
        daemon=True
    )
    
    proc.start()
    proc.join(timeout_sec)
    
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return "TIMEOUT", "執行超過硬上限", float(timeout_sec * 1000)
        
    if not queue.empty():
        status, payload, exec_time_ms = queue.get()
        return status, payload, exec_time_ms
        
    return "ERROR", "Process Failed to Return Data", 0.0

def compare_dataframes(df_gt: pd.DataFrame, df_gen: pd.DataFrame) -> dict:
    """
    比較 Ground Truth 與 Generated 的 Pandas DataFrame (Execution-Based Accuracy)。
    """
    if df_gt.empty and df_gen.empty:
        return {"ex_score": 1.0, "error_type": None}
        
    if df_gt.shape != df_gen.shape:
        return {"ex_score": 0.0, "error_type": "Shape Mismatch"}
        
    try:
        df_gt_sorted = df_gt.sort_values(by=df_gt.columns.tolist()).reset_index(drop=True)
        df_gen_sorted = df_gen.sort_values(by=df_gen.columns.tolist()).reset_index(drop=True)
        
        # Strip column names
        df_gt_sorted.columns = range(df_gt_sorted.shape[1])
        df_gen_sorted.columns = range(df_gen_sorted.shape[1])
        
        if df_gt_sorted.equals(df_gen_sorted):
            return {"ex_score": 1.0, "error_type": None}
        else:
            return {"ex_score": 0.0, "error_type": "Value Mismatch"}
            
    except Exception as e:
        return {"ex_score": 0.0, "error_type": f"Normalization Error: {type(e).__name__}"}


# ═══════════════════════════════════════════════════════════════════════════════
# § Ground Truth 預先驗證（§2.4）
# ═══════════════════════════════════════════════════════════════════════════════

def validate_ground_truth(
    dataset: list[dict],
    db_path: str = DB_PATH,
) -> dict[int, pd.DataFrame]:
    """
    預先執行所有 Ground Truth SQL，驗證其正確性並快取 Pandas DataFrame 結果。

    若任何 GT SQL 執行失敗 → 立即停止管線（RuntimeError）。
    """
    cache: dict[int, pd.DataFrame] = {}
    failures: list[dict] = []

    for item in dataset:
        qid = item["id"]
        gt_sql = item["sql"]
        status, payload, _ = _run_with_timeout(db_path, gt_sql, timeout_sec=10)

        if status != "SUCCESS":
            failures.append({"id": qid, "sql": gt_sql, "error": payload})
            continue

        cache[qid] = payload

    if failures:
        msg = "\n".join(
            f"  [ID={f['id']}] {f['error']}" for f in failures
        )
        raise RuntimeError(
            f"Ground Truth 驗證失敗！以下 {len(failures)} 筆 GT SQL 無法執行：\n{msg}\n"
            "管線已停止。請修正 eval_data.json 中的 GT SQL 後重試。"
        )

    return cache


# ═══════════════════════════════════════════════════════════════════════════════
# § 單筆查詢評估
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_single(
    item: dict,
    gt_cache: dict[int, pd.DataFrame],
    llm_router: Any,
    schema: str,
    db_path: str = DB_PATH,
) -> EvalResult:
    """
    對單筆題目執行完整評估流程。

    流程：LLM 生成 → 驗證 → 執行 → multiset 比較 → 計算指標 → 記錄延遲
    若執行失敗，進行收斂保護重試（最多 MAX_RETRY 次）。

    Args:
        item:       eval_data.json 中的單筆題目 dict
        gt_cache:   預先驗證的 Ground Truth 結果快取
        llm_router: LLMRouter 實例
        schema:     資料庫 DDL 字串
        db_path:    SQLite 路徑

    Returns:
        EvalResult: 完整評估結果
    """
    from part1.llm import SQLResponse
    from part1.retry import RetryController, MAX_RETRY
    from part1.validator import validate_sql, rewrite_sql

    qid = item["id"]
    category = item["category"]
    nl = item["nl"]
    gt_sql = item["sql"]
    gt_rows = gt_cache[qid]

    result = EvalResult(id=qid, category=category, nl=nl, gt_sql=gt_sql)
    retry_ctrl = RetryController()
    total_start = time.time()

    current_sql: Optional[str] = None
    last_error: Optional[str] = None

    while True:
        # ── LLM 生成 ──────────────────────────────────────────────────
        llm_start = time.time()
        try:
            response: SQLResponse = llm_router.generate_sql(
                nl_query=nl,
                schema=schema,
                previous_sql=current_sql,
                error_message=last_error,
            )
        except ValueError as e:
            result.error_type = "LLM Parse Error"
            result.error_detail = str(e)
            break
        except Exception as e:
            # 捕捉 APITimeoutError、網路錯誤等，不讓單題崩潰整條管線
            result.error_type = "LLM Timeout" if "timeout" in str(e).lower() else "LLM Error"
            result.error_detail = f"{type(e).__name__}: {e}"
            break
        llm_elapsed = (time.time() - llm_start) * 1000
        result.llm_latency_ms += llm_elapsed

        result.gen_status = response.status
        result.confidence = response.confidence

        # ── 非 SUCCESS 狀態 ───────────────────────────────────────────
        if response.status != "SUCCESS" or not response.sql:
            result.error_type = f"LLM: {response.status}"
            break

        candidate_sql = response.sql
        result.gen_sql = candidate_sql

        # ── SQL 驗證 ──────────────────────────────────────────────────
        try:
            validate_sql(candidate_sql)
        except ValueError as e:
            result.error_type = "Validation Error"
            result.error_detail = str(e)
            break

        # ── LIMIT 重寫 ────────────────────────────────────────────────
        safe_sql = rewrite_sql(candidate_sql)
        result.gen_sql = safe_sql

        # ── 安全執行 ──────────────────────────────────────────────────
        exec_start = time.time()
        status, payload, exec_elapsed = _run_with_timeout(db_path, safe_sql, timeout_sec=5)
        result.execution_latency_ms += exec_elapsed

        if status != "SUCCESS":
            last_error = payload
            current_sql = safe_sql

            # 判斷是否可重試
            import sqlite3 as _sqlite3
            mock_err = _sqlite3.OperationalError(payload)
            decision = retry_ctrl.should_retry(mock_err, safe_sql)

            if decision == "STOP" or retry_ctrl.retry_count >= MAX_RETRY:
                result.error_type = status if status == "TIMEOUT" else "Execution Error"
                result.error_detail = payload
                result.retry_count = retry_ctrl.retry_count
                break

            result.retry_count = retry_ctrl.retry_count
            continue  # 重試

        # ── 執行成功：比較 DataFrames ──────────────────────────────────
        df_gen = payload
        comp_result = compare_dataframes(gt_rows, df_gen)
        
        result.ex_score = comp_result["ex_score"]
        if comp_result["error_type"]:
            result.error_type = comp_result["error_type"]
            result.error_detail = "DataFrames do not match"

        if retry_ctrl.retry_count > 0:
            result.success_after_retry = True
        result.retry_count = retry_ctrl.retry_count
        break

    result.total_latency_ms = (time.time() - total_start) * 1000
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# § 主要管線入口
# ═══════════════════════════════════════════════════════════════════════════════

def run_evaluation(
    data_path: str,
    output_dir: str,
    db_path: str = DB_PATH,
) -> list[EvalResult]:
    """
    執行完整評估管線。

    步驟：
    1. 載入題目資料集
    2. 預先驗證所有 Ground Truth SQL
    3. 初始化 LLM Router
    4. 逐題評估（含重試）
    5. 儲存成品：eval_results.csv, failures.json, clustering_input.json

    Args:
        data_path:   eval_data.json 路徑
        output_dir:  輸出目錄路徑
        db_path:     SQLite 資料庫路徑

    Returns:
        list[EvalResult]: 所有題目的評估結果
    """
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
    from part1.llm import LLMRouter
    from part1 import database

    console = Console()
    os.makedirs(output_dir, exist_ok=True)

    # ① 載入題目
    console.print("[bold cyan]📂 載入評估資料集...[/bold cyan]")
    with open(data_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    console.print(f"   共 {len(dataset)} 筆題目")

    # ② 預先驗證 Ground Truth
    console.print("[bold cyan]🔍 驗證 Ground Truth SQL...[/bold cyan]")
    gt_cache = validate_ground_truth(dataset, db_path)
    console.print(f"   ✅ 全部 {len(gt_cache)} 筆 GT SQL 驗證通過")

    # ③ 初始化 LLM
    console.print("[bold cyan]🤖 初始化 LLM Router...[/bold cyan]")
    llm = LLMRouter()
    schema = database.get_schema()

    # 版本資訊
    version = PipelineVersion(
        model_name=llm.provider,
        model_version=llm.model,
    )

    # ④ 逐題評估
    results: list[EvalResult] = []
    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("評估中...", total=len(dataset))
        for item in dataset:
            # 外層安全網：任何單題錯誤都不會中斷整條管線
            try:
                result = evaluate_single(item, gt_cache, llm, schema, db_path)
            except Exception as e:
                # 最後防線：無論什麼例外都記錄為失敗並繼續
                result = EvalResult(
                    id=item["id"],
                    category=item["category"],
                    nl=item["nl"],
                    gt_sql=item["sql"],
                    error_type="LLM Timeout" if "timeout" in str(e).lower() else "Pipeline Error",
                    error_detail=f"{type(e).__name__}: {str(e)[:200]}",
                )
                console.print(f"\n   [yellow]⚠️  #{item['id']} 失敗: {type(e).__name__}，繼續下一題[/yellow]")
            results.append(result)
            progress.update(task, advance=1, description=f"[#{item['id']}] {item['nl'][:30]}...")
            # 題目間冷卻 1 秒，避免 Ollama 連線耗盡
            time.sleep(1)

    # ⑤ 儲存成品
    console.print("[bold cyan]💾 儲存評估成品...[/bold cyan]")
    _save_artifacts(results, version, output_dir)

    # 摘要統計
    total = len(results)
    success = sum(1 for r in results if r.ex_score > 0)
    perfect = sum(1 for r in results if r.ex_score == 1.0)
    avg_ex = sum(r.ex_score for r in results) / total if total > 0 else 0
    console.print(f"\n[bold green]📊 評估完成！[/bold green]")
    console.print(f"   總題數: {total}")
    console.print(f"   成功 (EX Score>0): {success}")
    console.print(f"   完美 (EX Score=1.0): {perfect}")
    console.print(f"   平均 EX Score: {avg_ex:.4f}")
    console.print(f"   成品目錄: {output_dir}/")

    return results


def _save_artifacts(
    results: list[EvalResult],
    version: PipelineVersion,
    output_dir: str,
) -> None:
    """
    儲存三項管線成品。

    1. eval_results.csv — 完整評估結果（含所有指標與延遲）
    2. failures.json    — 失敗題目的詳細資訊
    3. clustering_input.json — 餵給 cluster_analyzer 的精簡輸入
    """
    # ── eval_results.csv ──────────────────────────────────────────────
    csv_path = os.path.join(output_dir, "eval_results.csv")
    fieldnames = [
        "id", "category", "nl", "gt_sql", "gen_sql", "gen_status",
        "confidence", "ex_score",
        "error_type", "error_detail", "retry_count", "success_after_retry",
        "llm_latency_ms", "execution_latency_ms", "total_latency_ms",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    # ── failures.json ─────────────────────────────────────────────────
    failures = [asdict(r) for r in results if r.ex_score < 1.0]
    failures_path = os.path.join(output_dir, "failures.json")
    with open(failures_path, "w", encoding="utf-8") as f:
        json.dump(failures, f, ensure_ascii=False, indent=2)

    # ── clustering_input.json（§6.3 只傳 nl / generated_sql / error_type）
    clustering_input = [
        {
            "id": r.id,
            "nl": r.nl,
            "generated_sql": r.gen_sql,
            "error_type": r.error_type,
        }
        for r in results
        if r.ex_score < 1.0  # 只對失敗題目做聚類分析
    ]
    clustering_path = os.path.join(output_dir, "clustering_input.json")
    with open(clustering_path, "w", encoding="utf-8") as f:
        json.dump(clustering_input, f, ensure_ascii=False, indent=2)

    # ── version.json（§9 完整版本記錄）────────────────────────────────
    version_path = os.path.join(output_dir, "version.json")
    with open(version_path, "w", encoding="utf-8") as f:
        json.dump(asdict(version), f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# § CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    CLI 入口。

    用法：
        python eval_pipeline.py --data eval_data.json --output eval_results/
        python eval_pipeline.py  （使用預設路徑）
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Hardened Text-to-SQL 評估管線",
    )
    parser.add_argument(
        "--data", default="eval_data.json",
        help="評估資料集路徑（預設: eval_data.json）",
    )
    parser.add_argument(
        "--output", default="eval_results",
        help="輸出目錄路徑（預設: eval_results/）",
    )
    parser.add_argument(
        "--db", default=DB_PATH,
        help="SQLite 資料庫路徑",
    )
    args = parser.parse_args()

    # 搬移 eval_data.json 到 text_to_sql 目錄（若路徑不在當前目錄）
    data_path = args.data
    if not os.path.isabs(data_path) and not os.path.exists(data_path):
        # 嘗試從上層目錄找
        parent = os.path.join(os.path.dirname(__file__), "..", data_path)
        if os.path.exists(parent):
            data_path = parent

    run_evaluation(data_path, args.output, args.db)


if __name__ == "__main__":
    main()
