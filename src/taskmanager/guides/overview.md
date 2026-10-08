# How TaskManager works

The task database is the only record of what is planned, claimed, built, landed and finished; read it with `tm`, change it with `tm`, and never open the sqlite files.

Run `tm guide` for the topics and `tm guide <topic>` for the one that matches your role. Each prints the built-in guidance, then this project's own addendum when it has one.

After `tm init`, set `repos.<repo>.gates.main` for every target repo before the first dispatch. Without it, every landing on `main` is refused with `no gate`.

## The cycle

Every task, plan and spec stores one status: its position in this cycle. Everything else a reader sees (blocked, waiting, stale) is worked out from the graph, the leases and the jobs, and is never stored.

```
READY ──claim──▶ IMPLEMENTING ──complete──▶ IMPLEMENTED
IMPLEMENTED ──claim, a task with review on──▶ REVIEWING ──review──▶ REVIEWED
IMPLEMENTED ──claim, review off──▶ MERGING
IMPLEMENTED ──claim, a plan or spec with review on──▶ MERGING ──landed and verified──▶ LANDED ──claim──▶ REVIEWING
REVIEWING ──approved, claimed from LANDED──▶ COMPLETED
REVIEWED ──claim: approved, or rejected with fix off──▶ MERGING
REVIEWED ──claim: rejected with fix on, or a failed landing──▶ FIXING ──complete──▶ FIXED
FIXED ──claim, not sensitive──▶ MERGING
FIXED ──claim, sensitive──▶ REVIEWING
MERGING ──landed and verified──▶ COMPLETED
MERGING ──own defect, attempts left, fix on──▶ REVIEWED, outcome merge_failed
a cap reached, a rejected re-review, or a rejection at LANDED with fix off ──▶ FAILED ──the owner answers investigate──▶ reopened
set by a verb, from any stable status but COMPLETED: DEFERRED, ABANDONED, SUPERSEDED
```

`tm task start <id> --agent <name> --session <id> --worktree-dir <dir> --yaml` is the only claim. It reads the stored status and the node's flags, decides the next step, takes the lease under `<name>` and prints the step; `--worktree-dir` is where an implement or fix worktree is cut, in place of the estate's `worktree_dir`:

| `action` | Claimed from | Locks the declared files | Closed with |
|:--|:--|:--|:--|
| `implement` | `READY` | yes | `tm task complete <id> --agent <name> --token <token>` |
| `review` | `IMPLEMENTED` for a task, `LANDED` for a plan or spec, `FIXED` for a sensitive node | no | `tm task review <id> --agent <name> --token <token> --approve`, or `--reject` |
| `fix` | `REVIEWED` | yes | `tm task complete <id> --agent <name> --token <token>` |
| `merge` | `IMPLEMENTED`, `REVIEWED`, `FIXED` | no | nothing: tm lands it as a job, and hands the job to an agent only when it stops |
| `sync` | a container branch behind its base | no | nothing: tm merges it as a job, and hands the job to an agent only when it stops |
| `blocked` | nothing is claimed; exit 3 | | the printed `reason` says what it waits on |

The printout also names the `model` family the step runs on (`haiku`, `sonnet`, `opus` or `fable`), the `repos` it touches, the `branch` (for a review claimed from `LANDED`, the target its code landed on), its `base` (`main`, or the container branch it lands on), and for `implement` and `fix` the `worktree`, with `worktrees` naming one per repository when a plan or spec spans several. It also prints the claim's `token`. `--agent <name>` on a closing verb is refused unless the live lease is that agent's, and `--token <token>` unless it is that claim's, so a step closes only for whoever holds it, even when a later claim reuses the agent name. A step that cannot go on ends with `tm task release <id> --agent <name> --token <token> --blocked` naming what it now waits on; `tm task release <id> --agent <name> --token <token>` alone ends it as a failed step. `tm task heartbeat <id>` renews the lease, which lasts `lease_ttl.<action>` seconds.

Three flags on every node decide the path through the cycle: `review` (a review follows implement, or for a plan or spec, its landing), `fix` (this node fixes its own rejections; it needs `review`) and `merge` (`main`, or `parent` to land on the branch of the plan or spec above it). A task has `review` and `fix` on and lands on `main` unless the document says otherwise, and a plan or spec has both off. Children under a reviewed plan or spec take `review: false`, `fix: false` and `merge: parent` by default, because its one review covers what lands on its branch; a sensitive child keeps `review` and `fix` on, and an explicit flag still wins. A fix lands without a re-review unless the node is sensitive: its `sensitive:` key names an area, or it writes a migration (`tm guide plan`, §2).

## Plans and specs

A plan or spec is a container: it is never implemented itself, and its status follows its children in the same write that moves any of them. Once every counted child (not `DEFERRED`, `ABANDONED` or `SUPERSEDED`) is `COMPLETED`, the container is `IMPLEMENTED`, and from there it lands before any review. With `review` off, the landing completes it. With `review` on it reads `LANDED`: its code is on its target, and its one review runs there. An approval completes it; a rejection is fixed on a branch cut from that target, the spent branch set aside as `tm/<container-id>@<n>`, and the fix lands without a second review unless the container is sensitive. A container whose branches hold nothing its base lacks has its code on its target already: it completes at once, or with `review` on reads `LANDED` and still takes its one review.

