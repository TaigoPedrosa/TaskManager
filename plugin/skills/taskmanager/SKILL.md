---
name: taskmanager
description: Use when about to claim, build, review, fix, merge or hand off a task, when reading a task's brief or status, or when anyone asks to use TaskManager or the `tm` CLI. Run `tm guide <role>` before the first command.
---

# TaskManager

`tm` is a local CLI over a SQLite task graph: it holds the specs, plans and tasks, claims each step of one with a lease and tells you which step it is, cuts the worktree the work happens in, lands the branch on its parent's branch or on the branch its spec targets, and verifies it there. It is the only record of what is planned, claimed, built and finished, and the only thing that writes it.

`tm` installs separately from this plugin: `uv tool install git+https://github.com/TaigoPedrosa/TaskManager@v<this plugin's version>`. When `tm` is not on PATH, say so and stop; run nothing in its place.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## The flow

A piece of work goes through four steps, each ending where the next begins:

1. `tm init` prepares the project: the estate, the repositories and their gates.
2. `/taskmanager:init` starts a piece of work, turning its source into a spec: what was asked, the repos, the landing branch, and the open questions as decisions.
3. `/taskmanager:design` settles those decisions and writes the design into the spec, ending on an approval decision.
4. `/taskmanager:plan` turns the approved design into plans and tasks with `tm import`, each proved by a verification that fails before the work.

Then dispatch: the `dispatcher` skill reads `tm guide dispatch` and claims each step.

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
| starting a piece of work from a tracker item, a file or the user's words | `/taskmanager:init` |
| designing a spec `/taskmanager:init` recorded | `/taskmanager:design` |
| turning an approved design into plans and tasks | `/taskmanager:plan` |
| anything else, or first contact with `tm` | `tm doctor`, then `tm guide overview` |

Run it before your first `tm` command, not after: it names the flags, what each refusal means, and the verb that closes your step. A project's own conventions are appended to the same output, so the guide you read is the one that applies here.
