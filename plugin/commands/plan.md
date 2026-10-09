---
description: Turn a spec's approved design into plans and tasks inside tm, each proved by a verification that fails before the work.
argument-hint: "[<spec-id>]"
---

Follow the `plan` skill with `$ARGUMENTS` as the spec id. With no arguments, take the spec
`/taskmanager:design` last handed off, and ask the user when there is none.

It starts with `tm decision list`, and refuses while the spec's "approve the design" decision is
open or answered with anything but approve, naming that decision.
It ends with the spec ready for `tm wave discover`: the plans, the tasks, their dependencies, and
each task's verification failing on its landing target.
