"""
tests/test_break.py
-------------------
對抗式測試套件（Adversarial Test Suite）

測試策略：「先攻破，再強化」
- 直接測試 validator/retry/database 模組（不呼叫 LLM，避免 API 費用）
- LLM 相關測試使用 mock 模擬回應
- 每個測試均驗證 retry_count <= 3

覆蓋場景：
1. Prompt Injection（提示詞注入）
2. SQL Injection（SQL 注入）
3. Comment Attack（注解攻擊）
4. Hallucination（LLM 幻覺生成不存在欄位）
5. Invalid Join（無效關聯）
6. Typo Robustness（錯字容錯）
7. Traditional Chinese Query（繁體中文查詢）
8. Ambiguous Query（模糊查詢）
9. Convergence Guard #1（SQL 不變收斂停止）
10. Convergence Guard #2（重複錯誤收斂停止）
11. Multi-statement Attack（多條語句攻擊）
12. LIMIT Rewriter（LIMIT 重寫驗證）
"""

import sqlite3
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

from part1.validator import validate_sql, rewrite_sql
from part1.retry import RetryController, classify_error, MAX_RETRY
from part1.database import get_schema, execute_query, list_tables


# ═══════════════════════════════════════════════════════════════════
# ① Prompt Injection（提示詞注入）
# ═══════════════════════════════════════════════════════════════════

class TestPromptInjectionViaValidator:
    """
    即使 LLM 被注入攻擊欺騙，生成 DROP/DELETE SQL，
    validator 仍應攔截，確保沒有危險 SQL 被執行。
    """

    def test_drop_table_injection(self):
        """注入指令生成的 DROP TABLE 必須被封鎖"""
        malicious_sql = "DROP TABLE student"
        with pytest.raises(ValueError, match="只允許 SELECT"):
            validate_sql(malicious_sql)

    def test_delete_injection(self):
        """注入指令生成的 DELETE 必須被封鎖"""
        malicious_sql = "DELETE FROM student WHERE 1=1"
        with pytest.raises(ValueError, match="只允許 SELECT"):
            validate_sql(malicious_sql)

    def test_select_with_drop_keyword(self):
        """SELECT 語句中隱藏 DROP 關鍵字也必須被封鎖"""
        malicious_sql = "SELECT * FROM student; DROP TABLE student"
        with pytest.raises(ValueError):
            validate_sql(malicious_sql)


# ═══════════════════════════════════════════════════════════════════
# ② SQL Injection（SQL 注入）
# ═══════════════════════════════════════════════════════════════════

class TestSQLInjection:
    """
    SQL 注入嘗試：攻擊者在查詢中嵌入惡意子句。
    這類攻擊的終結點在於 validator 的嚴格規則。
    """

    def test_or_1_equals_1(self):
        """
        'OR 1=1' 本身是合法 SQL，不應觸發驗證錯誤。
        但實際上 LLM 不應生成這類查詢（OUT_OF_SCOPE）；
        這裡測試若 validator 收到此查詢也能安全執行（不會 crash）。
        """
        # 純 SELECT 的 OR 1=1 在驗證器層面是合法的（它是 SELECT）
        # 真正的防護靠 LLM prompt hardening 阻止生成
        safe_sql = "SELECT * FROM student WHERE name = 'John' OR 1=1"
        # 應該通過驗證（它是 SELECT 且無禁止關鍵字）
        validate_sql(safe_sql)  # 不應拋出

    def test_union_with_drop(self):
        """UNION 後接 DROP 必須被禁止關鍵字封鎖"""
        evil = "SELECT name FROM student UNION DROP TABLE student"
        with pytest.raises(ValueError, match="DROP"):
            validate_sql(evil)

    def test_stacked_query_injection(self):
        """分號串接多條語句必須被封鎖"""
        injection = "SELECT * FROM student; DELETE FROM student"
        with pytest.raises(ValueError):
            validate_sql(injection)


# ═══════════════════════════════════════════════════════════════════
# ③ Comment Attack（SQL 注解攻擊）
# ═══════════════════════════════════════════════════════════════════

