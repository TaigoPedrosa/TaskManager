---
name: taskmanager
description: Use when about to claim, build, review, fix, merge or hand off a task, when reading a task's brief or status, or when anyone asks to use TaskManager or the `tm` CLI. Run `tm guide <role>` before the first command.
---

# TaskManager

`tm` is a local CLI over a SQLite task graph: it holds the specs, plans and tasks, claims a task with a lease, cuts the worktree and branch the work happens in, runs the task's verifications, and moves it through the lifecycle. It is the only record of what is planned, claimed, built and finished, and the only thing that writes it.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read your role's guide first

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| building a `READY` task | `tm guide implement` |
| reviewing a `WAITING_REVIEW` task | `tm guide review` |
| closing findings on a `WAITING_FIXES` task | `tm guide fix` |
| landing a `WAITING_MERGE` task | `tm guide merge` |
| anything else, or first contact with `tm` | `tm guide overview` |

Run it before your first `tm` command, not after: it names the flags, what each refusal means, and the handoff you owe. A project's own conventions are appended to the same output, so the guide you read is the one that applies here.
