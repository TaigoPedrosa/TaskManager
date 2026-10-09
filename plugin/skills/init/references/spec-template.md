# The context section

The skeleton of a spec's `context`. Every line states what the source says, with the source line it comes from; where the source says nothing, the line reads "not stated in the source". Delete nothing from the skeleton: an empty heading is a silence the next stage has to see.

```markdown
### What

<what the source asks for, in its own terms> (source: "<quoted line>")

### Why

<the reason the source gives> (source: "<quoted line>"), or: not stated in the source

### Scope

In:
- <what the work covers> (source: "<quoted line>")

Out:
- <what the source excludes> (source: "<quoted line>"), or: not stated in the source

### Contracts

<each field, format, limit, endpoint or behaviour the source fixes, as it states it> (source: "<quoted line>"), or: not stated in the source

### Constraints

<deadlines, environments, dependencies, rulings the source names> (source: "<quoted line>"), or: not stated in the source

### Answered from the code

- <question>: <answer> (`<file>:<line>`)

### Open questions

- <question, verbatim> — <decision id>

### Repos

- `<repo>`: <why it is involved>; validated: work tree at `<root>/<repo>`, origin `<url>`
```
