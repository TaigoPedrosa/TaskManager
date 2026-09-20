---
description: Claim, inspect, heartbeat, or complete the active task in the current worktree.
argument-hint: "[start <task_id> | heartbeat | verify | stop [status]]"
---

Manage task execution lifecycle:

- `start <task_id>`: Claim task lease and initialize isolated worktree:
  `tm run start <task_id> --worktree`
- `heartbeat`: Refresh the active lease heartbeat:
  `tm run heartbeat`
- `verify`: Run declared AST and filesystem verifications:
  `tm verify run`
- `stop`: Complete or hand off the active task:
  `tm run stop --status ${ARGUMENTS:-WAITING_REVIEW}`
- If no arguments provided:
  Show active leases and file locks with `tm run list`
