---
name: taskmanager
description: Use when about to claim, build, review, fix, merge or hand off a task, when reading a task's brief or status, or when anyone asks to use TaskManager or the `tm` CLI. Run `tm guide <role>` before the first command.
---

# TaskManager

`tm` is a local CLI over a SQLite task graph: it holds the specs, plans and tasks, claims each step of one with a lease and tells you which step it is, cuts the worktree the work happens in, lands the branch on its parent's branch or on the branch its spec targets, and verifies it there. It is the only record of what is planned, claimed, built and finished, and the only thing that writes it.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read your role's guide first

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| a step `tm task start` printed as `action: implement` | `tm guide implement` |
| a step printed as `action: review` | `tm guide review` |
| a step printed as `action: fix` | `tm guide fix` |
| a landing or a sync tm stopped for an agent | `tm guide merge` |
| anything else, or first contact with `tm` | `tm guide overview` |

Run it before your first `tm` command, not after: it names the flags, what each refusal means, and the verb that closes your step. A project's own conventions are appended to the same output, so the guide you read is the one that applies here.
