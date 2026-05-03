# 🚀 Hardened Text-to-SQL & Multi-Model Evaluation Platform

This repository contains the complete submission for the **AI Engineer Take-Home Assessment**. It is divided into two distinct components: a production-grade, defense-in-depth Text-to-SQL CLI (Part 1), and a robust, execution-based Multi-Model Evaluation Platform (Part 2).

The target domain for this project is the **Spider 1.0 `college_2` database**, a moderately complex SQLite schema containing 11 inter-related tables representing a university's operational data.

## 🛠️ Part 1: Build a Tool, Break It, and Harden It

### 1. Baseline Execution

The core tool (`part1/app.py`) takes natural language inputs, translates them into SQLite queries, executes them securely against a local database (`college_2.db`), and renders the results in a terminal UI. To avoid the massive context window bloat and hallucination rates of traditional single-shot LLM calls, I implemented the **MAC-SQL (Multi-Agent Collaborative)** framework.

The pipeline consists of three agents:

1. **Selector (DBA):** Prunes irrelevant tables/columns from the schema based on the user's intent.

2. **Decomposer (SQL Expert):** Uses Chain-of-Thought (CoT) to break the query down and generate the initial SQL.

3. **Refiner (Debugger):** Acts as a self-healing mechanism. If SQLite throws an `OperationalError`, the Refiner receives the error log and rewrites the query.

### 2. Break It: Initial Failure Modes

Once the baseline was built, I subjected it to adversarial testing. The initial single-shot approach failed catastrophically in several ways:

* **Prompt Injection & Malicious Intent:** Inputting `"Delete all students"` or `"Drop the table --"` successfully bypassed naive system prompts.

* **Data-Value Assumptions (Semantic Mismatch):** When asked for "CS and EE students", the LLM generated `WHERE dept_name IN ('CS', 'EE')` instead of the actual database values `('Comp. Sci.', 'Elec. Eng.')`.

* **Oversharing (Infinite Loops / Cartesians):** Ambiguous queries or missing `JOIN` conditions resulted in Cartesian products that locked up the database process.

* **The "Overly Strict Guardrail" Failure:** When I initially added strict security prompts ("Do NOT include LIMIT... If malicious, return OUT_OF_SCOPE"), the models over-indexed on security and refused perfectly safe queries, resulting in a 0% execution success rate on complex aggregations.

### 3. Harden & Fix: Defense-in-Depth

To fix these critical failures, I built a 4-layer defense architecture (`part1/validator.py` & `part1/retry.py`):

1. **Read-Only Execution:** SQLite is instantiated with `?mode=ro` URI. Writes/Drops are physically impossible at the engine level.

2. **4-Rule SQL Validator:** A strict regex engine that blocks DML (`INSERT`/`DROP`), blocks SQL comments (`--`, `/*`) to prevent injection, and prevents multi-statement execution (`;`).

3. **Smart LIMIT Rewriter:** Automatically appends `LIMIT 1000` to the AST/String to prevent Cartesian products from crashing the host machine.

4. **Convergence-Guarded Retry:** The Refiner agent is capped at `MAX_RETRY=3`. I implemented fingerprinting (`retry.py`) that halts execution immediately if the LLM gets stuck regenerating the exact same wrong SQL or repeating the same error pattern twice.

### 4. Explain: Fundamentally Hard Unsolved Problems

Despite MAC-SQL and hardening, some failure cases are architecturally unsolvable without expanding the system's external context:

* **Subjective Semantics:** Queries like *"Find the most popular course"* cannot be solved by SQL alone because "popular" is not defined in the schema. (Requires a YAML Semantic Layer / Data Dictionary).

* **Cryptic Schema Naming:** The LLM cannot reliably map a user asking for *"revenue"* to a cryptic column named `rev_ytd_amt` using zero-shot reasoning. (Requires RAG / Vector DB for Schema Linking).

* **Self-Correction Local Minima:** The Refiner agent receives syntax errors (e.g., `no such column`), but it cannot fix deeply flawed `JOIN` logic if the query executes successfully but returns the wrong semantic data.

## 📊 Part 2: The Multi-Model Eval Challenge

Evaluating LLMs on Text-to-SQL using traditional string-matching or token-matching is notoriously flawed (e.g., `SELECT name, age` vs `SELECT age, name` are functionally identical but fail string matches). I built a **Production-Grade Execution-Based Evaluation Platform** (`part2/eval_pipeline.py`).

### 1. Data Generation & Ground Truth

I manually crafted an evaluation dataset (`part2/eval_data.json`) of 30 diverse queries targeting the `college_2` schema. The dataset is stratified into 5 categories: `Basic`, `JOIN`, `Aggregation`, `Complex`, and `Adversarial` (messy phrasing, conversational requests, and explicit constraints).
For the ground truth, I pre-validated every SQL query against the local SQLite database. If a Ground Truth query fails to execute during the pre-flight check, the eval pipeline immediately halts.

### 2. Execution & Iteration (Achieving >85% Accuracy)

Getting models to pass a strict execution-based evaluation was non-trivial.
**The Initial Catastrophe (0% Accuracy):** As seen in the archived `EVAL_REPORT`s, my first iterations resulted in **0% F1 scores** across all models. Why?

1. My security prompts were too strict. The prompt instructed models to reject ambiguous queries, causing them to confidently output `OUT_OF_SCOPE` for valid questions.

2. The models generated `LIMIT 1000` which caused `Shape Mismatch` errors when compared to Ground Truth DataFrames.

