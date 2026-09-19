# TaskManager CLI — Design Specification

**Date:** 2026-09-19  
**Status:** Approved in Chat, Pending Spec Review  
**Target Repository:** `/Users/taigo.pedrosa/Documents/TaskManager`  
**Package / Executable:** `taskmanager` (alias `tm`)  

---

## 1. Goal

Build a production-grade, open-source-ready local agentic task tracker system as a fast CLI tool (`taskmanager` / `tm`) in Python 3.14 with `uv`, SQLite, and `dishka` dependency injection. 

The system treats task management and plan tracking as an explicit, queryable directed acyclic graph (DAG) stored inside a local `.taskmanager/` directory. It replaces ad-hoc markdown regex parsing with structured, multi-database persistence while dynamically generating token-efficient markdown projections for LLM agents on demand. It provides deterministic concurrency control, git worktree orchestration, hybrid semantic search, AST-based static verification, multi-factor recommendation heuristics, and built-in skills for dispatchers and implementers.

---

## 2. Non-Goals

* External centralized database servers (PostgreSQL/MySQL): SQLite WAL mode is the exclusive storage engine.
* Multi-user cloud web UI: The system is designed for local CLI and agentic execution (single machine, multi-agent / multi-session concurrency).
* Git commit attribution injection: Strict adherence to zero AI-attribution trailers.
* Mandatory reliance on external services: All core features, including graph evaluation, AST verification, and local vector embeddings, work 100% offline.

---

## 3. Architecture & Multi-Database Storage

All project metadata is stored in a `.taskmanager/` directory at the repository's root. For git worktrees, the CLI resolves the root directory via `git rev-parse --git-common-dir`, ensuring all concurrent worktrees access the same database state.

```
<project-root>/
└── .taskmanager/
    ├── spec.db         # Storage & definition: nodes, sections, relations, verifications, embeddings
    ├── runtime.db      # High-frequency runtime: active leases, heartbeats, file locks, sessions
    ├── ledger.db       # Append-only audit trail: immutable log of every CLI action and transition
    ├── config.json     # Configuration: embedding provider, model tiers, worktree defaults
    ├── worktrees/      # Isolated git worktrees managed for active tasks (auto-gitignored)
    └── .gitignore      # Ignores worktrees/ and runtime ephemeral files
```

### Database Separation & Concurrency Controls
1. **`spec.db`**: Read-heavy, holding structural definitions, relationships, and embeddings. Operates in SQLite WAL mode (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`).
2. **`runtime.db`**: High-frequency read/write lease table with short transactions to prevent contention between parallel subagents executing heartbeats or claims.
3. **`ledger.db`**: Append-only log table recording user and agent commands with timestamps, actor IDs, and state diffs for auditability.

---

## 4. Data Model & Schema

### 4.1 `spec.db` Schema

```sql
-- 1. Polymorphic Nodes Table
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,                       -- e.g. "AUTH-S01", "AUTH-P01", "AUTH-T01"
    kind TEXT NOT NULL,                        -- "spec" | "plan" | "task" | "review_gate"
    title TEXT NOT NULL,                       -- Human and agent readable title
    status TEXT NOT NULL,                      -- Stored explicit status (e.g. NOT_STARTED, IMPLEMENTING)
    priority INTEGER NOT NULL DEFAULT 50,      -- 1-100 (higher = higher scheduling priority)
    target_repo TEXT,                          -- Relative repo path or slug for multi-repo environments
    acceptable_models TEXT NOT NULL DEFAULT '[]', -- JSON array of strings: e.g. ["claude-3-7-sonnet", "gemini-3.8-flash-high"]
    frontmatter_json TEXT NOT NULL DEFAULT '{}', -- Arbitrary structured metadata (tags, owners, risk, etc.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Structured Markdown Sections
CREATE TABLE node_sections (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,                 -- "overview", "files", "interfaces", "steps", "rubric"
    ordinal INTEGER NOT NULL,                  -- Display order for rendering
    header TEXT NOT NULL,                      -- e.g. "## Interfaces", "### Implementation Steps"
    content TEXT NOT NULL,                     -- Section markdown body
    PRIMARY KEY (node_id, section_key)
);

-- 3. Unified Graph Relations (Composition & Dependencies)
CREATE TABLE node_relations (
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,               -- "contains", "depends_on", "blocks", "supersedes"
    metadata_json TEXT NOT NULL DEFAULT '{}',  -- e.g. {"strict": true}
    PRIMARY KEY (source_id, target_id, relation_type)
);

-- 4. Declared Static Verifications
CREATE TABLE node_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    verification_type TEXT NOT NULL,           -- "file_exists", "file_absent", "symbol_signature", "ast_export", "test_command"
    target_path TEXT NOT NULL,                 -- Relative file path
    expected_pattern TEXT,                     -- Expected regex, AST signature, or test command
    codegraph_query_json TEXT                  -- Optional structured query payload for codegraph CLI
);

-- 5. Lexical Full-Text Search (FTS5)
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    frontmatter_text,
    content_text,
    tokenize='porter unicode61'
);

