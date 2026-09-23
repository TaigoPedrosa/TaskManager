# CLI notes from an owner-decision ruling session (2026-09-22)

Working through 112+ owner-decision tasks interactively with the owner, one ruling at a
time. Concrete friction, not a design — for whoever's picking up CLI ergonomics next.

## Ruling a decision is two calls that can partially fail

`tm section set <id>:ruling -f <file>` then `tm run stop <id> --status COMPLETED` — if the
first succeeds and the second fails (or the session dies in between), the task sits with a
ruling written but still `NOT_STARTED`. Never hit this in practice this session, but nothing
stops it. A single call that writes the section and moves the status atomically would remove
the gap. (If `tm decision answer` lands as its own first-class op, this is probably moot —
haven't looked at what else is planned there, just noting the pain from where I sit.)

## No way to hand the owner a clean evidence pointer

Every ruling round this session, the owner wanted a file path they could click open in
VS Code. That's fine for a static PNG someone captured once, but nothing in `tm` associates
"this evidence came from live source X at time T" with the node — so when a sibling agent
edits the Figma frame the evidence was captured from, the cached PNG goes stale with no
signal. I hit this for real: `align/evidence/92-2/figma.png` still showed a removed category
an hour after another agent removed it live in Figma, and I only caught it because the owner
said "consider this is being edited" — nothing in the tool told me. Some notion of "this
attachment's source, and whether it's been re-checked since" would have caught it
automatically instead of by luck.

## Rendering N specific tasks means N calls

No `tm render <id1> <id2> ... --view subagent` and no `tm task list --plan X --status
NOT_STARTED --render subagent` — looping single `tm render <id>` calls per task is what I did
whenever I needed a batch of full briefs (I did discover `tm render <id> -r` for "one node
plus its whole subtree" partway through, which helped, but a specific-id-list batch is a
different shape).

## `tm next` was the right tool the whole time, I just didn't reach for it early

Not a gap — a note on discoverability. I spent a while re-deriving "which owner-decision
tasks are still open" with my own scratch-file diffing against `tm task list --yaml`, when
`tm next --model owner -n N --yaml` already returns exactly that, scored and ready. Once
pointed at it, chaining `tm next` -> `tm render <id>` was strictly better than anything I'd
built by hand. If there's a natural spot to say this louder (the `owner` guide addendum, or
`tm next --help`'s own text), it would have saved real time.