Children with `merge: parent` land on the container's branch, `tm/<container-id>`, a local branch in each repository they touch that is never pushed. Their code reaches `main` only when the container lands. A container's review reads its whole landing on the target, so it sees what is true only between children. A container that touched several repositories lands them one at a time, in its `land_order`.

## What a node waits on

- **An edge.** `depends_on` points at another node or at a decision. An edge to a node is satisfied once that node's code has landed on a branch this node builds on, a container at `LANDED` included: the dependency itself when both land on the same container, the container above it when it lands there, and so on up to `main`. An edge on a container holds every node under it. When a dependency has landed further up than this node's base, the claim first syncs it down into each container branch in between.
- **A decision.** A question only the owner answers, raised with `tm decision add` or opened by tm itself. The node waits until it is answered or withdrawn, and an answer may carry an effect on every node it blocks: abandon, defer, reopen, or drop the edge.
- **A condition.** A state outside the corpus plus a shell command that exits 0 once it holds (`tm task condition add`). A `claim` condition holds every claim; a `landing` condition holds only the landing. tm runs it before the claim and caches the result for `condition_ttl` seconds.

`tm task get <id> --yaml` prints the stored `status`, the `next_action` a claim would take now (null while the node is mid-step, or has no next step), and the `state` a reader sees, first match wins:

| Display | When |
|:--|:--|
| the status itself | an exit, `COMPLETED` or `FAILED`, or an `-ING` status with a live lease |
| `WAITING_MERGE_AGENT` | `MERGING` with a landing job stopped for an agent |
| `STALE` | an `-ING` status whose lease expired or is gone; `tm run sweep` returns it |
| `AWAITING_DECISION` | an edge to an open decision |
| `BLOCKED_BY_TASK` | an unsatisfied edge |
| `BLOCKED_BY_CONDITION` | an unmet condition for its next step |
| `BLOCKED_BY_SYNC` | a sync its claim needs is running or waiting |
| `BLOCKED_BY_LEASE` | its next step's files are locked by another lease |
| `IMPLEMENTING` | a container at `READY` with a child already past `READY` |
| `LANDED` | `LANDED`: its code on its target, its one review owed |
| `WAITING_REVIEW` | a task at `IMPLEMENTED` with `review` on, or a sensitive node at `FIXED` |
| `WAITING_FIX` | `REVIEWED` after a rejection it fixes, or after a failed landing |
| `WAITING_MERGE` | a task at `IMPLEMENTED` with `review` off, a plan or spec at `IMPLEMENTED`, `FIXED` on a node that is not sensitive, or `REVIEWED` to be landed |
| `READY` | `READY` |

## Counters and FAILED

Nothing loops. Three counters end every repeated failure at `FAILED`:

- **Fix rounds**, `max_fix_rounds` (2 for a task, 3 for a container). A rejection buys one fix, which lands without a re-review unless the node is sensitive. A sensitive fix whose re-review rejects fails the node, never a second fix, and so does a rejection at `LANDED` with `fix` off, since that code is already on its target. The cap bounds only the sensitive path: a fix past round 1 follows only a `tm task reset` that kept an earlier count.
- **Own-defect landing failures**, `max_merge_attempts` (3): a red on the node's own verifications, failures the merged tip adds, a red verification after landing, or a defect an agent recorded. A conflict, a race or a red `main` never counts here.
- **Failed steps**, `max_step_failures` (3): a plain release, an expired lease, a landing job an agent left unresolved.

Entering `FAILED` opens a decision on the node, "abandon, or investigate?", carrying the reason and the relevant `:review` or `:merge` excerpt. `investigate` reopens the node with the answer as its note. When a node that others depend on is deferred, abandoned or failed, one decision asks what to do with those dependents: drop the edge, defer them, or abandon them.

## Repairs

- `tm task reopen <id> --note "<why>"`: `FAILED`, `DEFERRED` or `ABANDONED` back into the cycle, counters and outcome cleared, the note kept in `:reopen`. The branch is kept for the next implementer; `--new-branch` starts clean instead. Refused while an open decision blocks the node.
- `tm task reset <id> --to IMPLEMENTED --note "<why>"`: a ledgered repair to `READY`, `IMPLEMENTED`, `REVIEWED`, `FIXED`, `LANDED` or `COMPLETED` (with `--outcome` for `REVIEWED` and `FIXED`). A reset to `LANDED` or `COMPLETED` is refused unless the branch is already on its target and the node's verifications pass there.
- `tm task defer <id> --note "<why>"` and `tm task abandon <id> --note "<why>"`: from any stable status but `COMPLETED`; the note goes to `:deferral` or `:abandonment`.
- `tm task supersede <old-id> <new-id> --transfer-blocks all`: the old node is `SUPERSEDED` and every dependent points at the new one.

