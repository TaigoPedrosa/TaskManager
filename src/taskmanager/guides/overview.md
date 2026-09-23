# How TaskManager works

The task database is the only record of what is planned, claimed, built and finished; read it with `tm`, change it with `tm`, and never open the sqlite files.

Run `tm guide` for the topics and `tm guide <topic>` for the one that matches your role. Each prints the built-in guidance, then this project's own addendum when it has one.

## Lifecycle

`NOT_STARTED` becomes `READY` when every dependency is `COMPLETED` or `SUPERSEDED`. Each stage is claimed with `tm run start <task-id> --agent <name> --session <id>` and released with `tm run stop <task-id> --status <S>`:

| Claim on | Sets | Locks the task's files | Released with |
|:--|:--|:--|:--|
| a `READY` task | `IMPLEMENTING` | yes | `WAITING_REVIEW`, or `NOT_STARTED` when blocked |
| `WAITING_REVIEW` | `REVIEWING` | no | `WAITING_FIXES` or `WAITING_MERGE` |
| `WAITING_FIXES` | `FIXING` | yes | `WAITING_REVIEW` |
| `WAITING_MERGE` | `MERGING` | no | `COMPLETED`, after the merge verifies |
| (no claim) | | | `DEFERRED`; `SUPERSEDED`; `ABANDONED` |

While a lease is live the task reads `IN_FLIGHT`, whatever status it holds underneath: `tm task get <task-id> --yaml` prints both, `status` and `state`. A plan or spec is never leased, so its own `status` never moves by itself; its `state` is rolled up live from its children (all done is `COMPLETED`, all blocked is `BLOCKED`, all untouched is `NOT_STARTED`, anything else is `IMPLEMENTING`) and is the one to read for progress. `tm plan list --yaml` and `tm spec list --yaml` print both.

A `NOT_STARTED` task whose dependencies are all satisfied but whose own `declared_files` collide with a file another task's active lease already holds reads `BLOCKED_BY_LEASE`, not `READY`: the dependency graph has nothing left to say, but claiming it would collide. It clears back to `READY` on its own once the holding lease is released or swept, no action needed on the task itself.

A `depends_on` edge onto a **decision** (a question raised with `tm decision add`) reads `AWAITING_DECISION`, not `BLOCKED`, once every other dependency is clear: an open decision is `NOT_STARTED`, an answered one `COMPLETED`, a withdrawn one `ABANDONED` — either terminal state clears the edge, so answering or withdrawing it moves the task straight to `READY`. `tm decision list --status open` is the owner's queue of what is waiting on them.

A lease lasts 300 seconds unless `--ttl` or `TM_LEASE_TTL` says otherwise, and `tm run heartbeat` renews it. An expired lease does not free the task by itself: `tm run sweep` names it, drops the lease and its locks, and returns the task to the state before the claim (`NOT_STARTED`, `WAITING_REVIEW`, `WAITING_FIXES` or `WAITING_MERGE`), so it can be claimed again.

## Reading

- `tm next -n 5` lists what can start now. `tm next --model <id>` filters by an acceptable model.
- `tm task list --yaml` and `tm task get <task-id> --yaml` are the compact reads (`--json` is the same data); `tm plan list`, `tm spec list` and `tm run list` take the same flags. `--status <S>` filters a list.
- `tm render <task-id> --view subagent` is a task's full brief. `tm section get <task-id>:<key>` reads one section. `--recursive` (`-r`) also renders every child depth-first — a spec's plans and their tasks, or a plan's tasks — separated by `---`; a leaf task ignores it and a `:section` path refuses it.
- `tm verify run <task-id>` runs the task's checks and exits 1 if one fails.
- `tm audit list --target <task-id>` is the event log of everything done to a node.
- `tm search <words>` finds tasks, plans and specs by text, or by meaning once `tm index` has run; `--kind`, `--status` and `--plan` narrow it.
- `tm config list` shows every setting with its effective value and where it came from; `tm config set <key> <value>` changes one.
- `tm decision list --status open` and `tm decision get <id> --yaml` read the queue of open questions and one decision's options and answer.

## Files and worktrees

`--worktree` on `tm run start` cuts `tm/<task-id>` from the task's own repository's `origin/main` with no upstream, into `<worktree-dir>/<target_repo>-<task-id>`. A task's repository is `<project root>/<target_repo>`. `tm` finds its database from any directory, a worktree included, and inside a worktree it also resolves the task, so `tm run heartbeat`, `tm verify run` and `tm run stop` need no id there. `-C <project root>` or `TM_ROOT` overrides the search.

A claim locks the paths the task declares — its path-bearing verifications plus any `declared_files` in its frontmatter — so two tasks touching one file cannot be in flight at once.

## Verification

`tm verify run` resolves every path and runs every `test_command` **against the project root**, never against a worktree or a branch. Before a merge, a path check is therefore red for work that is finished but unmerged: read the table as the acceptance list and confirm each row where the work actually is. After the merge, the same command is the gate, and it must exit 0.

`No verifications to run` exits 2. Nothing was checked; that is not a pass. `tm render <unknown-id>` and `tm task get <unknown-id>` exit 1 with a one-line error. `tm root` prints the project root `tm` resolved (a task's `target_repo` is `<root>/<target_repo>`).

## Writing

Add new work with `tm import` (a document of specs, plans and tasks; an import that names an unknown dependency writes nothing and exits 1). Change an existing task with `tm task update`, `tm section set` and `tm verify add`. `tm export <dir>` writes the whole database as sorted text for version control.

A question nobody in the loop can answer is not a reason to stop and ask: raise it with `tm decision add "<question>" --option "a|Label" --option "b|Label" --recommend a --blocks <task-id>`, naming the options you considered and the one you recommend, then release the blocked task normally. `tm decision answer <id> --option a` or `tm decision withdraw <id>` unblocks it; `tm decision reopen <id>` re-blocks it.

## Messages you will meet

| Message | Exit | What to do |
|:--|:--|:--|
| `Invalid value: no .taskmanager at <dir>: pass -C, set TM_ROOT, or run tm init there` | 2 | you are outside the project; pass `-C <project root>` |
| `Task <id> is not ready to start (current state: BLOCKED)` | 1 | a dependency is unfinished; report it |
| `Task <id> is not ready to start (current state: IN_FLIGHT)` | 1 | another agent holds the lease; `tm run list` names them |
| `Cannot claim task <id> due to file collision: {'<path>': 'Task: <id>, Agent: <agent>'}` | 1 | a live lease holds a path you declared; claim nothing |
| `a review reads the branch; it does not cut a worktree` | 1 | drop `--worktree` from the review claim |
| `<dir> is not a git repository: set the task's target_repo` | 1 | the task's `target_repo` is wrong or unset |
| `No active lease found for <id>` | 1 | your lease expired; stop working and report |
| `import refused, nothing written: unknown ids [...]` | 1 | the document depends on ids that do not exist yet |
| `Section '<key>' not found on node '<id>'` | 1 | list the node's sections with `tm section get <task-id>` |