-- 6. Native Vector Search via sqlite-vec extension (vec0)
-- Provides in-engine C-level vector indexing and KNN matching
CREATE VIRTUAL TABLE vec_nodes USING vec0(
    node_id TEXT PRIMARY KEY,
    target_type TEXT,                          -- "title" | "frontmatter" | "section" | "full"
    section_key TEXT,                          -- NULL for title/frontmatter/full
    embedding FLOAT[384] DISTANCE_METRIC=cosine
);

CREATE TABLE embedding_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,                    -- "openai" | "local"
    model_name TEXT NOT NULL,                  -- e.g. "text-embedding-3-small" | "all-MiniLM-L6-v2"
    dimensions INTEGER NOT NULL,               -- e.g. 384 or 1536
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 4.2 `runtime.db` Schema

```sql
-- Active Agent Leases
CREATE TABLE leases (
    task_id TEXT PRIMARY KEY REFERENCES nodes(id),
    agent_id TEXT NOT NULL,                   -- Agent/subagent conversation ID or identifier
    session_id TEXT NOT NULL,                 -- Managing session manager / harness ID
    account_id TEXT,                          -- Tenant/developer account reference
    worktree_path TEXT,                       -- Absolute filesystem path to isolated worktree
    branch_name TEXT NOT NULL,                -- e.g. "tm/AUTH-P01/AUTH-T01"
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds INTEGER NOT NULL DEFAULT 300  -- Expiration TTL in seconds
);

-- File-Level Concurrency Locks (Disjointness Protection)
CREATE TABLE file_locks (
    file_path TEXT PRIMARY KEY,               -- Normalized relative repo path: e.g. "src/auth/jwt.py"
    task_id TEXT NOT NULL REFERENCES leases(task_id) ON DELETE CASCADE,
    lock_type TEXT NOT NULL DEFAULT 'write'   -- 'read' | 'write'
);
```

### 4.3 `ledger.db` Schema

```sql
CREATE TABLE ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor_id TEXT NOT NULL,                   -- Agent name, session ID, or user CLI
    command TEXT NOT NULL,                    -- CLI subcommand executed
    target_id TEXT,                           -- Target node ID (if applicable)
    payload_json TEXT NOT NULL DEFAULT '{}',  -- Command arguments and parameters
    diff_json TEXT NOT NULL DEFAULT '{}'      -- Before/after state diff of affected entities
);
CREATE INDEX idx_ledger_target ON ledger_events(target_id);
```

---

## 5. Semantic Short IDs & Relations

### 5.1 Hierarchical Composable Identifiers & Configuration
Node identifiers compose hierarchically down the tree (`spec -> plan -> task`), allowing fully semantic slugs, hybrid slugs, or pure auto-incremented numbering:

* **Composition Logic**:
  * **Spec Level**: Defines the root identifier, either an explicit semantic slug (`AUTH`) or auto-incremented (`S1`).
  * **Plan Level**: Composed as `<spec_id><separator><plan_segment>`:
    * Semantic slug provided (`USER`): `AUTH-USER` (or `S1-USER`).
    * No slug provided (auto-incremented): `AUTH-P1` (or `S1-P1`).
  * **Task Level**: Composed as `<plan_id><separator><task_segment>`:
    * Semantic slug provided (`LOGIN`): `AUTH-USER-LOGIN` (or `AUTH-P1-LOGIN`).
    * No slug provided (auto-incremented): `AUTH-USER-T1` (or `S1-P4-T17`).

