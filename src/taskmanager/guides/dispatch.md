# Dispatching

For the session manager: run the `tm-wave` workflow, which asks tm what each node needs next and hands every step to the model tm names, and keep no record of what is in flight that tm does not already hold.

## 1. The mechanism is the `tm-wave` workflow

The plugin ships the dispatcher as a workflow script, run by name, `Workflow({name: 'taskmanager:tm-wave', args: {...}})` with the plugin installed, or by `scriptPath`, the plugin's `workflows/tm-wave.js`. One run is one tick:

1. `tm wave discover` chooses a batch: every claimable node, within the session's slots, file-disjoint within the batch.
2. Each chosen node's next step is claimed once, and no node waits for a sibling. `tm task get` reads where it stands and its `next_action`; `tm task start --worktree-dir <worktreeDir>` claims that one step and names its action and model family; the workflow dispatches the agent that action needs on the model id `models` maps that family to; the agent does the step and closes it with its guide's verb, passing the lease's agent name with `--agent` and the claim's token with `--token`. For a `merge` or a `sync`, the workflow waits on `tm job status <job> --wait 540` instead. When the job is already parked for an agent — a merge claim of a node already `MERGING`, or any sync claim — that claim is the hand-over: the workflow dispatches the agent the job needs, on the new token `tm job resume` asks for, and this run's one step is that hand-over. A container landing in several repositories runs one job per repository within that step, and the workflow follows each to the next.
3. The run reads the node once more and stops, returning whatever its status now is — mid-step, `COMPLETED`, `FAILED`, or blocked on something outside the step. It claims nothing further: a node still short of `COMPLETED` goes back through `tm wave discover` on a later tick.

A step the agent leaves open, an agent that dies, or a handed-over job the agent never resumed is released by the workflow with `tm task release <id> --agent <its lease's agent> --token <its claim's token>` as a failed step, which tm counts; tm refuses that release once another claim holds the node. A job the agent resumed that stops again is left parked, since tm has counted it already, and the next claim hands it over. A claim naming a family `models` does not map is released the same way, never run on a guess. tm counts fix rounds and landing failures too, so the workflow keeps no counter and no hold of its own.

Because one run takes a node exactly one step, carrying it from `implement` through `review`, `fix` and `merge` takes several ticks — so the dispatching session loops itself, not the workflow: arm `/loop` with a dynamic interval unless the user says otherwise. Each wakeup is one tick: read `tm run list`, then one `tm wave discover` sized to the tick's budget, split into staggered waves of at most `wave_size` nodes; launch each wave as its own `tm-wave` run by `scriptPath`, with `maxBatch` set to `wave_size` and every other wave's chosen ids in `exclude`, so no two runs draw the same node. Record what each wave landed, failed or blocked, then schedule the next wakeup — shorter while nodes are mid-step, longer when nothing is claimable. Never run two ticks in one wakeup drawing from the same unclaimed pool. When `tm wave discover` offers nothing and no step is in flight, report and stop.

A question that holds work is a decision, not a chat message: `tm decision add … --blocks <ids>`. It outlives the session and holds exactly the nodes it names. Say in your status report that it is open.

**Typical target** (`tm config` keys `dispatch.tick_min`, `dispatch.tick_max`, `dispatch.wave_size`, `dispatch.tick_budget`, effective here): a wakeup every {{tick_min}}–{{tick_max}} s, waves of at most {{wave_size}} nodes, at most {{tick_budget}} nodes dispatched per tick across those staggered waves. A dispatch message's own numbers override these for that session; `tm config set` changes what this line prints.

Arguments, of which `session` and `worktreeDir` are required:

