"""
app.py
------
Hardened Text-to-SQL CLI 主程式入口。

執行流程：
User Input → [Query Normalizer] → [LLM Router] → [SQL Validator]
→ [Query Rewriter] → [Execution Engine] → [Error Classifier]
→ [Retry Controller] → [Structured Logger] → [Rich Renderer]

使用方式：
    python app.py "List all students in Computer Science"
    python app.py "列出修超過平均課程數的學生"
"""

import json
import sqlite3
import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from part1 import database
from part1.llm import SQLResponse
from part1.mac_agent import MACSQLPipeline
from part1.retry import RetryController
from part1.validator import validate_sql, rewrite_sql

# ── Rich 終端輸出物件 ──────────────────────────────────────────────────────────
console = Console()


# ── 結構化日誌輸出 ─────────────────────────────────────────────────────────────
def log_event(
    query: str,
    sql: str | None,
    status: str,
    retry_count: int,
    error: str | None = None,
) -> None:
    """
    以結構化 JSON 格式輸出每次查詢的完整日誌。

    記錄欄位：timestamp / query / sql / status / retry_count / error
    """
    record = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "sql": sql,
        "status": status,
        "retry_count": retry_count,
        "error": error,
    }
    console.print_json(json.dumps(record, ensure_ascii=False))


# ── Rich 表格渲染 ──────────────────────────────────────────────────────────────
def render_results(rows: list[dict], sql: str) -> None:
    """
    以 Rich Table 美化呈現查詢結果。

    若結果為空，顯示提示訊息。
    欄位名稱自動從第一行資料提取。

    Args:
        rows: execute_query 回傳的 list[dict]
        sql:  已執行的 SQL（顯示於標題）
    """
    console.print()

    if not rows:
        console.print(
            Panel("[yellow]查詢成功，但沒有符合的資料。[/yellow]",
                  title="🔍 查詢結果", border_style="yellow")
        )
        return

    # 建立 Rich 表格
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        border_style="bright_blue",
        title=f"[bold green]✅ 查詢結果 ({len(rows)} 筆)[/bold green]",
    )

    # 從第一筆資料自動提取欄位名稱
    columns = list(rows[0].keys())
    for col in columns:
        table.add_column(col, style="white", overflow="fold")

    # 填入資料列
    for row in rows:
        table.add_row(*[str(row.get(col, "")) for col in columns])

    console.print(table)

    # 顯示執行的 SQL（可 debug 用）
    console.print(
        Panel(
            f"[dim]{sql}[/dim]",
            title="[dim]執行的 SQL[/dim]",
            border_style="dim",
        )
    )


# ── 查詢正規化 ─────────────────────────────────────────────────────────────────
def normalize_query(query: str) -> str:
    """
    正規化使用者輸入：移除多餘空白，統一為單行格式。

    Args:
        query: 原始使用者輸入

    Returns:
        str: 清理後的查詢字串
    """
    return " ".join(query.split()).strip()


