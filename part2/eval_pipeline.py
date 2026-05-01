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
import sys

# Add part1 to sys.path so we can import its modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "part1")))

import time
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv

# ── 載入環境變數 ────────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "part1", ".env"))

# ── 常數設定 ────────────────────────────────────────────────────────────────────
MAX_ROWS = 100          # 結果行數上限，超過即標記為 Result Overflow
EXEC_TIMEOUT = 5        # 查詢執行硬超時（秒）
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "part1", "college_2.db"))


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
    precision: float = 0.0              # Multiset Precision
    recall: float = 0.0                 # Multiset Recall
    f1: float = 0.0                     # F1 Score
    jaccard: float = 0.0                # Jaccard Index
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
# § 結果正規化（Canonical Normalization — §2.3）
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_cell(value: Any) -> Any:
    """
    正規化單一儲存格值，確保比較一致性。

    規則：
    - None / NULL → None
    - 數值型別統一：int 與 float 等價（1 == 1.0）
    - 字串去除前後空白
    - 其他型別直接回傳
    """
    if value is None:
        return None
    if isinstance(value, float):
        # 若浮點數等於整數（如 1.0），轉為 int 以求一致
        if value == int(value):
            return int(value)
        return value
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_row(row: dict[str, Any]) -> tuple:
    """
    將一列資料正規化為可雜湊的 tuple。

    固定欄位排序（按欄位名字母序），確保欄位順序一致。
    """
    # 按欄位名排序，統一順序
    sorted_keys = sorted(row.keys())
    return tuple(normalize_cell(row[k]) for k in sorted_keys)


def normalize_results(rows: list[dict[str, Any]]) -> list[tuple]:
    """正規化整批結果為 tuple 列表"""
    return [normalize_row(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
# § 安全執行引擎（Process Isolation — §3）
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_in_process(
    db_path: str,
    sql: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """
    在獨立行程中執行 SQL（行程隔離，避免主行程阻塞或崩潰）。

    每次建立新的 SQLite 連線，執行後立即關閉。
    結果透過 Queue 回傳給主行程。
    """
    conn = None
    try:
        # 每次在子行程中建立新連線（唯讀模式）
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        result_queue.put(("OK", [dict(r) for r in rows]))
    except Exception as e:
        result_queue.put(("ERROR", f"{type(e).__name__}: {e}"))
    finally:
        if conn:
            conn.close()


def safe_execute(
    sql: str,
    db_path: str = DB_PATH,
    timeout: int = EXEC_TIMEOUT,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """
    安全執行 SQL：行程隔離 + 硬超時 + 結果大小護衛。

    Args:
        sql:      待執行的 SQL
        db_path:  SQLite 資料庫路徑
        timeout:  硬超時（秒）

    Returns:
        (rows, None)          — 成功，rows 為結果列表
        (None, error_string)  — 失敗，含錯誤型別與訊息
    """
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_execute_in_process,
        args=(db_path, sql, result_queue),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout)

    # 超時：強制終止子行程並清理
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)
        return None, "Timeout: 查詢執行超過 {timeout}s 硬上限"

    # 子行程已結束，取得結果
    if result_queue.empty():
        return None, "ProcessError: 子行程未回傳結果"

    status, payload = result_queue.get_nowait()

    if status == "ERROR":
        return None, payload

    rows = payload

    # §3.3 結果大小護衛
    if len(rows) > MAX_ROWS:
        return None, f"Result Overflow: 結果包含 {len(rows)} 行，超過上限 {MAX_ROWS}"

    return rows, None


# ═══════════════════════════════════════════════════════════════════════════════
# § Multiset 指標計算（§2.1 + §2.2 + §5.1）
# ═══════════════════════════════════════════════════════════════════════════════

def compute_metrics(
    gt_rows: list[tuple],
    gen_rows: list[tuple],
) -> dict[str, float]:
    """
    使用 multiset 語意計算 Precision / Recall / F1 / Jaccard。

    禁止使用 set()！全部使用 Counter 保留重複值。

    邊界情況（§2.1）：
    - GT=∅, GEN=∅  → P=1.0, R=1.0
    - GT≠∅, GEN=∅  → P=0.0, R=0.0
    - GT=∅, GEN≠∅  → P=0.0, R=1.0

    Returns:
        dict with keys: precision, recall, f1, jaccard
    """
    gt_empty = len(gt_rows) == 0
    gen_empty = len(gen_rows) == 0

    # 邊界情況處理（§2.1）
    if gt_empty and gen_empty:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "jaccard": 1.0}
    if gt_empty and not gen_empty:
        return {"precision": 0.0, "recall": 1.0, "f1": 0.0, "jaccard": 0.0}
    if not gt_empty and gen_empty:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "jaccard": 0.0}

    # Multiset 計算（§2.2 — 使用 Counter，禁止 set）
    gt_counter = Counter(gt_rows)
    gen_counter = Counter(gen_rows)

    # 交集：每個元素取 min(gt_count, gen_count)
    intersection = sum((gt_counter & gen_counter).values())
    # 聯集：每個元素取 max(gt_count, gen_count)
    union = sum((gt_counter | gen_counter).values())

    # Precision = 交集 / 生成結果數
    precision = intersection / len(gen_rows) if len(gen_rows) > 0 else 0.0
    # Recall = 交集 / 真值結果數
    recall = intersection / len(gt_rows) if len(gt_rows) > 0 else 0.0
    # F1
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    # Jaccard = 交集 / 聯集
    jaccard = intersection / union if union > 0 else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "jaccard": round(jaccard, 4),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# § Ground Truth 預先驗證（§2.4）