* **Configurable Enforcement (`.taskmanager/config.json`)**:
  ```json
  {
    "naming": {
      "separator": "-",
      "spec": { "prefix": "S", "require_slug": false, "pad": 1 },
      "plan": { "prefix": "P", "require_slug": false, "pad": 1 },
      "task": { "prefix": "T", "require_slug": false, "pad": 1 }
    }
  }
  ```
  * `require_slug`: When enabled for a specific level (`spec`, `plan`, or `task`), the CLI enforces that creation commands provide an explicit semantic slug (e.g. `tm task add "User login flow" --plan AUTH-USER --slug LOGIN`), rejecting fallback auto-increments.
  * `prefix`: Determines the prefix used when auto-incrementing at that level (`S`, `P`, `T`).
  * `separator`: Configurable token separator (defaults to `-`, supports `/` or `_`).
  * `pad`: Numeric padding for counters (e.g. `1` produces `T1`, `2` produces `T01`).

### 5.2 Composition and Edge Semantics
* `contains`: Models hierarchical composition (`Spec contains Plan`, `Plan contains Task`).
* `depends_on`: Models prerequisite DAG dependencies (`Task B depends_on Task A`).
* `blocks`: Inverted representation of `depends_on` (`Task A blocks Task B`).
* `supersedes`: Indicates a node replaces a deprecated or refactored node (`Task B supersedes Task A`).

### 5.3 Superseding Block Transfer
When executing `tm task supersede <old-id> <new-id>`, the user or agent specifies how downstream dependencies are reassigned via `--transfer-blocks`:
* `all` (default): All nodes that were blocked by `<old-id>` now become blocked by `<new-id>`.
* `none`: Dependency links are not transferred; `<old-id>` is marked `SUPERSEDED`, resolving blocks without transferring to `<new-id>`.
* `<id1>,<id2>`: Only the explicitly specified downstream tasks transfer their prerequisite link to `<new-id>`.

---

## 6. State Machine & Virtual Resolution

### 6.1 Explicit Stored Statuses (`nodes.status`)
1. `NOT_STARTED`: Created but unstarted.
2. `IMPLEMENTING`: Active development underway in an assigned worktree.
3. `WAITING_REVIEW`: Development complete and static verifications passed; queued for review.
4. `REVIEWING`: Under active code or design review.
5. `WAITING_FIXES`: Review returned defects; queued for remediation.
6. `FIXING`: Active defect remediation in progress.
7. `WAITING_MERGE`: Reviews and verifications passed; awaiting integration/merge into base branch.
8. `COMPLETED`: Finished, merged, and verified.
9. `SUPERSEDED`: Replaced by a newer task or architecture.
10. `ABANDONED`: Cancelled or discarded.
11. `DEFERRED`: Postponed to a later cycle.

### 6.2 Deterministic Virtual Statuses (Computed on Read)
Virtual statuses are computed dynamically via SQL CTEs or in-memory graph resolution:
* **`BLOCKED`**: The node is not finished (`COMPLETED` or `SUPERSEDED`), and at least one prerequisite `depends_on` node has not yet reached `COMPLETED` or `SUPERSEDED`.
* **`READY`**: The node is `NOT_STARTED`, all prerequisite dependencies are resolved, and none of its declared files overlap with any active lock in `file_locks`.
* **`IN_FLIGHT`**: The node has an active, unexpired lease in `runtime.db.leases`.
* **Plan-Level Rollup**:
  * `NOT_STARTED`: 100% of children are `NOT_STARTED`.
  * `COMPLETED`: 100% of children are `COMPLETED` or `SUPERSEDED`.
  * `BLOCKED`: All uncompleted children are currently `BLOCKED`.
  * `IN_PROGRESS`: Any child is `IN_FLIGHT`, `IMPLEMENTING`, `REVIEWING`, or partially completed.

### 6.3 Automated Review & Fix DAG Injection
1. **Strict Per-Task Review (`--review=strict`)**:
   * For tasks with high blast radius (migrations, crypto, security, core contracts). Downstream tasks depending on this task remain `BLOCKED` until this task transitions to `WAITING_MERGE` or `COMPLETED`.