None of these touches a node mid-step: wait for the step to end, or stop it.

## Reading

- `tm wave discover --session <id> --slots <n> --max-strong <n>`: every claimable node with its next action and model, a JSON line then a `__CHECK` line. `--hold-merge <node-id>`, repeatable, passes over that node's merge step and lists it under `held`, so a held landing takes no slot.
- `tm task list --yaml` and `tm task get <id> --yaml` are the compact reads (`--json` is the same data); `tm task get` accepts a plan or spec id too.
- `tm render <id> --view subagent` is a node's full brief; `--recursive` (`-r`) adds every child. `tm section get <id>:<key>` reads one section.
- `tm run list --yaml` is every lease, locked file and job in flight; `tm job status <job>` is one job, and `tm job status <job> --wait 540` blocks until it leaves `running` or the seconds pass.
- `tm verify run <id> --ref <ref>` runs the node's checks and exits 1 if one fails.
- `tm audit list --target <id>` is the event log of everything done to a node.
- `tm search <words>` finds nodes by text, or by meaning once `tm index` has run.
- `tm config list` shows every setting with its effective value and source; `tm config set <key> <value>` changes one.
- `tm decision list --status open` is the owner's queue; `tm decision get <id> --yaml` is one decision with its options.

## Verification

`tm verify run` reads `file_exists`, `file_absent`, `symbol_signature` and `ast_export` from a ref of the node's `target_repo` — `origin/main` by default, fetched first, never a working tree — and runs every `test_command` from the project root with the `--ref` it was given exported as `TM_VERIFY_REF`, unset when none was. `--ref tm/<id>` reads the node's own branch, with no fetch. A landing runs the node's verifications with `--ref` set to its target, so a `test_command` reads `"${TM_VERIFY_REF:-origin/main}"` rather than naming a branch; tm refuses a `test_command` that names `origin/main` itself on a node landing on its parent.

`No verifications to run` exits 2: nothing was checked, and that is not a pass. A landing with nothing to verify lands and says so in `:merge`.

## Writing

Add new work with `tm import` (an import that names an unknown id, breaks a flag rule or closes a cycle writes nothing and exits 1, printing the cycle as a path). Change a node with `tm task update`, `tm section set`, `tm verify add`, `tm task depends` and `tm task condition add`. `tm export <dir>` writes the whole database as sorted text for version control.

A question nobody in the loop can answer is not a reason to stop and ask: release the step with `tm task release <id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --option "b|Do Y|why" --recommend a`, or, outside a step, `tm decision add "<question>" --option "a|Label" --recommend a --blocks <id>`. `tm decision answer <id> --option a` or `tm decision withdraw <id>` lets the node move again.

## Messages you will meet

| Message | Exit | What to do |
|:--|:--|:--|
| `Invalid value: no .taskmanager at <dir>: pass -C, set TM_ROOT, or run tm init there` | 2 | you are outside the project; pass `-C <project root>` |
| `this directory holds a pre-lifecycle estate: run tm init --archive ...` | 1 | the owner's cutover has not run here; stop and report, never run `tm init` yourself |
| `action: blocked` with a `reason` | 3 | nothing was claimed; the reason names the edge, decision, condition, sync or lease |
| `import refused, nothing written: unknown ids [...]` | 1 | the document depends on ids that do not exist yet |
| a refusal naming a field and a fix | 1 | a flag rule or the cycle check refused the write; nothing was written |
| `Section '<key>' not found on node '<id>'` | 1 | list the node's sections with `tm section get <id>` |

## Moving an estate to this version

An estate written by 0.2 or earlier is not migrated. It is archived, and only the work still in flight is re-imported. Each step runs on the owner's go-ahead:

1. Stop every dispatcher, and wait until `tm run list --yaml` shows no lease.
2. With the old version still installed, `tm export <export dir>` and commit the export: that snapshot is the archive of record.
3. Author the re-import: `tm import` documents holding only the specs, plans, tasks and open decisions still in flight, each with its sections, verifications, edges, flags, conditions and `requires` in this version's shape. Completed work is not re-imported, and edges to it are dropped, because it is on `main`. A task whose branch `tm/<id>` already exists resumes on it at its next implement.
4. Install this version of the plugin and of `tm`, and replace any copy of the old `tm-wave` script a session keeps.
5. `tm init --archive` moves the old files to `.taskmanager/archive-<timestamp>/` and creates the new estate. Configure the landing gates as one YAML value, `tm config set repos '{<repo>: {gates: {main: {command: <template>, junit: <glob>, timeout: <seconds>}}}}'`, and the landing order with `tm config set repo_order '[<repo>, ...]'`.
6. `tm import --format yaml -f <document>` for each document; `tm wave discover --session <id> --slots <n> --max-strong <n>` shows what is claimable, for the owner to check.
7. Apply the project's prepared guide addendum and rules.
8. `tm export <export dir>` and commit.
9. Resume dispatching with the new `tm-wave`.