# ═══════════════════════════════════════════════════════════════════════════════

def validate_ground_truth(
    dataset: list[dict],
    db_path: str = DB_PATH,
) -> dict[int, list[tuple]]:
    """
    預先執行所有 Ground Truth SQL，驗證其正確性並快取結果。

    若任何 GT SQL 執行失敗 → 立即停止管線（RuntimeError）。

    Args:
        dataset:  eval_data.json 載入的題目列表
        db_path:  SQLite 資料庫路徑

    Returns:
        dict[id → normalized_rows]: 每題 GT 的正規化結果快取

    Raises:
        RuntimeError: 若任何 GT SQL 無法執行
    """
    cache: dict[int, list[tuple]] = {}
    failures: list[dict] = []

    for item in dataset:
        qid = item["id"]
        gt_sql = item["sql"]
        rows, error = safe_execute(gt_sql, db_path)

        if error is not None:
            failures.append({"id": qid, "sql": gt_sql, "error": error})
            continue

        cache[qid] = normalize_results(rows)

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
    gt_cache: dict[int, list[tuple]],
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
    from llm import SQLResponse
    from retry import RetryController, MAX_RETRY
    from validator import validate_sql, rewrite_sql

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
        gen_rows_raw, exec_error = safe_execute(safe_sql, db_path)
        exec_elapsed = (time.time() - exec_start) * 1000
        result.execution_latency_ms += exec_elapsed

        if exec_error is not None:
            last_error = exec_error
            current_sql = safe_sql

            # 判斷是否可重試
            import sqlite3 as _sqlite3
            mock_err = _sqlite3.OperationalError(exec_error)
            decision = retry_ctrl.should_retry(mock_err, safe_sql)

            if decision == "STOP" or retry_ctrl.retry_count >= MAX_RETRY:
                result.error_type = "Execution Error"
                result.error_detail = exec_error
                result.retry_count = retry_ctrl.retry_count
                break

            result.retry_count = retry_ctrl.retry_count
            continue  # 重試

        # ── 執行成功：multiset 比較 ──────────────────────────────────
        gen_rows = normalize_results(gen_rows_raw)
        metrics = compute_metrics(gt_rows, gen_rows)
        result.precision = metrics["precision"]
        result.recall = metrics["recall"]
        result.f1 = metrics["f1"]
        result.jaccard = metrics["jaccard"]

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
    from llm import LLMRouter
    import database

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
    success = sum(1 for r in results if r.f1 > 0)
    perfect = sum(1 for r in results if r.f1 == 1.0)
    avg_f1 = sum(r.f1 for r in results) / total if total > 0 else 0
    console.print(f"\n[bold green]📊 評估完成！[/bold green]")
    console.print(f"   總題數: {total}")
    console.print(f"   成功 (F1>0): {success}")
    console.print(f"   完美 (F1=1.0): {perfect}")
    console.print(f"   平均 F1: {avg_f1:.4f}")
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
        "confidence", "precision", "recall", "f1", "jaccard",
        "error_type", "error_detail", "retry_count", "success_after_retry",
        "llm_latency_ms", "execution_latency_ms", "total_latency_ms",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    # ── failures.json ─────────────────────────────────────────────────
    failures = [asdict(r) for r in results if r.f1 < 1.0]
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
        if r.f1 < 1.0  # 只對失敗題目做聚類分析
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
