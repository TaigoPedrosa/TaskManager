# Implementing a task

For the agent that builds a `READY` task and hands it to review with the work committed on its own branch.

## 1. Read the brief

```
tm render <task-id> --view subagent
```

That is the whole assignment: the task's frontmatter, its parent's context, its sections and its verifications. Then, only as needed:

- `tm task get <task-id> --yaml` — state, `target_repo`, `depends_on`, `blocked_by`, `declared_files`, verifications, current lease.
- `tm section get <task-id>:<key>` — one section; `tm section get <task-id>` prints them all.

## 2. Claim it

```
tm run start <task-id> --worktree --worktree-dir <dir> --agent <name> --session <id> --ttl 900
```

It sets the task to `IMPLEMENTING`, locks every path the task declares, and prints the worktree it created. The worktree is `<dir>/<target_repo>-<task-id>`; its branch is `tm/<task-id>`, cut from that repository's `origin/main` (from `HEAD` when the repository has no `origin/main`) with no upstream, so a bare `git push` cannot reach main. Without `--worktree-dir`, worktrees go to `$TM_WORKTREES`, else `<project root>/.worktrees`. Without `--ttl`, the lease lasts 300 seconds.

It refuses, exit 1, and claims nothing when:

| Message | What it means |
|:--|:--|
| `Task <id> is not ready to start (current state: BLOCKED)` | a dependency is unfinished — report it, start nothing |
| `Task <id> is not ready to start (current state: IN_FLIGHT)` | another agent holds the lease |
| `Task <id> is not ready to start (current state: IMPLEMENTING)` | a lease expired and has not been swept yet; the dispatcher runs `tm run sweep`, which returns the task to `NOT_STARTED` |
| `Cannot claim task <id> due to file collision: {'<path>': 'Task: <other-id>, Agent: <agent>'}` | a live lease holds a path you declared — report it, wait, claim nothing |
| `<dir> is not a git repository: set the task's target_repo` | the task's `target_repo` is wrong or unset |
| `Invalid value: no .taskmanager at <dir>: ...` (exit 2) | you are outside the project — pass `-C <project root>` or set `TM_ROOT` |

## 3. Work in that worktree and nowhere else

Every read, edit, command and commit happens under the printed worktree path. Check the prefix of each path you edit, not just its basename: the same file exists in the project's own checkout. Commit on `tm/<task-id>` with an explicit pathspec. Do not merge, do not rebase, do not push any other branch.

Inside the worktree, `tm` finds the project and the task by itself, so `tm run heartbeat`, `tm verify run` and `tm run stop` take no id.

## 4. Keep the lease alive

```
tm run heartbeat
```

Run it whenever you have been working longer than the TTL. `No active lease found for <id>` (exit 1) means the lease is gone and another agent may now hold the task: stop editing and report.

## 5. Verify

```
tm verify run <task-id>
```

A table of the task's checks, exit 1 if any failed. What each type asserts:

- `file_exists` / `file_absent` — the path is there, or is not.
- `symbol_signature` — a `def`, `async def` or `class` of that name parses in that file.
- `ast_export` — that name is in the file's `__all__`, or is a public top-level `def`/`class`.
- `test_command` — the command runs in a shell; exit 0 passes.
- `codegraph_query` — passes with `codegraph CLI not installed; skipped` where that tool is absent.

Two things to know before you trust it. Paths and commands resolve against the **project root**, never against your worktree, so the path checks stay red until the work is merged — before then, read the table as the acceptance list and confirm every row by hand inside the worktree. And `No verifications to run.` exits 0: a task with no checks has not passed anything, and that is worth a line in your report.

## 6. Hand it off — on every exit path

```
tm run stop <task-id> --status WAITING_REVIEW
```

`--status NOT_STARTED` instead when you are blocked or out of scope; the task returns to the ready list with the branch intact. Leave the worktree in place: the fix round reuses it, and the merge role removes it. Do this before you go idle, every time, including when you failed.

## 7. Report

Task id, branch name, the commits you made, the `tm verify run` exit code and which rows failed, and anything you could not do. Where the brief contradicts the tree — a file that does not exist, an interface that already differs — record the discrepancy in the report, implement against the tree, and keep going.

## Never

- Never open, copy or edit anything under `.taskmanager/`; the CLI is the only writer.
- Never set `COMPLETED`, `WAITING_MERGE` or `WAITING_FIXES`; those belong to the reviewer and the merge role.
- Never edit outside your worktree, and never merge or push the repository's main branch.
- Never change a task's definition to match what you built.
- Never end your turn holding a lease.