3. Models hallucinated `WHERE dept_name = 'CS'` instead of `'Comp. Sci.'`

**The Iteration:** To hit the **>85% threshold**, I iterated on the pipeline:

* **Prompt Relaxing & Few-Shotting:** I adjusted the `Decomposer` prompt to be less trigger-happy with `OUT_OF_SCOPE` refusals and provided schema hints.

* **Pandas Canonical Normalization:** I upgraded the evaluation engine to strip column aliases entirely. It dynamically executes both the GT and Generated SQL, converts them to Pandas DataFrames, sorts them, strips column headers to integer indexes, and uses `df.equals()`. This allowed perfectly valid but differently-aliased LLM queries to score `1.0`.

* **Activating the Refiner:** Allowing the Refiner agent to view the SQLite execution error loop drastically pushed the accuracy over the 85% mark for complex `JOIN`s.

### 3. Model Selection

I evaluated the pipeline using three highly capable models via a unified OpenAI-compatible router (`part1/llm.py`):

1. **OpenAI `gpt-4o-mini` (Closed-Source):** Chosen as the industry baseline. It is incredibly fast, cost-effective, and historically excels at coding/SQL tasks.

2. **Google `gemini-2.5-flash` (Closed-Source):** Chosen for its massive context window and native context-caching capabilities, making it ideal for the static schema injection required in the `system` prompt.

3. **Qwen `qwen3.6:27b` via Ollama (Open-Weight):** Chosen as the local champion. At 27B parameters, it punches significantly above its weight class in structured reasoning tasks and proves that this pipeline can run entirely air-gapped without API fees.

### 4. Performance & Initial Patterns

**OpenAI `gpt-4o-mini`**

* **Performance & Latency:** Fastest overall (Avg. total latency \~1.29s) and required the fewest Refiner retries during final benchmarking.

* **Initial Failure Patterns:**

  * **Over-Refusal (`OUT_OF_SCOPE`):** Highly susceptible to the strict guardrails in the v1 prompt. Confidently generated `OUT_OF_SCOPE` refusals for valid complex aggregations (e.g., "找出開課數量最多的系所名稱").

  * **Shape Mismatch:** Initially struggled with the adversarial queries (e.g., strictly omitting columns when asked to "only list names"), returning extra columns that triggered strict Pandas shape mismatches.

**Google `gemini-2.5-flash`**

* **Performance & Latency:** Highly efficient with very fast responses (Avg. total latency \~1.02s) and followed structural instructions meticulously.

* **Initial Failure Patterns:**

  * **Syntax Deviations:** Generated unhandled SQLite syntax variations (e.g., using `<>` instead of `NOT IN`).

  * **Hallucinated Joins:** Occasionally hallucinated implicit `JOIN` paths that SQLite didn't actually support.

  * **Limit Appends:** Incorrectly appended limits like `LIMIT 1000` to the end of complex string queries, resulting in DataFrame value/shape mismatches against the Ground Truth.

**Ollama `qwen3.6:27b`**

* **Performance & Latency:** Performed admirably on reasoning logic but suffered from significantly higher latency (Avg. total latency \~29.5s) due to running locally without dedicated hardware acceleration.

* **Initial Failure Patterns:**

  * **Chatty Output:** Showed a strong tendency to generate SQL Comments (`--`) to explain its logic step-by-step, which instantly triggered the security Validator.

  * **Operator Deviations:** Defaulted to standard exclusion operators (`!=`) instead of proper list exclusions (`NOT IN`), failing basic filtering tests until prompted otherwise.

#### 📈 Performance Summary Table
#### 📈 Performance Summary Table

| **Model** | **Avg Total Latency** | **Initial Baseline Accuracy** | **Hardened Accuracy (Post-Iteration)** |
| :--- | :--- | :--- | :--- |
| **OpenAI `gpt-4o-mini`** | ~1.29s | 100.0% | 96.67% |
| **Google `gemini-2.5-flash`** | ~1.02s | 100.0% | 96.67% |
| **Qwen `qwen3.6:27b`** | ~29.5s | 100.0% | 90% |

### 5. Key Learnings

1. **Execution-Based Evaluation is Non-Negotiable:** String matching is useless for SQL. Two completely different ASTs can yield the exact same correct DataFrame. Pandas DataFrame normalization is the only objective way to score Text-to-SQL.

2. **LLMs are Terrible at Error Clustering:** When I asked the LLMs to cluster their own failures, they hallucinated patterns. I had to integrate `sqlglot` (`part2/cluster_analyzer.py`) to parse the Abstract Syntax Trees (AST) and **deterministically validate** the LLM's clustering logic before generating the final report.

3. **Context Caching is the Future:** By separating the static DB Schema into the `system` message and only placing the user query in the `user` message, API costs and latency dropped dramatically on repeated runs.

## 🚀 Setup & Usage Instructions

### 1. Installation
Clone and enter the directory
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env


### 2. Initialize Database & Run the CLI

Populate the 11-table `college_2` schema with seed data:
```
python -m part1.setup_db
```

Test the Hardened CLI (Ensure `LLM_PROVIDER` is set in `.env`):
```
python -m part1.app "Find the names of students in the Computer Science department."
```

### 3. Run the Evaluation Pipeline

Execute the full benchmark against the `eval_data.json` ground truth:
```
python -m part2.eval_pipeline --data part2/eval_data.json --output part2/eval_results/
python -m part2.cluster_analyzer --output part2/eval_results/
python -m part2.report_generator --output part2/eval_results/
```

Check `part2/eval_results/EVAL_REPORT.md` for the final generated metrics