2. **Plan-Wide Review Gate (`tm plan add --require-review`)**:
   * Generates a synthetic node `[PLAN_PREFIX]-REV[N]` with `kind = 'review_gate'`.
   * Automatically establishes `depends_on` edges from all implementation tasks of the plan to the review gate.
   * Plan closure and downstream plans depend on the review gate reaching `COMPLETED`.

---

## 7. Concurrency, Git Worktrees & Leases

### 7.1 Collision Prevention & File Disjointness
Tasks declare files they modify in their `files` section or `node_verifications`.
* When an agent attempts `tm run start <task-id>`, the engine inspects active leases in `runtime.db.leases`.
* If any declared file matches an active lock in `runtime.db.file_locks` whose lease has not expired (`now - last_heartbeat <= ttl_seconds`), the command is rejected with an exit code indicating a file conflict, printing the colliding task ID and holder agent ID.

### 7.2 Semantic Branch & Worktree Composition
* **Branch Name**: `tm/<plan-id>/<task-id>` or `tm/<task-id>`.
* **Worktree Path**: `.taskmanager/worktrees/<task-id>`.
* **Isolation**: Worktrees are initialized off the plan's integration branch or `main`:
  ```bash
  git worktree add -b tm/AUTH-P01/AUTH-T01 .taskmanager/worktrees/AUTH-T01 <base_branch>
  ```

### 7.3 Deterministic Heartbeat & TTL Cleanup
* **Heartbeat**: Agents ping `tm run heartbeat <task-id>` periodically.
* **Auto-Sweep**: `tm run sweep [--ttl-grace <seconds>]` detects expired leases (`now - last_heartbeat > ttl_seconds`), releases associated `file_locks`, transitions node status to `NOT_STARTED` or flags for recovery, and logs a timeout warning in `ledger.db`.

---

## 8. Static Verification Engine & AST / Codegraph CLI

### 8.1 Verification Types
* `file_exists`: Validates file presence at path.
* `file_absent`: Validates file was removed/deleted.
* `symbol_signature`: In-process AST parsing of Python source using `ast.parse()` to inspect functions, methods, parameters, type annotations, and classes.
* `ast_export`: Checks module-level `__all__` or public exports.
* `test_command`: Runs targeted shell test command and asserts exit code 0.
* `codegraph_query`: Optional structural query executed via the `codegraph` CLI if installed on the host.

### 8.2 Execution & Fallback
* When running `tm verify run <task-id> [--worktree <path>]`:
  1. The CLI executes fast, zero-dependency in-process AST and filesystem checks.
  2. If a verification specifies a `codegraph_query`, the CLI checks if `codegraph` is available on the system PATH. If available, it invokes `codegraph` CLI to verify symbol call graphs or structural relationships; if absent, it logs an informational notice and relies on the AST assertions.
  3. Returns a structured report detailing pass/fail status per assertion.
* **Gate Enforcement**: `tm run stop <task-id> --status WAITING_REVIEW` automatically runs verification. If any assertion fails, the transition is halted unless explicitly forced with `--force`.

---

## 9. Recommendation Heuristics Engine (`next-tasks`)

### 9.1 Multi-Factor Scoring Formula
Given eligible candidate tasks (virtually `READY` and free of active file collisions), the engine scores each task $T$ belonging to plan $P$ on a scale of $0$ to $100$:

$$\text{Score}(T) = w_{\text{prio}} \cdot S_{\text{prio}}(T, P) + w_{\text{unlock}} \cdot S_{\text{unlock}}(T) + w_{\text{close}} \cdot S_{\text{close}}(P) + w_{\text{adv}} \cdot S_{\text{adv}}(T, P)$$

1. **Priority Factor ($S_{\text{prio}}$)**: Normalized blend of task priority (70%) and parent plan priority (30%). Default weight $w_{\text{prio}} = 0.35$.
2. **Unlocking Factor ($S_{\text{unlock}}$)**: Topological degree centrality. Count of downstream tasks across all plans that are currently blocked by $T$. Promotes unblocking wide parallel execution tracks. Default weight $w_{\text{unlock}} = 0.30$.
3. **Plan Closeness Factor ($S_{\text{close}}$)**: Ratio of completed tasks in $P$ to total tasks in $P$. Boosts tasks in plans that are close to completion, encouraging branch closure and merging. Default weight $w_{\text{close}} = 0.20$.
4. **Plan Advancement Bonus ($S_{\text{adv}}$)**: Binary boost ($100$ if $T$ is the last remaining uncompleted task of plan $P$, else $0$). Default weight $w_{\text{adv}} = 0.15$.

