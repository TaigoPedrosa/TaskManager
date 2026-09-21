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

A reviewer is dispatched without `--worktree`; its claim reads the branch and locks nothing, so it never holds a sibling out of a wave. A fix round returns to `WAITING_REVIEW` and reuses the same branch, so the reviewer re-reads a diff rather than a tree.

**That table is per task, not per wave.** A task advances the moment its own stage releases, so one task can be merging while another is on its first fix round and a third has not been claimed. One reviewer per task, dispatched as soon as *that* task reaches `WAITING_REVIEW` — do not collect a plan's `WAITING_REVIEW` tasks and review them together. Batching by plan makes the slowest task in the batch the release time of every task in it, spends one reviewer's context on work it was not briefed on, and produces a single findings list somebody then has to split back apart.

## 7. Chain the stages as one workflow

Where the orchestration tool takes a script, the table above *is* the script, and the whole of the design is that no stage waits for a sibling:

- **Pipeline, never a barrier.** Run each task through implement → review → fix → re-review → merge independently. Putting a barrier between stages — every task reviewed before any is fixed — makes each task wait for the slowest sibling at five separate points, and there is no cross-task decision at any of them to pay for it. Wall clock becomes the slowest single chain instead of the sum of the slowest per stage.
- **Enter at the current status, not at the start.** Read each task's status when the wave is built and let it skip what it is already past: `WAITING_REVIEW` enters at review, `WAITING_MERGE` enters at merge. Re-implementing a task that is already implemented is the common failure of a script that assumes a wave starts from `READY`.
- **Let each stage pick the next.** A review finding nothing blocking skips fix and re-review and goes to merge; one that finds something routes through fix and back. Have the stage return a structured verdict rather than prose, so the branch is a value and not a reading of a paragraph.
- **Declare the holds before the first dispatch, not inside a stage.** A merge that is irreversible, applies to production, or is the user's decision is excluded from the script's input and reported as held. A stage that discovers the hold has already spent an agent reaching it.
- **A status is not a lock.** Two stages of one task never run at once, so nothing needs a second lock beyond the lease — but a shared resource outside `tm` is not covered by either. A migration chain admits one **unmerged** writer, not one live lease: the seat stays taken until that branch merges, so a task releasing its lease at `WAITING_REVIEW` has not freed it, and the next claimant computes the same "next free" revision and builds a second head on one parent.

Build the wave against the trees, not against the last wave's reports. A pin, a migration head, an ahead/behind count and a seam's status all decay between waves, and re-deriving them is the dispatcher's job rather than the implementer's: a stale premise dispatched is an agent spent proving the brief wrong.

## 8. When the plan changes

- **Defer.** Write why first, then park it: `tm section set <id>:deferral --file <path>` then `tm run stop <id> --status DEFERRED`. A deferred task leaves `tm next` and keeps its dependents blocked, so defer a blocker only after superseding or re-pointing them.
- **Supersede.** `tm task supersede <old-id> <new-id> --transfer-blocks all` sets the old task `SUPERSEDED` and re-points every dependent at the new one. The new task must already exist (otherwise nothing is changed and it exits 1). A lease and file locks held by the old task are released. A comma-separated id list re-points only those; `--transfer-blocks none` re-points nobody and leaves each dependent pointing at a `SUPERSEDED` task, **which satisfies the dependency** — use it to release dependents, never to hold them.
- **Abandon.** `tm run stop <id> --status ABANDONED`, and only where nothing depends on it: `ABANDONED` never satisfies a dependency, so every dependent stays blocked forever.
- **Close a plan.** When its tasks are `COMPLETED` or `SUPERSEDED`, `tm run stop <plan-id> --status COMPLETED`. Check with `tm task list --plan <plan-id> --yaml` first; nothing closes it for you. `tm plan list --yaml` shows the stored `status` and a `state` worked out from the plan's tasks: a plan whose tasks are all done reads `state: COMPLETED` while its `status` stays until you stop it.

## 9. Write rulings down where the work is

A decision, a constraint, a hazard or an answer the next agent will need goes on the node it applies to, not into a document and not into your own notes:

```
tm section set <task-id>:context --file <path> --header "## Context"
tm section set <plan-id>:context --file <path>      # reaches every task's brief
```

Anything a `tm` command can answer — what is claimed, what is ready, who holds a file, which tasks are left in a plan — is not written down at all. It decays the moment someone claims something.

## 10. Escalate rather than repeat

An implementer that comes back blocked twice on the same task is not going to succeed on a third identical dispatch. Re-dispatch on a stronger model and say what changed, or split the task. Two review rounds on one task means the brief was wrong: fix the task's sections before the third.

## Never

- Never dispatch two agents at intersecting `declared_files`, and never work around a collision refusal.
- Never paste, summarise or extend the rendered brief.
- Never poll `tm run list` in a loop, and never end a turn waiting for a background job to report.
- Never dispatch the replacement of a lost agent before running `tm run sweep`.
- Never keep a second record of what is in flight; `tm run list` and `tm next` are it.
- Never hold a task at a stage because a sibling has not reached it; only `depends_on` makes one task wait for another.
- Never treat a released lease as a freed migration chain: that seat is held until the branch merges.
- Never dispatch on a premise carried from the last wave's report without re-deriving it against the tree.