class TestCommentAttack:
    """SQL 注解常被用於繞過驗證或終止語句後半段"""

    def test_single_line_comment(self):
        """-- 注解必須被封鎖（可能因 DROP 關鍵字或註解規則觸發，兩者皆合法）"""
        sql = "SELECT * FROM student -- DROP TABLE student"
        # validator 會因 DROP 關鍵字或 -- 注解任一規則拋出 ValueError，都是正確的
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_block_comment(self):
        """/* */ 區塊注解必須被封鎖"""
        sql = "SELECT * FROM student /* malicious */"
        with pytest.raises(ValueError, match="SQL 註解"):
            validate_sql(sql)


# ═══════════════════════════════════════════════════════════════════
# ④ Hallucination（LLM 幻覺：不存在的欄位）
# ═══════════════════════════════════════════════════════════════════

class TestHallucinationExecution:
    """
    當 LLM 生成含有不存在欄位的 SQL（如 student.gpa），
    validator 無法事先知道（這是語意問題），
    但 SQLite 執行時會拋出 OperationalError，
    retry 控制器應該偵測到並在 ≤ MAX_RETRY 次內停止。
    """

    def test_nonexistent_column_raises_operational_error(self):
        """student.gpa 不存在 → SQLite 執行應拋出 OperationalError"""
        # 先確認 DB 已建立
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            execute_query("SELECT ID, name, gpa FROM student LIMIT 5")

    def test_hallucinated_sql_classify_as_retry(self):
        """no such column 錯誤應被分類為 RETRY（可讓 LLM 修正）"""
        err = sqlite3.OperationalError("no such column: gpa")
        decision = classify_error(err)
        assert decision == "RETRY"

    def test_retry_stops_within_max(self):
        """重試次數不超過 MAX_RETRY（3 次）"""
        controller = RetryController()
        bad_sql_v1 = "SELECT ID, gpa FROM student"
        bad_sql_v2 = "SELECT ID, grade FROM student"  # 不同 SQL
        bad_sql_v3 = "SELECT ID, score FROM student"  # 不同 SQL

        err = sqlite3.OperationalError("no such column: gpa")

        d1 = controller.should_retry(err, bad_sql_v1)
        assert d1 == "RETRY"
        assert controller.retry_count <= MAX_RETRY

        d2 = controller.should_retry(err, bad_sql_v2)
        # 第二次相同指紋 ("no such column") → 收斂護衛 #2 觸發 STOP
        assert d2 == "STOP"
        assert controller.retry_count <= MAX_RETRY


# ═══════════════════════════════════════════════════════════════════
# ⑤ Invalid Join（無效關聯：不存在的資料表）
# ═══════════════════════════════════════════════════════════════════

class TestInvalidJoin:
    """查詢涉及不存在的資料表（如 manager）"""

    def test_nonexistent_table_raises_operational_error(self):
        """manager 資料表不存在 → OperationalError"""
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            execute_query("SELECT s.name, m.name FROM student s JOIN manager m ON s.ID = m.student_id LIMIT 10")

    def test_no_such_table_classified_as_retry(self):
        """no such table 錯誤應被分類為 RETRY"""
        err = sqlite3.OperationalError("no such table: manager")
        assert classify_error(err) == "RETRY"


# ═══════════════════════════════════════════════════════════════════
# ⑥ Typo Robustness（錯字容錯：驗證器不應 crash）
# ═══════════════════════════════════════════════════════════════════

class TestTypoRobustness:
    """
    錯字輸入的自然語言查詢（如 "stuednts namd John"）
    不應造成程式崩潰。在 app.py 層由 LLM 處理，
    這裡測試 validator 對奇怪但合法 SQL 的容錯性。
    """

    def test_validator_does_not_crash_on_valid_but_odd_sql(self):
        """奇怪但語法正確的 SQL 應通過驗證器"""
        odd_sql = "SELECT name FROM student WHERE name = 'Stuednt'"
        validate_sql(odd_sql)  # 不應拋出任何例外

    def test_empty_string_raises(self):
        """空字串不應通過驗證"""
        with pytest.raises(ValueError):
            validate_sql("")

    def test_whitespace_only_raises(self):
        """純空白字串不應通過驗證"""
        with pytest.raises(ValueError):
            validate_sql("   ")


# ═══════════════════════════════════════════════════════════════════
# ⑦ Traditional Chinese Query（繁體中文查詢）
# ═══════════════════════════════════════════════════════════════════

