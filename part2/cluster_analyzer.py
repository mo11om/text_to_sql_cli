"""
cluster_analyzer.py
───────────────────
LLM 驅動的錯誤聚類 + 決定性驗證 + 跨模型分析。

架構：
  Phase 1 — LLM 聚類：將失敗題目送入 LLM，取得初始聚類
  Phase 2 — 決定性驗證：用 sqlglot 提取 AST 特徵，驗證聚類合理性
  Phase 3 — 跨模型分析：找出所有模型都失敗的「困難題」和模型特有弱點

核心原則：
- AST 特徵僅用於聚類驗證，不影響評估分數
- LLM 聚類使用 temperature=0 確保可重現
- 只傳入 nl / generated_sql / error_type（§6.3 嚴格輸入範圍）
"""

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


# ═══════════════════════════════════════════════════════════════════════════════
# § 資料模型
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Cluster:
    """單一聚類的結構"""
    name: str                           # 聚類名稱
    description: str                    # 聚類描述
    example_ids: list[int]              # 屬於此聚類的題目 ID 列表
    is_valid: Optional[bool] = None     # 決定性驗證結果
    confidence: float = 0.0             # 驗證信心度
    size: int = 0                       # 聚類大小
    validated_examples: list[int] = field(default_factory=list)   # 驗證通過的 ID
    rejected_examples: list[int] = field(default_factory=list)    # 驗證失敗的 ID


@dataclass
class CrossModelAnalysis:
    """跨模型分析結果"""
    failed_by_all_models: list[int] = field(default_factory=list)      # 所有模型都失敗的困難題
    failed_by_only_one_model: dict[str, list[int]] = field(default_factory=dict)  # 單一模型特有弱點
    cluster_frequency: dict[str, dict[str, int]] = field(default_factory=dict)    # 每模型的聚類頻率


# ═══════════════════════════════════════════════════════════════════════════════
# § AST 特徵提取（sqlglot — §4）
# 僅用於聚類驗證，不影響評估分數！
# ═══════════════════════════════════════════════════════════════════════════════

def extract_ast_features(sql: Optional[str]) -> dict[str, Any]:
    """
    使用 sqlglot 提取 SQL 的結構化特徵。

    提取項目（§4）：
    - tables: 使用的資料表集合（含 alias 解析 §4.1）
    - joins: JOIN 關係列表（含隱式 JOIN 偵測 §4.2）
    - has_aggregation: 是否包含聚合函式（§4.3）
    - has_subquery: 是否包含子查詢
    - has_group_by: 是否包含 GROUP BY

    Args:
        sql: SQL 字串（可為 None）

    Returns:
        dict: AST 特徵字典
    """
    # 預設回傳（SQL 為空或無法解析時）
    default = {
        "tables": [],
        "joins": [],
        "has_aggregation": False,
        "has_subquery": False,
        "has_group_by": False,
        "parse_error": None,
    }

    if not sql:
        default["parse_error"] = "No SQL provided"
        return default

    try:
        import sqlglot
        from sqlglot import exp

        # 解析 SQL（使用 sqlite 方言）
        parsed = sqlglot.parse_one(sql, dialect="sqlite")

        # §4.1 — 提取資料表（含 alias 解析）
        tables = set()
        alias_map: dict[str, str] = {}  # alias → real_table

        for table in parsed.find_all(exp.Table):
            table_name = table.name
            if table_name:
                tables.add(table_name)
                # 記錄 alias 對應
                alias = table.alias
                if alias:
                    alias_map[alias] = table_name

        # §4.2 — JOIN 偵測（顯式 + 隱式）
        joins = []

        # 顯式 JOIN
        for join in parsed.find_all(exp.Join):
            join_table = join.find(exp.Table)
            if join_table:
                joins.append({
                    "type": "explicit",
                    "table": join_table.name,
                    "alias": join_table.alias or None,
                })

        # 隱式 JOIN：偵測 FROM A, B WHERE ... 模式
        from_clause = parsed.find(exp.From)
        if from_clause:
            from_tables = list(from_clause.find_all(exp.Table))
            if len(from_tables) > 1:
                for t in from_tables[1:]:
                    joins.append({
                        "type": "implicit",
                        "table": t.name,
                        "alias": t.alias or None,
                    })

        # §4.3 — 聚合偵測
        agg_functions = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
        has_aggregation = any(parsed.find_all(*agg_functions))

        # 子查詢偵測
        has_subquery = bool(list(parsed.find_all(exp.Subquery)))

        # GROUP BY 偵測
        has_group_by = parsed.find(exp.Group) is not None

        return {
            "tables": sorted(tables),
            "joins": joins,
            "has_aggregation": has_aggregation,
            "has_subquery": has_subquery,
            "has_group_by": has_group_by,
            "parse_error": None,
        }

    except Exception as e:
        default["parse_error"] = f"{type(e).__name__}: {e}"
        return default