| Argument | What it is |
|:--|:--|
| `session` | this dispatching session's name; every lease carries it |
| `worktreeDir` | where implement and fix worktrees are cut, passed to every claim as `tm task start --worktree-dir` |
| `specs` | spec ids to discover under; omitted means every spec and every node with no spec |
| `slots` | agents this session may hold at once (default 9) |
| `maxStrong` | of those, how many may run on `opus` or `fable` (default 5) |
| `maxBatch` | the most nodes one tick takes on; the rest wait for the next tick |
| `exclude` | node ids this tick never chooses |
| `holdMerge` | node ids whose landing this tick never starts: discovery passes over their merge step (`--hold-merge`), and a node reaching it mid-loop stops there; a task is implemented, reviewed and fixed, and waits at its merge step for the owner; a reviewed plan or spec lands before its review, so holding its landing holds that review too |
| `root` | the tm root every command runs from; defaults to the session's own directory |
| `tm` | the `tm` executable every command runs; defaults to the one on `PATH` |
| `agentTypes` | repository → agent type for implement and fix |
| `reviewerTypes` | `task`, `rereview` and `container` → agent type for a task's review, a sensitive fix's re-review, and a plan's or spec's review of its landed target |
| `capabilities` | agent type → the `requires` values it can serve; a node needing one no preferred type serves goes to the default agent |
| `preamble` | repository → a line prepended to every brief for it, plus a `default` key |
| `rulesDir` | a directory every agent reads before its first edit |
| `gateLane` | where suites and gates run, as text or repository → text with a `default` key; `{task}` becomes the node id |
| `models` | the family tm names on a claim → the model id every brief's `Model:` line carries; each family given overrides the current Claude id, and a family missing from both is released unrun |

A run holds at most `min(16, CPUs - 2)` agents at once, so a batch larger than that queues inside the run; `maxBatch` keeps it from sitting claimed but idle.

What still binds you when you run it:

- **Never hand-roll the loop.** Chaining single dispatches by hand appoints the session as the scheduler and rebuilds, a notification at a time, the barrier a per-node loop removes.
- **The workflow claims; agents close.** It runs every `tm task start` before it dispatches, so no agent explores before its claim and no second dispatcher sends a second agent. Each agent closes its own step with the verb its guide names.
- **A resumed run replays, then reads.** Resuming replays the longest unchanged prefix of the run's agent calls from cache, `tm task get` reads included, so the replayed part says what happened before the run stopped, not where the nodes stand now. Only the calls after it reach tm. A node that moved while the run was down is caught by its next live claim, which tm refuses or blocks; start a fresh tick rather than resume one that ran long ago.

## 2. What is claimable

```
tm wave discover --session <id> --slots <n> --max-strong <n>
tm wave discover --spec <spec-id> --session <id> --slots <n> --max-strong <n> --exclude <node-id>
tm wave discover --session <id> --slots <n> --max-strong <n> --hold-merge <node-id>
```

Every node of every kind whose next step can be claimed now, with that step and its model, plus landings and syncs stopped for an agent; a JSON line, then `__CHECK n=<chosen> h=<djb2>`. A node is claimable when none of these holds, checked in this order: it is mid-step or its job is running; an edge (its own, or one on a container above it) points at an open decision; an edge is unsatisfied; a `claim` condition is unmet; a sync its claim needs is running or waiting; its next step would lock a file another lease holds; its status has no next step.

A reviewed plan or spec lands before its review: once its code is on its target it reads `LANDED`, and its one review is claimable. Nodes that depend on it may start meanwhile, since an edge is satisfied once the code has landed. A `FIXED` node's next step is its landing, unless the node is sensitive (`tm guide plan`, §2): then it is one re-review, scoped to the open findings. A dispatcher never re-dispatches a review of a fix that is not sensitive: not by hand, not through a `tm task reset`, not as a `rereview` agent sent anyway.

Within a batch no two nodes declare the same file. Across a repository, a node writing a migration holds every other migration writer back until it has landed on its spec's target, except siblings building on its own container branch. Two nodes touching one schema, one generated file or one shared table are not disjoint whatever their file lists say: give them an edge.

## 3. Models are tm's

`tm task start` names the model family; the workflow runs the step on the id `models` maps it to.

| Step | Family |
|:--|:--|
| implement | the cheapest family in `acceptable_models` |
| review of a task | the family of `review_models` when set, else `sonnet` |
| review of a plan or spec | the family of `review_models` when set, else the strongest in `acceptable_models`, never below `opus` |
| fix after a rejection, rounds 1 and 2 | the implement family when it is `opus` or `fable`, else `sonnet` |
| fix after a rejection, round 3 onward (containers only) | the strongest in `acceptable_models`, never below `opus` |
| fix after a failed landing; a landing or sync agent | `sonnet` |

