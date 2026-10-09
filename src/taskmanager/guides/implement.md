# Implementing a task

For the agent that builds a task `tm task start` claimed for `implement`, and closes the step with the work committed on the task's own branch.

## 1. The claim

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to step 2, and never run `tm task start` yourself. On your own, claim it:

```
tm task start <task-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

```yaml
action: implement
model: sonnet
repos: [backend]
branch: tm/<task-id>
base: main
bases: {backend: main}
worktree: <dir>/backend-<task-id>
worktrees: {backend: <dir>/backend-<task-id>}
token: <token>
```

`model` is the family the step runs on; `<name>` is the agent the lease is held under and `<token>` names this one claim, and every verb that closes or releases the step passes them back with `--agent <name> --token <token>`. The token matters because an agent name can repeat: a later claim of the same node under the same name gets a new token, and tm refuses the old one. When the workflow claimed for you, its prompt carries both flags. The claim locks every path the task declares until the step closes. The branch is cut from `base` in the task's own `target_repo`, with no upstream: `base` is the branch the task's spec lands on, read as `origin/<base>`, or for a task with `merge: parent` the container branch it lands on; a spec's target that origin does not have yet reads as the repository's default branch, which the branch is cut from, until the first landing on it creates it. `bases` names the same branch by repository. A task with no `target_repo` is refused. Any other `action` is another role's step: read that role's guide instead.

`action: blocked` exits 3 and writes nothing. Its `reason` names what the task waits on — an edge, a decision, a condition, a sync, or a lease holding one of its files. Report it and start nothing.

## 2. Read the brief

```
tm render <task-id> --view subagent
```

That is the whole assignment: the task's frontmatter, its parent's context, its sections and its verifications. Then, only as needed:

- `tm task get <task-id> --yaml` — status, `next_action`, flags, `target_repo`, `depends_on`, `declared_files`, verifications, conditions, lease.
- `tm section get <task-id>:<key>` — one section; `tm section get <task-id>` prints them all. Content only goes to stdout, the header to stderr, so `tm section get id:key > f` then `tm section set id:key -f f` round-trips without folding the header back into the content.

A task that was reopened carries a `:reopen` note and the earlier `:review`, and its branch still holds the earlier work: read both, and decide what to keep.

An instruction about a third-party artifact's structure (which layers a style holds, the order an API returns) is checked against the artifact itself before you follow it. When the artifact contradicts the brief, release blocked on a decision (below) instead of following the brief.

## 3. Work in that worktree and nowhere else

Every read, edit, command and commit happens under the printed worktree path. Check the prefix of each path you edit, not just its basename: the same file exists in the project's own checkout. Commit on the branch with an explicit pathspec. Do not merge and do not push: tm lands the branch. You may rewrite your own branch until its first review, never after.

Create, modify or delete nothing outside `declared_files`. Before touching another file, read the locks in `tm run list --yaml`. When another live node holds the file, release blocked and name that node (below). Otherwise add the file with `tm task update <task-id> --set declared_files='[...]'` before the edit, and name it in the report.

## 4. Keep the lease alive

```
tm task heartbeat <task-id>
```

An implement lease lasts `lease_ttl.implement` seconds (three hours by default). A refused heartbeat means the lease is gone and the step was swept back to `READY`: stop editing and report.

## 5. Verify

```
tm verify run <task-id> --ref tm/<task-id>
```

A table of the task's checks against your branch, exit 1 if any failed. What each type asserts:

- `file_exists` / `file_absent` — the path is there, or is not.
- `symbol_signature` — a `def`, `async def` or `class` of that name parses in that file.
- `ast_export` — that name is in the file's `__all__`, or is a public top-level `def`/`class`.
- `test_command` — the command runs in a shell from the project root; exit 0 passes.
- `codegraph_query` — `codegraph query --json` for the search in `target_path`, run over the ref's tree, matches the regex in `expected_pattern`.

The path checks and codegraph queries read the ref, never your worktree, so without `--ref` they read `origin/<the branch the task's spec lands on>` and stay red until the task's code reaches it. A `test_command` reads the same ref from `TM_VERIFY_REF`, which every run exports: the `--ref` given, that landing branch without one, and the landing target when tm lands the task. So write `"${TM_VERIFY_REF:-origin/main}"` into the command rather than a branch name. `No verifications to run.` exits 2: a task with no checks has not passed anything, and that is worth a line in your report.

