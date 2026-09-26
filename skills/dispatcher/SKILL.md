---
name: dispatcher
description: Use when planning a wave, dispatching a subagent, routing a task to a model, ordering review, fixes or a merge across a plan's work, or writing new specs, plans and tasks into TaskManager. Run `tm guide dispatch` before the first dispatch of the session.
---

# Dispatching with TaskManager

`tm` holds the specs, plans and tasks, decides the next step of each, locks the files a claim covers, lands finished work and records every state change. It is the only record of what is planned, claimed, built and finished — there is no second tracker, and nothing it can answer is written down anywhere else.

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
| first contact with `tm`, or a command you have not met | `tm guide overview` |

Run `tm guide dispatch` before the first dispatch of the session. It names the arguments, what each refusal means and which step tm chooses when; a project's own conventions are appended to the same output, so the guide you read is the one that applies here.

The subagents you dispatch read `tm guide implement`, `review`, `fix` or `merge` themselves. Do not carry their guidance into the prompt.

## The mechanism is the `tm-wave` workflow

Orchestrate the queue with the `Workflow` tool running the plugin's `tm-wave` workflow (`workflows/tm-wave.js`), by name — `Workflow({name: 'tm-wave', args: {session, worktreeDir, ...}})` — or by path. This is required rather than preferred: chaining one-off `Agent` calls by hand appoints the session as the scheduler — the job the script exists to do — and rebuilds, a notification at a time, the barrier a per-node loop removes.

Each tick, `tm wave discover` chooses a batch of claimable nodes, disjoint by their declared files, and each node's next step is claimed once: `tm task start` names it and the model it runs on, the workflow dispatches that agent, and the agent closes its step with its guide's verb. One run claims and runs that one step and stops — implement, review, fix and merge each wait for their own tick, and a node needing more goes back through `tm wave discover` next time. tm counts fix rounds, landing failures and failed steps into `FAILED` and opens a decision for the owner, so the workflow keeps no counter and no hold of its own. The one hold a dispatcher keeps is `holdMerge`, which discovery reads as `tm wave discover --hold-merge <node-id>`: nodes whose landing is irreversible, deploys, or is the owner's call are implemented, reviewed and fixed, and wait at their merge step.

Because a run takes one step per node, the dispatching session loops itself to carry a node the rest of the way: arm the self-paced `/loop` (no interval) unless the user says otherwise. Each wakeup is one tick — read `tm run list` and `tm wave discover`, launch one `tm-wave` run by `scriptPath` with `maxBatch` 10 when something is claimable and slots are free, record what landed, failed or blocked, and schedule the next wakeup, 300–900 s out, shorter while nodes are mid-step and longer when nothing is claimable. Nothing claimable, or everything held on a decision, does not stop it — it still ticks, at the 900 s ceiling; only the user stops it. Never run two ticks in one wakeup off the same unclaimed pool.

If the harness demands a permission for that mechanism which the session cannot grant itself, ask the user for it as a question, with the options and a recommendation, in the same response, and keep doing every part that does not depend on the answer. Falling back to sequential dispatches without saying so is the failure this paragraph exists to name: each dispatch looks correct on its own, so nothing in the transcript shows the mechanism was abandoned.

`session` and `worktreeDir` are required; every other argument is listed in `tm guide dispatch`. A run holds at most `min(16, CPUs - 2)` agents concurrently, so pass `maxBatch` at or under that to keep a large batch from sitting claimed but idle instead of waiting for the next tick.

## The shape of it

`tm wave discover --session <id> --slots <n> --max-strong <n>` offers what is claimable; `acceptable_models` and `review_models` route each step, and tm prints the model family with every claim, which the workflow maps to a model id through its `models` argument; the brief is `tm render <task-id> --view subagent`, with nothing added to it. `tm run list --yaml` is what is in flight. `tm task defer`, `tm task supersede` and `tm task reopen` are how a plan changes shape.
