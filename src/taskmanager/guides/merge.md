# Merging an approved task

For the agent that lands a `WAITING_MERGE` task's branch on its repository's main branch and completes the task once the deliverable is provably there.

## 1. Take the work

```
tm task list --status WAITING_MERGE --yaml
tm run start <task-id>
tm task get <task-id> --yaml
```

`tm run start` claims a `WAITING_MERGE` task into `MERGING`, no `--worktree` flag (there is no fresh worktree to cut — the merge works from the branch `tm/<task-id>` already sitting in `<project root>/<target_repo>`). The lease locks no file: a merge writes the repository's main branch, not the worktree, so a sibling whose task shares a declared file is free to start while this runs. Two agents claiming the same `WAITING_MERGE` task race on this exactly like a review does: the second gets `Task <id> is not ready to start (current state: MERGING)`. `tm task get` names the `target_repo` and the `declared_files` you will confirm in step 3.

## 2. Merge in a worktree of your own, and push by refspec

```
git -C <repo> fetch origin
git -C <repo> log --oneline origin/main..tm/<task-id>
git -C <repo> worktree add --detach <scratch>/<repo>-merge-<task-id> origin/main
git -C <scratch>/<repo>-merge-<task-id> merge --no-ff -m "<subject naming the change>" tm/<task-id>
git -C <repo> ls-remote origin refs/heads/main
git -C <scratch>/<repo>-merge-<task-id> push origin HEAD:main
```

**Never merge into the shared checkout's local `main`, and never `git push origin main`.** That local branch is shared by every agent working in the repository: `git fetch` never moves it, so it is routinely behind `origin/main`, and a merge whose push was refused stays on it, where the next agent's push publishes it. A detached worktree cut from a freshly fetched `origin/main` has neither problem, and pushing `HEAD:main` sends exactly the commit you built.

- **Fetch immediately before the merge, not when you planned it**, and re-read `ls-remote` just before the push. If `origin/main` moved in between, the push is rejected as non-fast-forward: remove the merge worktree and redo this step from a fresh fetch. Never force.
- **Always pass `-m`.** A `--no-ff` merge with no subject commits itself with git's default one.
- **Run the repository's own gate in the merge worktree before pushing** whenever `origin/main` has moved since the branch was cut. The review saw the branch on an older base, so only the merged tip shows how the two combine.
- **On a conflict**, `git -C <scratch>/<repo>-merge-<task-id> merge --abort`, remove the worktree, change no status, and report. The task stays `WAITING_MERGE`.
- **If the push is refused** by a permission check rather than rejected by the remote, stop. Report the merge worktree's path and the merge commit's sha so the owner can push it with one command. Never retry it, force it, or ask another agent or session to push it.
- **Where `main` deploys on push, the push is the deploy.** Judge CI by the run at the current tip of `origin/main`, not the run your push triggered: a sibling landing seconds later cancels yours.

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

Add `--remove-worktree` to the completing stop, `tm run stop <task-id> --status COMPLETED --remove-worktree`. The `MERGING` lease never recorded a worktree path (none was cut), so `tm` finds it by the branch `tm/<task-id>` in the task's repository and removes it — same fallback used for a task completed without ever claiming a lease at all. It never forces: a refusal means the worktree holds uncommitted work or is not yours, and that is information. Leave the branch `tm/<task-id>` in place unless your project says otherwise; the merge commit is what makes it disposable.

Remove the merge worktree from step 2 as well, by name and without `--force`: `git -C <repo> worktree remove <scratch>/<repo>-merge-<task-id>`.

## Waiting on something that takes time

A gate, a push, an external state change — pick by duration, because duration is what you actually know:

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: the tool's own blocking `wait` where one exists, else `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | `tm run stop <task-id> --status WAITING_MERGE`, report what is pending, and let it be picked up again rather than holding your own turn open |

Never end your turn to wait on a background run "until notified." A background command's completion notification reaches you only while you are still working — ending your turn is what loses it, and nothing resumes you afterward. The output is already on disk; `tail` it instead of waiting for word of it.

## 7. Report

Task id, merge commit sha, the merge worktree path, the push and its `ls-remote` before and after, each declared path with its `cat-file` result, the `tm verify run` exit code, the status you set, and whether the worktree was removed.

## Never

- Never complete a task whose verification failed or whose deliverable you did not find on `origin/main`.
- Never force-push, never rewrite the repository's main branch, never `git checkout`, `reset` or `clean` in a checkout you share.
- Never merge into, commit on, or push the shared checkout's local `main`; the merge lives in your own worktree and is pushed as `HEAD:main`.
- Never merge a branch you did not claim into `MERGING` yourself, and never review or fix the code while merging it — it goes back to `WAITING_FIXES` instead.
- Run `tm run heartbeat <task-id>` if any step runs long; a swept `MERGING` lease returns the task to `WAITING_MERGE` for someone else to pick up.
- Never open or edit anything under `.taskmanager/`.
