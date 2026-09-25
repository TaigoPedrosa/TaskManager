# Lifecycle redesign: one stored status, derived displays, node-level landing

Date: 2026-09-24. Status: design agreed section by section with the owner. The owner waived review of
the written spec and authorised going straight to the plan; an independent design review's findings
are folded in. Plan: `docs/superpowers/plans/2026-09-24-lifecycle-redesign.md`.

This is sub-project 1 of 4. The others follow strictly after this one is implemented and published,
each with its own spec and plan:

1. **Lifecycle model and cutover** (this spec).
2. Data access: graph-query speed, API pagination, progressive loading, websocket deltas with a
   hash drift check.
3. Web wave simulator, replacing the tree view.
4. Decisions UX.

## 1. Goals

1. **A stored status means one thing.** The stored status is a position in the dispatch cycle.
   Everything a reader sees beyond that (blocked, waiting, stale, phase) is derived, never written.
2. **tm decides the next step.** `tm task start` reads the stored state and the node's flags and
   answers which action comes next. A workflow no longer encodes the lifecycle; each wave picks up
   one step per node.
3. **Flags replace appendage tasks.** `review` and `fix` stand for the review and fix work that would
   otherwise be separate tasks depending on the node. Plans and specs carry the same flags.
4. **Every node lands itself**, on its parent's branch or on `main`, as the planner declares. A
   dependency is satisfied only by landed code.
5. **Merging is deterministic.** tm merges, gates, pushes and verifies; an agent is dispatched only
   for a failure it can act on.
6. **Nothing waits on an unnamed reason, and nothing loops.** A node waits on an edge, a decision or
   a checkable condition. Every kind of repeated failure is counted and ends in `FAILED` with a
   decision. Holds are removed.
7. **The live SocialSrc estate moves by a fresh start**: the old estate is archived, not migrated, and
   only the ongoing work is re-imported, on the owner's go-ahead (§9).

Out of scope: graph-query performance, pagination and websocket protocol (sub-project 2); the wave
simulator and the tree view's removal (sub-project 3); decisions UX beyond statuses, option effects
and linking (sub-project 4); agents spanning machines (§6.4).

## 2. Stored state

### 2.1 Node columns

Every task, plan and spec stores these as `nodes` columns (not frontmatter), each with the named
constraint:

| Column | Type, constraint | Default | Meaning |
|---|---|---|---|
| `status` | text, `CHECK` by kind (§2.2) | `READY` | position in the cycle |
| `claimed_from` | text, nullable | null | the status an `-ING` was claimed from; set with the claim, cleared when the step ends |
| `review` | int 0/1 | task 1, container 0 | a review step follows implement |
| `fix` | int 0/1, `CHECK (fix <= review)` | task 1, container 0 | a rejection is fixed by this node |
| `merge` | text, `CHECK (merge IN ('parent','main'))` | `main` | where this node lands, §6.1 |
| `outcome` | text, `CHECK (outcome IN ('approve','reject','merge_failed'))`, nullable | null | drives routing after `REVIEWED` |
| `verdict` | text, nullable | null | the reviewer's or landing's free-text verdict; open-ended, never routes |
| `fix_for` | text, nullable, same values as `outcome` | null | which outcome the current or last fix answers |
| `review_cycles` | int ≥ 0 | 0 | counted reviews since the last reopen (§3.2) |
| `merge_attempts` | int ≥ 0 | 0 | own-defect landing failures since the last reopen |
| `step_failures` | int ≥ 0 | 0 | consecutive steps that ended without progress (§3.2) |
| `branch` | text | `tm/<id>` | the node's branch name in each repository it touches |
| `requires` | text (JSON list) | `[]` | capabilities the executing agent needs, §5.5 |
| `land_order` | text (JSON list), containers only | `[]` | repository order for a container's landing, §6.5 |

A **container** is a plan or a spec. **Decisions** keep their kind with their own statuses.

### 2.2 Statuses

For tasks, plans and specs — the cycle: `READY`, `IMPLEMENTING`, `IMPLEMENTED`, `REVIEWING`,
`REVIEWED`, `FIXING`, `FIXED`, `MERGING`, `COMPLETED`, `FAILED`; the exits: `DEFERRED`, `ABANDONED`,
`SUPERSEDED`.

For decisions: `OPEN`, `ANSWERED`, `WITHDRAWN`.

`CHECK ((kind = 'decision' AND status IN (<decision values>)) OR (kind <> 'decision' AND status IN
(<cycle and exit values>)))`.

An `-ING` status is entered only by a successful claim. A refused claim writes nothing.

### 2.3 Write-time validation

Every write runs these over every node it affects — the node written, and for a write to a
container or a move, every child whose rule depends on its parent. Refused with a message naming the
field and the fix:

1. `fix` without `review` (also the column `CHECK`).
2. `review = 1, fix = 0` unless `merge = parent` and the parent has `review = 1, fix = 1`: a rejection
   nobody below fixes must land where a review above will see it (§3.3). A write to a parent that
   breaks this for any child is refused too.
