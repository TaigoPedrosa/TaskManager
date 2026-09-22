---
name: dispatcher
description: Use when planning a wave, dispatching a subagent, routing a task to a model, ordering review, fixes or a merge across a plan's work, or writing new specs, plans and tasks into TaskManager. Run `tm guide dispatch` before the first dispatch of the session.
---

# Dispatching with TaskManager

`tm` holds the specs, plans and tasks, decides what is claimable, locks the files a claim covers, and records every state change. It is the only record of what is planned, claimed, built and finished — there is no second tracker, and nothing it can answer is written down anywhere else.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read the topic for what you are doing

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| planning a wave, routing models, dispatching, ordering review and merge | `tm guide dispatch` |
| authoring new specs, plans and tasks, or amending landed ones | `tm guide plan` |
| first contact with `tm`, or a command you have not met | `tm guide overview` |

Run `tm guide dispatch` before the first dispatch of the session. It names the flags, what each refusal means and who sets which status; a project's own conventions are appended to the same output, so the guide you read is the one that applies here.

The subagents you dispatch read `tm guide implement`, `review`, `fix` or `merge` themselves. Do not carry their guidance into the prompt.

## The mechanism is a workflow, not a run of single dispatches

Orchestrate the queue with the `Workflow` tool, as one script that pipelines every task through
implement → review → fix → merge independently. This is required rather than preferred: chaining
one-off `Agent` calls by hand appoints the session as the scheduler — the job the script exists to
do — and rebuilds, a notification at a time, the wave barrier a pipeline removes.

`tm guide dispatch` §7 carries the script's own rules. The load-bearing ones: read the task set
from `tm` at runtime instead of freezing it in a literal, let the agent inside each stage write the
status rather than the script, cap the fix rounds at two and report what is still open, and hold
the *merge stage* — not the whole task — for anything irreversible, production-applying or marked
the owner's.

If the harness demands a permission for that mechanism which the session cannot grant itself, ask
the user for it as a question, with the options and a recommendation, in the same response, and
keep doing every part that does not depend on the answer. Falling back to sequential dispatches
without saying so is the failure this paragraph exists to name: each dispatch looks correct on its
own, so nothing in the transcript shows the mechanism was abandoned.

## The shape of it

`tm next -n 5 --strategy balanced --yaml` offers what is claimable; you check the batch is file-disjoint, route each task by its `acceptable_models`, and dispatch with `tm render <task-id> --view subagent` as the brief and nothing added to it. `tm run list --yaml` is what is in flight. Statuses carry the work through review, fixes and merge; `tm task supersede` and a `DEFERRED` stop are how a plan changes shape.
