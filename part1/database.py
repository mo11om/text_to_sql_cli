"""
database.py
-----------
唯讀 SQLite 執行引擎與資料庫綱要自省模組。

安全設計：
- 使用 URI 模式以唯讀方式開啟資料庫 (?mode=ro)
- 不允許任何寫入操作（INSERT / UPDATE / DELETE / DROP）
- 回傳結果為 list[dict]，避免暴露底層 cursor 物件
"""

import sqlite3
import os
from typing import Any

# ── 資料庫路徑 ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "college_2.db")

# ── URI 模式：唯讀連線字串 ─────────────────────────────────────────────────────
# ?mode=ro 確保 SQLite 不允許任何寫入操作
DB_URI = f"file:{DB_PATH}?mode=ro"


def _get_connection() -> sqlite3.Connection:
    """
    建立並回傳一個唯讀的 SQLite 連線。

    使用 uri=True 啟用 URI 模式，
    搭配 ?mode=ro 參數強制唯讀，防止資料被竄改。

    Returns:
        sqlite3.Connection: 唯讀資料庫連線物件

    Raises:
        FileNotFoundError: 若資料庫檔案不存在
        sqlite3.OperationalError: 若無法開啟唯讀連線
    """
    # 先確認資料庫檔案存在，給出友善錯誤訊息
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"資料庫檔案不存在: {DB_PATH}\n"
            "請先執行 `python setup_db.py` 建立資料庫。"
        )
    # 以唯讀 URI 模式開啟
    conn = sqlite3.connect(DB_URI, uri=True)
    # 讓查詢結果以字典方式存取 (column_name → value)
    conn.row_factory = sqlite3.Row
    return conn


def get_schema() -> str:
    """
    從 sqlite_master 取得所有資料表的 DDL 定義，
    格式化後作為 LLM Prompt 的 schema context 使用。

    Returns:
        str: 所有資料表 CREATE 語句的字串，以換行分隔

    Example:
        >>> schema = get_schema()
        >>> print(schema[:200])
        -- Table: classroom
        CREATE TABLE classroom ( ...
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        # 查詢所有使用者建立的資料表（排除 SQLite 內部資料表）
        cursor.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return "-- (無資料表 / No tables found)"

        # 格式化輸出：每個資料表加上標題註解，方便 LLM 閱讀
        parts: list[str] = []
        for row in rows:
            table_name = row["name"]
            ddl = row["sql"] or ""
            parts.append(f"-- Table: {table_name}\n{ddl};")

        return "\n\n".join(parts)
    finally:
        conn.close()


def execute_query(sql: str) -> list[dict[str, Any]]:
    """
    在唯讀 SQLite 連線上執行一條 SELECT 查詢，回傳結果列表。

    注意：此函式不負責驗證 SQL 安全性，
    驗證工作由上游的 validator.py 負責，
    本函式只負責執行並整齊地回傳結果。

    Args:
        sql: 已通過驗證的 SELECT SQL 語句

    Returns:
        list[dict]: 每列資料以 {欄位名: 值} 形式回傳

    Raises:
        sqlite3.OperationalError: 查詢語法錯誤或欄位/資料表不存在時拋出
        FileNotFoundError: 資料庫檔案不存在時拋出
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        # 將 sqlite3.Row 物件轉為普通 dict，方便後續序列化處理
        return [dict(row) for row in rows]
    finally:
        # 確保連線一定會被關閉，即使發生例外
        conn.close()


def list_tables() -> list[str]:
    """
    列出資料庫中所有使用者資料表的名稱。

    Returns:
        list[str]: 資料表名稱列表，按字母排序

    主要用途：測試與診斷，確認資料庫已正確初始化。
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        return [row["name"] for row in cursor.fetchall()]
    finally:
        conn.close()
