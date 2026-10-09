---
name: design
description: Use when a tm spec holds a source and open questions and needs a design before any plan, to settle each question as a decision, write the design into the spec's sections and end on an approval decision. Not for writing the plan; that follows the approval.
---

# Designing a spec

This turns a spec that `/taskmanager:init` recorded into an approved design: every question it left open answered, one approach chosen, and the design written into the spec itself. There is no separate design file: the spec's `design` section is the design's home, and the decisions that shaped it stay on the estate.

Hard gate: no plan is written, imported or dispatched before the "approve the design" decision is answered approve. This skill ends at that decision.

Every question for the owner is a decision blocking the spec, raised with `tm decision add` and read back from `tm decision list` and `tm decision get`. A question asked only in the conversation leaves no record and holds nothing: show the owner each decision's question and context, and take the answer from tm, never from the conversation.

YAGNI: every component, contract and check in the design traces to a line of the spec's `source` or to an answered decision. Anything that traces to neither is cut.

## 1. Start

Take the spec id from the call, or the spec `/taskmanager:init` just reported. Read what the design builds on:

```
tm section get <spec-id>:source
tm section get <spec-id>:context
tm section get <spec-id>:design      # present only on a re-run
tm task get <spec-id> --yaml         # awaiting_decisions: the open decisions holding the spec
tm decision list --status open
tm guide plan                        # §1-§2: what a plan will need from this design
```

When `awaiting_decisions` lists any decision, stop: list each one with its question (`tm decision get <id>`) and tell the owner the design resumes once they are answered. Never design around an open question.

On a re-run, each decision this skill raised earlier is named in the `design` section's **Decisions** list. Read each answer with `tm decision get <id>` and pick up at the first step whose work is not recorded yet.

## 2. Scale

Classify the work, say which and why in one line, and let the owner overrule it:

- **Bounded**: a change to a flow that already exists in the code.
- **Architectural**: a new subsystem, or a change to an interface others depend on.

When in doubt, take architectural. Record the scale as the first line of the `design` section (`Scale: bounded` or `Scale: architectural`, with its reason), written with `tm section set` as in step 6.

## 3. Explore

Read the code the work touches before asking anything the code can answer. When `tm doctor` prints a `codegraph index (<repo>)` line that is not `missing`, ask the index first (`codegraph explore -p <repo> "<what the source asks for>"`, `codegraph query`) and read only the files it points to; otherwise search and read the files directly.

Record every answer the code gives, with its `file:line`, under **Code read** in the `design` section. A question the code answers is never raised as a decision.

## 4. Ask

Raise what only the owner can answer, one question at a time, each a decision blocking the spec, with options and a recommendation:

```
tm decision add "<question>" --context-file <path> --option "a|Do X|why" --option "b|Do Y|why" --recommend a --blocks <spec-id>
```

The context file says what the question turns on: the source line or the `file:line` that raised it, and what each option changes in the design. Add the decision id to the **Decisions** list in the `design` section, show the owner the question with that context, and stop: the next question waits for this answer, because an answer can change or remove the questions after it.

Read the answer back once `tm decision list --status open` no longer lists the decision: `tm decision get <id>` prints it. Then raise the next question, until none is left.

## 5. Approaches

For architectural work, set out two or three approaches, each with what it builds, what it costs and what it rules out, and recommend one. Raise them as one decision blocking the spec, one option per approach, in the same way as step 4, and wait for its answer the same way. Bounded work skips this step: it extends the flow that exists.

## 6. Write the design

Write the whole `design` section from a file, following `references/design-sections.md`:

```
tm section set <spec-id>:design --file <design-file> --header "## Design"
```

It holds the chosen approach, the components and their contracts, the data flow, failure handling, what is out of scope, and how each part will be verified. Bounded work gets a few paragraphs under the approach and verification and a line under each other heading; architectural work fills every heading. Keep the scale line, **Code read** and **Decisions** from the earlier steps. Each run rewrites the section whole, in the present tense.

## 7. Self-review

Read the written section back with `tm section get <spec-id>:design` and fix it in place for:

- **Placeholders**: an unfilled `<...>`, "TBD", "to be decided", or a heading with nothing under it.
- **Contradictions**: two parts that disagree, or a part that disagrees with an answered decision or the source.
- **Ambiguity**: a contract a dependent could read two ways; name the shape, field or id scheme exactly.
- **Scope**: a component that traces to no source line and no answered decision is cut; a source line no part covers is either covered or listed under out of scope with the decision that put it there.

A gap only the owner can close goes back to step 4 as a decision, never a guess.

## 8. Gate

Raise one decision to approve the design, blocking the spec:

```
tm decision add "Approve the design of <spec-id>?" --context-file <summary> --option "approve|Approve|the plan is written against this design" --option "change|Change it|name what to change; the design is revised and raised again" --recommend approve --blocks <spec-id>
```

The summary names the scale, the chosen approach and each component in one line. Report the decision id, and stop. No plan is written before this decision is answered approve; once it is, `/taskmanager:plan` writes the plan as `tm guide plan` describes. An answer of change goes back to step 6 with what it names, and the gate is raised again.
