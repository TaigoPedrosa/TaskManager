---
description: Query TaskManager DAG state, inspect specs/plans/tasks, or execute tm commands.
argument-hint: "[command] [args...]"
---

Run TaskManager (`tm`) operations. `tm` resolves the project from the working directory, from
`TM_ROOT`, or from `-C <project root>`, so it works from any directory inside it, worktrees
included; `--yaml` is the compact read on every list and get.

- If `$ARGUMENTS` is provided:
  Run `tm $ARGUMENTS`
- If no arguments are provided:
  Run `tm next --limit 5 --yaml` followed by `tm task list --yaml` for the current state.
- If you are about to work a task rather than look at one:
  Run `tm guide` and then the topic for your role — `tm guide implement`, `tm guide review`,
  `tm guide fix` or `tm guide merge`.

## Standing authorization

The user invoking this command is their explicit opt-in to both of the following, for the rest of
the session. Neither is asked for again.

- **Dynamic workflows.** The `Workflow` tool may be called to orchestrate the queue without a
  further prompt. Use `pipeline()`, not `parallel()`: a task reaches review, fix and merge as soon
  as its own chain clears, and no stage waits on the rest of the wave. Agent count follows the
  queue rather than the session's default workflow size guideline.
- **A self-paced loop, capped at 15 minutes.** `ScheduleWakeup` may be armed to resume unattended.
  `delaySeconds` never exceeds 900. Pass `noop: true` on a tick that found nothing, and
  `ScheduleWakeup({stop: true})` once the queue holds no dispatchable work — a loop still waking on
  an empty queue is the failure this cap is here to bound.

**This authorizes the mechanism, not the act.** What was the user's to decide stays theirs: an
irreversible teardown, a merge that applies to production, and every task whose plan marks it as
the owner's are held and reported, never dispatched by a loop or a workflow. A wave that finds only
owner-gated work reports that and stops, rather than picking something to do.
