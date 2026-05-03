# Hardened Text-to-SQL CLI (MAC-SQL Edition)

A production-grade, defense-in-depth Text-to-SQL command-line tool powered by the **MAC-SQL (Multi-Agent Collaborative)** framework. This system translates natural language queries into executable SQLite SQL against the Spider 1.0 `college_2` dataset. Rather than relying on a single LLM call, it orchestrates three specialized agents — **Selector**, **Decomposer**, and **Refiner** — to achieve higher accuracy and self-healing capabilities.

---

## ✨ Key Features

- **🤖 MAC-SQL Multi-Agent Architecture** — Three collaborative LLM agents (Schema Selector → CoT Decomposer → Error Refiner) replace the traditional single-call pattern, dramatically reducing hallucinations and improving SQL correctness.
- **🌐 Multi-Provider LLM Support** — Seamlessly switch between **OpenAI** (`gpt-4o-mini`), **Google Gemini** (`gemini-1.5-flash`), and **Ollama** (local open-weight models) via a single `LLM_PROVIDER` environment variable. All providers use a unified OpenAI-compatible interface.
- **⚡ Context Caching Optimization** — Prompts are architecturally split: large, static content (DB schema, rules, security constraints) lives in the `system` message (cacheable), while only the dynamic user question occupies the `user` message. This maximizes LLM cache hit rates and reduces latency/cost.
- **🛡️ Defense-in-Depth Security** — Read-only SQLite execution (`?mode=ro`), 4-rule SQL validation (blocks DML, comments, multi-statements), smart `LIMIT 1000` rewriting, and convergence-guarded retries (`MAX_RETRY=3`) ensure safety against prompt injection, SQL injection, and infinite loops.
- **🧪 35 Adversarial Tests** — A comprehensive `test_break.py` suite covering 14 attack categories passes in **0.03s**, proving the hardening is battle-tested.

---

## 🏗️ System Architecture

The following diagram illustrates how a user's natural language query flows through the MAC-SQL pipeline:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User Natural Language Query                  │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Agent 1: SELECTOR (Database Administrator)                        │
│  ─────────────────────────────────────────                         │
│  Input:  User question + Full DB Schema (11 tables)                │
│  Action: Prune irrelevant tables & columns                         │
│  Output: Pruned Schema (only relevant tables)                      │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Agent 2: DECOMPOSER (SQLite Expert)                               │
│  ─────────────────────────────────────                              │
│  Input:  User question + Pruned Schema                             │
│  Action: Chain-of-Thought reasoning → Break into sub-questions     │
│  Output: Initial SQL (as structured JSON with confidence score)    │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SECURITY GATE                                                      │
│  ─────────────                                                      │
│  1. validator.py  →  Block DML, comments, multi-statements          │
│  2. validator.py  →  Smart LIMIT 1000 rewrite                       │
│  3. database.py   →  Execute on read-only SQLite (?mode=ro)         │
└──────────┬──────────────────────────────────┬───────────────────────┘
           │ ✅ Success                        │ ❌ sqlite3.OperationalError
           ▼                                   ▼
┌──────────────────────┐    ┌─────────────────────────────────────────┐
│  Return Results      │    │  Agent 3: REFINER (SQL Debugger)        │
│  (Rich Table UI)     │    │  ─────────────────────────────────      │
└──────────────────────┘    │  Input:  Pruned Schema + Failed SQL +   │
                            │          Error Message                   │
                            │  Action: Self-correct SQL               │
                            │  Output: Fixed SQL → Re-execute         │
                            │  Guard:  RetryController (max 3,        │
                            │          convergence detection)          │
                            └─────────────────────────────────────────┘
```

### Prompt Structure (Context Caching Optimized)

All agent prompts follow a strict **static/dynamic separation** to maximize LLM context caching:

| Message Role | Content Type | What Goes Here |
| :--- | :--- | :--- |
| **`system`** | Static (cacheable) | Agent instructions + DB Schema + Strict rules + Security constraints |
| **`user`** | Dynamic (per-turn) | User question only (Selector/Decomposer) or Failed SQL + Error (Refiner) |

---

## 🚀 Setup & Installation

> **All commands must be run from the project root** (`text_to_sql/`).

### 1. Install Dependencies
```bash
conda activate text_to_sql
pip install -r requirements.txt
```

### 2. Configure Environment Variables
```bash
cp .env.example .env
```

Edit `.env` and set the provider you want to use:

```env
# ── Choose ONE provider ──────────────────────────
LLM_PROVIDER=gemini          # Options: openai | ollama | gemini

# ── OpenAI ───────────────────────────────────────
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o-mini

# ── Google Gemini ────────────────────────────────
GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-1.5-flash

# ── Ollama (Local) ───────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen3.6:27b

