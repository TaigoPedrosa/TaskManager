# Taking in outside work

For the agent turning a request from outside tm (a ticket, an issue, a message) or a defect report against work that already landed into specs, tasks and decisions, so nothing that changes the plan lives only in chat.

tm reads no tracker. Whatever the source is, its words enter the estate as an attachment, and everything tm acts on is a node, a section or a decision.

## 1. Capture the source before reasoning about it

```
tm attach <spec-id> <file> --source <uri> --caption "<what it is>"
```

- Capture the whole source: the body, every comment in its thread, and every linked document that governs the work. A preview or a summary is not the source.
- Count what you received against what the source says it holds (comments, attachments, linked items). A mismatch means part of it is missing: fetch the rest before going on.
- When an outside source changes, re-capture it with `tm attach <spec-id> <file> --source <uri> --replace <asset>`: tm cannot tell when a ticket or URL changed. `tm attachments <spec-id> --check` reports stale copies only for sources that are files inside the project.
- Attach to the spec the work belongs under. For new work, import that spec first, holding only its title, and attach to it.

## 2. A request becomes a spec, a plan and decisions

- Record only what the source states. Anything it does not state is written down as "not stated", never filled in. Intake invents no contract: no field, limit, format or behaviour the source does not give.
- A later comment outranks the body. Keep both, and say which one governs and why.
- Split the open questions in two:
  - **The code answers it.** Read the code and answer it, citing `file:line`. Those answers go in the plan's `context`, where every task's brief reads them.
  - **Only a person can answer it.** Raise each one as `tm decision add "<question>" --context-file <path> --option "a|Do X|why" --option "b|Do Y|why" --recommend a --blocks <spec-id>`, the code-side facts that bear on it in its context. One question per decision, all raised at once.
- What the work excludes goes in the plan's `context` too: a spec's sections never reach a task's brief.
- Then write the plan as `tm guide plan` describes, and import it with `tm import --format yaml -f <file>`.

## 3. A defect against completed work

A node that is `COMPLETED` has landed. Never reset it, and never reopen it: the defect is new work.

1. **Attach the evidence first.** Screenshots, logs and links to a running system can expire. `tm attach <node-id> <file> --source <uri>` onto the completed node, before anything else.
2. **Read the code that ran.** Find the build or commit the reporter actually used and read the code there, not on the branch or the target's tip. `tm section get <node-id>:merge` names the node's landing merge.
3. **Treat the reporter's attribution as a hypothesis.** Who or what the report blames is a lead to check, not a finding.
4. **Group the points.** Points that name the same ids, screens or records are one defect until the code says otherwise.
5. **Triage every point into exactly one class:**

| Class | What tm gets |
|:--|:--|
| this work's defect | a new task whose `context` names the completed node and its landing merge, with a test that fails before the fix |
| another node's defect | a task under that node's spec, or a decision when its owner must rule |
| pre-existing, not caused by any landed node | its own node, independent of the report |
| by design | a decision quoting the ruling it follows; no code |
| environment or data | a line in the intake report; no node |
| cannot reproduce | a line naming what is missing to reproduce it; no node until it arrives |

6. **Scope nobody tested is not a pass.** A caveat that some path, platform or data set was never exercised becomes a node that exercises it, or a condition the landing waits on.

Every point ends as a node, a decision, or a line in the report. None ends as a chat message.

## 4. State what was read

- A claim that something is absent names the search that would have found it: the command, the path and pattern, and the range it covered.
- A fact you read is marked as read; a fact you derived is not a finding until you read it.

## Never

- Never act on a request or a report that is not attached to a node.
- Never fill a gap in the source with a guess; a gap is "not stated" or a decision.
- Never reset or reopen a `COMPLETED` node to fix what it shipped.
- Never leave a question that holds work in chat; it is `tm decision add`.
