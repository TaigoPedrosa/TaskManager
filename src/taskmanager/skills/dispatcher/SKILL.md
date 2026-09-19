---
name: dispatcher
description: Use when planning waves, routing tasks, balancing concurrency, and dispatching subagents
---

# Dispatcher Skill

The dispatcher orchestrates agent sessions, plans concurrent waves, matches task difficulty to model tiers, oversees review gates, and manages dependency graph evolution via `taskmanager` (`tm`).

## Core Principles

### 1. Wave Planning & File Disjointness
- Plan and dispatch in **waves** of mutually file-disjoint tasks.
- The wave is the unit of disjointness: never dispatch two concurrent tasks that declare mutations to overlapping files.
- Inspect the declared file lists of candidate tasks rather than task titles or high-level goals.
- Tasks that share a database table or schema file are not disjoint even if their application files differ. Sequence schema changes ahead of consumers, or establish clear single-owner boundaries.
- The TaskManager runtime actively enforces file locks in `runtime.db.file_locks` when agents invoke `tm run start <id> --worktree`. Dispatchers must prevent conflicts upstream rather than relying on runtime collisions.

### 2. Discovering Next Tasks (`tm next`)
Query the heuristic recommendation engine to select the highest-priority unblocked tasks:

```bash
tm next -n 5 --strategy balanced
```

Supported heuristic strategies:
- `balanced`: Multi-factor blend optimizing critical path depth, dependency unblocking factor, explicit priority, and file diversity.
- `critical_path`: Prioritizes tasks with the deepest downstream dependency chains to minimize overall completion latency.
- `quick_wins`: Prioritizes lower-complexity, unblocking tasks to maximize immediate parallelism.
- `unblock_breadth`: Prioritizes tasks that unlock the highest number of direct dependents.

Filter by plan or model capability:

```bash
tm next -n 3 --plan AUTH-CORE --model claude-sonnet-4-6
```

Inspect the dependency graph visualization:

```bash
tm graph --plan AUTH-CORE --mermaid
```

### 3. Model Tier Matching (`acceptable_models`)
Every task node in `spec.db` specifies an `acceptable_models` constraint list. Dispatchers must route tasks only to agents equipped with an acceptable model tier:

| Work Category | Typical `acceptable_models` | Recommended Model Tier |
| :--- | :--- | :--- |
| System architecture, plan briefs, UX/API design decisions | `["claude-opus-4-6-thinking"]` | Opus |
| Migrations, row-level security, tenant boundary, crypto | `["claude-opus-4-6-thinking"]` | Opus (implementation) |
| Feature implementation against written specifications | `["claude-sonnet-4-6", "gemini-3.8-flash-high"]` | Sonnet or Flash |
| Code review & defect detection | `["claude-sonnet-4-6"]` | Sonnet |
| Mechanical refactoring, single-file fixes, lint updates | `["claude-sonnet-4-6", "gemini-3.8-flash-high"]` | Flash / Haiku |

When dispatching, evaluate the candidate's `acceptable_models` attribute and select the most economical tier satisfying the constraint.

### 4. Managing Review Gates
TaskManager supports two review gate mechanisms:

1. **Task-Level Review (`--require-review`)**:
   High-blast-radius tasks (such as migrations or security boundaries) remain blockers until their status reaches `WAITING_MERGE` or `COMPLETED`. When an implementer finishes and calls `tm run stop <task_id> --status WAITING_REVIEW`, dispatch a dedicated review subagent.

2. **Plan-Wide Review Gates**:
   Plans created with `tm plan add --require-review` automatically inject a synthetic review gate node (`<PLAN>-REV<N>`, `kind="review_gate"`). All implementation tasks within the plan feed into this review gate. The plan cannot close until the review gate task completes.

Reviewers verify implementation diffs, run `tm verify run <task_id>`, and report findings or approvals without editing the code.

### 5. Superseding Tasks & Reassigning Blocks
When architectural changes or user requirements render an existing task obsolete, supersede it rather than silently abandoning or deleting it:

```bash
tm task supersede <old_task_id> <new_task_id> --transfer-blocks all
```

Transfer block policies (`--transfer-blocks`):
- `all` (default): All downstream tasks blocked by `<old_task_id>` now depend on `<new_task_id>`.
- `none`: Downstream dependencies on `<old_task_id>` are removed without attaching to `<new_task_id>`.
- `<id1>,<id2>`: Selectively transfer blocker relationships for specified downstream tasks.

The old task transitions to `SUPERSEDED`, recording a traceable event in `ledger.db`.

## Dispatcher Workflow Checklist

1. Run `tm next -n 5 --strategy balanced` to fetch candidates.
2. Verify that none of the candidate tasks share target files with tasks currently in flight (`tm run list`).
3. Match candidate `acceptable_models` to determine the subagent model tier.
4. Render the task brief with `tm render <task_id> --view subagent` and dispatch the implementer.
5. Monitor active execution and sweeps (`tm run list`, `tm run sweep`).
6. When tasks transition to `WAITING_REVIEW`, dispatch a reviewer against the isolated worktree branch.
7. Merge verified worktree branches and mark completion (`tm run stop <task_id> --status COMPLETED --remove-worktree`).
