---
description: Query TaskManager DAG state, inspect specs/plans/tasks, or execute tm commands.
argument-hint: "[command] [args...]"
---

Run TaskManager (`tm`) operations in the current project repository:

- If `$ARGUMENTS` is provided:
  Run `tm $ARGUMENTS`
- If no arguments are provided:
  Run `tm next --limit 5` followed by `tm task list` to show the current active workflow state.
