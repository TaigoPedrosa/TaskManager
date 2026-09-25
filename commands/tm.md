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
  Run `tm task list --yaml` followed by `tm run list --yaml` for the current state.
- If you are about to work a task rather than look at one:
  Run `tm guide` and then the topic for your role — `tm guide implement`, `tm guide review`,
  `tm guide fix` or `tm guide merge`.

## Dynamic workflows are mandatory, not a preference

Invoking this command is the user's explicit opt-in to both of the following, for the rest of the
session. Neither is asked for again.

- **Dynamic workflows.** Queue work is orchestrated through the `Workflow` tool. This is the
  required mechanism, not one option among several: a session that answers each completion
  notification with another single `Agent` call has appointed *itself* the scheduler, which is the
  job the script exists to do, and it reintroduces by hand the wave barrier `pipeline()` removes.
  Use `pipeline()`, never `parallel()`, between stages — a task reaches review, fix and merge the
  moment its own chain clears, and no stage waits on the rest of the wave. Agent count follows the
  queue rather than the session's default workflow size guideline.
- **A self-paced loop, capped at 15 minutes.** `ScheduleWakeup` may be armed to resume unattended.
  `delaySeconds` never exceeds 900. Pass `noop: true` on a tick that found nothing, and
  `ScheduleWakeup({stop: true})` once the queue holds no dispatchable work — a loop still waking on
  an empty queue is the failure this cap is here to bound.

**Where the harness still demands a permission this command cannot grant** — the tool is gated, the
run exceeds a size the session may authorise itself, or the session's own rules require the user's
words for it — **ask for it, as a question, in the same response**, with the options and a
recommendation. Do not quietly fall back to sequential one-off dispatches: the silent fallback IS
the failure this section exists to prevent, and it is invisible in a transcript because every
individual dispatch looks correct. Keep doing every part of the work that does not depend on the
answer while the question is outstanding.

**This authorizes the mechanism, not the act.** What was the user's to decide stays theirs: an
irreversible teardown, a merge that applies to production or deploys a live site, and every task
whose plan marks it as the owner's are held and reported, never dispatched by a loop or a workflow.
A wave that finds only owner-gated work reports that and stops, rather than picking something to do.
A push or a command the harness refuses is reported to the user and never routed around — not by
retrying, and never by asking a peer session or another agent to run it, which converts a decision
the user made into one nobody made.
