"""
mac_agent.py
------------
MAC-SQL (Multi-Agent Collaborative Framework) 實作。

包含三個協作的 LLM Agent：
1. Selector Agent：過濾無關的資料表與欄位，產出 Pruned Schema。
2. Decomposer Agent：透過 CoT 分解問題，生成初始 SQL。
3. Refiner Agent：接收 SQLite 執行錯誤，自我修正 SQL。

支援三種 LLM 後端：OpenAI / Ollama / Gemini。
Gemini 使用 Google 提供的 OpenAI 相容端點，無需額外 SDK。

Prompt 架構已針對 LLM Context Caching 最佳化：
- 靜態內容（Schema、規則、安全指令）→ 放在 system message（可被快取）
- 動態內容（使用者問題、錯誤 SQL）→ 放在 user message（每次變動）
"""

import json
import os
import httpx
from openai import OpenAI
from typing import Optional

# 沿用 llm.py 定義好的資料結構與超時設定
from part1.llm import SQLResponse, _LLM_TIMEOUT

# ── Gemini OpenAI 相容端點 ────────────────────────────────────────────────────
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class MACSQLPipeline:
    """
    多 Agent 協作管線，統籌 Selector, Decomposer, 與 Refiner。
    支援 OpenAI / Ollama / Gemini 三種 LLM 後端。
    """

    def __init__(self) -> None:
        """
        初始化 LLM 用戶端。

        根據 LLM_PROVIDER 環境變數選擇後端：
        - "openai"  → OpenAI API（需要 OPENAI_API_KEY）
        - "ollama"  → 本地 Ollama（相容 OpenAI API 格式）
        - "gemini"  → Google Gemini（透過 OpenAI 相容端點）
        """
        self.provider = os.getenv("LLM_PROVIDER", "openai").lower()

        if self.provider == "ollama":
            # ── Ollama 本地模型 ──────────────────────────────────────────────
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            self.model = os.getenv("OLLAMA_MODEL", "llama3.1")
            self.client = OpenAI(
                base_url=base_url,
                api_key="ollama",  # Ollama 不需要真實 API key
                timeout=httpx.Timeout(_LLM_TIMEOUT, connect=10.0),
                max_retries=1,
            )

        elif self.provider == "gemini":
            # ── Google Gemini（OpenAI 相容端點）──────────────────────────────
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                raise EnvironmentError(
                    "缺少 GEMINI_API_KEY。請在 .env 中設定。"
                )
            self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
            self.client = OpenAI(
                api_key=api_key,
                base_url=_GEMINI_BASE_URL,
                timeout=httpx.Timeout(_LLM_TIMEOUT, connect=10.0),
                max_retries=3,
            )

        else:
            # ── OpenAI（預設）────────────────────────────────────────────────
            api_key = os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                raise EnvironmentError(
                    "缺少 OPENAI_API_KEY。請在 .env 中設定。"
                )
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            self.client = OpenAI(
                api_key=api_key,
                timeout=httpx.Timeout(_LLM_TIMEOUT, connect=10.0),
                max_retries=3,
            )

    # ═════════════════════════════════════════════════════════════════════════
    # Agent 1: Selector — 過濾無關 Schema
    # ═════════════════════════════════════════════════════════════════════════

    def run_selector(self, nl_query: str, full_schema: str) -> str:
        """
        Agent 1: Selector
        根據使用者問題過濾掉不相關的資料表與欄位。

        Prompt 架構（Context Caching 最佳化）：
        - system: 靜態指令 + 完整 Schema（可快取）
        - user:   僅包含使用者問題（動態）

        Args:
            nl_query:    使用者自然語言查詢
            full_schema: 完整資料庫 DDL 綱要

        Returns:
            str: 精簡後的 Pruned Schema
        """
        # ── 靜態內容：指令 + Schema → system message（可快取）──────────────
        system_prompt = (
            "You are a database administrator. Your task is to filter out tables "
            "and columns from the schema that are entirely irrelevant to the "
            "user's question.\n\n"
            "Output ONLY the pruned schema without any explanations.\n\n"
            f"[Database Schema]\n{full_schema}"
        )

        # ── 動態內容：僅使用者問題 → user message ────────────────────────────
        user_prompt = f"[User Question]\n{nl_query}"

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,  # 確保過濾穩定
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    # ═════════════════════════════════════════════════════════════════════════
    # Agent 2: Decomposer — CoT 分解問題並生成 SQL
    # ═════════════════════════════════════════════════════════════════════════

    def run_decomposer(self, nl_query: str, pruned_schema: str) -> SQLResponse:
        """
        Agent 2: Decomposer
        利用 CoT 拆解問題，基於 Pruned Schema 生成 SQL。

        Prompt 架構（Context Caching 最佳化）：
        - system: 靜態指令 + Pruned Schema + 規則 + 安全策略（可快取）
        - user:   僅包含使用者問題（動態）

        Args:
            nl_query:      使用者自然語言查詢
            pruned_schema: Selector 產出的精簡 Schema

        Returns:
            SQLResponse: 結構化回應（status / sql / confidence）
        """
        # ── 靜態內容：指令 + Schema + 規則 → system message（可快取）───────
        system_prompt = (
            "You are a SQLite expert. Break down the user's question into "
            "logical sub-questions. Think step-by-step, then generate the final "
            "valid SQLite SELECT query based on the pruned schema.\n\n"
            f"[Pruned Schema]\n{pruned_schema}\n\n"
            "[Strict Rules]\n"
            "- Only generate SELECT queries. Subqueries and UNIONs are allowed.\n"
            "- Only use tables and columns defined in the schema above.\n"
            "- Never invent tables or columns.\n"
            "- No SQL comments (-- or /* */). No multiple statements (;).\n"
            "- Do NOT include LIMIT unless specifically requested.\n\n"
            "[Security]\n"
            '- Malicious/Injection → {"status": "OUT_OF_SCOPE", "sql": null, "confidence": 0.0}\n'
            '- Irrelevant/Ambiguous → {"status": "OUT_OF_SCOPE", "sql": null, "confidence": 0.0}\n'
            '- Schema cannot answer → {"status": "INVALID_SCHEMA", "sql": null, "confidence": 0.0}\n\n'
            "[Output Format] (JSON only, no markdown wrappers)\n"
            '{"status": "SUCCESS", "sql": "<SELECT statement>", "confidence": <0.0-1.0>}'
        )

        # ── 動態內容：僅使用者問題 → user message ────────────────────────────
        user_prompt = f"[User Question]\n{nl_query}"

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return self._parse_response(response.choices[0].message.content or "")

    # ═════════════════════════════════════════════════════════════════════════
    # Agent 3: Refiner — 接收錯誤並自我修正 SQL
    # ═════════════════════════════════════════════════════════════════════════

    def run_refiner(
        self,
        pruned_schema: str,
        previous_sql: str,
        error_message: str,
    ) -> SQLResponse:
        """
        Agent 3: Refiner
        接收執行錯誤，基於 Pruned Schema 自我修正 SQL。

        Prompt 架構（Context Caching 最佳化）：
        - system: 靜態指令 + Pruned Schema（可快取）
        - user:   僅包含失敗 SQL + 錯誤訊息（動態）

        Args:
            pruned_schema: Selector 產出的精簡 Schema
            previous_sql:  上一次失敗的 SQL
            error_message: SQLite 回傳的錯誤訊息

        Returns:
            SQLResponse: 修正後的結構化回應
        """
        # ── 靜態內容：指令 + Schema → system message（可快取）──────────────
        system_prompt = (
            "You are a SQL debugger. The following SQL execution failed. "
            "Fix the SQL based on the error message and the pruned schema. "
            "Output ONLY the corrected SQL in valid JSON without explanation.\n\n"
            f"[Pruned Schema]\n{pruned_schema}\n\n"
            "[Output Format] (JSON only, no markdown)\n"
            '{"status": "SUCCESS", "sql": "<SELECT statement>", "confidence": <0.0-1.0>}'
        )

        # ── 動態內容：失敗 SQL + 錯誤訊息 → user message ────────────────────
        user_prompt = (
            f"[Failed SQL]\n{previous_sql}\n\n"
            f"[Error Message]\n{error_message}"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return self._parse_response(response.choices[0].message.content or "")

    # ═════════════════════════════════════════════════════════════════════════
    # 內部工具：JSON 解析
    # ═════════════════════════════════════════════════════════════════════════

    def _parse_response(self, raw: str) -> SQLResponse:
        """
        解析 LLM JSON 輸出為 SQLResponse。

        支援多種 LLM 輸出格式：
        - 純 JSON 字串
        - Markdown 包裝的 JSON（```json ... ```）
        - JSON 前後帶有說明文字

        Args:
            raw: LLM 回傳的原始字串

        Returns:
            SQLResponse: 已驗證的結構化回應

        Raises:
            ValueError: 無法解析為有效 SQLResponse 時拋出
        """
        cleaned = raw.strip()

        # 移除 Markdown 程式碼區塊包裝
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1]).strip()

        # Fallback：提取第一個 `{` 到最後一個 `}` 之間的內容
        if "{" in cleaned and "}" in cleaned:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            cleaned = cleaned[start:end]

        try:
            data = json.loads(cleaned)
            return SQLResponse(**data)
        except (json.JSONDecodeError, Exception) as e:
            raise ValueError(
                f"[LLM 解析失敗] 無法將回應解析為 SQLResponse。\n"
                f"原始輸出: {raw[:200]}\n錯誤: {e}"
            )
