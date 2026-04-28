"""
validator.py
------------
SQL 安全驗證層與智慧 LIMIT 重寫器。

防禦目標：
1. 只允許 SELECT 查詢（子查詢和 UNION 皆可）
2. 封鎖所有資料變更與 DDL 關鍵字
3. 禁止 SQL 註解（防注入攻擊）
4. 禁止多條語句（防止語句串接攻擊）
5. 自動附加 LIMIT 上限，防止全表掃描
"""

import re

# ── 禁止关键字清单（任何一个出現就直接拒絕）─────────────────────────────────────
# 涵蓋所有資料變更、DDL 與危險 pragma 操作
_FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "pragma",
    "create", "replace", "truncate", "attach", "detach",
]

# ── 編譯正則式（只編譯一次，提升效能）────────────────────────────────────────────
# 匹配行內單行註解 (--) 或多行區塊註解 (/* ... */)
_COMMENT_RE = re.compile(r"(--)|(/\*)", re.IGNORECASE)

# 匹配查詢中間的分號（字串結尾的分號不算）
# 例如 "SELECT 1; DROP TABLE" → 危險；"SELECT 1;" → 安全
_MULTI_STMT_RE = re.compile(r";(?!\s*$)", re.IGNORECASE)

# 匹配 LIMIT 子句出現在查詢結尾（允許 ORDER BY ... LIMIT N ... OFFSET M）
# 使用 IGNORECASE + MULTILINE 以應對各種格式
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)

# 預設行數上限（避免 LLM 生成大量資料撈取）
DEFAULT_LIMIT = 100


def validate_sql(sql: str) -> None:
    """
    驗證 SQL 安全性，若有問題則拋出 ValueError。

    規則（全部符合才通過）：
    ① 必須以 SELECT 開頭（lstrip 後的小寫）
    ② 不含任何禁用關鍵字
    ③ 不含 SQL 註解 (-- 或 /* */)
    ④ 不含多條語句（中間不含 ;）

    Args:
        sql: 待驗證的 SQL 字串

    Raises:
        ValueError: 任何規則不符時拋出，附帶說明訊息
    """
    # ① 必須是 SELECT 語句（允許前置空白）
    normalized = sql.strip().lower()
    if not normalized.startswith("select"):
        raise ValueError(
            f"[驗證失敗] 只允許 SELECT 查詢。收到: '{sql[:50]}...'"
        )

    # ② 封鎖禁用關鍵字
    # 使用單字邊界 \b 避免誤判（例如 "update" 在欄位名中）
    for kw in _FORBIDDEN_KEYWORDS:
        pattern = re.compile(rf"\b{kw}\b", re.IGNORECASE)
        if pattern.search(sql):
            raise ValueError(
                f"[驗證失敗] 禁用關鍵字 '{kw.upper()}' 出現在查詢中。"
            )

    # ③ 封鎖 SQL 註解（防注入攻擊載體）
    if _COMMENT_RE.search(sql):
        raise ValueError(
            "[驗證失敗] 查詢含有 SQL 註解 (-- 或 /* */)，已被封鎖。"
        )

    # ④ 封鎖多條語句（防語句串接攻擊，結尾分號可接受）
    if _MULTI_STMT_RE.search(sql):
        raise ValueError(
            "[驗證失敗] 查詢含有多條語句 (;分號在中間)，已被封鎖。"
        )


def rewrite_sql(sql: str) -> str:
    """
    智慧 LIMIT 重寫器：若查詢尚無 LIMIT 子句，自動在結尾附加。

    設計原則：
    - 若已有 LIMIT → 保留不變（尊重 LLM 意圖）
    - 若無 LIMIT  → 安全附加 LIMIT 100
    - 不破壞 ORDER BY ... LIMIT N 的語法正確性
    - 先移除結尾分號再附加，最終不帶分號（SQLite 不需要）

    Args:
        sql: 已通過 validate_sql 的 SELECT 語句

    Returns:
        str: 帶有 LIMIT 保護的 SQL 語句
    """
    # 移除結尾空白與分號，統一格式
    cleaned = sql.strip().rstrip(";").strip()

    # 若已包含 LIMIT 子句，直接回傳（不重複添加）
    if _LIMIT_RE.search(cleaned):
        return cleaned

    # 安全附加預設 LIMIT
    return f"{cleaned} LIMIT {DEFAULT_LIMIT}"
