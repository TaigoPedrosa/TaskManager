---
description: Claim, inspect, heartbeat, or complete the active task in the current worktree.
argument-hint: "[start <task-id> | heartbeat | verify | stop [status]]"
---

Run `tm guide implement` (or `review`, `fix`, `merge` for your role) before the first command of a
task: it names what each command refuses and the handoff you owe. `tm` finds the project and, inside
a task worktree, the task, so the id is optional there.

- `start <task-id>`: claim the task and cut its worktree and branch:
  `tm run start <task-id> --worktree --agent <name> --session <id>`
- `heartbeat`: renew the lease before its TTL runs out:
  `tm run heartbeat`
- `verify`: run the task's declared verifications, exit 1 on a failure:
  `tm verify run`
- `stop`: release the lease and set the next status:
  `tm run stop --status ${ARGUMENTS:-WAITING_REVIEW}`
- no arguments: show what is claimed and which files are locked:
  `tm run list --yaml`
