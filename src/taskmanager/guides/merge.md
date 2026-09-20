# Merging an approved task

For the agent that lands a `WAITING_MERGE` task's branch on its repository's main branch and completes the task once the deliverable is provably there.

## 1. Take the work

```
tm task list --status WAITING_MERGE --yaml
tm task get <task-id> --yaml
```

A merge holds no lease: `tm run start <task-id>` is refused with `Task <id> is not ready to start (current state: WAITING_MERGE)`. Nothing is claimed, so merge one task at a time and finish it before starting the next. `tm task get` names the `target_repo` and the `declared_files` you will confirm in step 3; the branch is `tm/<task-id>` in `<project root>/<target_repo>`.

## 2. Merge and push

```
git -C <repo> fetch origin
git -C <repo> log --oneline origin/main..tm/<task-id>
git -C <repo> merge --no-ff -m "<subject naming the change>" tm/<task-id>
git -C <repo> push origin main
```

Fetch immediately before the merge, not when you planned it: a sibling may have landed in between. A `--no-ff` merge with no `-m` commits itself with git's default subject, so always pass the subject. On a conflict, `git -C <repo> merge --abort`, change no status, and report — the task stays `WAITING_MERGE`. Never force-push, and never merge a task that is not in `WAITING_MERGE`.

## 3. Prove the deliverable is on the remote

For every path `tm task get <task-id> --yaml` listed under `declared_files`:

```
git -C <repo> cat-file -e origin/main:<path>
```

Exit 0 means that path is on `origin/main`. A push that reported success and a file that is on `origin/main` are two different claims; this is the second one.

## 4. Verify

```
tm verify run <task-id>
```

It must exit 0. Now that the work is on the project's own checkout, the path checks measure the merged tree, which is what they were written for. If it exits 1, **do not complete the task**: report the failing rows, leave the status at `WAITING_MERGE`, and let it go back to fixes. If it prints `No verifications to run` it exits 2 having checked nothing: that is not a pass, so report it and let the task's owner attest it or add a check.

## 5. Complete it

```
tm run stop <task-id> --status COMPLETED
```

This is the only place `COMPLETED` is ever set, and only after steps 3 and 4 both came back clean. Completing a task is what makes its dependents `READY`, so a wrong completion releases work onto a tree that does not hold what it needs.

## 6. Remove the worktree

Add `--remove-worktree` to the completing stop, `tm run stop <task-id> --status COMPLETED --remove-worktree`. A task in `WAITING_MERGE` holds no lease, so `tm` finds its worktree by the branch `tm/<task-id>` in the task's repository and removes it. It never forces: a refusal means the worktree holds uncommitted work or is not yours, and that is information. Leave the branch `tm/<task-id>` in place unless your project says otherwise; the merge commit is what makes it disposable.

## 7. Report

Task id, merge commit sha, the push, each declared path with its `cat-file` result, the `tm verify run` exit code, the status you set, and whether the worktree was removed.

## Never

- Never complete a task whose verification failed or whose deliverable you did not find on `origin/main`.
- Never force-push, never rewrite the repository's main branch, never `git checkout`, `reset` or `clean` in a checkout you share.
- Never merge a branch whose task is not `WAITING_MERGE`, and never review or fix the code while merging it — it goes back to `WAITING_FIXES` instead.
- Never open or edit anything under `.taskmanager/`.
