"""
retry.py
--------
錯誤分類器與收斂保護型重試控制器。

設計原則：
- 僅在 sqlite3.OperationalError 時觸發重試（語法/欄位/資料表問題可修正）
- 權限錯誤或未知錯誤立即停止（STOP），不浪費 API 配額
- 收斂護衛 #1：若新 SQL 與前次 SQL 相同 → 停止（LLM 卡住了）
- 收斂護衛 #2：若相同錯誤模式重複出現 ≥2 次 → 停止（無法修正）
- MAX_RETRY=3 硬上限確保不無限循環
"""

import os
import re
import sqlite3
from typing import Literal


# ── 可重試 / 停止 型別標注 ─────────────────────────────────────────────────────
RetryDecision = Literal["RETRY", "STOP"]

# ── 最大重試次數（可由環境變數覆蓋）─────────────────────────────────────────────
MAX_RETRY: int = int(os.getenv("MAX_RETRY", "3"))

# ── 錯誤模式分類（只有這些才可重試）─────────────────────────────────────────────
# 語法錯誤、欄位不存在、資料表不存在 → LLM 可能可以修正
_RETRIABLE_PATTERNS = [
    re.compile(r"syntax error", re.IGNORECASE),
    re.compile(r"no such column", re.IGNORECASE),
    re.compile(r"no such table", re.IGNORECASE),
    re.compile(r"ambiguous column", re.IGNORECASE),
    re.compile(r"unrecognized token", re.IGNORECASE),
]

# ── 絕對停止模式（出現即停止，不管類型）────────────────────────────────────────
_STOP_PATTERNS = [
    re.compile(r"unable to open database", re.IGNORECASE),
    re.compile(r"attempt to write", re.IGNORECASE),
    re.compile(r"readonly database", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
]


def classify_error(error: Exception) -> RetryDecision:
    """
    根據例外類型與錯誤訊息決定是否重試。

    分類邏輯：
    - 非 OperationalError → STOP（無法自動修正）
    - 含停止模式 → STOP（寫入嘗試/權限問題）
    - 含可重試模式 → RETRY（語法/結構問題，LLM 可修正）
    - 無法判斷 → STOP（保守策略：不確定就停止）

    Args:
        error: 捕獲到的例外

    Returns:
        "RETRY" 或 "STOP"
    """
    # 只有 OperationalError 才考慮重試
    if not isinstance(error, sqlite3.OperationalError):
        return "STOP"

    msg = str(error).lower()

    # 先檢查停止模式（優先於可重試模式）
    for pattern in _STOP_PATTERNS:
        if pattern.search(msg):
            return "STOP"

    # 再檢查可重試模式
    for pattern in _RETRIABLE_PATTERNS:
        if pattern.search(msg):
            return "RETRY"

    # 無法歸類的 OperationalError → 保守停止
    return "STOP"


def extract_error_fingerprint(error_message: str) -> str:
    """
    從錯誤訊息中提取「指紋」，用於收斂護衛 #2 的重複偵測。

    原理：只取錯誤類型部分（去掉具體的欄位名或值），
    讓相似錯誤（如不同欄位的 "no such column" 錯誤）可以被識別為同類。

    Args:
        error_message: 完整錯誤訊息字串

    Returns:
        str: 錯誤類型指紋（小寫）

    Examples:
        "no such column: gpa"     → "no such column"
        "no such column: grade"   → "no such column"（與上面相同指紋）
        "syntax error near 'FROM'" → "syntax error"
    """
    msg = error_message.lower()
    for pattern in _RETRIABLE_PATTERNS:
        m = pattern.search(msg)
        if m:
            return m.group(0).lower()
    return msg[:40]  # 未知錯誤取前40字元作為指紋


class RetryController:
    """
    收斂保護型重試控制器。

    職責：
    1. 追蹤重試次數 → 超過 MAX_RETRY 停止
    2. 收斂護衛 #1：偵測 SQL 無變化（LLM 卡住）→ 停止
    3. 收斂護衛 #2：偵測相同錯誤指紋重複 ≥2 次 → 停止

    使用方式：
        controller = RetryController()
        decision = controller.should_retry(error, new_sql)
        if decision == "STOP":
            break
    """

    def __init__(self) -> None:
        # 重試計數器
        self.retry_count: int = 0
        # 上一次執行的 SQL（用於收斂護衛 #1）
        self._last_sql: str = ""
        # 錯誤指紋出現次數（用於收斂護衛 #2）
        self._error_fingerprints: dict[str, int] = {}

    def should_retry(
        self,
        error: Exception,
        new_sql: str,
    ) -> RetryDecision:
        """
        決定是否繼續重試。

        按順序檢查以下停止條件：
        ① 超過最大重試次數
        ② 錯誤類型不可重試（classify_error 判斷）
        ③ 收斂護衛 #1：新 SQL 與前次相同
        ④ 收斂護衛 #2：相同錯誤指紋出現 ≥2 次

        Args:
            error:   觸發重試的例外
            new_sql: LLM 本次生成的 SQL

        Returns:
            "RETRY" 繼續 / "STOP" 放棄
        """
        error_msg = str(error)

        # ① 最大重試次數限制
        if self.retry_count >= MAX_RETRY:
            return "STOP"

        # ② 錯誤類型不可重試
        if classify_error(error) == "STOP":
            return "STOP"

        # ③ 收斂護衛 #1：SQL 無變化（LLM 沒有修正）
        if new_sql and new_sql == self._last_sql:
            return "STOP"

        # ④ 收斂護衛 #2：相同錯誤指紋重複 ≥2 次
        fingerprint = extract_error_fingerprint(error_msg)
        count = self._error_fingerprints.get(fingerprint, 0) + 1
        self._error_fingerprints[fingerprint] = count
        if count >= 2:
            return "STOP"

        # 通過所有檢查 → 繼續重試
        self.retry_count += 1
        self._last_sql = new_sql
        return "RETRY"

    def reset(self) -> None:
        """重置控制器狀態（用於新的查詢請求）"""
        self.retry_count = 0
        self._last_sql = ""
        self._error_fingerprints = {}
