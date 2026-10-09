---
description: Turn a tm spec's open questions into a design written into the spec, ending on an approval decision.
argument-hint: "[<spec-id>]"
---

Follow the `design` skill with `$ARGUMENTS` as the spec id. With no arguments, take the spec
`/taskmanager:init` just reported; with none reported, run `tm spec list`, print it, say the
design runs as `/taskmanager:design <spec-id>`, and stop.

It starts with `tm decision list` and refuses while a decision blocking the spec is open.
It ends with the spec's `design` section written and one open "approve the design" decision
blocking the spec; no plan is written before that decision is answered approve.
