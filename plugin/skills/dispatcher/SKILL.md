---
name: dispatcher
description: Use when planning a wave, dispatching a subagent, routing a task to a model, ordering review, fixes or a merge across a plan's work, or writing new specs, plans and tasks into TaskManager. Run `tm guide dispatch` before the first dispatch of the session.
---

# Dispatching with TaskManager

`tm` holds the specs, plans and tasks, decides the next step of each, locks the files a claim covers, lands finished work and records every state change. It is the only record of what is planned, claimed, built and finished — there is no second tracker, and nothing it can answer is written down anywhere else.

`tm` installs separately from this plugin: `uv tool install git+https://github.com/TaigoPedrosa/TaskManager@v<this plugin's version>`. When `tm` is not on PATH, say so and stop; run nothing in its place.

`tm --version` must print this plugin's version; otherwise stop and say which one to upgrade.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read the topic for what you are doing

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| running waves, routing models, holding landings, handling failures | `tm guide dispatch` |
| authoring new specs, plans and tasks, or amending landed ones | `tm guide plan` |
| turning a ticket, an outside request or a defect report against landed work into nodes | `tm guide intake` |
| first contact with `tm`, or a command you have not met | `tm guide overview` |

Run `tm guide dispatch` before the first dispatch of the session. It names the arguments, what each refusal means and which step tm chooses when; a project's own conventions are appended to the same output, so the guide you read is the one that applies here.

The subagents you dispatch read `tm guide implement`, `review`, `fix` or `merge` themselves. Do not carry their guidance into the prompt.

## The mechanism is the `tm-wave` workflow

Orchestrate the queue with the `Workflow` tool running the plugin's `tm-wave` workflow, by name, `Workflow({name: 'taskmanager:tm-wave', args: {session, worktreeDir, ...}})` with the plugin installed, or by `scriptPath`, the plugin's `workflows/tm-wave.js`. This is required rather than preferred: chaining one-off `Agent` calls by hand appoints the session as the scheduler — the job the script exists to do — and rebuilds, a notification at a time, the barrier a per-node loop removes.

Each tick, `tm wave discover` chooses a batch of claimable nodes, disjoint by their declared files, and each node's next step is claimed once: `tm task start` names it and the model it runs on, the workflow dispatches that agent, and the agent closes its step with its guide's verb. One run claims and runs that one step and stops — implement, review, fix and merge each wait for their own tick, and a node needing more goes back through `tm wave discover` next time. tm counts fix rounds, landing failures and failed steps into `FAILED` and opens a decision for the owner, so the workflow keeps no counter and no hold of its own. The one hold a dispatcher keeps is `holdMerge`, which discovery reads as `tm wave discover --hold-merge <node-id>`: nodes whose landing is irreversible, deploys, or is the owner's call wait at their merge step, and a reviewed plan or spec, which lands before its review, waits with that review still owed.

A reviewed plan or spec lands first and reads `LANDED` until its one review, on its landed target, runs. A fix lands without a re-review unless the node is sensitive, so never re-dispatch a review of a fix that is not sensitive.

Because a run takes one step per node, the dispatching session loops itself to carry a node the rest of the way: arm `/loop` with a dynamic interval unless the user says otherwise. Each wakeup is one tick — read `tm run list`, then one `tm wave discover` sized to the tick's budget, split into staggered waves of at most `wave_size` nodes; launch each wave as its own `tm-wave` run by `scriptPath` with `maxBatch` set to `wave_size` and every other wave's chosen ids in `exclude`, so no two runs draw the same node. Record what each wave landed, failed or blocked, and schedule the next wakeup, shorter while nodes are mid-step and longer when nothing is claimable. Never run two ticks in one wakeup off the same unclaimed pool. When `tm wave discover` offers nothing and no step is in flight, report and stop. `tm guide dispatch` prints the effective `tick_min`, `tick_max`, `wave_size` and `tick_budget` as the typical target; a dispatch message's own numbers override them for that session.

A question that holds work is a decision, not a chat message: `tm decision add … --blocks <ids>`. It outlives the session and holds exactly the nodes it names. Say in your status report that it is open.

If the harness demands a permission for that mechanism which the session cannot grant itself, ask the user for it as a question, with the options and a recommendation, in the same response, and keep doing every part that does not depend on the answer. Falling back to sequential dispatches without saying so is the failure this paragraph exists to name: each dispatch looks correct on its own, so nothing in the transcript shows the mechanism was abandoned.

`session` is required; `worktreeDir` defaults to the estate's `worktree_dir` config, and every other argument is listed in `tm guide dispatch`. A run holds at most `min(16, CPUs - 2)` agents concurrently, so pass `maxBatch` at or under that to keep a large batch from sitting claimed but idle instead of waiting for the next tick.

## The shape of it

`tm wave discover --session <id> --slots <n> --max-strong <n>` offers what is claimable; `acceptable_models` and `review_models` route each step, and tm prints the model id with every claim, which the workflow runs the agent on; the brief is `tm render <task-id> --view subagent`, with nothing added to it. `tm run list --yaml` is what is in flight. `tm task defer`, `tm task supersede` and `tm task reopen` are how a plan changes shape.

## Shared outputs and contract decisions

- When tasks share an output directory, the brief names each task's owned paths and forbids deleting anything else.
- When a report records contract decisions, copy them into the plan overview before any dependent is claimed.
- A brief that runs work in parallel names every file two processes write and how those writes serialize.
- A task that consumes another task's derived structure (ids, orderings, mappings) reads that task's output and never re-derives it.
- A UI brief over lazily-loaded data defines each panel's states (loading, partial, empty, error, ready) and the reads each one depends on.
- A brief that has the implementer step or search over a value names the value's allowed range.