3. `merge = parent` on a spec or on a node with no parent.
4. A change to `merge`, or a move, once the node's branch exists in any repository, unless
   `git merge-base <branch> <new target>` equals the branch's recorded base: a branch cut from `tm/P`
   must never land on `main` carrying `tm/P`'s unreviewed code.
5. `merge = parent` (set, imported or reached by a move) on a node owning a `test_command`
   verification that names `origin/main` literally instead of `TM_VERIFY_REF` (§6.2).
6. A new child, a move into, or a reopen of a child under a container that is `COMPLETED`, or that
   holds a live lease. Under a container at `IMPLEMENTED`, `REVIEWED` or `FIXED` the container moves
   back to `READY` in the same write.
7. `defer`, `abandon`, `supersede`, `reset`, `move` or a decision effect on a node holding a live
   lease or a running job (§6.2): wait for the step to end, or stop it.
8. Any write that makes the expanded step graph cyclic (§4.4).

## 3. Transitions

### 3.1 The cycle

| From | Event | To |
|---|---|---|
| `READY` | claim (implement) | `IMPLEMENTING` |
| `IMPLEMENTING` | `complete` | `IMPLEMENTED` |
| `IMPLEMENTED` | claim, `review` on | `REVIEWING` (counted) |
| `IMPLEMENTED` | claim, `review` off | `MERGING` |
| `REVIEWING` | `review --approve` | `REVIEWED`, outcome `approve` |
| `REVIEWING` | `review --reject`, fix rounds left or `fix` off | `REVIEWED`, outcome `reject` |
| `REVIEWING` | `review --reject`, `fix` on, no fix round left | `FAILED` |
| `REVIEWED` (`reject`, `fix` on) | claim | `FIXING`, `fix_for = reject` |
| `REVIEWED` (`merge_failed`) | claim | `FIXING`, `fix_for = merge_failed` |
| `REVIEWED` (`reject`, `fix` off) | claim | `MERGING` (§3.3) |
| `REVIEWED` (`approve`) | claim | `MERGING` |
| `FIXING` | `complete` | `FIXED` |
| `FIXED` | claim | `REVIEWING`, counted only when `fix_for = reject` |
| `MERGING` | landing succeeds | `COMPLETED` |
| `MERGING` | landing: own defect, attempts left, `fix` on | `REVIEWED`, outcome `merge_failed`, `merge_attempts`+1 |
| `MERGING` | landing: own defect, no attempt left or `fix` off | `FAILED` |
| `MERGING` | landing needs an agent (§6.2) | stays `MERGING`; the claim hands the job to an agent |
| any `-ING` | `release` (transient) | `claimed_from`, `step_failures`+1 |
| any `-ING` | `release --blocked` with what it waits on | `claimed_from`, the edge, decision or condition written |
| any `-ING` | lease expires, then sweep | `claimed_from`, `step_failures`+1 |
| a stable status | `step_failures` reaches its cap | `FAILED` |

