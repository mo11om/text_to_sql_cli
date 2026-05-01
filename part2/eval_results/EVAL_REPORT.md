# 📊 Text-to-SQL 評估報告 (EVAL_REPORT)

> 產生時間: 2026-04-29 21:20:48

## 📌 版本資訊

| 項目 | 值 |
|---|---|
| model_name | ollama |
| model_version | qwen3.6:27b |
| prompt_version | v5 |
| schema_version | college_2_v1 |
| dataset_version | eval_data_v1 |
| timestamp | 2026-04-29T20:42:54.509001 |

## 📈 指標摘要

| 指標 | 值 |
|---|---|
| 總題數 | 30 |
| 完美匹配 (F1=1.0) | 19 (63.3%) |
| 完全失敗 (F1=0.0) | 8 (26.7%) |
| **平均 Precision** | **0.7048** |
| **平均 Recall** | **0.7552** |
| **平均 F1** | **0.7021** |
| **平均 Jaccard** | **0.6933** |

## 📊 分類別指標

| 類別 | 題數 | Precision | Recall | F1 | Jaccard | 完美率 |
|---|---|---|---|---|---|---|
| Adversarial | 5 | 0.6000 | 0.7600 | 0.5778 | 0.5600 | 2/5 |
| Aggregation | 5 | 0.6286 | 0.8000 | 0.6500 | 0.6286 | 3/5 |
| Basic | 5 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 5/5 |
| Complex | 10 | 0.6000 | 0.6000 | 0.6000 | 0.6000 | 6/10 |
| JOIN | 5 | 0.8000 | 0.7714 | 0.7846 | 0.7714 | 3/5 |

## ⏱️ 延遲分析

| 指標 | 平均 (ms) | 最大 (ms) | 最小 (ms) |
|---|---|---|---|
| LLM 延遲 | 38124 | 90602 | 0 |
| 執行延遲 | 3 | 4 | 0 |
| 總延遲 | 70206 | 240625 | 9865 |

## 🔄 重試分析

> 沒有題目需要重試。

## ✅ 已驗證聚類

以下聚類通過了 AST 決定性驗證（valid_ratio ≥ 0.6）：

### 🏷️ LLM_Generation_Failure

Model failed to produce any SQL output (generated_sql is null), typically due to internal processing errors, prompt complexity, or inability to map natural language to schema.

- 大小: 4
- 信心度: 1.00
- 題目 IDs: [10, 18, 22, 24]
- 驗證通過: [10, 18, 22, 24]
- 驗證失敗: []

### 🏷️ Aggregation_and_Grouping_Errors

Queries involving GROUP BY, aggregate functions (COUNT, MAX), or subqueries for group-wise calculations. Failures likely stem from incorrect aggregation logic, missing HAVING clauses, or improper subquery structuring.

- 大小: 3
- 信心度: 1.00
- 題目 IDs: [14, 15, 23]
- 驗證通過: [14, 15, 23]
- 驗證失敗: []

### 🏷️ Join_and_Filtering_Issues

Queries relying on table joins or WHERE clause filtering. Failures may be due to incorrect join keys, missing joins, schema confusion, or improper condition application despite syntactically valid SQL.

- 大小: 4
- 信心度: 1.00
- 題目 IDs: [9, 27, 29, 30]
- 驗證通過: [9, 27, 29, 30]
- 驗證失敗: []

## ❌ 被拒絕聚類

> 所有聚類均通過驗證。

## 🎯 困難題分析

以下題目評估失敗（F1 < 1.0）：

| ID | 類別 | 自然語言 | 錯誤類型 | F1 |
|---|---|---|---|---|
| 9 | JOIN | 找出修過 'CS-101' 這門課的所有學生姓名。... | N/A | 0.92 |
| 10 | JOIN | 列出 'Physics' 系上所有教授的姓名及其教授的課程代碼。... | LLM Error | 0.00 |
| 14 | Aggregation | 找出開課數量最多的系所名稱。... | N/A | 0.25 |
| 15 | Aggregation | 列出每個學生的 ID 以及他們修過的總課程數量。... | N/A | 0.00 |
| 18 | Complex | 找出至少開設了兩門 3 學分以上課程的系所名稱。... | LLM Error | 0.00 |
| 22 | Complex | 列出成績拿到 'A' 最多次的學生姓名。... | LLM Error | 0.00 |
| 23 | Complex | 找出每個系所薪水最高的教授姓名。... | N/A | 0.00 |
| 24 | Complex | 列出修習了 'Comp. Sci.' 系開設的所有課程的學生 ID。... | LLM Error | 0.00 |
| 27 | Adversarial | 找一夏資工系(Comp. Sci.)的學省... | N/A | 0.00 |
| 29 | Adversarial | Show me the names of classes taking plac... | N/A | 0.89 |
| 30 | Adversarial | 找出所有既不是 CS 系也不是 EE 系，而且名字開頭是 'A' 的學生... | N/A | 0.00 |

