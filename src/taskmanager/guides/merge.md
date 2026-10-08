# Resolving a stopped landing

For the agent handed a landing or a sync that tm stopped because it needs judgement: a conflict, a red it cannot attribute, a refused push or a missing gate.

## What tm already did

tm lands every node itself, as a job, one repository at a time in the node's landing order:

1. **Already landed?** A branch that is already on its target, or adds nothing to it, skips straight to the verification.
2. **Landing conditions.** An unmet `landing` condition ends the job; the node waits on it.
3. **Build.** A fresh merge worktree cut from the target (`origin/<target>` when it is the branch the node's spec lands on, or the container branch), and `git merge --no-ff` of the node's branch with the subject `merge(<node-id>): land <branch> on <target>`. A spec's target that origin does not have yet is cut from the repository's `default_branch`.
4. **Gate.** On a container branch: the node's own verifications, then the repository's `parent` gate when one is configured. On the spec's target: the repository's `main` gate, whatever the target is named, and when it is red, the same gate on the untouched target, cached per target commit, to attribute the red.
5. **Push.** To the spec's target: re-read the remote, merge it in again and re-gate if it moved, push `HEAD:<target>`, never force; a target origin does not have yet is created by this push. To a container branch: a compare-and-swap of the local ref.
6. **Verify.** The node's verifications at the target; a red here is the node's own defect.
7. **Complete.** The merge worktree is removed and the node is `COMPLETED`, or `LANDED` when it is a plan or spec with review on: its one review reads what landed.

A sync merges a target into a container branch the same way, under that branch's lock. Either job stops at `needs_agent` only for something a rule cannot settle, and that is the one moment an agent is dispatched:

| Stopped for | What it needs |
|:--|:--|
| `conflict` | resolve the merge in the job's worktree and commit it, then resume |
| `unattributed` | the tip and the untouched target are both red and no report names the failures: read both outputs; if the tip adds a failure, record an own defect, and if it adds none, resume with `--push` |
| `push_failed` | three failed tries. A push to the spec's target records each in `result.push_errors` with its command, exit code and stderr: an `ls-remote` with no answer is the network or the remote, a refused `push` a permission, a protection rule or a hook. A container branch, moved by a landing or a sync, records none: it moved under each of three compare-and-swaps, so other landings or syncs onto it kept moving it. Report it, resume only once the cause is gone, and never force |
| `no gate` | the repository has no `main` gate, which every landing on a spec's target needs: report it; the owner sets it with `tm config set repos.<repo>.gates.main.command "<command>"` |
| a red sync | the container's `parent` gate went red after the target was merged in: fix it in the job's worktree, commit, resume |

## 1. Take the job

A dispatcher's workflow usually hands you the job and says so in the prompt. On your own, claim the stopped node, which hands you the job and its lease:

```
tm task start <node-id> --agent <name> --session <id> --yaml
tm job status <job>
```

The claim prints the lease's `token`; pass it, with your name, to `tm job resume`, which refuses anyone the claim did not hand the job to. The job's `step`, `result`, `repo`, `target` and `worktree` say where it stopped, why, and where the merge in progress is. Run `tm task heartbeat <node-id>` if the work runs long.

## 2. Work in the job's worktree

It is tm's own merge worktree, holding the merge in progress. Resolve there, commit there with an explicit pathspec, and push nothing: tm pushes when it resumes. Never rebase, never force, and never merge in the project's own checkout or on its local copy of the target.

- A generated file (a built stylesheet, a lockfile, a schema dump) is regenerated, never edited or hand-merged: a branch that changes any of its inputs rebuilds it before closing, and a conflict on it is resolved by rebuilding it on the merged tree.

## 3. Resume

```
tm job resume <job> --agent <name> --token <token>
tm job resume <job> --agent <name> --token <token> --own-defect "<the finding, one line>"
tm job resume <job> --agent <name> --token <token> --push
```

- Plain `resume` continues from where the job stopped, gates the tip again and lands it.
- `--own-defect` records that the node's own change is at fault. The node goes back for a fix with your finding in `:merge`, or to `FAILED` when it has no landing attempt left or does not fix its own defects.
- `--push` is for `unattributed` only, after you proved the tip adds no failure the target lacks.

A resume runs on in the background. `tm job status <job> --wait 540` blocks until the job leaves `running` or nine minutes pass, and prints where it went. A job that stops again is handed back: tm parks it, counts the stop against the node, and refuses your next `resume`. On your own, claim the node again with `tm task start`, which hands it to you with a new token; under a workflow, report and stop, and the workflow claims it again.

Append what you found and did to the node's `:merge` section (`tm section get <node-id>:merge > <path>` first — content lands on stdout, header on stderr, so this never captures the header — then `tm section set <node-id>:merge --file <path>`).

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
- Never merge into, commit on, or push the project checkout's local copy of a spec's target branch.
- Never set a node `COMPLETED` or resume with `--push` on a red you have not attributed.
- Never review or fix the node's own code while resolving its landing: that is an own defect, recorded with `--own-defect`.
- Never open or edit anything under `.taskmanager/`.
