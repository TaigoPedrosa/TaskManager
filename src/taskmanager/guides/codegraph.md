# codegraph

`tm guide` appends a role's block below to that role's guide only when the `codegraph` CLI is on PATH and the repository has a codegraph index.

## implement

The repository has a codegraph index, and the claim gave your worktree its own. The claim prints a `codegraph` list:

```yaml
codegraph:
- ready <dir>/backend-<task-id>
- <symbol> reaches <file> held by <other-id>
```

`ready` says your worktree holds its own copy of the index, and `<symbol> reaches <file> held by <other-id>` says a symbol in one of your declared files has direct dependents in a file the live lease of `<other-id>` holds, so changing what that symbol takes or returns changes code that node is working on. A failure prints `unavailable (<reason>)` instead, and no line ever blocks the claim. When the workflow claimed for you, its prompt carries the same lines, each as `codegraph: <line>`, under its `Brief:` line.

Ask the index before a grep-and-read loop:

- `codegraph explore -p <worktree> --max-files 5 "<what the task changes>"` — the relevant symbols' source and the call paths between them, in one call.
- `codegraph node -p <worktree> <symbol>` — one symbol's source with its callers and callees; `codegraph node -p <worktree> -f <file> --symbols-only` lists a file's symbols and the files that depend on it.
- `codegraph callers -p <worktree> --limit 50 <symbol>` — every caller of a symbol you are about to change.
- `codegraph impact -p <worktree> --depth 2 <symbol>` — everything a change to the symbol reaches. A method is `Class.method`.

The index answers for the tree as of its last sync: after editing, run `codegraph sync --quiet <worktree>` before the next query. `codegraph affected -p <worktree> --filter '<test-glob>' --quiet <changed-file>...` lists the test files that depend on the changed files, the ones to run first; `--filter` names the project's test files, which codegraph's default pattern can miss. The whole test gate still runs before the step closes.

## fix

The repository has a codegraph index, and the claim gave your worktree its own. Before changing what a finding names:

- `codegraph node -p <worktree> <symbol>` — the symbol's source with its callers and callees.
- `codegraph callers -p <worktree> --limit 50 <symbol>` — every caller the fix has to keep working.
- `codegraph impact -p <worktree> --depth 2 <symbol>` — everything the fix reaches. A method is `Class.method`.
- `codegraph explore -p <worktree> --max-files 5 "<what the finding describes>"` — when the finding names a behaviour rather than a symbol.

After editing, run `codegraph sync --quiet <worktree>` before the next query. `codegraph affected -p <worktree> --filter '<test-glob>' --quiet <changed-file>...` lists the test files that depend on the changed files, the ones to run first; the whole test gate still runs before the step closes.

## review

The repository has a codegraph index of `<repo>`'s checkout, not of the branch under review: a symbol the branch adds has no entry, and a symbol it changes reads as the checkout has it. Run `codegraph sync --quiet <repo>` first, then for each symbol the diff changes or removes:

- `codegraph impact -p <repo> --depth 2 <symbol>` — every dependent of the symbol. A dependent the branch neither changed nor covers with a test is a finding. A method is `Class.method`.
- `codegraph callers -p <repo> --limit 50 <symbol>` — the direct callers alone, when the impact list is too long to read.

`codegraph affected -p <repo> --filter '<test-glob>' --quiet <changed-file>...` lists the test files that depend on the changed files: the tests to run against the branch.

## plan

The repository has a codegraph index of `<repo>`'s checkout. Run `codegraph sync --quiet <repo>` first, then author each task's `declared_files` from it:

- `codegraph explore -p <repo> --max-files 10 "<the change>"` — where the code the change touches lives.
- `codegraph impact -p <repo> --depth 2 <symbol>` for each symbol a task changes — a dependent the change forces to change puts its file in that task's `declared_files`, or in the `declared_files` of a task it depends on. A method is `Class.method`.
- `codegraph node -p <repo> -f <file> --symbols-only` — a file's symbols and the files that depend on it.