# ── 主要查詢處理函式 ───────────────────────────────────────────────────────────
def process_query(nl_query: str) -> None:
    """
    完整的查詢處理管線。

    步驟：
    1. 正規化輸入
    2. 取得資料庫綱要
    3. LLM 生成 SQL
    4. 驗證 SQL 安全性
    5. 重寫 SQL（附加 LIMIT）
    6. 執行查詢
    7. 若執行失敗：錯誤分類 → 收斂保護重試
    8. 結構化日誌 + Rich 渲染

    Args:
        nl_query: 使用者的自然語言查詢字串
    """
    # ① 正規化輸入
    query = normalize_query(nl_query)
    if not query:
        console.print("[red]錯誤：查詢不可為空白。[/red]")
        return

    console.print(
        Panel(
            f"[bold]{query}[/bold]",
            title="🧠 Text-to-SQL 查詢",
            border_style="bright_blue",
        )
    )

    # ② 初始化元件
    try:
        mac_pipeline = MACSQLPipeline()
    except EnvironmentError as e:
        console.print(f"[red]⚠️  環境設定錯誤: {e}[/red]")
        log_event(query, None, "CONFIG_ERROR", 0, str(e))
        return

    retry_ctrl = RetryController()
    schema = database.get_schema()

    # Phase 1: Selector Agent
    with console.status("[cyan]🤖 Agent 1: Selector 正在過濾 Schema...[/cyan]"):
        pruned_schema = mac_pipeline.run_selector(query, schema)

    current_sql: str | None = None
    last_error: str | None = None

    # ---------- 查詢生成與執行迴圈 ----------
    while True:
        # Phase 2 & 3: Decomposer / Refiner Agent
        with console.status("[cyan]🤖 Agent 2/3: 正在生成/修正 SQL...[/cyan]"):
            try:
                if last_error and current_sql:
                    response: SQLResponse = mac_pipeline.run_refiner(
                        pruned_schema=pruned_schema,
                        previous_sql=current_sql,
                        error_message=last_error,
                    )
                else:
                    response: SQLResponse = mac_pipeline.run_decomposer(
                        nl_query=query,
                        pruned_schema=pruned_schema,
                    )
            except ValueError as e:
                # LLM 輸出無法解析
                console.print(f"[red]❌ LLM 輸出解析失敗: {e}[/red]")
                log_event(query, current_sql, "LLM_PARSE_ERROR",
                          retry_ctrl.retry_count, str(e))
                return

        # ④ 處理非 SUCCESS 狀態
        if response.status != "SUCCESS" or not response.sql:
            console.print(
                f"[yellow]⚠️  LLM 回應: {response.status}[/yellow]\n"
                f"[dim]信心分數: {response.confidence:.2f}[/dim]"
            )
            log_event(query, None, response.status,
                      retry_ctrl.retry_count, None)
            return

        candidate_sql = response.sql

        # ⑤ 驗證 SQL 安全性
        try:
            validate_sql(candidate_sql)
        except ValueError as e:
            # 驗證失敗的 SQL 不執行也不重試（可能是惡意生成）
            console.print(f"[red]🚫 SQL 驗證失敗: {e}[/red]")
            log_event(query, candidate_sql, "VALIDATION_ERROR",
                      retry_ctrl.retry_count, str(e))
            return

        # ⑥ 重寫 SQL（附加 LIMIT 保護）
        safe_sql = rewrite_sql(candidate_sql)
        current_sql = safe_sql

        # ⑦ 執行查詢
        try:
            rows = database.execute_query(safe_sql)
            # 執行成功：記錄日誌並渲染結果
            log_event(query, safe_sql, "SUCCESS",
                      retry_ctrl.retry_count, None)
            render_results(rows, safe_sql)
            return

        except sqlite3.OperationalError as exec_err:
            last_error = str(exec_err)
            console.print(
                f"[red]❌ 執行錯誤 (第 {retry_ctrl.retry_count + 1} 次): "
                f"{last_error}[/red]"
            )

            # ⑧ 收斂保護重試決策
            decision = retry_ctrl.should_retry(exec_err, safe_sql)
            if decision == "STOP":
                console.print(
                    "[red]🛑 已達到重試上限或收斂停止條件，放棄查詢。[/red]"
                )
                log_event(query, safe_sql, "FAILED",
                          retry_ctrl.retry_count, last_error)
                return

            console.print(
                f"[yellow]🔄 重試第 {retry_ctrl.retry_count}/{retry_ctrl.__class__.__module__} 次...[/yellow]"
            )

        except Exception as unexpected_err:
            # 未預期的例外：立即停止
            error_msg = str(unexpected_err)
            console.print(f"[red]💥 未預期錯誤: {error_msg}[/red]")
            log_event(query, safe_sql, "UNEXPECTED_ERROR",
                      retry_ctrl.retry_count, error_msg)
            return


# ── CLI 入口點 ─────────────────────────────────────────────────────────────────
def main() -> None:
    """
    CLI 入口點。

    使用方式：
        python app.py "your natural language query"

    若未提供查詢，顯示使用說明並退出。
    """
    console.print(
        Panel(
            "[bold cyan]🔒 Hardened Text-to-SQL CLI[/bold cyan]\n"
            "[dim]安全、可觀測、多 LLM 支援的自然語言轉 SQL 系統[/dim]",
            border_style="cyan",
        )
    )

    if len(sys.argv) < 2:
        console.print(
            "[yellow]使用方式: python app.py \"your query here\"[/yellow]\n"
            "[dim]範例: python app.py \"List all students in Computer Science\"[/dim]"
        )
        sys.exit(1)

    # 支援帶空格的查詢（多個 argv 合併）
    nl_query = " ".join(sys.argv[1:])
    process_query(nl_query)


if __name__ == "__main__":
    main()
