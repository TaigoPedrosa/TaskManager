# Fixing review findings

For the agent that closes a reviewer's findings on a `WAITING_FIXES` task and hands it back for review.

## 1. Take the task and the findings

```
tm task list --status WAITING_FIXES --yaml
tm run start <task-id> --worktree --worktree-dir <dir> --agent <name> --session <id> --ttl 900
```

The claim sets the task to `FIXING` and locks the task's declared files. `--worktree` reuses branch `tm/<task-id>` with every commit the earlier rounds made on it: if the worktree still exists it is handed back as it was, and if it was removed it is re-added on the same branch. Nothing is cut from `origin/main` a second time.

Refusals are the implementer's, exit 1: `Cannot claim task <id> due to file collision: {...}` when a live lease holds a path you declared, `Task <id> is not ready to start (current state: IN_FLIGHT)` when someone else already claimed it.

The findings are the numbered list the reviewer returned. Read the brief again only where a finding disputes it:

```
tm render <task-id> --view subagent
```

## 2. Fix exactly the findings

One commit per finding, or one commit naming them all — either way, every commit is on `tm/<task-id>` inside your worktree, with an explicit pathspec. Do not rewrite the branch's earlier commits: the review cites them.

- A finding you can close, close.
- A finding you judge wrong is answered in the report with the evidence that refutes it, and the code is left alone. It is never silently skipped.
- Anything else you notice goes in the report, not in the diff. Widening the scope is what forces a third round.
- A finding whose fix needs a call only the owner can make is raised as a decision rather than guessed: `tm decision add "<question>" --option "a|Do X" --recommend a --blocks <task-id>`, then answered in the report as "raised as `<id>`, not closed."

## 3. Verify and keep the lease alive

```
tm run heartbeat
tm verify run <task-id>
```

Exit 1 names the failing rows. Its paths resolve against the project root, not against your worktree, so the path checks stay red until the merge; confirm each one by hand inside the worktree instead, and treat `No verifications to run.` (exit 0) as no evidence at all.

## 4. Hand it back

```
tm run stop <task-id> --status WAITING_REVIEW
```

On every exit path, including when you could not close a finding. `--status NOT_STARTED` only when the task turns out to be the wrong work entirely. Leave the worktree in place.

## 5. Report

Answer each finding by its number, with the commit that closed it or the words "not done" and why. Then the `tm verify run` exit code, and anything you found and did not touch.

## Never

- Never set `WAITING_MERGE` or `COMPLETED`: only the reviewer approves, and only the merge role completes.
- Never edit outside your worktree, and never merge or push the repository's main branch.
- Never force-push, rebase or squash the task branch.
- Never change a test so a finding stops firing; close the finding the test names.
- Never end your turn holding a lease.
