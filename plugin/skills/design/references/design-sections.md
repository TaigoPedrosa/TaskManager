# The design section

The skeleton of a spec's `design`. Every component, contract and check traces to a line of the spec's `source` or to an answered decision, named in brackets after it: `(source: "<quoted line>")` or `(decision-D12: a)`. Anything that traces to neither is cut, not kept for later.

Bounded work writes a few paragraphs under **Approach** and **Test plan**, and one line under each other heading. Architectural work fills every heading.

```markdown
Scale: <bounded | architectural> — <why, in one line>

### Architecture breakdown

<the global view: the components, which service owns what, the data flow across services, and the order the plans build in>

### Assumptions

- <each assumption the design takes, stated plainly> — source: <code (`<file>:<line>`) | data (what was queried) | owner (decision-<id>) | unverified>

### Seams and contracts

Every value that crosses a service or plan boundary, one entry each:

- `<value>`: owner <component>; shape <the exact shape, name, id scheme or signature>. Risks, from the checklist below; write "none" with the reason where one does not apply.
  - tenant scoping;
  - escaping and normalisation ownership;
  - time limits, thread pools and shutdown;
  - rollback and replay;
  - composition-root boot;
  - secrets and personal data in logs;
  - existing callers whose behaviour changes.

Each shared contract becomes either a first task that the others depend on, or a block copied verbatim into every brief that touches it; say which.

### Prior issues

- <a defect, debt or complication already in the code the change touches> (`<file>:<line>`); anything security-relevant already in master is a decision: fix it here, or split it into its own spec

### Premises checked

- <a premise about production behaviour or data a choice rests on>: <what was queried and what it showed>

### Approach

<the chosen approach in two or three sentences, and the decision that chose it> (decision-<id>: <answer>)

### Components and contracts

- `<component or file>`: <what it owns>. Contract: <the shape, name, id scheme or signature others build on> (source: "<quoted line>")

### Data flow

<what enters, what each component does with it, what leaves, in order>

### Failure handling

- <what fails>: <what the code does, and what the caller or owner sees>

### Out of scope

- <what this design does not do> (source: "<quoted line>"), or (decision-<id>: <answer>)

### Test plan

- Seams: <each seam that gets an integration test running the real producer into the real consumer, and which>
- Composition roots: <each root that gets a boot test, and how it starts>
- Flows: <each flow that gets end-to-end coverage>
- Not tested: <what is deliberately left out, and why>

### Code read

- <question the code answered>: <answer> (`<file>:<line>`)

### Decisions

- <decision id>: <question> — <answer, or open>
```
