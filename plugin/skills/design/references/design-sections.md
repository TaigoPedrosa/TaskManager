# The design section

The skeleton of a spec's `design`. Every component, contract and check traces to a line of the spec's `source` or to an answered decision, named in brackets after it: `(source: "<quoted line>")` or `(decision-D12: a)`. Anything that traces to neither is cut, not kept for later.

Bounded work writes a few paragraphs under **Approach** and **Verification**, and one line under each other heading. Architectural work fills every heading.

```markdown
Scale: <bounded | architectural> — <why, in one line>

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

### Verification

- <the check that proves each component, as a command or an observable result, and what it fails on without the change>

### Code read

- <question the code answered>: <answer> (`<file>:<line>`)

### Decisions

- <decision id>: <question> — <answer, or open>
```