# ── System ───────────────────────────────────────
MAX_RETRY=3
```

### 3. Initialize the Database
```bash
python -m part1.setup_db
```
This creates the Spider 1.0 `college_2` SQLite database with **11 tables and 125 rows**. The script is idempotent — safe to run multiple times.

---

## 💻 Usage

### Single Query Mode
```bash
python -m part1.app "List all students in Computer Science"
python -m part1.app "列出修超過平均學分數的學生"
python -m part1.app "Find the average salary of instructors in the Physics department"
python -m part1.app "Show students and their advisor's name"
```

### What You'll See in the Terminal
The Rich UI displays the progress of each agent in real-time:
```
🤖 Agent 1: Selector 正在過濾 Schema...
🤖 Agent 2/3: 正在生成/修正 SQL...
✅ 查詢結果 (5 筆)
┌──────────┬───────────────┐
│ name     │ dept_name     │
├──────────┼───────────────┤
│ Zhang    │ Comp. Sci.    │
│ ...      │ ...           │
└──────────┴───────────────┘
```

---

## 🛡️ Security & Observability

This system assumes the LLM is **unreliable and adversarially breakable**. Every generated SQL passes through multiple defense layers before touching the database:

| Layer | Module | Mechanism |
| :--- | :--- | :--- |
| **Schema Pruning** | `mac_agent.py` | Selector agent removes irrelevant tables/columns, shrinking the attack surface. |
| **Prompt Hardening** | `mac_agent.py` | Strict system prompts enforce SELECT-only output and block injection patterns. |
| **SQL Validation** | `validator.py` | 4-rule validator rejects `INSERT`, `DROP`, `DELETE`, SQL comments (`--`, `/* */`), and multi-statement attacks (`;`). |
| **Read-Only Execution** | `database.py` | SQLite connection enforces `?mode=ro` URI — writes are physically impossible. |
| **Smart LIMIT Rewrite** | `validator.py` | Automatically appends `LIMIT 1000` to queries missing a LIMIT clause. |
| **Convergence-Guarded Retry** | `retry.py` | Two convergence guards detect stuck LLMs: (1) identical SQL re-generation → STOP, (2) repeated error fingerprint ≥2 → STOP. Hard cap at `MAX_RETRY=3`. |

### Adversarial Test Suite
```bash
PYTHONPATH=. pytest part1/tests/test_break.py
```
**Result: 35 passed in 0.03s** across 14 attack categories:
- ✅ Prompt Injection (3) · SQL Injection (3) · Comment Attack (2) · Multi-statement (2)
- ✅ Hallucination traps (3) · Invalid JOINs (2) · Typo robustness (3) · Chinese queries (2)
- ✅ Ambiguous queries (2) · Convergence guards (2) · Read-only protection (2) · LIMIT rewriter (5)

---

## 📂 Project Structure

```text
text_to_sql/                    ← Project root (run all commands from here)
├── .env.example                ← Environment variable template
├── .env                        ← Your local configuration
├── requirements.txt            ← Python dependencies
├── college_2.db                ← Read-only SQLite database
│
├── part1/                      ← Text-to-SQL Core System
│   ├── __init__.py
│   ├── app.py                  ← CLI entry point & MAC-SQL orchestrator
│   ├── mac_agent.py            ← ⭐ MAC-SQL Pipeline (Selector/Decomposer/Refiner)
│   ├── llm.py                  ← Legacy LLM router & Pydantic SQLResponse model
│   ├── database.py             ← Read-only SQLite engine (?mode=ro)
│   ├── validator.py            ← SQL security validator & smart LIMIT rewriter
│   ├── retry.py                ← Error classifier & convergence-guarded retry
│   ├── setup_db.py             ← Idempotent database seeder (11 tables, 125 rows)
│   └── tests/
│       └── test_break.py       ← 35 adversarial tests (14 categories)
│
└── part2/                      ← Evaluation & Analysis Pipeline (see part2/README.md)
```

---

## 🧠 Design Decisions & Known Limitations

### Design Decisions
1. **Composite PK/FK in SQLite** — `section.time_slot_id` cannot reference `time_slot`'s 4-column composite PK. We retain the column with type constraints but drop the FK, testing whether the LLM can infer JOIN paths without explicit FK hints.
2. **UNION + LIMIT Conflict** — The smart LIMIT rewriter appends `LIMIT 1000` strictly at the end of the entire SQL statement, avoiding syntax errors when `UNION` is used.
3. **Comment Attack Priority** — The validator uses a "keyword-first block" strategy: `DROP` is caught before `--` comment parsing, ensuring double-layer protection.
4. **Single-Agent → Multi-Agent Evolution** — The original single LLM call caused context window bloat and frequent hallucinations. MAC-SQL splits responsibilities across three focused agents, each operating on a minimal, task-specific context.

### Fundamentally Hard Problems
Even with MAC-SQL, certain failure modes remain **architecturally unsolvable** without external systems:

| Problem | Example | Root Cause | Solution Path |
| :--- | :--- | :--- | :--- |
| **Subjective semantics** | *"Find the most popular course"* | "Popular" has no SQL definition | Semantic Layer / Data Dictionary |
| **Cryptic schema naming** | `rev_ytd_amt` vs. *"revenue"* | Lexical distance too large for LLM | Embedding-based Schema Linking (RAG) |
| **Self-correction local minimum** | Refiner can't fix wrong JOIN paths | Error feedback is syntactic, not semantic | ER diagram / Join Graph injection |
| **Unsupported SQL functions** | *"Find the median"* in SQLite | `MEDIAN()` doesn't exist in SQLite | Allow Python/Pandas code generation |

> **💡 To solve these**, the system would need an **Agentic Data Stack**: Vector DB for semantic schema search, YAML Data Dictionary for business logic, and multi-step analytical code execution (Python/Pandas) beyond pure SQL.