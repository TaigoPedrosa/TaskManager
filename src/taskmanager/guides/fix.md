# Fixing a node

For the agent that closes a rejection's findings, or a failed landing's defect, on the node's own branch after `tm task start` claimed its `fix`.

## 1. The claim, and what it answers

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to the next paragraph. On your own:

```
tm task start <node-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

`action: fix` sets the node to `FIXING`, locks its declared files again, names the `model` family, and hands back a worktree of the node's existing branch with every earlier commit on it: `worktree` for a task, and `worktrees`, one per repository, for a plan or spec whose branch spans several. Nothing is cut from `origin/main` a second time. The lease is held under `<name>` and the claim prints its `token`; the verbs closing the step pass both back with `--agent <name> --token <token>`, and a workflow's prompt carries them.

What you answer is the node's `outcome`, printed by `tm task get <node-id> --yaml`:

- `reject` — every finding in `tm section get <node-id>:review` not yet recorded as closed, from any round.
- `merge_failed` — the landing's own defect, in the latest entry of `tm section get <node-id>:merge`: a red on the node's own verifications, failures the merged tip added to the gate, a red verification after landing, or what an agent recorded.

Read the brief again only where a finding disputes it: `tm render <node-id> --view subagent`.

## 2. Fix exactly the findings

One commit per finding, or one commit naming them all — either way on the node's branch, inside your worktree, with an explicit pathspec. Do not rewrite the branch's earlier commits: the review cites them.

- A finding you can close, close.
- A finding you judge wrong is answered in the report with the evidence that refutes it, and the code is left alone. It is never silently skipped.
- A finding you close lands with a test that fails when the fix is reverted: revert it once, watch that test go red, then restore the fix. A comment the fix touches states the present reason the code holds, never what it replaced.
- Anything else you notice goes in the report, not in the diff. Widening the scope is what spends the next round.
- A landing failure whose red lies in files this node does not declare belongs to the node that caused it: find or file that node, then `tm task release <node-id> --agent <name> --token <token> --blocked --depends <that-node>`, and say so in the report.
- A finding whose fix needs a call only the owner can make: `tm task release <node-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`, and answer it in the report as raised, not closed.

## 3. Verify and keep the lease alive

```
tm task heartbeat <node-id>
tm verify run <node-id> --ref tm/<node-id>
```

Exit 1 names the failing rows. `No verifications to run.` exits 2 and is no evidence at all.

## 4. Report

Answer each finding by its number, with the commit that closed it or the words "not done" and why, appended to the report:

```
tm section get <node-id>:report
tm section set <node-id>:report --file <path> --header "## Report"
```

Then the `tm verify run` exit code, and anything you found and did not touch.

## 5. Close the step

```
tm task complete <node-id> --agent <name> --token <token>
```

On every path that finished the round, including one where a finding was contested rather than closed. The node moves to `FIXED` and is reviewed again. A fix answering a failed landing is checked by that review but does not spend a fix round. Leave the worktree in place.

## Never

- Never approve, land or complete a node: the reviewer approves, and tm lands.
- Never edit outside your worktree, and never merge or push anything.
- Never force-push, rebase or squash the node's branch.
- Never change a test so a finding stops firing; close the finding the test names.
- Never close a finding with nothing that fails when the fix is reverted, and never leave a comment naming what it replaced instead of the present reason it holds.
- Never end your turn with the step open: close it, or release it naming why.
