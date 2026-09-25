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
worktree: <dir>/backend-<task-id>
worktrees: {backend: <dir>/backend-<task-id>}
token: <token>
```

`model` is the family the step runs on; `<name>` is the agent the lease is held under and `<token>` names this one claim, and every verb that closes or releases the step passes them back with `--agent <name> --token <token>`. The token matters because an agent name can repeat: a later claim of the same node under the same name gets a new token, and tm refuses the old one. When the workflow claimed for you, its prompt carries both flags. The claim locks every path the task declares until the step closes. The branch is cut from `base` in the task's own `target_repo`, with no upstream: `main` means that repository's `origin/main`, and a branch name means the container branch the task lands on. A task with no `target_repo` is refused. Any other `action` is another role's step: read that role's guide instead.

`action: blocked` exits 3 and writes nothing. Its `reason` names what the task waits on — an edge, a decision, a condition, a sync, or a lease holding one of its files. Report it and start nothing.

## 2. Read the brief

```
tm render <task-id> --view subagent
```

That is the whole assignment: the task's frontmatter, its parent's context, its sections and its verifications. Then, only as needed:

- `tm task get <task-id> --yaml` — status, `next_action`, flags, `target_repo`, `depends_on`, `declared_files`, verifications, conditions, lease.
- `tm section get <task-id>:<key>` — one section; `tm section get <task-id>` prints them all.

A task that was reopened carries a `:reopen` note and the earlier `:review`, and its branch still holds the earlier work: read both, and decide what to keep.

## 3. Work in that worktree and nowhere else

Every read, edit, command and commit happens under the printed worktree path. Check the prefix of each path you edit, not just its basename: the same file exists in the project's own checkout. Commit on the branch with an explicit pathspec. Do not merge and do not push: tm lands the branch. You may rewrite your own branch until its first review, never after.

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
- `codegraph_query` — passes with `codegraph CLI not installed; skipped` where that tool is absent.

The path checks read the ref, never your worktree, so without `--ref` they read `origin/main` and stay red until the task lands. A `test_command` reads the same ref from `TM_VERIFY_REF`; when tm lands the task it sets that to the landing target, so write `"$TM_VERIFY_REF"` into the command rather than a branch name. `No verifications to run.` exits 2: a task with no checks has not passed anything, and that is worth a line in your report.

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

- Another node must land first: `tm task release <task-id> --agent <name> --blocked --depends <other-id>`.
- Only the owner can answer: `tm task release <task-id> --agent <name> --blocked --decision "<question>" --option "a|Do X|why" --option "b|Do Y|why" --recommend a`. Name the options you considered and the one you recommend.
- A state outside the corpus: `tm task release <task-id> --agent <name> --blocked --needs "<what must hold>" --command "<a command that exits 0 once it holds>"`.

`--blocked` with nothing named is refused. `tm task release <task-id> --agent <name>` alone is a failed step, counted towards `FAILED`: use it only when you cannot go on and nothing names why, and say why in the report. A release or close refused for `--agent` means the lease is no longer yours: stop and report.

## 6. Report

Write the report before closing the step, appending to what is there:

```
tm section get <task-id>:report
tm section set <task-id>:report --file <path> --header "## Report"
```

The branch, the commits you made, the `tm verify run` exit code and which rows failed, and anything you could not do. Where the brief contradicts the tree — a file that does not exist, an interface that already differs — record the discrepancy, implement against the tree, and keep going.

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
