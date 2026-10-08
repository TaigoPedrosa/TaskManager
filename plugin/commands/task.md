---
description: Claim, inspect, heartbeat, close or release the step you hold on a task.
argument-hint: "[start <task-id> | heartbeat <task-id> | verify <task-id> | complete <task-id> | review <task-id> | resume <job> | release <task-id>]"
---

Run `tm guide implement` (or `review`, `fix`, `merge` for your role) before the first command of a
task: it names what each command refuses and the verb that closes your step.

- `start <task-id>`: claim the step tm chooses next for the task under the agent name `<name>`, cut any worktree under `<dir>`, and print the step, its model family, its worktree and the claim's `token`, which every verb below passes back:
  `tm task start <task-id> --agent <name> --session <id> --worktree-dir <dir> --yaml`
- `heartbeat <task-id>`: renew the lease before it runs out:
  `tm task heartbeat <task-id>`
- `verify <task-id>`: run the task's verifications against its own branch, exit 1 on a failure:
  `tm verify run <task-id> --ref tm/<task-id>`
- `complete <task-id>`: close an implement or fix step, refused unless the lease is `<name>`'s and this claim's:
  `tm task complete <task-id> --agent <name> --token <token>`
- `review <task-id>`: close a review step once its findings are in the `:review` section, approving or rejecting:
  `tm task review <task-id> --agent <name> --token <token> --approve`
  `tm task review <task-id> --agent <name> --token <token> --reject --verdict "<one line>"`
- `resume <job>`: finish a landing or sync tm stopped for an agent, once a merge claim has handed you its job; refused unless the lease is `<name>`'s and this claim's:
  `tm job resume <job> --agent <name> --token <token>`
- `release <task-id>`: hand the step back naming what it waits on:
  `tm task release <task-id> --agent <name> --token <token> --blocked --depends <other-id>`
- no arguments: show every lease, locked file and job in flight:
  `tm run list --yaml`