Every event that advances a node resets `step_failures` to 0. A `review` event whose `:review`
section is unchanged since the claim is refused (tm snapshots the section's hash at claim).

**Containers.** A container's implement step is never claimed. Its stored status is re-derived in
the same transaction as every status change of any child and every add, move or removal of one:

- counted children (not `DEFERRED`, `ABANDONED` or `SUPERSEDED`) all `COMPLETED`, at least one →
  `IMPLEMENTED` if it was `READY`;
- no counted children but at least one child → `SUPERSEDED`-only: `COMPLETED`; any `DEFERRED`:
  `DEFERRED`; else `ABANDONED`;
- no children at all → `READY`.

A container at `IMPLEMENTED` whose branches differ from their base in no repository
(`git diff --quiet <base>...<branch>`, trees not commits, so a sync merge commit alone does not
count) completes in the same transaction whatever its `review` flag: there is nothing to review, and
code already on `main` is never reviewed again.

A container's review reads `git diff <base>...<branch>` in each repository it touched. Its fix works
in a worktree of its branch and locks the container's own `declared_files`, which default to the
union of its descendants'.

### 3.2 Counters, caps and `FAILED`

Config (§8): `max_fix_rounds: {task: 2, container: 3}`, `max_merge_attempts: 3`,
`max_step_failures: 3`.

- **Counted reviews.** `review_cycles` counts reviews started from `IMPLEMENTED`, or from `FIXED`
  with `fix_for = reject`. A review after a merge fix checks that fix and is not counted: a landing
  failure is not a review. Fix rounds used = `review_cycles - 1` when the last outcome was `reject`.
  A rejection when the fix rounds are used up goes to `FAILED` (task: implement, review, fix, review,
  fix, review, `FAILED`).
- **Landing failures.** `merge_attempts` counts own-defect failures only (§6.3), and bounds merge
  fixes: past the cap, `FAILED`.
- **Step failures.** `step_failures` counts consecutive steps that ended without progress: a
  transient `release`, a sweep, and every landing job an agent took over and left unresolved (§6.2).
  At the cap, `FAILED`. This bounds every loop the other two counters do not see.

Entering `FAILED` opens a decision blocking the node: "`<id>` failed: abandon, or investigate?",
with the reason and the relevant `:review` or `:merge` excerpt as its evidence. Options carry effects
(§4.2): `abandon` → `abandon`; `investigate`, or any custom answer → `reopen`, with the answer as the
reopen note.

### 3.3 A rejection nobody below fixes

A node with `review = 1, fix = 0` whose review rejects proceeds to `MERGING` and lands on its
parent's branch; §2.3 rule 2 guarantees that parent reviews and fixes. At that landing the node's own
verifications run but are advisory: their result is written to `:merge`, and a red does not stop the
landing. The parent's review brief lists every child whose stored outcome is `reject`, with a pointer
to its `:review`. The list is derived, never copied.

### 3.4 Reopen, reset, defer, abandon, supersede

- `tm task reopen <id> --note <text> [--new-branch]`: from `FAILED`, `DEFERRED` or `ABANDONED`.
  A task goes to `READY`; a container goes through the rollup of §3.1 (to `IMPLEMENTED` when its
  counted children are all `COMPLETED`). Counters reset, outcome, verdict and `fix_for` clear, the
  note is appended to `:reopen` and the ledger. The branch is kept: the next implementer starts on it
  with the note and the old `:review`, and decides what to keep. `--new-branch` renames the old one to
  `<branch>@<n>` and starts clean; it is required when the reopen also changes `merge` (§2.3 rule 4).
  Refused while an open decision blocks the node.
- `tm task reset <id> --to <status> --note <text> [--outcome <o>]`: ledgered repair. It moves a node
  with no live lease or job to `READY`, `IMPLEMENTED`, `REVIEWED`, `FIXED` or `COMPLETED`. A reset to
  `COMPLETED` also needs `git merge-base --is-ancestor <branch> <target>` in each repository and a
  passing `tm verify run --ref <target>`. This replaces hand-setting status with `tm run stop`.
- `tm task defer <id> --note`, `tm task abandon <id> --note`: from any stable status except
  `COMPLETED`, which is landed code and cannot be un-landed. The note goes to `:deferral` or
  `:abandonment`.
- `tm task supersede` keeps its behaviour, under §2.3 rule 7.
- **Stranded dependents.** A node that moves to `DEFERRED`, `ABANDONED` or `FAILED` while nodes depend
  on it opens one decision on those dependents: "`<id>` was `<status>`: drop the edge, defer, or
  abandon the dependents?", with effects `drop_edge`, `defer`, `abandon`. `FAILED` opens it only once
  its own decision is answered `abandon`.

## 4. What a node waits on

### 4.1 Dependency edges

`depends_on` edges run from any node to any node or decision. The per-edge status gate is removed:
an edge to a node means "that node's code has reached a branch I build on". An edge on a container is
inherited: it gates every descendant's claim, not only the container's own steps.

Satisfaction is decided by **landing level**, from stored statuses and the tree, with no git call and
no repository comparison:

- A node's **landing target** `t(N)` is its parent when `merge = parent`, else `MAIN`. Its **base
  chain** is `t(N), t(t(N)), …` up to and including `MAIN`: the branches it builds on, nearest first.
  A node with `merge = main` has base chain `[MAIN]`.
- A dependency `Y`'s **landing chain** is `Y`, then the node owning the branch `Y` lands on, and so
  on, up to the first node that lands on `MAIN`.
- `X → Y` is satisfied when `Y` is `SUPERSEDED`, or when the first node `Z` in `Y`'s landing chain
  whose landing target is in `X`'s base chain is `COMPLETED`.

When `Z`'s landing target is below `X`'s base in `X`'s base chain (for example `Y` landed on `main`
and `X` builds on `tm/P`), `X`'s claim first syncs the target into each base branch between them, in
each repository where both exist (§6.4). Across repositories there is nothing to sync: the edge only
orders the work.

### 4.2 Decisions

- Statuses `OPEN`, `ANSWERED`, `WITHDRAWN`.
- `--blocks` and `tm decision link` accept any non-decision node, containers included.
- An option may carry an **effect**, applied in the answering transaction to every node the decision
  blocks, and ledgered: `none` (default), `abandon`, `defer`, `reopen`, `drop_edge` (removes the
  blocked node's edge to the decision's subject). A decision cannot be linked to a node holding a
  live lease or job (§2.3 rule 7), so an effect never lands mid-step.
- A decision blocks at any stored status.

### 4.3 Conditions