## 🔍 失敗明細

<details>
<summary>展開查看所有失敗題目的 SQL 比較</summary>

### #9: 找出修過 'CS-101' 這門課的所有學生姓名。

**Ground Truth:**
```sql
SELECT T1.name FROM student AS T1 JOIN takes AS T2 ON T1.ID = T2.ID WHERE T2.course_id = 'CS-101'
```

**Generated:**
```sql
SELECT DISTINCT student.name FROM student JOIN takes ON student.ID = takes.ID WHERE takes.course_id = 'CS-101' LIMIT 100
```

---

### #10: 列出 'Physics' 系上所有教授的姓名及其教授的課程代碼。

**Ground Truth:**
```sql
SELECT T1.name, T2.course_id FROM instructor AS T1 JOIN teaches AS T2 ON T1.ID = T2.ID WHERE T1.dept_name = 'Physics'
```

**Generated:**
```sql
（未生成）
```

**錯誤:** APITimeoutError: Request timed out.

---

### #14: 找出開課數量最多的系所名稱。

**Ground Truth:**
```sql
SELECT dept_name FROM course GROUP BY dept_name ORDER BY COUNT(*) DESC LIMIT 1
```

**Generated:**
```sql
SELECT dept_name FROM course GROUP BY dept_name ORDER BY count(course_id) DESC LIMIT 100
```

---

### #15: 列出每個學生的 ID 以及他們修過的總課程數量。

**Ground Truth:**
```sql
SELECT ID, COUNT(course_id) FROM takes GROUP BY ID
```

**Generated:**
```sql
SELECT ID , count(course_id) FROM takes GROUP BY ID LIMIT 100
```

---

### #18: 找出至少開設了兩門 3 學分以上課程的系所名稱。

**Ground Truth:**
```sql
SELECT dept_name FROM course WHERE credits >= 3 GROUP BY dept_name HAVING COUNT(*) >= 2
```

**Generated:**
```sql
（未生成）
```

**錯誤:** APITimeoutError: Request timed out.

---

### #22: 列出成績拿到 'A' 最多次的學生姓名。

**Ground Truth:**
```sql
SELECT T1.name FROM student AS T1 JOIN takes AS T2 ON T1.ID = T2.ID WHERE T2.grade = 'A' GROUP BY T1.ID ORDER BY COUNT(*) DESC LIMIT 1
```

**Generated:**
```sql
（未生成）
```

**錯誤:** APITimeoutError: Request timed out.

---

### #23: 找出每個系所薪水最高的教授姓名。

**Ground Truth:**
```sql
SELECT T1.name FROM instructor AS T1 WHERE T1.salary = (SELECT MAX(salary) FROM instructor AS T2 WHERE T1.dept_name = T2.dept_name)
```

**Generated:**
```sql
SELECT T1.name , T1.dept_name FROM instructor AS T1 JOIN (SELECT dept_name , MAX(salary) AS max_salary FROM instructor GROUP BY dept_name) AS T2 ON T1.dept_name  =  T2.dept_name AND T1.salary  =  T2.max_salary LIMIT 100
```

---

### #24: 列出修習了 'Comp. Sci.' 系開設的所有課程的學生 ID。

**Ground Truth:**
```sql
SELECT ID FROM takes JOIN course ON takes.course_id = course.course_id WHERE course.dept_name = 'Comp. Sci.' GROUP BY ID HAVING COUNT(DISTINCT takes.course_id) = (SELECT COUNT(*) FROM course WHERE dept_name = 'Comp. Sci.')
```

**Generated:**
```sql
（未生成）
```

**錯誤:** APITimeoutError: Request timed out.

---

### #27: 找一夏資工系(Comp. Sci.)的學省

**Ground Truth:**
```sql
SELECT name FROM student WHERE dept_name = 'Comp. Sci.'
```

**Generated:**
```sql
SELECT T1.name FROM student AS T1 JOIN takes AS T2 ON T1.ID  =  T2.ID WHERE T1.dept_name = 'Comp. Sci.' AND T2.semester = 'Summer' LIMIT 100
```

---

### #29: Show me the names of classes taking place in the 'Taylor' building.

**Ground Truth:**
```sql
SELECT T1.title FROM course AS T1 JOIN section AS T2 ON T1.course_id = T2.course_id WHERE T2.building = 'Taylor'
```

**Generated:**
```sql
SELECT DISTINCT course.title FROM course JOIN section ON course.course_id = section.course_id WHERE section.building = 'Taylor' LIMIT 100
```

---

### #30: 找出所有既不是 CS 系也不是 EE 系，而且名字開頭是 'A' 的學生

**Ground Truth:**
```sql
SELECT name FROM student WHERE dept_name NOT IN ('Comp. Sci.', 'Elec. Eng.') AND name LIKE 'A%'
```

**Generated:**
```sql
SELECT * FROM student WHERE dept_name NOT IN ('CS', 'EE') AND name LIKE 'A%' LIMIT 100
```

---

</details>
