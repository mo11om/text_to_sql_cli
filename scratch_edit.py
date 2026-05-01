import re

with open("part2/eval_pipeline.py", "r") as f:
    content = f.read()

# 1. Imports
content = content.replace("from collections import Counter\nfrom dataclasses import dataclass, field, asdict\nfrom datetime import datetime\nfrom typing import Any, Optional", "import pandas as pd\nfrom dataclasses import dataclass, field, asdict\nfrom datetime import datetime\nfrom typing import Any, Optional, Tuple, Dict")

# 2. EvalResult
old_fields = """    confidence: float = 0.0              # LLM 信心分數
    precision: float = 0.0              # Multiset Precision
    recall: float = 0.0                 # Multiset Recall
    f1: float = 0.0                     # F1 Score
    jaccard: float = 0.0                # Jaccard Index
    error_type: Optional[str] = None    # 錯誤類型（Execution Error / Result Overflow / ...）"""
new_fields = """    confidence: float = 0.0              # LLM 信心分數
    ex_score: float = 0.0                # Execution-Based Accuracy (1.0 or 0.0)
    error_type: Optional[str] = None    # 錯誤類型（Execution Error / Result Overflow / ...）"""
content = content.replace(old_fields, new_fields)

# 3. Execution logic
# Find start of Normalization
start_idx = content.find("# ═══════════════════════════════════════════════════════════════════════════════\n# § 結果正規化（Canonical Normalization — §2.3）")
# Find end of compute_metrics
end_marker = "    return {\n        \"precision\": round(precision, 4),\n        \"recall\": round(recall, 4),\n        \"f1\": round(f1, 4),\n        \"jaccard\": round(jaccard, 4),\n    }\n"
end_idx = content.find(end_marker, start_idx) + len(end_marker)

new_execution_logic = """# ═══════════════════════════════════════════════════════════════════════════════
# § 安全執行引擎與 Pandas 驗證 (Execution & Pandas Validation)
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_query_worker(db_path: str, sql: str, queue: multiprocessing.Queue) -> None:
    \"\"\"
    在獨立行程中執行 SQL，將結果讀入 Pandas DataFrame。
    \"\"\"
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
    \"\"\"執行 SQL 並加上硬超時機制\"\"\"
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
    \"\"\"
    比較 Ground Truth 與 Generated 的 Pandas DataFrame (Execution-Based Accuracy)。
    \"\"\"
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
"""

content = content[:start_idx] + new_execution_logic + content[end_idx:]

with open("part2/eval_pipeline.py", "w") as f:
    f.write(content)