A condition is `{needs, command, stage}` in a new `node_conditions(node_id, idx, needs, command,
stage)` table: a state outside the corpus, and a shell command that exits 0 once it holds. `stage` is
`claim` (default: every claim waits on it) or `landing` (only the landing does). A condition with no
command, or with a command that is prose, is refused: that is a decision.

tm runs a node's conditions outside any open transaction, before it takes the claim, from the
project root, with `condition_timeout` (default 60 s). A command may call `tm`, but must not depend
on the text format of its output beyond `--json`/`--yaml`. Results are cached in the cache database
(§9.1) for `condition_ttl` seconds (default 300). An unmet condition makes the node unclaimable and
clears itself once the command exits 0.

`tm task condition add <id> --needs <text> --command <cmd> [--stage claim|landing]`,
`tm task condition remove <id> <idx>`. Import accepts `conditions: [{needs, command, stage}]`.

### 4.4 The cycle check

Every write that changes the graph — an edge, a flag, `merge`, a move, an import, a supersede, a
reopen — is checked against the **expanded step graph** and refused if it becomes cyclic:

- For each non-decision node `N`: vertices `N.start`, `N.implemented`, `N.landed`, with
  `N.start → N.implemented → N.landed`.
- For a container `C` and each counted child `K`: `K.landed → C.implemented`.
- For each edge `X → Y` to a non-decision node, with `Z` as in §4.1: `Z.landed → D.start` for `X` and
  every descendant `D` of `X`.
- **Migration chain.** For each repository, a node writing a migration and each other node writing a
  migration in the same repository whose base chain does not contain the first's landing target:
  the chain is modelled as `A.landed → B.start` in the order tm will grant it (§5.5), so a chain held
  across a container boundary is visible to the check.
- Edges to decisions are left out: a decision has no out-edges and cannot close a cycle.

The refusal prints the cycle as a path, for example
`A.start ← P.landed ← P.implemented ← A.landed ← A.start`. The check is the only deadlock guard.

### 4.5 Holds are removed

- `tm task release <id> --blocked --depends <id> | --decision <question> ... | --condition ...`
  names what the node now waits on, and writes it in the same call. `--blocked` with nothing named is
  refused.
- `tm task release <id>` alone is a transient failure, counted in `step_failures`.
- `holdMerge` stays a `tm-wave` argument: the workflow does not claim a listed node whose next action
  is `merge`. That is dispatcher policy, not node state.

## 5. Claims and the CLI

### 5.1 Coordination state lives with the nodes

`leases` and `file_locks` live in `state.db` with the nodes, beside two new tables, `branch_locks`
and `jobs` (§6.2). A claim is one `BEGIN IMMEDIATE` transaction: it checks the status and flags,
checks file locks, `INSERT`s the lease (a plain insert that fails on conflict, never an upsert),
inserts the file locks, sets `status` and `claimed_from`. Two racing claims cannot both succeed.

The lease row gains `action`, `review_hash` (for `review`), and a nullable `ttl` (null while a stopped
job waits for an agent, §6.2).

### 5.2 `tm task start`

```
tm task start <id> --agent <agent> --session <session> [--ttl <s>] [--yaml|--json]
```

Accepts any non-decision node. Runs the conditions (§4.3), then the claim (§5.1), and prints:

```yaml
action: implement | review | fix | merge | sync | blocked
reason: <why blocked, or null>
model: <model id>               # §5.4
job: <job id, merge and sync>
repo: <repo>                    # containers list one entry per repository
branch: <branch>
base: main | <ancestor branch>
worktree: <path: implement, fix, and a merge or sync handed to an agent>
```

`blocked` exits 3 and writes nothing. `implement` and `fix` lock `declared_files`; `review`,
`merge` and `sync` lock none. Lease TTL per action comes from `lease_ttl.<action>` (implement 10800,
review 3600, fix 7200, merge 3600, sync 3600).