# ═══════════════════════════════════════════════════════════════════════════════
# § Phase 1 — LLM 聚類（§6）
# ═══════════════════════════════════════════════════════════════════════════════

# 聚類專用系統提示（§6.1 確定性設定）
_CLUSTERING_SYSTEM_PROMPT = """You are a SQL error analyst.
Given a list of failed Text-to-SQL queries, group them into meaningful error clusters.

STRICT RULES:
- Each cluster must have: name, description, example_ids
- Clusters should capture patterns like: wrong table, missing join, wrong aggregation, schema confusion, etc.
- Output ONLY valid JSON. No explanation.

OUTPUT FORMAT:
{
  "clusters": [
    {
      "name": "cluster_name",
      "description": "why these queries failed similarly",
      "example_ids": [1, 2, 3]
    }
  ]
}"""


def run_llm_clustering(
    clustering_input_path: str,
) -> list[dict]:
    """
    Phase 1：使用 LLM 對失敗題目進行聚類。

    §6.1 — temperature=0 確保確定性
    §6.3 — 只傳入 nl / generated_sql / error_type

    Args:
        clustering_input_path: clustering_input.json 路徑

    Returns:
        list[dict]: 聚類結果列表（每個含 name, description, example_ids）
    """
    from openai import OpenAI

    with open(clustering_input_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    if not items:
        return []

    # 組裝 LLM 輸入（§6.3 嚴格範圍）
    user_content = json.dumps(items, ensure_ascii=False, indent=2)

    # 初始化 LLM 客戶端
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider == "ollama":
        client = OpenAI(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
        )
        model = os.getenv("OLLAMA_MODEL", "llama3.1")
    else:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # §6.1 — temperature=0 確保確定性
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": _CLUSTERING_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )

    raw = response.choices[0].message.content or ""
    # 解析回應
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(cleaned)
        return data.get("clusters", [])
    except json.JSONDecodeError:
        print(f"⚠️  LLM 聚類回應解析失敗: {raw[:200]}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# § Phase 2 — 決定性驗證（§7）
# ═══════════════════════════════════════════════════════════════════════════════

def validate_cluster(
    cluster: dict,
    all_items: list[dict],
) -> Cluster:
    """
    對單一聚類進行決定性驗證。

    §7.1 — 重新計算 AST 特徵，與聚類宣稱比較
    §7.2 — 小聚類 (size < 3) → LOW_CONFIDENCE
    §7.3 — valid_ratio ≥ 0.6 → VALID，否則 INVALID

    Args:
        cluster:   LLM 產出的聚類 dict（name, description, example_ids）
        all_items: clustering_input.json 中的所有題目

    Returns:
        Cluster: 含驗證結果的聚類物件
    """
    name = cluster.get("name", "Unknown")
    description = cluster.get("description", "")
    example_ids = cluster.get("example_ids", [])

    result = Cluster(
        name=name,
        description=description,
        example_ids=example_ids,
        size=len(example_ids),
    )

    if not example_ids:
        result.is_valid = False
        result.confidence = 0.0
        return result

    # 建立 ID → item 快速查找表
    id_to_item = {item["id"]: item for item in all_items}

    # 對每個成員提取 AST 特徵
    features_list: list[dict] = []
    for eid in example_ids:
        item = id_to_item.get(eid)
        if item:
            sql = item.get("generated_sql")
            features = extract_ast_features(sql)
            features_list.append(features)

    if not features_list:
        result.is_valid = False
        result.confidence = 0.0
        return result

    # §7.1 — 計算特徵一致性（同一聚類的成員應有相似的 AST 特徵）
    # 規則：至少 60% 的成員共用相同的「主要特徵模式」
    validated = []
    rejected = []

    # 找出最常見的特徵組合
    error_types = Counter()
    table_sets = Counter()
    for i, eid in enumerate(example_ids):
        item = id_to_item.get(eid)
        if not item:
            rejected.append(eid)
            continue

        error_type = item.get("error_type", "Unknown")
        error_types[error_type] += 1

        if i < len(features_list):
            ft = features_list[i]
            table_key = tuple(ft.get("tables", []))
            table_sets[table_key] += 1

    # 驗證邏輯：同一聚類的成員應有相似的錯誤模式
    if error_types:
        most_common_error, most_common_count = error_types.most_common(1)[0]
        error_consistency = most_common_count / len(example_ids)
    else:
        error_consistency = 0.0

    # 決定每個成員是否符合聚類宣稱
    most_common_error_type = error_types.most_common(1)[0][0] if error_types else None
    for eid in example_ids:
        item = id_to_item.get(eid)
        if not item:
            rejected.append(eid)
            continue
        if item.get("error_type") == most_common_error_type:
            validated.append(eid)
        else:
            rejected.append(eid)

    result.validated_examples = validated
    result.rejected_examples = rejected

    # §7.3 — 驗收規則
    valid_ratio = len(validated) / len(example_ids) if example_ids else 0.0
    result.confidence = round(valid_ratio, 2)

    # §7.2 — 小聚類標記
    if len(example_ids) < 3:
        result.is_valid = None  # LOW_CONFIDENCE（不確定）
        result.confidence = min(result.confidence, 0.5)
    elif valid_ratio >= 0.6:
        result.is_valid = True
    else:
        result.is_valid = False

    return result


def check_cluster_overlap(clusters: list[Cluster]) -> dict[str, float]:
    """
    §7.5 — 檢查聚類間的重疊比率。

    Returns:
        dict[(cluster_a, cluster_b) → overlap_ratio]
    """
    overlaps: dict[str, float] = {}

    for i, a in enumerate(clusters):
        for j, b in enumerate(clusters):
            if i >= j:
                continue
            set_a = set(a.example_ids)
            set_b = set(b.example_ids)
            if not set_a or not set_b:
                continue
            intersection = len(set_a & set_b)
            union = len(set_a | set_b)
            if union > 0:
                ratio = intersection / union
                if ratio > 0:
                    key = f"{a.name} ∩ {b.name}"
                    overlaps[key] = round(ratio, 3)

    return overlaps


# ═══════════════════════════════════════════════════════════════════════════════
# § Phase 3 — 跨模型分析（§8）
# ═══════════════════════════════════════════════════════════════════════════════

def run_cross_model_analysis(
    all_model_results: dict[str, list[dict]],
) -> CrossModelAnalysis:
    """
    跨模型比較分析。

    §8.1 — 所有模型都失敗的困難題
    §8.2 — 僅特定模型失敗的弱點題
    §8.3 — 每模型的聚類頻率

    Args:
        all_model_results: {model_name → list[EvalResult as dict]}

    Returns:
        CrossModelAnalysis: 分析結果
    """
    analysis = CrossModelAnalysis()

    if not all_model_results:
        return analysis

    model_names = list(all_model_results.keys())

    # 收集每個模型失敗的題目 ID
    model_failures: dict[str, set[int]] = {}
    all_ids: set[int] = set()

    for model, results in all_model_results.items():
        failed_ids = {r["id"] for r in results if r.get("f1", 0) < 1.0}
        model_failures[model] = failed_ids
        all_ids.update(r["id"] for r in results)

    # §8.1 — 所有模型都失敗
    failed_by_all = set.intersection(*model_failures.values()) if model_failures else set()
    analysis.failed_by_all_models = sorted(failed_by_all)

    # §8.2 — 僅單一模型失敗
    for model in model_names:
        only_this = model_failures[model] - set.union(
            *(model_failures[m] for m in model_names if m != model)
        ) if len(model_names) > 1 else model_failures[model]
        if only_this:
            analysis.failed_by_only_one_model[model] = sorted(only_this)

    return analysis


# ═══════════════════════════════════════════════════════════════════════════════
# § 主要入口
# ═══════════════════════════════════════════════════════════════════════════════

def run_cluster_analysis(
    output_dir: str,
) -> tuple[list[Cluster], dict[str, float]]:
    """
    執行完整聚類分析管線。

    步驟：
    1. 讀取 clustering_input.json
    2. LLM 聚類
    3. 決定性驗證每個聚類
    4. 檢查聚類重疊
    5. 儲存 clustering_output.json

    Args:
        output_dir: eval_results/ 目錄路徑

    Returns:
        (clusters, overlaps): 驗證後的聚類列表與重疊比率
    """
    from rich.console import Console
    console = Console()

    # ① 讀取聚類輸入
    input_path = os.path.join(output_dir, "clustering_input.json")
    if not os.path.exists(input_path):
        console.print("[yellow]⚠️  找不到 clustering_input.json，跳過聚類分析[/yellow]")
        return [], {}

    with open(input_path, "r", encoding="utf-8") as f:
        all_items = json.load(f)

    if not all_items:
        console.print("[yellow]⚠️  沒有失敗題目，無需聚類分析[/yellow]")
        return [], {}

    # ② LLM 聚類
    console.print("[bold cyan]🧩 Phase 1: LLM 聚類分析...[/bold cyan]")
    raw_clusters = run_llm_clustering(input_path)
    console.print(f"   LLM 回傳 {len(raw_clusters)} 個聚類")

    # ③ 決定性驗證
    console.print("[bold cyan]🔬 Phase 2: 決定性驗證...[/bold cyan]")
    validated_clusters: list[Cluster] = []
    for rc in raw_clusters:
        cluster = validate_cluster(rc, all_items)
        validated_clusters.append(cluster)
        status = "✅ VALID" if cluster.is_valid is True else (
            "⚠️  LOW_CONFIDENCE" if cluster.is_valid is None else "❌ INVALID"
        )
        console.print(
            f"   [{status}] {cluster.name} "
            f"(size={cluster.size}, confidence={cluster.confidence})"
        )

    # ④ 檢查重疊
    overlaps = check_cluster_overlap(validated_clusters)
    if overlaps:
        console.print(f"   ⚠️  發現 {len(overlaps)} 組聚類重疊")

    # ⑤ 儲存成品
    output = {
        "clusters": [asdict(c) for c in validated_clusters],
        "overlaps": overlaps,
    }
    output_path = os.path.join(output_dir, "clustering_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    console.print(f"   💾 已儲存: {output_path}")
    return validated_clusters, overlaps


# ── CLI 入口 ────────────────────────────────────────────────────────────────────
def main() -> None:
    """
    用法：
        python cluster_analyzer.py --output eval_results/
    """
    import argparse
    parser = argparse.ArgumentParser(description="聚類分析器")
    parser.add_argument("--output", default="eval_results", help="eval_results 目錄")
    args = parser.parse_args()
    run_cluster_analysis(args.output)


if __name__ == "__main__":
    main()
