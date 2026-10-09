---
name: plan
description: Use when a spec's design is approved and its work must become plans and tasks in tm, sliced, imported with `tm import` and each proved by a verification that fails before the change. Not for designing; that is `/taskmanager:design`, and nothing here runs before its approval.
---

# Planning an approved design

This turns a spec's approved `design` section into plans and tasks inside tm: one import document, checked, imported, and proved, so `tm wave discover` can hand each task to an agent that needs to ask nobody anything.

`tm guide plan` is the authority on the document: its fields, sections, flags, verifications and sizing. This skill says when to read which part of it and what to check before the import; where the two seem to differ, the guide wins.

Two hard gates: no plan before the design is approved, and no dispatch before the plan is imported and its verifications are proved to fail. The import document is a scratch file outside the repo and is never committed: the spec, its plans and their sections are the plan's home, and there is no plan file anywhere else.

## 1. Gate

Take the spec id from the call. Find its approval decision, the "approve the design" decision `/taskmanager:design` raised blocking the spec, asked as "Approve the design of <spec-id>?" with the options `approve` and `change`: every decision blocking the spec is listed with its state under `depends_on` in `tm task get`, and `tm decision list` gives each one's question.

```
tm task get <spec-id> --yaml
tm decision list --yaml
tm decision get <decision-id> --yaml
```

A `change` answer makes the design skill raise the question again, so the spec can hold several; the gate is the one raised last, the highest `decision-D<n>` among them, and the earlier ones are history.

Go on only when that decision is `ANSWERED` and its `answer.option` is `approve`. Otherwise stop and name it: the decision id, its question, and its state (open, answered with another option, withdrawn, or not raised at all, in which case the next stage is `/taskmanager:design`). An answer that asks for changes sends the work back to design; nothing here edits the `design` section.

Also stop while any other decision blocking the spec is open: list each one with its id.

## 2. Read

Read all of it before writing a line:

- `tm section get <spec-id>`: the `design` section first, then `source` and `context`;
- `tm decision list` and `tm decision get` for every decision the spec raised: an answer is a ruling the plan carries;
- `tm task get <spec-id> --yaml`: `land_order` names the repos, `lands_on` the branch every node lands on;
- `tm guide plan`, in full.

Cite the guide's sections in what follows; never restate a rule from it into a section, where it would drift from the guide.

## 3. Slice

Slice the design into plans and tasks, and write them as one import document in a scratch path outside the repo:

```
doc="$(mktemp -d)/plan.yaml"
```

The document opens with `spec:` holding only `id: <spec-id>`: a key the document omits keeps the spec's current value, and plans in a document with no `spec` land under no spec at all.

- A **plan** is a slice that ships on its own: once its tasks land, something the design promised works. Pick its landing shape from `tm guide plan` §4: each task reviewed and landed alone, or one review for the whole plan.
- A **task** is one agent's unit, sized by `tm guide plan` §7: an objective of one sentence, a handful of acceptance lines, a `declared_files` list no sibling wants.
- Every task traces to a line of the `design` or an answered decision. One that traces to neither is out (YAGNI); one the design names for later is its own node with `depends_on` naming the node it waits for, never a sentence in a section (`tm guide plan` §2).
- Waits are ids in `depends_on`, decisions, or conditions with a command (`tm guide plan` §5), never prose.
- Section keys and their order, and what goes on the plan rather than the task, follow `tm guide plan` §2. A contract one task writes and another reads, and a ruling several tasks share, go in the plan's `overview`, since only the parent's `context` and `overview` reach a task's brief.
- Acceptance follows `tm guide plan` §8: one check a reviewer can run per line.
- Verifications follow `tm guide plan` §6, with `${TM_VERIFY_REF:-origin/main}` in every `test_command` rather than a branch name.

## 4. Check the document

Before importing, go through `references/plan-checklist.md` item by item against the document, and fix each miss in the document. Each item names the `tm guide plan` section it comes from; read that section when an item is unclear, never guess.

## 5. Import

```
tm import --format yaml --file "$doc"
```

The import is all or nothing. On a refusal, nothing was written: fix the document where the message points and import it again. Re-importing the same document before any work starts is safe (`tm guide plan` §10); once a task is claimed, amend it in place instead.

## 6. Prove

Every task's verifications must fail before its change. For each task, read its landing target and run its checks against it:

```
tm task get <task-id> --yaml        # lands_on
tm verify run <task-id> --ref origin/<lands_on>
```

Each run must exit 1 with at least one of the task's checks `FAILED`, and none failing for a reason other than the missing deliverable (a typo in a path, a command that cannot run). A verification that passes on the target proves nothing: rewrite it so it checks this task's own deliverable, put it back with `tm verify remove` and `tm verify add` (or fix the document and re-import while nothing is claimed), and run it again. `No verifications to run.` is a miss from step 4: add one. Do the same for each plan's joined verification.

## 7. Self-review

Read the imported tree as the agents will:

```
tm render <plan-id> --view full
tm render <task-id> --view subagent
```

Check it against the `design`, line by line: every component, contract, failure path and verification step the design names has a task that builds it and an acceptance line that checks it; no task builds what the design leaves out; no brief is missing a section you meant to write; no two tasks that can run together share a declared file. Fix each gap in place (`tm guide plan` §10) and prove any changed verification again as in step 6.

## 8. Hand off

Show the spec is dispatchable:

```
tm wave discover --session <session-id> --spec <spec-id> --lines
```

It must offer the first task or tasks. When it offers nothing, read what it prints as held or waiting and fix the cause before handing off.

Report, in this order:

- the spec id and its landing branch;
- each plan with its landing shape, and its tasks with their `depends_on`;
- each task's verification and the `tm verify run` result that proved it fails;
- what the design leaves for later, each as its own node with the `depends_on` it waits on;
- the next stage: dispatch, with `tm wave discover` naming what starts first.

Never write the plan as a markdown file in the repo, and never export the estate into the repo to keep one: the spec and its nodes are the plan.
