# Reviewing a task

For the agent that reads a `WAITING_REVIEW` task's branch and returns it either with findings or approved for merge.

## 1. Take the task

```
tm task list --status WAITING_REVIEW --yaml
tm run start <task-id> --agent <name> --session <id>
```

The claim sets the task to `REVIEWING` and locks nothing, because a review writes nothing. `--worktree` is refused with `this stage works from the branch already cut; it does not cut a worktree` — a review needs no checkout of its own.

Other refusals, exit 1: `Task <id> is not ready to start (current state: IN_FLIGHT)` means another agent already claimed the review; `(current state: WAITING_FIXES)` or `(current state: WAITING_MERGE)` means someone already reviewed it.

## 2. Read what was asked for

```
tm render <task-id> --view subagent
```

That is the brief the implementer was given, and it is the only standard you review against. `tm task get <task-id> --yaml` names the task's `target_repo` and `declared_files`.

## 3. Read the branch

The work is on `tm/<task-id>` in `<project root>/<target_repo>`:

```
git -C <repo> log --oneline main..tm/<task-id>
git -C <repo> diff main...tm/<task-id>
git -C <repo> show tm/<task-id>:<path>
```

Read only. Do not check the branch out in the project's own checkout, do not edit a file, do not run a formatter. If you must execute the code, do it in a worktree of your own making, outside the project, and say so in the review.

## 4. Run the checks

```
tm verify run <task-id> --ref tm/<task-id>
```

`file_exists`, `file_absent`, `symbol_signature` and `ast_export` read that ref directly, with no fetch, so a check against the unmerged branch is real evidence, not a guess. Without `--ref` they read the task's `target_repo` at `origin/main`, fetched first — which is still red before the merge for work that is genuinely finished, so use `--ref` here rather than a manual `git -C <repo> show tm/<task-id>:<path>`. `test_command` still runs from the project root regardless, and sees the same ref as `TM_VERIFY_REF` in its environment — a command that needs to check the branch itself reads `${TM_VERIFY_REF:-origin/main}`, unset when `--ref` is omitted. Exit 1 names each failing row; `No verifications to run.` exits 0 and proves nothing — a task with no checks is itself a finding.

## 5. Write the findings

One line per defect: the file, the symbol or line, and what breaks. No summary, no praise, no restatement of the task, no severity essay. Order them and number them, because the fix round answers them by number. Cite a symbol rather than a line number wherever you can.

Nothing to say is a valid review. Say it in one line.

A judgement call the brief itself cannot settle — not a defect, a genuine open question — is raised as a decision rather than left unresolved in prose: `tm decision add "<question>" --option "a|Do X" --recommend a --blocks <task-id>`. Say so in the review and release the task to `WAITING_FIXES` as usual; it reads `AWAITING_DECISION` once the fix round releases it back.

## 6. Release it

```
tm run stop <task-id> --status WAITING_FIXES
tm run stop <task-id> --status WAITING_MERGE
```

`WAITING_FIXES` when you found anything, `WAITING_MERGE` when you did not. Release on every exit path, including when you ran out of budget — in that case release to `WAITING_FIXES` with what you have and say the review is partial. Run `tm run heartbeat <task-id>` if the read runs long.

## 7. Report

The verdict, the numbered findings, the `tm verify run` exit code with the rows that failed, and what you did not cover.

## Never

- Never edit code, tests, fixtures or configuration — not even a one-line fix you can see.
- Never set `COMPLETED`, and never merge or push anything.
- Never claim the review with `--worktree`.
- Never leave the task in `REVIEWING`.
- Never file a finding you have not read in the branch's own content.
