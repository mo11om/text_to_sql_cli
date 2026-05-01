"""
report_generator.py
───────────────────
評估報告產生器：從 eval_results/ 成品生成 EVAL_REPORT.md。

報告內容（§10）：
1. 指標摘要（整體 + 分類別 Precision / Recall / F1 / Jaccard）
2. 已驗證的有效聚類（VALID 的聚類）
3. 被拒絕的聚類（LLM 幻覺）
4. 困難題分析（所有模型都失敗的題目）
5. 跨模型比較（如有多模型資料）

使用方式：
    python report_generator.py --output eval_results/
"""

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# § 報告產生器
# ═══════════════════════════════════════════════════════════════════════════════

def generate_report(output_dir: str) -> str:
    """
    讀取 eval_results/ 目錄中的所有成品，產生 EVAL_REPORT.md。

    Args:
        output_dir: eval_results/ 路徑

    Returns:
        str: 報告輸出路徑
    """
    from rich.console import Console
    console = Console()

    # ── 載入成品 ──────────────────────────────────────────────────────
    results = _load_csv(os.path.join(output_dir, "eval_results.csv"))
    failures = _load_json(os.path.join(output_dir, "failures.json"))
    clustering = _load_json(os.path.join(output_dir, "clustering_output.json"))
    version = _load_json(os.path.join(output_dir, "version.json"))

    # ── 組裝 Markdown 報告 ────────────────────────────────────────────
    sections: list[str] = []

    # 標題
    sections.append("# 📊 Text-to-SQL 評估報告 (EVAL_REPORT)")
    sections.append(f"\n> 產生時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # §1 — 版本資訊
    if version:
        sections.append("## 📌 版本資訊\n")
        sections.append("| 項目 | 值 |")
        sections.append("|---|---|")
        for k, v in version.items():
            sections.append(f"| {k} | {v} |")
        sections.append("")

    # §2 — 指標摘要
    sections.append(_build_metrics_summary(results))

    # §3 — 分類別指標
    sections.append(_build_category_metrics(results))

    # §4 — 延遲分析
    sections.append(_build_latency_section(results))

    # §5 — 重試分析
    sections.append(_build_retry_section(results))

    # §6 — 已驗證聚類（§7 VALID only）
    if clustering:
        sections.append(_build_validated_clusters(clustering))

    # §7 — 被拒絕聚類（LLM 幻覺）
    if clustering:
        sections.append(_build_rejected_clusters(clustering))

    # §8 — 困難題分析
    if failures:
        sections.append(_build_hard_query_analysis(failures))

    # §9 — 失敗題目明細
    if failures:
        sections.append(_build_failure_details(failures))

    # ── 寫出檔案 ──────────────────────────────────────────────────────
    report_content = "\n".join(sections)
    report_path = os.path.join(output_dir, "EVAL_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    console.print(f"[bold green]📝 報告已產生: {report_path}[/bold green]")
    return report_path


# ═══════════════════════════════════════════════════════════════════════════════
# § 報告各段落建構
# ═══════════════════════════════════════════════════════════════════════════════

def _build_metrics_summary(results: list[dict]) -> str:
    """§1 — 整體指標摘要"""
    total = len(results)
    if total == 0:
        return "## 📈 指標摘要\n\n> 無評估結果。\n"

    # 計算整體統計
    avg_p = _avg(results, "precision")
    avg_r = _avg(results, "recall")
    avg_f1 = _avg(results, "f1")
    avg_j = _avg(results, "jaccard")
    perfect = sum(1 for r in results if _float(r.get("f1", 0)) == 1.0)
    failed = sum(1 for r in results if _float(r.get("f1", 0)) == 0.0)

    lines = [
        "## 📈 指標摘要\n",
        "| 指標 | 值 |",
        "|---|---|",
        f"| 總題數 | {total} |",
        f"| 完美匹配 (F1=1.0) | {perfect} ({perfect/total*100:.1f}%) |",
        f"| 完全失敗 (F1=0.0) | {failed} ({failed/total*100:.1f}%) |",
        f"| **平均 Precision** | **{avg_p:.4f}** |",
        f"| **平均 Recall** | **{avg_r:.4f}** |",
        f"| **平均 F1** | **{avg_f1:.4f}** |",
        f"| **平均 Jaccard** | **{avg_j:.4f}** |",
        "",
    ]
    return "\n".join(lines)


def _build_category_metrics(results: list[dict]) -> str:
    """按題目類別分組的指標表格"""
    categories: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        cat = r.get("category", "Unknown")
        categories[cat].append(r)

    lines = [
        "## 📊 分類別指標\n",
        "| 類別 | 題數 | Precision | Recall | F1 | Jaccard | 完美率 |",
        "|---|---|---|---|---|---|---|",
    ]

    for cat in sorted(categories.keys()):
        rs = categories[cat]
        n = len(rs)
        p = _avg(rs, "precision")
        r = _avg(rs, "recall")
        f1 = _avg(rs, "f1")
        j = _avg(rs, "jaccard")
        perfect = sum(1 for x in rs if _float(x.get("f1", 0)) == 1.0)
        lines.append(f"| {cat} | {n} | {p:.4f} | {r:.4f} | {f1:.4f} | {j:.4f} | {perfect}/{n} |")

    lines.append("")
    return "\n".join(lines)


def _build_latency_section(results: list[dict]) -> str:
    """延遲分析（§5.2）"""
    if not results:
        return ""

    llm_latencies = [_float(r.get("llm_latency_ms", 0)) for r in results]
    exec_latencies = [_float(r.get("execution_latency_ms", 0)) for r in results]
    total_latencies = [_float(r.get("total_latency_ms", 0)) for r in results]

    lines = [
        "## ⏱️ 延遲分析\n",
        "| 指標 | 平均 (ms) | 最大 (ms) | 最小 (ms) |",
        "|---|---|---|---|",
        f"| LLM 延遲 | {_safe_avg(llm_latencies):.0f} | {max(llm_latencies):.0f} | {min(llm_latencies):.0f} |",
        f"| 執行延遲 | {_safe_avg(exec_latencies):.0f} | {max(exec_latencies):.0f} | {min(exec_latencies):.0f} |",
        f"| 總延遲 | {_safe_avg(total_latencies):.0f} | {max(total_latencies):.0f} | {min(total_latencies):.0f} |",
        "",
    ]
    return "\n".join(lines)


def _build_retry_section(results: list[dict]) -> str:
    """重試分析（§5.3）"""
    retried = [r for r in results if int(r.get("retry_count", 0)) > 0]
    if not retried:
        return "## 🔄 重試分析\n\n> 沒有題目需要重試。\n"

    success_after = sum(1 for r in retried if str(r.get("success_after_retry", "")).lower() == "true")

    lines = [
        "## 🔄 重試分析\n",
        f"- 需要重試的題目: {len(retried)}",
        f"- 重試後成功: {success_after}",
        f"- 重試後仍失敗: {len(retried) - success_after}",
        "",
    ]
    return "\n".join(lines)


def _build_validated_clusters(clustering: dict) -> str:
    """§7 — 只顯示已驗證的有效聚類"""
    clusters = clustering.get("clusters", [])
    valid = [c for c in clusters if c.get("is_valid") is True]

    if not valid:
        return "## ✅ 已驗證聚類\n\n> 沒有聚類通過決定性驗證。\n"

    lines = [
        "## ✅ 已驗證聚類\n",
        "以下聚類通過了 AST 決定性驗證（valid_ratio ≥ 0.6）：\n",
    ]

    for c in valid:
        lines.append(f"### 🏷️ {c['name']}")
        lines.append(f"\n{c.get('description', 'N/A')}\n")
        lines.append(f"- 大小: {c.get('size', 0)}")
        lines.append(f"- 信心度: {c.get('confidence', 0):.2f}")
        lines.append(f"- 題目 IDs: {c.get('example_ids', [])}")
        lines.append(f"- 驗證通過: {c.get('validated_examples', [])}")
        lines.append(f"- 驗證失敗: {c.get('rejected_examples', [])}")
        lines.append("")

    # 重疊資訊
    overlaps = clustering.get("overlaps", {})
    if overlaps:
        lines.append("### ⚠️ 聚類重疊\n")
        lines.append("| 聚類對 | 重疊比率 |")
        lines.append("|---|---|")
        for pair, ratio in overlaps.items():
            lines.append(f"| {pair} | {ratio:.3f} |")
        lines.append("")

    return "\n".join(lines)


def _build_rejected_clusters(clustering: dict) -> str:
    """被拒絕的聚類（LLM 幻覺）"""
    clusters = clustering.get("clusters", [])
    invalid = [c for c in clusters if c.get("is_valid") is False]
    low_conf = [c for c in clusters if c.get("is_valid") is None]

    if not invalid and not low_conf:
        return "## ❌ 被拒絕聚類\n\n> 所有聚類均通過驗證。\n"

    lines = ["## ❌ 被拒絕聚類\n"]

    if invalid:
        lines.append("以下聚類未通過決定性驗證（valid_ratio < 0.6），視為 **LLM 幻覺**：\n")
        for c in invalid:
            lines.append(f"- **{c['name']}** (confidence={c.get('confidence', 0):.2f}, size={c.get('size', 0)})")

    if low_conf:
        lines.append("\n以下聚類因樣本不足 (size < 3) 信心度低：\n")
        for c in low_conf:
            lines.append(f"- **{c['name']}** (size={c.get('size', 0)})")

    lines.append("")
    return "\n".join(lines)


def _build_hard_query_analysis(failures: list[dict]) -> str:
    """困難題分析"""
    if not failures:
        return ""

    lines = [
        "## 🎯 困難題分析\n",
        "以下題目評估失敗（F1 < 1.0）：\n",
        "| ID | 類別 | 自然語言 | 錯誤類型 | F1 |",
        "|---|---|---|---|---|",
    ]

    for f in failures[:20]:  # 最多顯示 20 題
        qid = f.get("id", "?")
        cat = f.get("category", "?")
        nl = f.get("nl", "?")[:40]  # 截斷
        err = f.get("error_type", "N/A") or "N/A"
        f1 = _float(f.get("f1", 0))
        lines.append(f"| {qid} | {cat} | {nl}... | {err} | {f1:.2f} |")

    lines.append("")
    return "\n".join(lines)


def _build_failure_details(failures: list[dict]) -> str:
    """詳細失敗分析（含 GT SQL vs Gen SQL）"""
    if not failures:
        return ""

    lines = [
        "## 🔍 失敗明細\n",
        "<details>",
        "<summary>展開查看所有失敗題目的 SQL 比較</summary>\n",
    ]

    for f in failures:
        qid = f.get("id", "?")
        nl = f.get("nl", "?")
        gt = f.get("gt_sql", "N/A")
        gen = f.get("gen_sql", "N/A") or "（未生成）"
        err = f.get("error_detail", "") or f.get("error_type", "")

        lines.append(f"### #{qid}: {nl}\n")
        lines.append("**Ground Truth:**")
        lines.append(f"```sql\n{gt}\n```\n")
        lines.append("**Generated:**")
        lines.append(f"```sql\n{gen}\n```\n")
        if err:
            lines.append(f"**錯誤:** {err}\n")
        lines.append("---\n")

    lines.append("</details>\n")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# § 工具函式
# ═══════════════════════════════════════════════════════════════════════════════

def _load_csv(path: str) -> list[dict]:
    """安全讀取 CSV 檔案為 list[dict]"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_json(path: str) -> Any:
    """安全讀取 JSON 檔案"""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _float(v: Any) -> float:
    """安全轉換為 float"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _avg(rows: list[dict], key: str) -> float:
    """計算指定欄位的平均值"""
    vals = [_float(r.get(key, 0)) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def _safe_avg(vals: list[float]) -> float:
    """安全平均值計算"""
    return sum(vals) / len(vals) if vals else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# § CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    用法：
        python report_generator.py --output eval_results/
    """
    import argparse
    parser = argparse.ArgumentParser(description="評估報告產生器")
    parser.add_argument("--output", default="eval_results", help="eval_results 目錄")
    args = parser.parse_args()
    generate_report(args.output)


if __name__ == "__main__":
    main()
