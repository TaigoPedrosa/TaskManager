# Resolving a stopped landing

For the agent handed a landing or a sync that tm stopped because it needs judgement: a conflict, a red it cannot attribute, a refused push or a missing gate.

## What tm already did

tm lands every node itself, as a job, one repository at a time in the node's landing order:

1. **Already landed?** A branch that is already on its target, or adds nothing to it, skips straight to the verification.
2. **Landing conditions.** An unmet `landing` condition ends the job; the node waits on it.
3. **Build.** A fresh merge worktree cut from the target (`origin/main`, or the container branch), and `git merge --no-ff` of the node's branch with a subject naming the node.
4. **Gate.** On a container branch: the node's own verifications, then the repository's `parent` gate when one is configured. On `main`: the repository's `main` gate, and when it is red, the same gate on the untouched target, cached per target commit, to attribute the red.
5. **Push.** To `main`: re-read the remote, merge it in again and re-gate if it moved, push `HEAD:main`, never force. To a container branch: a compare-and-swap of the local ref.
6. **Verify.** The node's verifications at the target; a red here is the node's own defect.
7. **Complete.** The merge worktree is removed and the node is `COMPLETED`.

A sync merges a target into a container branch the same way, under that branch's lock. Either job stops at `needs_agent` only for something a rule cannot settle, and that is the one moment an agent is dispatched:

| Stopped for | What it needs |
|:--|:--|
| `conflict` | resolve the merge in the job's worktree and commit it, then resume |
| `unattributed` | the tip and the untouched target are both red and no report names the failures: read both outputs; if the tip adds a failure, record an own defect, and if it adds none, resume with `--push` |
| `push_failed` | three refused pushes: find out why (a permission, a protection rule, a hook) and report it; resume only once the cause is gone, and never force |
| `no gate` | the repository has no `main` gate configured: report it; the owner configures `repos.<repo>.gates.main` |
| a red sync | the container's `parent` gate went red after the target was merged in: fix it in the job's worktree, commit, resume |

## 1. Take the job

A dispatcher's workflow usually hands you the job and says so in the prompt. On your own, claim the stopped node, which hands you the job and its lease:

```
tm task start <node-id> --agent <name> --session <id> --yaml
tm job status <job>
```

The job's `step`, `result`, `repo`, `target` and `worktree` say where it stopped, why, and where the merge in progress is. Run `tm task heartbeat <node-id>` if the work runs long.

## 2. Work in the job's worktree

It is tm's own merge worktree, holding the merge in progress. Resolve there, commit there with an explicit pathspec, and push nothing: tm pushes when it resumes. Never rebase, never force, and never merge in the project's own checkout or on its local `main`.

## 3. Resume

```
tm job resume <job>
tm job resume <job> --own-defect "<the finding, one line>"
tm job resume <job> --push
```

- Plain `resume` continues from where the job stopped, gates the tip again and lands it.
- `--own-defect` records that the node's own change is at fault. The node goes back for a fix with your finding in `:merge`, or to `FAILED` when it has no landing attempt left or does not fix its own defects.
- `--push` is for `unattributed` only, after you proved the tip adds no failure the target lacks.

A resume runs on in the background. `tm job status <job> --wait 540` blocks until the job leaves `running` or nine minutes pass, and prints where it went; a job stopped again is still yours.

Append what you found and did to the node's `:merge` section (`tm section get <node-id>:merge` first, then `tm section set <node-id>:merge --file <path>`).

## Waiting on something that takes time

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | leave the job stopped and report what is pending |

A job you leave unresolved counts as a failed step for the node, and its lease returns to the job for the next agent. Never end your turn to wait on a background run "until notified."

## 4. Report

The job, where it stopped, what you changed and in which commit, which resume you ran and what `tm job status <job>` printed after it.

## Never

- Never push, force-push, rebase or reset anything; tm pushes.
- Never merge into, commit on, or push the project checkout's local `main`.
- Never set a node `COMPLETED` or resume with `--push` on a red you have not attributed.
- Never review or fix the node's own code while resolving its landing: that is an own defect, recorded with `--own-defect`.
- Never open or edit anything under `.taskmanager/`.