### 9.2 Dispatcher Strategies & Multi-Model Filtering
* `--strategy balanced`: Default formula above.
* `--strategy unblock-first`: Boosts $w_{\text{unlock}} = 0.60$ (ideal for dispatching parallel waves).
* `--strategy finish-plans`: Boosts $w_{\text{close}} = 0.50, w_{\text{adv}} = 0.30$ (ideal for closing active worktrees).
* `--strategy priority-strict`: 100% priority ordering.
* `--model <name>`: Filters only tasks whose `acceptable_models` JSON array contains `<name>` (or where `acceptable_models` is empty/unrestricted).

---

## 10. Semantic & Hybrid Search (sqlite-vec Extension)

### 10.1 In-Engine Vector Search (`sqlite-vec`)
Rather than pulling vectors into Python to compute cosine distance in user-space, `taskmanager` loads the official `sqlite-vec` C extension into SQLite connections (`sqlite_vec.load(conn)`).
* **Native KNN Virtual Table**: Vectors are stored in a `vec0` virtual table (`vec_nodes`) with indexed nearest-neighbor lookup (`MATCH :query_vector AND k = :limit`).
* **Scale**: Runs entirely inside SQLite's C core, scaling to hundreds of thousands of vectors with sub-millisecond query latency.
* **Configurable Dimensions**: The vector dimension matches the active model (e.g. 384 for local MiniLM, 1536 for OpenAI `text-embedding-3-small`), tracked in `embedding_metadata`.

### 10.2 Storage & Dual Providers
* **Storage**: FTS5 table `nodes_fts` for lexical keyword search + `sqlite-vec` virtual table `vec_nodes` for vector embeddings.
* **Environment Configuration**: Explicit prefixing avoids credential bleeding:
  * `TASKMANAGER_EMBEDDING_PROVIDER`: `openai` | `local` | `none`
  * `TASKMANAGER_OPENAI_BASE_URL`: API base URL (e.g. `https://api.openai.com/v1` or local Ollama/vLLM)
  * `TASKMANAGER_OPENAI_API_KEY`: API authentication key
  * `TASKMANAGER_OPENAI_MODEL`: Embedding model name (e.g. `text-embedding-3-small`)
  * `TASKMANAGER_LOCAL_MODEL`: Local HuggingFace / sentence-transformers model (e.g. `all-MiniLM-L6-v2`)

### 10.3 In-Database Hybrid Query Execution
`tm search "<query>" [options]`:
* `--target title|frontmatter|content|all`
* `--kind spec,plan,task` (supports comma-separated multi-select)
* `--status READY,IMPLEMENTING,COMPLETED` (supports real and virtual statuses, multi-select)
* `--limit <N>`
* **Execution**:
  1. Generates query vector via the configured provider.
  2. Executes in-database KNN search joined directly with relational filtering:
     ```sql
     SELECT
         n.id,
         n.title,
         n.status,
         v.target_type,
         v.section_key,
         v.distance
     FROM vec_nodes v
     JOIN nodes n ON n.id = v.node_id
     WHERE v.embedding MATCH :query_vector
       AND k = :limit
       AND (:status_filter IS NULL OR n.status IN (:statuses))
       AND (:kind_filter IS NULL OR n.kind IN (:kinds))
     ORDER BY v.distance ASC;
     ```
  3. Blends in-database vector rankings with FTS5 lexical scores using Reciprocal Rank Fusion (RRF) when hybrid mode is active.

---

## 11. Dynamic Rendering & Projections

`taskmanager` does not persist markdown files to disk, keeping the database as the sole source of truth while rendering dynamic markdown projections tailored for LLM context windows:

* **`tm render <id> --view summary`** (Dispatcher View):
  * Emits YAML frontmatter (ID, kind, title, status, priority, acceptable models, target repo) and a one-paragraph overview. Minimal token footprint.