- `merge` on a node at `IMPLEMENTED` or `REVIEWED` starts a landing job and returns immediately.
- `merge` on a node already `MERGING` whose job is stopped waiting for an agent hands the job and its
  lease to the caller (the lease's agent is replaced, `ttl` restored).
- `sync` is returned when the claim's sync (§6.4) stopped on a conflict or a red: the caller takes
  the sync job, not the node. The node itself stays unclaimed and displays `BLOCKED_BY_SYNC`.

### 5.3 Closing a step

- `tm task complete <id>`: implement → `IMPLEMENTED`; fix → `FIXED`.
- `tm task review <id> --approve|--reject [--verdict <text>]`: → `REVIEWED`, §3.1.
- `tm job resume <job>`: the only way an agent finishes a stopped landing or sync (§6.2).
- `tm task release <id> [--blocked ...]`: §4.5.
- `tm task heartbeat <id>`, `tm run list`, `tm run sweep` stay. **`tm run start` and `tm run stop` are
  removed**, as is every path that sets `status` to an arbitrary value (the web's
  `POST /api/nodes/{id}/status` included).

### 5.4 Claimability

A node is claimable when none of these hold, checked in this order:

1. It holds a live lease, or a job of it is running.
2. An edge, its own or inherited, points at an open decision.
3. An edge is unsatisfied (§4.1).
4. A `claim`-stage condition is unmet.
5. A sync its claim needs is running or waiting for an agent.
6. Its next action locks a declared file another live lease holds.
7. Its status has no next action (`COMPLETED`, `FAILED`, an exit, or a container at `READY`).

### 5.5 Model routing

`tm task start` names the model; the workflow uses it.

- implement: the cheapest family in `acceptable_models`.
- review of a task: `review_models`' family if set, else `sonnet`.
- review of a container: `review_models`' family if set, else the strongest family in
  `acceptable_models`, never below `opus`: a container review is a branch review.
- fix answering `reject`, rounds 1 and 2: the implement family if it is `opus` or `fable`, else
  `sonnet`. Round 3 (containers only): the strongest family in `acceptable_models`, never below
  `opus`. The round is `review_cycles - 1`.
- fix answering `merge_failed`, and merge and sync agents: `sonnet`.

### 5.6 Discovery

`tm wave discover` returns every claimable node of every kind with its next action and model, plus
`MERGING` nodes whose job waits for an agent and sync jobs waiting for one. `--spec` becomes optional
and repeatable; omitted means every spec and every node with no spec. It keeps slots, strong slots,
excludes and file disjointness within the batch.

The per-repository **migration chain** holds while a node writing a migration has not landed on
`main`, except for nodes whose base chain contains that node's landing target: a sibling in the same
container builds on the landed migration and may proceed. `requires` is returned with each node, so
the workflow routes it to an agent type that has the capability. `--release` is removed with holds.

### 5.7 Import and update

`tm import` accepts, per task, plan and spec: `review`, `fix`, `merge`, `requires`, `conditions`, and
`land_order` on containers. `tm task update` gains `--review/--no-review`, `--fix/--no-fix`,
`--merge parent|main`, `--requires`, `--land-order`. The `id:STATUS` gate syntax of
`tm task depends --add` is removed. Every write runs §2.3.

## 6. Landing

### 6.1 Targets and bases

- `merge = main`: cut from `origin/main`, lands on `main`.
- `merge = parent`: cut from the parent's branch, lands on it.
- A node's base is its landing target, so nothing reaches `main` carrying a container's code unless
  that container landed.
- A task's branch is created at implement, in its `target_repo`; a task with no `target_repo` cannot
  be claimed for implement and cannot land. A container's branch is created per repository the first
  time a child needs it, cut from the container's own base.

### 6.2 Jobs: `tm land` and `tm job`

A landing or a sync can outlast the command runner's 10-minute limit, so each runs as a detached
**job**, a row of `jobs(id, kind, node_id, repo, target, state, step, worktree, pid, heartbeat,
result)` in `state.db`. `kind` is `land` or `sync`. `tm job status <job>` reports it;
`tm job resume <job>` continues it after an agent resolved what stopped it. The running process
heartbeats the lease; a job stopped for an agent keeps its lease with `ttl` null, so sweep does not
roll it back, and discovery offers it (§5.6).

A landing, per repository in order:

1. **Already landed.** If `git merge-base --is-ancestor <branch> <target>`, or
   `git diff --quiet <target>...<branch>`, skip to step 6. This covers a landing killed after its
   push, a repository with nothing to land, and a partial container landing retried.
2. **Landing-stage conditions.** Unmet → the job ends, the node returns to `claimed_from`, displays
   `BLOCKED_BY_CONDITION`, `step_failures` unchanged.
3. **Build.** Fetch; cut a merge worktree owned by tm from the target (`origin/main`, or the local
   container branch); `git merge --no-ff <branch>` with a subject naming the node. Conflict → state
   `needs_agent: conflict`.
4. **Gate.**
   - Landing on a container branch: the node's own `tm verify run --ref <merge HEAD>`. A red is an own
     defect with no baseline, since a node's own verification is red on its target by construction.
     Advisory for a `reject`/`fix = 0` node (§3.3). An optional per-repository quick `parent` gate
     runs after it; its red is attributed as below.
   - Landing on `main`: the repository's `main` gate. Green → step 5. Red → run the same gate on the
     untouched target sha, cached in `gate_baselines(repo, target_sha, template_hash)` so every
     landing on one sha shares one baseline run:
     - baseline green → own defect;
     - both red with a JUnit report → the tip removes a failure and adds none: push; the same set:
       the node waits on the automatic `red-target` condition (below); adds any: own defect;
     - both red with no report → `needs_agent: unattributed`, with both outputs; the agent records an
       own defect or resumes with `--push`.
   - Own defect → the job ends and §3.1's own-defect rows apply.
5. **Push.**
   - To `main`: `git ls-remote`; if the target moved, merge it in again with `--no-ff` and re-run
     step 4 (the estate's rule: the full gate runs at the tip that is pushed). Conflict →
     `needs_agent: conflict`. Push `HEAD:main`. A refused push re-runs this step up to 3 times,
     never counted against the node, then `needs_agent: push_failed`. Never force.
   - To a container branch: under the branch's `branch_locks(repo, branch, holder, heartbeat)` row,
     `git update-ref <branch> <new> <old>` (compare-and-swap). A moved branch re-runs from step 3.
6. **Verify.** `tm verify run <id> --ref <target>`; for a container, the union of its descendants'
   verifications. An empty set passes and says so in `:merge`. Red → an own defect (the code is on
   the target but the node's assertion does not hold there), counted in `merge_attempts`: the fix
   commits land again on the next landing.
7. **Complete.** Remove the merge worktree; after the last repository, `COMPLETED`.

An agent handed a stopped job (`needs_agent: <reason>`) works in its worktree and ends with
`tm job resume <job>`, or `tm job resume <job> --own-defect "<finding>"` to record an own defect.
A job it leaves unresolved (lease expiry, transient release) counts one `step_failure`.

**The `red-target` condition.** An automatic `landing`-stage condition on the node, evaluated by tm
itself rather than a shell command: "the `main` gate's baseline for `<repo>`'s current `main` sha is
green or no longer contains this failing set". It is removed when it clears. When landings have been
parked on the same red for `red_target_decision_after` (default 3600 s), tm opens one decision naming
the repository, the sha and the failing set, blocking every parked node, so a red `main` nobody fixes
reaches the owner.

`TM_VERIFY_REF`, which `--ref` already exports, is how a `test_command` reads the landing target.

### 6.3 What counts

Only an own defect counts against `merge_attempts`: a red on the node's own verifications, a red the
baseline does not share, failures the tip adds, a red post-landing verify, or an agent's
`--own-defect`. A conflict, a race, a red target and an unattributed red never count there; an agent
failing to resolve one counts in `step_failures`.

### 6.4 Syncing a container branch

When a claim needs a sync (§4.1), `tm task start` starts a `sync` job and returns `blocked` with
reason `syncing <branch>`. The job, under the branch's lock, merges the target into the base with
`git merge --no-ff` in a tm-owned worktree, runs the container's `parent` gate if one is configured,
and moves the branch by compare-and-swap. A conflict or a red stops it at `needs_agent`, offered by
discovery like a stopped landing.

`ponytail:` container branches are local refs in the shared clones, never pushed: every agent works
in worktrees of the same clones, and pushing would trigger CI on repositories that build every
branch. The ceiling is a single host; the upgrade is pushing container branches once agents span
machines.

### 6.5 Containers across repositories

A container lands each repository in `land_order`, else in config `repo_order`, one landing job per
repository, in sequence. It stays `MERGING` while its jobs run or wait for an agent, and is
`COMPLETED` once every repository has landed. An own defect in a later repository moves it to
`REVIEWED`/`merge_failed` like any node. Repositories already landed are recognised by step 1 on the
next landing, so only what is left lands. A pushed `main` is never rolled back.

### 6.6 No rebase

tm never rebases or squashes. Rebasing a container branch orphans every child cut from it; ancestry
must keep answering "is this code on that branch"; and a merge keeps the reviewed commits as the
landed commits. One `--no-ff` merge commit per landing keeps `git log --first-parent <target>` a list
of landed nodes. An implementer may rewrite its own branch before its first review.

## 7. Derived views

### 7.1 Phase

`QUEUED` for `READY`; `DISPATCHED` from `IMPLEMENTING` through `MERGING`; `COMPLETED`; `FAILED`; or
the exit. Never stored.

### 7.2 Display status

The first matching row wins:

| Condition | Display |
|---|---|
| an exit, `COMPLETED` or `FAILED` | itself |
| `MERGING` with a job waiting for an agent | `WAITING_MERGE_AGENT` |
| `-ING` with a live lease | itself |
| `-ING` with an expired lease, or with no lease row | `STALE` |
| an edge to an open decision | `AWAITING_DECISION` |
| an unsatisfied edge | `BLOCKED_BY_TASK` |
| an unmet condition (for its next stage) | `BLOCKED_BY_CONDITION` |
| a sync its claim needs is running or waiting | `BLOCKED_BY_SYNC` |
| next action's files locked by another lease | `BLOCKED_BY_LEASE` |
| container at `READY` with a descendant past `READY` | `IMPLEMENTING` |
| `READY` | `READY` |
| `IMPLEMENTED` | `WAITING_REVIEW` if `review`, else `WAITING_MERGE` |
| `REVIEWED`, `approve` | `WAITING_MERGE` |
| `REVIEWED`, `reject`, `fix` on; or `merge_failed` | `WAITING_FIX` |
| `REVIEWED`, `reject`, `fix` off | `WAITING_MERGE` |
| `FIXED` | `WAITING_REVIEW` |

A partially landed container shows its per-repository landed state beside the display status.

`NodeStatus` and `VirtualStatus` are replaced by `Status` (stored), `DecisionStatus`, `Phase` and
`DisplayStatus`. `IN_FLIGHT` and `BLOCKED` go. Sweep restores every `-ING` whose lease is expired or
missing, from `claimed_from`.

## 8. Configuration

Added to `.taskmanager/config.yaml`, each settable and validated through `tm config`:

```yaml
max_fix_rounds: {task: 2, container: 3}
max_merge_attempts: 3
max_step_failures: 3
condition_ttl: 300
condition_timeout: 60
red_target_decision_after: 3600
lease_ttl: {implement: 10800, review: 3600, fix: 7200, merge: 3600, sync: 3600}
repo_order: [core, api, web]
repos:
  <repo>:
    gates:
      main:   {command: "<template>", junit: "<glob, optional>", timeout: <s>}
      parent: {command: "<template, optional>", timeout: <s>}
```

A gate command is a template over `{worktree}`, `{node}`, `{repo}` and `{target}`. The baseline
cache hashes the template, not the rendered command, so every node shares a baseline. For SocialSrc,
`main` wraps the repository's CI command in `infra/agent-test.sh --task-id {node} ... {worktree}`. A
repository with no `main` gate configured cannot land on `main`: the job stops at
`needs_agent: no gate`. The existing scalar `lease_ttl` becomes the `implement` default when the map
is absent. A node's `gate_lane` frontmatter stays what implement, review and fix briefs carry for
their own gates.

## 9. Storage and cutover

There is no in-place migration. The owner ruled (2026-09-24) that the ongoing SocialSrc work is
re-imported strategically into a fresh estate instead.

### 9.1 Fresh storage

- The new version stores its state in new files under `.taskmanager/`: `state.db` (nodes, sections,
  relations, verifications, conditions, search indexes, leases, file locks, branch locks, jobs),
  `cache.db` (`gate_baselines`, `condition_results`) and `ledger.db`, each created at its current
  schema by `tm init` and versioned by `PRAGMA user_version` for future changes. The self-healing
  `ALTER`s go.
- Opened in a directory holding a pre-lifecycle estate (a SQLite `spec.db` present, no `state.db`),
  every command except `tm init --archive` refuses with: "this directory holds a
  pre-lifecycle estate: run `tm init --archive` to move it to `.taskmanager/archive-<timestamp>/`
  and start fresh, then re-import the ongoing work". `tm init --archive` moves the old files, never
  deletes them.
- A pre-lifecycle binary fails loudly against the new estate: `tm init` writes `spec.db` and
  `runtime.db` as plain-text tombstones naming the version that owns the directory, so the old
  binary's first query fails with SQLite's "file is not a database" instead of quietly opening an
  empty estate.
- `tm restore` reads only the new export format and refuses an old one, naming the last
  pre-lifecycle release (tagged `v0.2.0` before this work lands) as the version that reads it.

### 9.2 Cutover runbook (in `tm guide overview`)

Each step runs on the owner's go-ahead:

1. Stop every dispatcher; wait for an empty `tm run list`.
2. With the old version: `tm export` into `spec/tm`, commit; that snapshot is the archive of record.
3. Author the re-import: `tm import` documents for the specs, plans, tasks and open decisions still
   ongoing, carrying their sections, verifications, edges, flags, conditions and `requires` in the new
   shape. Completed work is not re-imported; edges to it are dropped, since it is on `main`.
4. Install the new plugin and `tm`; replace any session-local copy of the old `tm-wave` script.
5. `tm init --archive`; configure `repos.<repo>.gates` and `repo_order`.
6. `tm import` the documents; `tm wave discover` shows what is claimable, for the owner to check.
7. Apply the prepared guide addendum and rules changes (§10.3).
8. `tm export` into `spec/tm` and commit.
9. Resume dispatching with the new `tm-wave`.

## 10. Everything else that changes

### 10.1 `tm-wave.js`

One loop per node: `tm task start` → dispatch the agent the action names, on the model it names, or
poll `tm job status` for a merge or sync → `complete`, `review`, `release` or `tm job resume` →
repeat until the action is `blocked` or the status is `COMPLETED` or `FAILED`.

Removed from the script: the in-memory fix counter, the review-hash check, the status argument of
`release()`, `hold()`, `--release`, the post-merge verification, and the merge brief's procedure (tm
runs it). Kept: `holdMerge` (§4.5). `requires` replaces the Figma special case. A merge or sync
agent's brief is the job's stopped state, its output and its worktree.

### 10.2 The web, this sub-project only

- Enums, icons and colours for every display status and phase; filters by both.
- The detail drawer shows the stored status, outcome, verdict, counters, flags, `merge`, the landing
  chain ("on `tm/P`; waits for P → main"), conditions with their stage and last result, `requires`,
  and any job with its state.
- Edit forms for flags, `merge`, conditions, `requires` and `land_order`, through the same validation
  and cycle check as the CLI.
- The status dropdown is replaced by the verbs: reopen, reset, defer, abandon, supersede.
- Decisions use `OPEN`, `ANSWERED`, `WITHDRAWN`; option effects are shown; a `FAILED` or stranded
  decision links its nodes.
- Static export matches. The tree view stays until sub-project 3.

### 10.3 Documentation and packaging

- **TaskManager:** all seven guides rewritten (`overview` gains the lifecycle diagram and the
  cutover runbook; `plan` covers flags, `merge`, conditions, `requires`, cycles and container review;
  `merge` shrinks to the stopped-job brief), both skills and their bundled copies, `commands/task.md`,
  the README, and a version bump across `pyproject.toml`, `plugin.json`, `marketplace.json` and
  `gemini-extension.json`.
- **SocialSrc, prepared ahead and applied at cutover:** `.claude/rules/35-taskmanager.md`,
  `85-reports-gates-copy.md` and their ledger entries on a `--no-track` worktree branch, pushed at
  cutover so the rules never describe an uninstalled tm; the project guide addendum (sections of the
  `guide` node in the live database) as a file applied with `tm section set` after the re-import; the
  owner's global `dispatcher` skill copy.

## 11. Testing

Test-first; the repository's gates (`uv run pytest`, `uv run ruff check`, `uv run ruff format
--check`, `uv run mypy`) run at every step.

- **Transitions:** every (stored status, flags, outcome, `fix_for`, counters, event) combination,
  asserted to its next state or its refusal, including every row that reaches `FAILED`.
- **Containers:** the rollup on every child change, the empty-diff completion, reopen of a container
  with completed children, a child added under each container status.
- **Display:** every row of §7.2, including precedence and `-ING` with no lease row.
- **Claims:** two racing claims in threads, exactly one wins; a refused claim leaves both databases
  byte-identical.
- **Cycle check:** randomised graphs; a write is refused exactly when the expanded graph has a cycle,
  and the printed path is a real cycle; the migration-chain case across a container boundary.
- **Satisfaction:** landing and base chains across `parent`/`main` mixes, three levels deep, and
  cross-repository edges.
- **Validation:** every rule of §2.3, including a parent write that strands a child and a `merge`
  change after the branch exists.
- **Loops:** each counter reaching its cap from each failure source, including an unresolvable
  conflict handed to agents that keep dying.
- **Landing, against real temporary git repositories:** clean landing; already-landed and empty
  landings; conflict handed to an agent and resumed; a push landing mid-gate and the re-gate; baseline
  cache reuse across nodes; JUnit strict subset, same set, added failure; the `red-target` condition
  and its decision; unattributed red; post-landing verify red; a parent landing's compare-and-swap
  race; a container across repositories with a later one failing and then landing only what is left;
  claim-time sync with and without conflict; a killed job recovered.
- **Storage:** a fresh `tm init`; refusal on a pre-lifecycle directory and `tm init --archive`
  moving it intact; `tm restore` refusing an old export; the kind-aware `CHECK` refusing an old
  status on write.
- **CLI and web API:** integration tests for every verb and refusal.
- **End to end, before publishing:** the new `tm-wave` on a scratch estate of toy repositories with
  real cheap agents, covering at least one landing on a parent branch, one container review, one
  refused cycle, one `FAILED` node and one reopen.

The live SocialSrc database is not read by tests and not written by anything in this work until the
owner authorises the cutover (§9.2).

## 12. Choices made without an explicit owner ruling

Listed so the owner can overturn them:

1. Decision options carry effects (§4.2), used by `FAILED` and stranded dependents.
2. `investigate` on a `FAILED` decision reopens the node with the answer as the note.
3. Edges on a container are inherited by every descendant (§4.1); today they gate nothing below.
4. Container branches are local, never pushed (§6.4).
5. Model routing (§5.5): container reviews at `opus` or above; task reviews fall back to `sonnet`;
   third fix rounds at `opus` or above.
6. A condition with no executable command is refused; it is a decision instead (§4.3).
7. `reset` exists as a ledgered repair verb, with landing proven for `COMPLETED` (§3.4).
8. A same-set red on tip and target waits on the `red-target` condition, and reaches the owner as a
   decision after an hour (§6.2).
9. `outcome` (closed, routes) and `verdict` (free text, never routes) are separate columns (§2.1).
10. A review after a merge fix is not counted (§3.2).
11. `step_failures` bounds agent deaths, transient releases and unresolved jobs (§3.2).
12. Leases and locks live in `state.db` with the nodes, so a claim is one transaction (§5.1).
13. Children cannot be added under a `COMPLETED` container; a new plan is filed instead (§2.3).
14. `holdMerge` stays a dispatcher argument rather than node state (§4.5).
15. `tm init` writes plain-text tombstones at the old database paths so an old binary fails loudly
    (§9.1).