- A generated file (a built stylesheet, a lockfile, a schema dump) is regenerated, never edited or hand-merged: a branch that changes any of its inputs rebuilds it before closing, and a conflict on it is resolved by rebuilding it on the merged tree.
- A test selects only markup that its own task's declared files render. It reaches another file's control by what that control shows the user (role, accessible name), never by its classes or inner elements.
- After removing something, search for every file, symbol, field, and computation or load whose last reader was the removed code, and delete each one in the same step, a payload field's producer included. Name them in the report.
- When the task writes or reads a format another task also writes or reads (an id scheme, a URL key, a payload), run the round trip across both ends before closing the step: write it with one end, read it back with the other, get back the same thing, and put that run in the report. A mismatch with the other end is a decision (below), never a local reinterpretation.

## Waiting on something that takes time

A gate, a build, an external state change — pick by duration, because duration is what you actually know:

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: the tool's own blocking `wait` where one exists, else `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | it is a condition, not a wait: release the step naming it (below) |

Never end your turn to wait on a background run "until notified." A background command's completion notification reaches you only while you are still working — ending your turn is what loses it, and nothing resumes you afterward.

## Something outside this step must happen first

Release the step and name what it waits on, in one call; the task returns to `READY` with its branch and worktree intact, and becomes claimable again the moment the named thing clears:

- Another node must land first: `tm task release <task-id> --agent <name> --token <token> --blocked --depends <other-id>`.
- Only the owner can answer: `tm task release <task-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --option "b|Do Y|why" --recommend a`. Name the options you considered and the one you recommend. A blocker that turns on "the brief doesn't say" or "which of these is correct" is that same call, raised at once — never a guess spent on a review cycle.
- A state outside the corpus: `tm task release <task-id> --agent <name> --token <token> --blocked --needs "<what must hold>" --command "<a command that exits 0 once it holds>"`.

`--blocked` with nothing named is refused. `tm task release <task-id> --agent <name> --token <token>` alone is a failed step, counted towards `FAILED`: use it only when you cannot go on and nothing names why, and say why in the report. A release or close refused for `--agent` or `--token` means the step is no longer yours: stop and report.

## 6. Report

Write the report before closing the step, appending to what is there:

```
tm section get <task-id>:report
tm section set <task-id>:report --file <path> --header "## Report"
```

The branch, the commits you made, the `tm verify run` exit code and which rows failed, and anything you could not do. Where the brief contradicts the tree — a file that does not exist, an interface that already differs — record the discrepancy, implement against the tree, and keep going.

A report that fixes a contract its dependents build on (a shape, a name, an id scheme) adds the contract to what `tm section get <plan-id>:overview` prints, or replaces the earlier wording of the same contract, and writes the whole of it back with `tm section set <plan-id>:overview --file <path>` before the step closes. Only the parent's `context` and `overview` reach a dependent's brief, and no step runs between tasks to copy it there.

## 7. Close the step

```
tm task complete <task-id> --agent <name> --token <token>
```

After the last commit and the report, on every path that finished the work. The task moves to `IMPLEMENTED`, and what follows — a review, or the landing — is tm's to choose. Leave the worktree in place: a fix round reuses it.

## Never

- Never open, copy or edit anything under `.taskmanager/`; the CLI is the only writer.
- Never claim, close or release a step you do not hold, and never hold two.
- Never edit outside your worktree, and never merge or push anything: tm lands the branch.
- Never change a task's definition to match what you built.
- Never end your turn with the step open: close it, or release it naming why.
