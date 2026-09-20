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
