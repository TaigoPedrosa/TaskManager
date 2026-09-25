---
description: Claim, inspect, heartbeat, or complete the active task in the current worktree.
argument-hint: "[verify]"
---

Run `tm guide implement` (or `review`, `fix`, `merge` for your role) before the first command of a
task: it names what each command refuses and the handoff you owe. `tm` finds the project and, inside
a task worktree, the task, so the id is optional there.

- `verify`: run the task's declared verifications, exit 1 on a failure:
  `tm verify run`
- no arguments: show what is claimed and which files are locked:
  `tm run list --yaml`