A list you disagree with is a plan defect: fix it with `tm task update <id> --models a,b` and say so, never dispatch around it.

A rejection buys one fix, which lands without a re-review unless the node is sensitive; a sensitive fix whose re-review rejects is `FAILED` with a decision, never a second fix. So `max_fix_rounds.task` and `max_fix_rounds.container` bound only the sensitive path, and a fix past round 1 follows only a `tm task reset` that kept an earlier count. The lever on a node that keeps failing is widening `acceptable_models`, tried before anyone answers `investigate`.

## 4. Holds are edges, decisions and conditions

A node waits only on something named: an edge (`tm task depends <id> --add <other-id>`), a decision (`tm decision add ... --blocks <id>`), or a condition (`tm task condition add <id> --needs "<what>" --command "<check>"`). `holdMerge` is the one hold the dispatcher keeps, and it is policy for this tick, not state on the node: a landing that is irreversible, deploys, or is the owner's call is listed there and reported, and the node's earlier steps still run.

## 5. What is in flight

```
tm run list --yaml
tm run sweep
tm job status <job>
tm job status <job> --wait 540
```

`tm run list` is every lease, locked file and job; nothing else needs writing down, and never a session's own agent list. A lease past its TTL reads `STALE` until `tm run sweep` returns the step to where it was claimed from and counts a failed step. A landing stopped for an agent reads `WAITING_MERGE_AGENT`, and discovery offers it.

## 6. When something fails

A node that spends its fix rounds, its landing attempts or its failed steps, or whose sensitive fix is rejected on its re-review, is `FAILED`, and tm opens a decision on it; a target branch that stays red under parked landings for an hour opens one too; and a node deferred, abandoned or failed while others depend on it opens one on those dependents. `tm decision list --status open` is the owner's queue, not yours: do not answer a decision on the owner's behalf, and do not chase an agent to withdraw one.

Re-running a failed step unchanged is not a fix. Before anyone answers `investigate`, change what made it fail: correct the brief with `tm section set`, widen `acceptable_models`, or split the node. A rejection whose findings are rulings — "the brief doesn't say", "which of these is correct" — is answered by a decision, not another fix round: the reviewer raises it instead of rejecting on it.

- A rejection whose findings are only about the record's wording or accuracy, on work whose ruling is already made, does not buy another fix round: the findings go in the node's `report` section and the node closes as the ruling stands.

## 7. When the plan changes

- **Defer**: `tm task defer <id> --note "<why>"`. The note is kept in `:deferral`.
- **Abandon**: `tm task abandon <id> --note "<why>"`.
- **Supersede**: `tm task supersede <old-id> <new-id> --transfer-blocks all` sets the old node `SUPERSEDED` and re-points every dependent at the new one, which must already exist.
- **Reopen**: `tm task reopen <id> --note "<why>"` puts a failed, deferred or abandoned node back into the cycle.
- **Repair**: `tm task reset <id> --to READY --note "<why>"`, ledgered, for a stored status that is wrong.

A dependent of a deferred, abandoned or failed node is never stranded silently: the decision tm opens on it asks whether to drop the edge, defer it, or abandon it.

## 8. Write rulings down where the work is

A ruling, a constraint, a hazard or an answer the next agent will need goes on the node it applies to:

```
tm section set <task-id>:context --file <path> --header "## Context"
tm section set <plan-id>:context --file <path>
```

A plan's `context` reaches every task's brief. Anything a `tm` command can answer — what is claimed, what is ready, who holds a file — is not written down at all.

## Never

- Never dispatch a step `tm task start` did not claim, and never two agents on one node.
- Never paste, summarise or extend the rendered brief; `tm render <id> --view subagent` is the brief.
- Never keep a second record of what is in flight, a fix counter, or a hold outside `holdMerge`.
- Never answer a decision that is the owner's.
- Never re-run a failed node without changing what made it fail.
- Never re-dispatch a review of a fix that is not sensitive.
