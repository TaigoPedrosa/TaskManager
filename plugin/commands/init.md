---
description: Start a piece of work from a tracker item, a spec file or your own words, as a tm spec ready for design.
argument-hint: "[<tracker item reference> | <file> | <what the work is about>]"
---

Follow the `init` skill with `$ARGUMENTS` as the source: a reference to a tracker item, a file or
document, or the user's own description of the work. With no arguments, ask the user for the source.

It starts with `tm doctor`, and runs `tm init` with the user first when the project has no estate.
It ends with the spec id, the repos, the landing branch and the open questions verbatim.
