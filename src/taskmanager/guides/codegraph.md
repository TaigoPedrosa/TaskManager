# codegraph

How to ask the codegraph CLI about code before grepping and reading it, which `tm guide <role>` appends to a role's guide, as the `general` section below and that role's own, whenever the `codegraph` CLI is on PATH, indexed or not.

## general

Ask codegraph, from its CLI only, before a grep-and-read loop, then read only the files it points to.

- `codegraph status <path>` tells whether `<path>` has an index: `Not initialized` means it has none. Run `codegraph init -y <path>` and wait for it to finish before the first query; it takes about 20 seconds on a 1,000-file service. Nothing is left running afterwards.
- `codegraph explore -p <path> --max-files 5 "<the question>"` is the first call for any question about the code. It prints the relevant symbols' verbatim source, the call paths between them with the condition each call runs under, and what depends on them. It follows a page's fetch to the route that serves it.
- `codegraph query "<name>" -p <path> --kind function --limit 10` searches symbols by name.
- Before changing a symbol: `codegraph callers -p <path> --limit 50 <symbol>` lists every caller to keep working, and `codegraph impact -p <path> --depth 2 <symbol>` everything the change reaches. A method is `Class.method`. `codegraph node -p <path> <symbol>` prints one symbol's source with its callers and callees; `codegraph node -p <path> -f <file> --symbols-only` lists a file's symbols and the files that depend on it.
- Tests to run first: `git -C <path> diff --name-only <base>...HEAD | codegraph affected --stdin --quiet -p <path> --filter '<test-glob>'`, where `--filter` names the project's test files, which codegraph's default pattern can miss. The whole test gate still runs before the step closes.

Nothing watches the files: the index answers for the tree as of its last sync. After editing, or after the tree moved under you (a pull, a checkout), run `codegraph sync --quiet <path>` before the next query. `codegraph status <path>` prints `Pending Changes: Modified: N files` when files changed since, with the count and not their names. A stale `callers`, `impact` or `node` says nothing: a symbol you added reads `not found`. A stale `explore` prints the files' current source under `changed since last index sync` and `Changed on disk after the last index sync`, but its symbol list and call paths may be outdated, so sync and ask again.

Never run `codegraph unlock`. A lock means another process is indexing; wait for it.

## implement

The claim prints a `codegraph` list when the CLI is on PATH:

```yaml
codegraph:
- ready <dir>/backend-<task-id>
- <symbol> reaches <file> held by <other-id>
```

`ready <path>` says your worktree holds its own copy of the index, built from the repository's index (the claim ran `codegraph init -y` on the repository first when it had none). Use `<path>` as `-p`. `<symbol> reaches <file> held by <other-id>` says a symbol in one of your declared files has direct dependents in a file the live lease of `<other-id>` holds, so changing what that symbol takes or returns changes code that node is working on. A failure prints `unavailable (<reason>)` instead, and no line ever blocks the claim: run `codegraph status <worktree>`, and `codegraph init -y <worktree>` when it has no index. When the workflow claimed for you, its prompt carries the same lines, each as `codegraph: <line>`, under its `Brief:` line.

Start with `explore` on what the task changes. Before changing a symbol, run `callers` and `impact` on it, then edit, sync, and run the affected tests first.

## fix

The claim prints the same `codegraph` list as an implement claim: `ready <worktree>` is the index to ask, and `unavailable (<reason>)` sends you to `codegraph status <worktree>` and, when it has no index, `codegraph init -y <worktree>`.

Before changing what a finding names, run `callers` and `impact` on its symbol, so the fix keeps every caller working. Use `explore` when the finding names a behaviour rather than a symbol. After editing, sync and run the affected tests first.

## review

The claim prints `codegraph: ready <path>`: `<path>` is an index of the commit under review, exported from the repository, not of its checkout, so a symbol the branch adds has an entry and a symbol it removes has none. It never changes, so there is nothing to sync. Use it as `-p`. `unavailable (<reason>)` means the claim could not build one: read the diff and the files directly, and do not index the checkout, which is at another commit.

For each symbol the diff changes or removes, run `codegraph impact -p <path> --depth 2 <symbol>`, and `callers` when the list is too long to read. A dependent of a changed symbol that the branch neither changed nor covers with a test is a finding. `affected` on the changed files lists the tests to run against the branch.

## plan

The index is of `<repo>`'s checkout: run `codegraph sync --quiet <repo>` first, after `codegraph init -y <repo>` when `codegraph status <repo>` reports no index. Then author each task's `declared_files` from it:

- `codegraph explore -p <repo> --max-files 10 "<the change>"` shows where the code the change touches lives.
- `codegraph impact -p <repo> --depth 2 <symbol>` for each symbol a task changes: a dependent the change forces to change puts its file in that task's `declared_files`, or in the `declared_files` of a task it depends on.
- `codegraph node -p <repo> -f <file> --symbols-only` lists a file's symbols and the files that depend on it.