* **`tm render <id> --view subagent`** (Implementer Brief View):
  * Emits parent plan context, global constraints, declared files, interfaces, implementation steps, and verification assertions. Ready for direct injection into a subagent prompt.
* **`tm render <id> --view full`**:
  * Emits complete reconstructed markdown document with all sections and metadata.

---

## 12. CLI Command Surface & High-Density Bulk Operations

### 12.1 High-Density Bulk Operations (Agent-Optimized)
To avoid high turn counts and excessive bash roundtrips:
* **`tm import --format markdown|yaml|json [--file <path> | stdin]`**:
  * Ingests an entire spec document containing multiple plans, tasks, sections, and dependencies in a single invocation, creating all graph nodes and edges atomically.
* **`tm batch update --file <updates.json>`**:
  * Applies multiple status transitions, priority adjustments, or relation links in one call.

### 12.2 Full Command Reference

```bash
# Workspace Management
tm init [--path <dir>]
tm config set <key> <value>
tm config get <key>

# Node Administration
tm spec add <title> [--prefix <p>] [--priority <n>]
tm plan add <title> --spec <id> [--require-review] [--priority <n>] [--repo <r>]
tm task add <title> --plan <id> [--depends-on <ids>] [--models <m1,m2>] [--priority <n>] [--require-review]
tm task supersede <old-id> <new-id> [--transfer-blocks all|none|<ids>]
tm section set <node-id> <key> [--header <h>] [--content <text> | --file <path>]
tm section get <node-id> <key>

# Recommendations & Discovery
tm next [-n 5] [--plan <id>] [--model <m>] [--strategy <strat>] [--json]
tm graph [--mermaid] [--plan <id>]
tm search "<query>" [--kind <k1,k2>] [--status <s1,s2>] [--target <t>] [--limit <n>]

# Runtime & Execution
tm run start <task-id> [--worktree] [--agent <id>] [--session <id>] [--account <id>]
tm run heartbeat <task-id>
tm run stop <task-id> [--status <status>] [--remove-worktree]
tm run list [--json]
tm run sweep [--ttl-grace <sec>]

# Static Verification
tm verify add <task-id> --type <type> --target <path> [--pattern <pat>]
tm verify run <task-id> [--worktree <path>]

# Projections & Bulk
tm render <node-id> [--view summary|subagent|full]
tm import --format <markdown|yaml|json> [--file <path>]
tm export [--plan <id>] --format <markdown|yaml|json>
tm batch update --file <updates.json>

# Audit Ledger
tm audit list [--target <id>] [--limit <n>]
```

---

## 13. Bundled Skills

The package bundles two specialized agent skills:

1. **`taskmanager` Skill (`skills/taskmanager/SKILL.md`)**:
   * Instructions for worker/implementer agents: how to read task briefs with `tm render <id> --view subagent`, start worktrees with `tm run start <id> --worktree`, run heartbeats, perform verification with `tm verify run`, and stop upon review readiness.
2. **`dispatcher` Skill (`skills/dispatcher/SKILL.md`)**:
   * Instructions for session managers and dispatching agents: wave planning, collision prevention (disjoint file lists), model tier matching from `acceptable_models`, multi-task scheduling with `tm next`, review gate orchestrations, and branch/worktree cleanup.

---

## 14. Testing & Quality Strategy

1. **Unit Tests (`tests/unit/`)**:
   * SQLite relational schemas, constraints, and cascading deletes.
   * DAG cycle detection and topological sorting.
   * Multi-factor heuristic scoring calculations.
   * In-process AST signature matching.
   * Markdown frontmatter and section parser/renderer roundtrips.
2. **Integration Tests (`tests/integration/`)**:
   * Git worktree creation, branch isolation, and removal.
   * Active lease acquisition, collision rejection, and TTL sweep expiration.
   * Full lifecycle: `tm import` -> `tm next` -> `tm run start` -> `tm verify run` -> `tm run stop` -> plan rollup.
3. **Quality Gates**:
   * Linting: `ruff check` and `ruff format --check`.
   * Type Safety: `mypy --strict src/taskmanager`.
   * Test Coverage: `pytest -v --cov=taskmanager --cov-report=term-missing`.
