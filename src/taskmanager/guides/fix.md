# Fixing a node

For the agent that closes a rejection's findings, or a failed landing's defect, on the node's own branch after `tm task start` claimed its `fix`.

## 1. The claim, and what it answers

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to the next paragraph. On your own:

```
tm task start <node-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

`action: fix` sets the node to `FIXING`, locks its declared files again, names the `model` id, and hands back a worktree of the node's existing branch with every earlier commit on it: `worktree` for a task, and `worktrees`, one per repository, for a plan or spec whose branch spans several. Nothing is cut a second time, with one exception: a plan or spec whose review ran on its landed target has a branch carrying nothing that target lacks, so tm sets it aside as `tm/<node-id>@<n>` and cuts the fix from the target, on top of what landed. The lease is held under `<name>` and the claim prints its `token`; the verbs closing the step pass both back with `--agent <name> --token <token>`, and a workflow's prompt carries them.

What you answer is the node's `outcome`, printed by `tm task get <node-id> --yaml`:

- `reject` — every finding in `tm section get <node-id>:review` not yet recorded as closed, from any round.
- `merge_failed` — the landing's own defect, in the latest entry of `tm section get <node-id>:merge`: a red on the node's own verifications, failures the merged tip added to the gate, a red verification after landing, or what an agent recorded.

Read the brief again only where a finding disputes it: `tm render <node-id> --view subagent`.

A fix lands without a re-review unless the node is sensitive: its `sensitive:` frontmatter key names an area, or it writes a migration (`tm guide plan`, §2). A sensitive node's fix gets one re-review, scoped to the findings it answers, and a rejection there makes the node `FAILED` with a decision for the owner, never a second fix. Either way this is the one pass: close every finding now.

## 2. Fix exactly the findings

One commit per finding, or one commit naming them all — either way on the node's branch, inside your worktree, with an explicit pathspec. Do not rewrite the branch's earlier commits: the review cites them.

- A finding you can close, close.
- A finding you judge wrong is answered in the report with the evidence that refutes it, and the code is left alone. It is never silently skipped. Evidence is what was read; a derived claim refutes nothing until it is read. A claim that something is absent (no caller, no other reader, not reachable) names the search that would have found it: the command, the path and pattern, and the range it covered.
- A finding you close starts with its test: write it first, run it on the unfixed branch and watch it fail for the reason the finding names, then fix and watch it pass. A test first seen green proves nothing about the finding.
- A finding you close lands with a test for each branch the fix adds (each stage a fold adds, each guard, each argument threaded through a call), and each test fails when its branch alone is reverted: revert each branch on its own, watch the suite go red, then restore it. Reverting the whole fix at once proves nothing about its parts.
- Anything else you notice goes in the report, not in the diff. Widening the scope is what spends the next round.
- A landing failure whose red lies in files this node does not declare belongs to the node that caused it: find or file that node, then `tm task release <node-id> --agent <name> --token <token> --blocked --depends <that-node>`, and say so in the report.
- A finding whose fix needs a call only the owner can make: `tm task release <node-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`, and answer it in the report as raised, not closed. A finding that turns on "the brief doesn't say" or "which of these is correct" is exactly that call, raised at once rather than guessed at.
- A test of behaviour runs the code it tests: it calls the function, drives the page's scripts, or runs the command, and asserts what comes out. Searching the source for a call or a string is not a test of behaviour, even when it fails once the line is deleted.
- A test that checks a generated artifact against its generator fails, never skips, when the generator is missing: a skipped check reads as a pass in every gate that runs it.

## 3. Verify and keep the lease alive

```
tm task heartbeat <node-id>
tm verify run <node-id> --ref tm/<node-id>
```

Exit 1 names the failing rows. `No verifications to run.` exits 2 and is no evidence at all.

Before closing, read `tm verify list <node-id>` against the fix, and for a plan or spec the list of each task under it, since its landing runs them all. A row that checks what the fix replaced (an identifier, a file, a pattern) is updated in the same step, with `tm verify remove` and `tm verify add`, and named in the report.

## 4. Report

Answer each finding by its number, with the commit that closed it or the words "not done" and why, appended to the report:

```
tm section get <node-id>:report > <path>
tm section set <node-id>:report --file <path> --header "## Report"
```

`tm section get` writes the header to stderr and the content alone to stdout, so `> <path>` captures content only and the round trip above never folds the header back in.

Then the `tm verify run` exit code, and anything you found and did not touch.

## 5. Close the step

```
tm task complete <node-id> --agent <name> --token <token>
```

On every path that finished the round, including one where a finding was contested rather than closed. The node moves to `FIXED`. Leave the worktree in place.

## Never

- Never approve, land or complete a node: the reviewer approves, and tm lands.
- Never edit outside your worktree, and never merge or push anything.
- Never force-push, rebase or squash the node's branch.
- Never change a test so a finding stops firing; close the finding the test names.
- Never close a finding with a branch that nothing fails on when that branch alone is reverted.
- Never end your turn with the step open: close it, or release it naming why.