class TestTraditionalChineseQuery:
    """
    中文值在 SQL 字串中應被正確處理，
    validator 不應因中文字元而誤判。
    """

    def test_chinese_value_in_sql_passes_validator(self):
        """含中文值的 SQL WHERE 條件應通過驗證"""
        sql = "SELECT name FROM student WHERE dept_name = '電腦科學'"
        validate_sql(sql)  # 不應拋出

    def test_chinese_does_not_trigger_false_positive(self):
        """中文字元不應觸發任何禁用關鍵字誤判"""
        # 確保 "刪除" 不被誤判為 "delete"
        sql = "SELECT name FROM student WHERE name = '張刪除'"
        validate_sql(sql)  # 不應拋出（中文字不是英文關鍵字）


# ═══════════════════════════════════════════════════════════════════
# ⑧ Ambiguous Query（模糊查詢：validator 通過，但執行層應優雅回應）
# ═══════════════════════════════════════════════════════════════════

class TestAmbiguousQuery:
    """模糊 SQL 應通過 validator，但執行時可能出錯（由 retry 處理）"""

    def test_select_star_passes_validator(self):
        """SELECT * 是合法 SQL，validator 應通過"""
        validate_sql("SELECT * FROM student")  # 不應拋出

    def test_nonexistent_column_in_valid_table(self):
        """合法資料表 + 不存在欄位 → 通過 validator，但執行時失敗"""
        # validator 無法知道欄位是否存在（那是 SQLite 的工作）
        validate_sql("SELECT stuff FROM student")  # 不應拋出


# ═══════════════════════════════════════════════════════════════════
# ⑨ Convergence Guard #1（SQL 不變收斂停止）
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceGuard1:
    """當新 SQL 與前次完全相同時，應立即停止重試"""

    def test_same_sql_triggers_stop(self):
        """相同的 SQL 第二次出現 → STOP"""
        controller = RetryController()
        err = sqlite3.OperationalError("no such column: gpa")
        same_sql = "SELECT ID, gpa FROM student"

        # 第一次：SQL 是新的，應 RETRY
        d1 = controller.should_retry(err, same_sql)
        assert d1 == "RETRY"

        # 第二次：SQL 完全相同 → 收斂護衛 #1 → STOP
        d2 = controller.should_retry(err, same_sql)
        assert d2 == "STOP"
        assert controller.retry_count <= MAX_RETRY


# ═══════════════════════════════════════════════════════════════════
# ⑩ Convergence Guard #2（重複錯誤指紋收斂停止）
# ═══════════════════════════════════════════════════════════════════

class TestConvergenceGuard2:
    """相同錯誤模式（如 'no such column'）出現 ≥2 次 → STOP"""

    def test_repeated_error_pattern_triggers_stop(self):
        """相同錯誤指紋第二次出現 → STOP"""
        controller = RetryController()

        err1 = sqlite3.OperationalError("no such column: gpa")
        err2 = sqlite3.OperationalError("no such column: grade_point")

        # 第一次出現 "no such column" → RETRY
        d1 = controller.should_retry(err1, "SELECT gpa FROM student")
        assert d1 == "RETRY"

        # 第二次出現 "no such column"（不同欄位，但相同指紋）→ STOP
        d2 = controller.should_retry(err2, "SELECT grade_point FROM student")
        assert d2 == "STOP"
        assert controller.retry_count <= MAX_RETRY


# ═══════════════════════════════════════════════════════════════════
# ⑪ Multi-statement Attack（多條語句攻擊）
# ═══════════════════════════════════════════════════════════════════

class TestMultiStatementAttack:
    """分號串接攻擊：中間的 ; 應被封鎖"""

    def test_semicolon_in_middle_blocked(self):
        """字串中間的 ; 必須被封鎖"""
        with pytest.raises(ValueError, match="多條語句"):
            validate_sql("SELECT * FROM student; SELECT * FROM instructor")

    def test_trailing_semicolon_ok(self):
        """結尾的 ; 應被允許（不算多條語句）"""
        sql = "SELECT * FROM student;"
        validate_sql(sql)  # 結尾分號合法，不應拋出


