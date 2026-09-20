# Dispatching a wave

For the session manager: pick the next disjoint tasks, hand each to the cheapest model that can do it, and move them through review and merge without a second record of what is in flight.

## 1. Ask what can start

```
tm next -n 5 --yaml
tm next -n 5 --strategy unblock-first --plan NOTIFY-EMAIL --model gemini-3.8-flash-high --yaml
```

A task is returned only when it is `READY`: status `NOT_STARTED`, every `depends_on` at `COMPLETED` or `SUPERSEDED`, no live lease on it, and no declared file locked by somebody else's lease. Strategies are `balanced` (default), `unblock-first`, `finish-plans` and `priority-strict`; they reorder the same set, they never widen it.

`--model <id>` keeps a task whose `acceptable_models` lists that id **and every task whose list is empty** — an empty list is "anyone", not "the strongest one".

## 2. Read the wave's files

`tm next` excludes tasks colliding with a lease that already exists, and returns a batch in which no two tasks declare the same path. A claim on a file a live lease holds is refused, exit 1:

```
Cannot claim task <id> due to file collision: {'<path>': 'Task: <other-id>, Agent: <who>'}
```

A refusal is the lock working: dispatch a different task, never a retry and never a claim with the files removed from the plan. Correct a wrong list with `tm task update <id> --set 'declared_files=["path", ...]'`.

Two tasks touching one schema, one migration chain or one generated file are not disjoint whatever their file lists say. Sequence them.

## 3. Route to a model

`acceptable_models` is the routing decision and the only one: a task is delegable to a cheaper family exactly when a model of that family is listed. Take the cheapest listed model that can do the work. A list you disagree with is a plan defect — fix it with `tm task update <id> --models a,b` and say so — never a reason to dispatch outside it.

## 4. Write the prompt

The brief is `tm render <task-id> --view subagent`. The agent runs it; you do not paste it, summarise it or add to it, because a restated brief drifts from the one the reviewer will read. The prompt carries only:

- the task id, and the command to read it: `tm render <id> --view subagent`
- the model you routed it to
- the worktree base, so its claim lands where the others do: `tm run start <id> --worktree --worktree-dir <dir> --agent <name> --session <id>`, which prints the path it created
- the report path
- its role's guide: `tm guide implement`

Nothing else. A brief that sends an agent to read a plan document, a long history or a whole directory spends the agent before the work starts; point it at `tm render <id>` and `tm section get <id>:<key>`.

## 5. Watch, do not poll

```
tm run list --yaml     # every lease and every locked file
tm run sweep           # drops leases past their TTL
```

`tm run list` is the whole of what is in flight; nothing else needs writing down. Run it when an agent reports, not on a timer, and never end a turn waiting to be told about a job whose status you can ask for.

Wave size is how many reports you can read carefully, not how many tasks `tm next` offers. One agent per task and one task per agent: a second agent on a live task is refused, and an agent holding two leases cannot heartbeat either reliably. Keep the slots full by replacing a finished task rather than by dispatching a whole new wave.

A lease past its TTL does not free its task until it is swept. `tm run sweep` prints `Swept 1 expired lease(s): <id>`, releases the lease and its locks, and returns the task to the state before the abandoned claim: `NOT_STARTED` for an implementation (it appears in `tm next` again), `WAITING_REVIEW` for a review, `WAITING_FIXES` for a fix round, `WAITING_MERGE` for a merge. The lost agent's branch and worktree remain; read them before dispatching the replacement.

## 6. Move it through the cadence

Each stage is one claim and one release. The implementer, the reviewer, the fixer and the merge agent each hold their own lease, so `tm run list` always names who has it — including a merge in progress, which used to be invisible to it.

| State | Dispatch | It claims, setting | It releases to |
|:--|:--|:--|:--|
| `READY` | an implementer | `IMPLEMENTING`, locking the task's files | `WAITING_REVIEW`, or `NOT_STARTED` if blocked |
| `WAITING_REVIEW` | a reviewer | `REVIEWING`, locking nothing | `WAITING_FIXES` or `WAITING_MERGE` |
| `WAITING_FIXES` | a fixer | `FIXING`, locking the task's files again | `WAITING_REVIEW` |
| `WAITING_MERGE` | a merge agent | `MERGING`, locking nothing | `COMPLETED`, after the merge verifies |

Find each wave's next move with `tm task list --status <S> --yaml`. `COMPLETED` is set by the merge agent and nowhere else; what you set directly, with `tm run stop <id> --status <S>` and no lease, is `NOT_STARTED`, `DEFERRED` and `ABANDONED`.

A reviewer is dispatched without `--worktree`; its claim reads the branch and locks nothing, so it never holds a sibling out of a wave. Batch review by plan, not by task: collect the plan's `WAITING_REVIEW` tasks, review them in one dispatch, and send one findings list per task. A fix round returns to `WAITING_REVIEW` and reuses the same branch, so the reviewer re-reads a diff rather than a tree.

## 7. When the plan changes

- **Defer.** Write why first, then park it: `tm section set <id>:deferral --file <path>` then `tm run stop <id> --status DEFERRED`. A deferred task leaves `tm next` and keeps its dependents blocked, so defer a blocker only after superseding or re-pointing them.
- **Supersede.** `tm task supersede <old-id> <new-id> --transfer-blocks all` sets the old task `SUPERSEDED` and re-points every dependent at the new one. The new task must already exist (otherwise nothing is changed and it exits 1). A lease and file locks held by the old task are released. A comma-separated id list re-points only those; `--transfer-blocks none` re-points nobody and leaves each dependent pointing at a `SUPERSEDED` task, **which satisfies the dependency** — use it to release dependents, never to hold them.
- **Abandon.** `tm run stop <id> --status ABANDONED`, and only where nothing depends on it: `ABANDONED` never satisfies a dependency, so every dependent stays blocked forever.
- **Close a plan.** When its tasks are `COMPLETED` or `SUPERSEDED`, `tm run stop <plan-id> --status COMPLETED`. Check with `tm task list --plan <plan-id> --yaml` first; nothing closes it for you. `tm plan list --yaml` shows the stored `status` and a `state` worked out from the plan's tasks: a plan whose tasks are all done reads `state: COMPLETED` while its `status` stays until you stop it.

## 8. Write rulings down where the work is

A decision, a constraint, a hazard or an answer the next agent will need goes on the node it applies to, not into a document and not into your own notes:

```
tm section set <task-id>:context --file <path> --header "## Context"
tm section set <plan-id>:context --file <path>      # reaches every task's brief
```

Anything a `tm` command can answer — what is claimed, what is ready, who holds a file, which tasks are left in a plan — is not written down at all. It decays the moment someone claims something.

## 9. Escalate rather than repeat

An implementer that comes back blocked twice on the same task is not going to succeed on a third identical dispatch. Re-dispatch on a stronger model and say what changed, or split the task. Two review rounds on one task means the brief was wrong: fix the task's sections before the third.

## Never

- Never dispatch two agents at intersecting `declared_files`, and never work around a collision refusal.
- Never paste, summarise or extend the rendered brief.
- Never poll `tm run list` in a loop, and never end a turn waiting for a background job to report.
- Never dispatch the replacement of a lost agent before running `tm run sweep`.
- Never keep a second record of what is in flight; `tm run list` and `tm next` are it.
