---
name: taskmanager
description: Use when executing, tracking, or updating tasks and plans via the taskmanager CLI
---

# TaskManager Agent Skill

The `taskmanager` (`tm`) CLI provides structured task lifecycle management, isolated git worktree orchestration, deterministic concurrency control, static verification, and hierarchical DAG navigation.

## Implementer Workflow

Implementers claim, execute, verify, and complete assigned tasks using the following sequence:

### 1. Read Task Brief
Retrieve the focused, token-efficient projection of the task:

```bash
tm render <task_id> --view subagent
```

The `--view subagent` projection contains the task objective, interfaces, implementation steps, dependencies, and declared verification checks without irrelevant plan overhead.

### 2. Claim Task & Enter Worktree
Claim the task lease, acquire file locks, and initialize an isolated git worktree:

```bash
tm run start <task_id> --worktree
```

Optional flags:
- `--agent <agent_id>`: Associate the lease with an agent identifier.
- `--session <session_id>`: Associate with a dispatcher session.

When executing within the resulting worktree directory, subsequent CLI commands automatically resolve the active task when `<task_id>` is omitted.

### 3. Maintain Active Heartbeats
While performing active development, periodically pulse a heartbeat to keep the lease active:

```bash
tm run heartbeat <task_id>
```

*(Inside the task worktree, `<task_id>` may be omitted: `tm run heartbeat`).*
Leases have a configured TTL. Unrenewed leases are subject to cleanup by `tm run sweep`.

### 4. Query Qualified Paths & Sections
TaskManager models tasks and sections hierarchically using qualified paths in the format `<slug>:<section_key>`:

- `AUTH-USER-LOGIN:steps`: Implementation steps for the `AUTH-USER-LOGIN` task.
- `AUTH-USER-LOGIN:interfaces`: Declared interfaces or types for the task.
- `AUTH-USER:context`: Plan-level context and constraints.
- `AUTH:overview`: Spec-level overview.

Fetch or inspect specific sections directly:

```bash
tm section get AUTH-USER-LOGIN:steps
```

Inside an active worktree, use contextual relative paths:

```bash
tm section get :steps
```

### 5. Run Static Verification
Before marking a task ready for review, run declared static verification checks:

```bash
tm verify run <task_id>
```

Static verification validates:
- Target file presence/absence (`file_exists`, `file_absent`).
- AST exported symbols and signatures (`symbol_signature`, `ast_export`).
- Configured automated test commands (`test_command`).

Fix any reported failures before proceeding to handoff.

### 6. Stop and Hand Off for Review
Once changes are committed and verification passes, release the lease and file locks, setting the status to `WAITING_REVIEW`:

```bash
tm run stop <task_id> --status WAITING_REVIEW
```

To clean up the temporary git worktree upon completion or handoff, add `--remove-worktree`:

```bash
tm run stop <task_id> --status WAITING_REVIEW --remove-worktree
```

## Command Reference

| Command | Description |
| :--- | :--- |
| `tm render <task_id> --view subagent` | Render concise subagent brief |
| `tm render <qualified_path>` | Render node or section projection |
| `tm run start <task_id> --worktree` | Claim lease, acquire locks, create worktree |
| `tm run heartbeat [<task_id>]` | Renew lease TTL |
| `tm section get <qualified_path>` | Inspect specific markdown section |
| `tm verify run [<task_id>]` | Execute AST and static verification suite |
| `tm run stop [<task_id>] --status <status>` | Release lease, unlock files, transition status |