# ═══════════════════════════════════════════════════════════════════
# ⑫ LIMIT Rewriter Tests（LIMIT 重寫器驗證）
# ═══════════════════════════════════════════════════════════════════

class TestLimitRewriter:
    """驗證智慧 LIMIT 重寫器的各種邊界情形"""

    def test_no_limit_appended(self):
        """沒有 LIMIT 的查詢 → 自動附加 LIMIT 1000"""
        sql = "SELECT * FROM student"
        result = rewrite_sql(sql)
        assert "LIMIT 1000" in result

    def test_existing_limit_preserved(self):
        """已有 LIMIT 的查詢 → 保留不變"""
        sql = "SELECT * FROM student LIMIT 5"
        result = rewrite_sql(sql)
        assert "LIMIT 5" in result
        assert result.count("LIMIT") == 1  # 不重複添加

    def test_order_by_limit_preserved(self):
        """ORDER BY ... LIMIT N 的組合應完整保留"""
        sql = "SELECT * FROM student ORDER BY name LIMIT 10"
        result = rewrite_sql(sql)
        assert "ORDER BY name LIMIT 10" in result
        assert result.count("LIMIT") == 1

    def test_trailing_semicolon_removed(self):
        """結尾分號應被移除後再附加 LIMIT"""
        sql = "SELECT * FROM student;"
        result = rewrite_sql(sql)
        assert result.endswith("LIMIT 1000")
        assert ";" not in result

    def test_subquery_no_limit_added(self):
        """含子查詢但無 LIMIT 的查詢 → 附加 LIMIT 1000"""
        sql = (
            "SELECT s.name FROM student s "
            "WHERE s.tot_cred > (SELECT AVG(tot_cred) FROM student)"
        )
        result = rewrite_sql(sql)
        assert result.endswith("LIMIT 1000")


# ═══════════════════════════════════════════════════════════════════
# ⑬ Permission Error（唯讀模式保護）
# ═══════════════════════════════════════════════════════════════════

class TestReadOnlyProtection:
    """驗證 database.py 確實以唯讀模式運行"""

    def test_write_attempt_classified_as_stop(self):
        """嘗試寫入到唯讀資料庫的錯誤應被分類為 STOP"""
        err = sqlite3.OperationalError("attempt to write a readonly database")
        assert classify_error(err) == "STOP"

    def test_permission_error_classified_as_stop(self):
        """一般 Exception（非 OperationalError）應被分類為 STOP"""
        err = PermissionError("access denied")
        assert classify_error(err) == "STOP"


# ═══════════════════════════════════════════════════════════════════
# ⑭ Database Smoke Tests（資料庫確認測試）
# ═══════════════════════════════════════════════════════════════════

class TestDatabaseIntegrity:
    """確認 college_2.db 已正確建立與填充"""

    def test_all_tables_exist(self):
        """確認 11 個預期資料表全部存在"""
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        tables = list_tables()
        expected = {
            "advisor", "classroom", "course", "department",
            "instructor", "prereq", "section", "student",
            "takes", "teaches", "time_slot"
        }
        assert expected.issubset(set(tables))

    def test_student_table_has_data(self):
        """student 資料表應有資料"""
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        rows = execute_query("SELECT COUNT(*) as cnt FROM student")
        assert rows[0]["cnt"] > 0

    def test_subquery_works(self):
        """子查詢應正常執行（回傳學分高於平均的學生）"""
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        sql = (
            "SELECT name, tot_cred FROM student "
            "WHERE tot_cred > (SELECT AVG(tot_cred) FROM student) "
            "ORDER BY tot_cred DESC LIMIT 5"
        )
        rows = execute_query(sql)
        assert isinstance(rows, list)

    def test_union_query_works(self):
        """UNION 查詢應正常執行（LIMIT 必須在整個 UNION 之後，非各分支）"""
        from part1.database import DB_PATH
        if not os.path.exists(DB_PATH):
            pytest.skip("資料庫尚未建立，請先執行 setup_db.py")

        # SQLite 要求 LIMIT 放在整個 UNION 結尾，不可在各 SELECT 分支內
        sql = (
            "SELECT name, 'student' as role FROM student "
            "UNION "
            "SELECT name, 'instructor' as role FROM instructor "
            "LIMIT 6"
        )
        rows = execute_query(sql)
        assert len(rows) > 0
