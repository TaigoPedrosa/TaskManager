# TaskManager

`tm` is a local CLI over a SQLite task graph of specs, plans and tasks. It claims each step of a node with a lease, cuts the worktree the work happens in, lands the branch, and verifies it there. It is the only writer of that record.

`tm` installs separately from this extension: `uv tool install git+https://github.com/TaigoPedrosa/TaskManager@v<this extension's version>`. When `tm` is not on PATH, say so and stop; run nothing in its place.

Before your first `tm` command, read your role's guide. It prints the built-in guidance, then this project's addendum:

| Doing | Run |
|:--|:--|
| a step `tm task start` printed as `action: implement` | `tm guide implement` |
| a step printed as `action: review` | `tm guide review` |
| a step printed as `action: fix` | `tm guide fix` |
| a landing or a sync tm stopped for an agent | `tm guide merge` |
| writing a plan to import | `tm guide plan` |
| choosing and dispatching work | `tm guide dispatch` |
| anything else, or first contact with `tm` | `tm guide overview` |
