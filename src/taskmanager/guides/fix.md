# Fixing a node

For the agent that answers a node's open findings, a rejection's, a failed landing's or an approve's minor ones, on the node's own branch after `tm task start` claimed its `fix`.

## 1. The claim, and what it answers

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to the next paragraph. On your own:

```
tm task start <node-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

`action: fix` sets the node to `FIXING`, locks its declared files again, names the `model` id, and hands back a worktree of the node's existing branch with every earlier commit on it: `worktree` for a task, and `worktrees`, one per repository, for a plan or spec whose branch spans several. Nothing is cut a second time, with one exception: a plan or spec whose review ran on its landed target has a branch carrying nothing that target lacks, so tm sets it aside as `tm/<node-id>@<n>` and cuts the fix from the target, on top of what landed. The lease is held under `<name>` and the claim prints its `token`; the verbs closing the step pass both back with `--agent <name> --token <token>`, and a workflow's prompt carries them.

What you answer is every open finding on the node, from any round:

```
tm finding list <node-id> --open
```

Each names its number, severity, where it is (`at`) and what breaks. A failed landing is one of them, recorded by tm with `at: landing`: a red on the node's own verifications, failures the merged tip added to the gate, a red verification after landing, or what an agent recorded; the latest entry of `tm section get <node-id>:merge` carries its full output. When the node's `outcome` (`tm task get <node-id> --yaml`) is `approve`, the node has landed and the open findings are the minor ones its review left: the fix is cut from the target, on top of what landed.

Read the brief again only where a finding disputes it: `tm render <node-id> --view subagent`.

A fix lands without a re-review unless the node is sensitive: its `sensitive:` frontmatter key names an area, or it writes a migration (`tm guide plan`, §2). A sensitive node's fix of blocking findings gets one re-review, scoped to the findings it answers, and a rejection there makes the node `FAILED` with a decision for the owner, never a second fix. A fix of minor findings is never re-reviewed. Either way this is the one pass: answer every open finding now, minor ones included.

## 2. Fix exactly the findings

One commit per finding, or one commit naming them all — either way on the node's branch, inside your worktree, with an explicit pathspec. Do not rewrite the branch's earlier commits: the review cites them.

- A finding you can close, close, then record it: `tm finding close <node-id> <n> --agent <name> --token <token> --commit <sha>`, naming the commit on the branch that fixed it.
- A finding you judge wrong is answered with the evidence that refutes it, `tm finding dispute <node-id> <n> --agent <name> --token <token> --evidence "<what was read>"`, and the code is left alone. It is never silently skipped. Evidence is what was read; a derived claim refutes nothing until it is read. A claim that something is absent (no caller, no other reader, not reachable) names the search that would have found it: the command, the path and pattern, and the range it covered.
- A finding you close starts with its test: write it first, run it on the unfixed branch and watch it fail for the reason the finding names, then fix and watch it pass. A test first seen green proves nothing about the finding.
- A finding you close lands with a test for each branch the fix adds (each stage a fold adds, each guard, each argument threaded through a call), and each test fails when its branch alone is reverted: revert each branch on its own, watch the suite go red, then restore it. Reverting the whole fix at once proves nothing about its parts.
- Before closing, delete each guard, bound, filter and escape in your diff one at a time (one branch, one statement; null, empty and whitespace as separate inputs) and confirm a test turns red. A deletion that stays green gets a test before the step closes, and a filter a mock stands in for (a repository query, an HTTP route) gets a test through the real component. Record each deletion as it runs: `tm check add <node-id> --agent <name> --token <token> --deleted <file>:<line> --red <test>`, once per `&&` or `||` operand, or `--green` in place of `--red` when every test still passed. A green one is answered by the test you then add and a `--red` record of the same `--deleted`: `tm task complete` refuses while a deletion's latest record is green, and while a guard the diff adds or changes outside test files (a condition and each of its operands, an early return, a `catch`, a field assignment, a list element) has no record naming its `<file>:<line>`. A line tm reads as a guard that is none takes `--waive <why>` in place of `--red`, and the why is recorded with it.
- Anything else you notice goes in a note, not in the diff. Widening the scope is what spends the next round.
- A landing failure whose red lies in files this node does not declare belongs to the node that caused it: find or file that node, then `tm task release <node-id> --agent <name> --token <token> --blocked --depends <that-node>`, and say so in a note.
- A finding whose fix needs a call only the owner can make: `tm task release <node-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`, and leave the finding open: the fix claimed after the owner answers closes it. A finding that turns on "the brief doesn't say" or "which of these is correct" is exactly that call, raised at once rather than guessed at.
- A test of behaviour runs the code it tests: it calls the function, drives the page's scripts, or runs the command, and asserts what comes out. Searching the source for a call or a string is not a test of behaviour, even when it fails once the line is deleted.
- A test that checks a generated artifact against its generator fails, never skips, when the generator is missing: a skipped check reads as a pass in every gate that runs it.

## 3. Verify and keep the lease alive

```
tm task heartbeat <node-id>
tm task heartbeat <node-id> -- <command>
tm verify run <node-id> --ref tm/<node-id>
```

A fix lease lasts `lease_ttl.fix` seconds (two hours by default), and only a heartbeat extends it. Run every build, gate or test that may run or queue for more than a few minutes as the command of the second form: it heartbeats every minute until the command exits, exits with its code, and stops the command when the lease is gone.

Exit 1 names the failing rows. `No verifications to run.` exits 2 and is no evidence at all.

Before closing, read `tm verify list <node-id>` against the fix, and for a plan or spec the list of each task under it, since its landing runs them all. A row that checks what the fix replaced (an identifier, a file, a pattern) is updated in the same step, with `tm verify remove` and `tm verify add`, and named in a note.

## Waiting on something that takes time

A gate, a build, an external state change — pick by duration, because duration is what you actually know:

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: the tool's own blocking `wait` where one exists, else `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | it is a condition, not a wait: `tm task release <node-id> --agent <name> --token <token> --blocked --needs "<what must hold>" --command "<a command that exits 0 once it holds>"` |

Never end your turn to wait on a background run "until notified." A background command's completion notification reaches you only while you are still working — ending your turn is what loses it, and nothing resumes you afterward.

A command that waits inside itself (a build queued on a slot, a long test run) runs as `tm task heartbeat <node-id> -- <command>`, so the lease outlives the wait.

## 4. Report

Every finding is answered by `tm finding close` or `tm finding dispute`, and every deletion by `tm check add`. What neither carries is one line each:

```
tm note add <node-id> --agent <name> --token <token> "<one line>"
```

A note for each `tm verify run` row that failed, and for anything you found and did not touch.

## 5. Close the step

```
tm task complete <node-id> --agent <name> --token <token>
```

On every path that finished the round, including one where a finding was disputed rather than closed. tm refuses it while any finding on the node is still open. The node moves to `FIXED`. Leave the worktree in place.

## Never

- Never approve, land or complete a node: the reviewer approves, and tm lands.
- Never edit outside your worktree, and never merge or push anything.
- Never force-push, rebase or squash the node's branch.
- Never change a test so a finding stops firing; close the finding the test names.
- Never close a finding with a branch that nothing fails on when that branch alone is reverted.
- Never end your turn with the step open: close it, or release it naming why. Never end it while a command you started in the background still runs: wait for it, read its result, then close.
