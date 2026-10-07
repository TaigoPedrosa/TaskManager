# Reviewing a node

For the agent that reads a node's work after `tm task start` claimed its `review`, writes the findings on the node, and approves or rejects it.

## 1. The claim

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to step 2. On your own:

```
tm task start <node-id> --agent <name> --session <id> --yaml
```

`action: review` sets the node to `REVIEWING`, names the `model` family, the `repos` it touched (several for a plan or spec), its `branch` and `base`, and locks nothing: a review writes no code, so it never holds a sibling out. It cuts no worktree. The lease is held under `<name>` and the claim prints its `token`; the verbs closing the step pass both back with `--agent <name> --token <token>`, and a workflow's prompt carries them. `action: blocked` (exit 3) claimed nothing; report its `reason`.

What this review covers follows from the status it was claimed from, which `tm task get <node-id> --yaml` prints as `claimed_from`:

- `IMPLEMENTED`: a task's review, of its own branch before it lands.
- `LANDED`: a plan's or spec's one review. Its branch has already landed, so the claim's `branch` names the target it landed on, not its own.
- `FIXED`: the re-review of a sensitive node's fix (`tm guide plan`, §2).

## 2. Read what was asked for

```
tm render <node-id> --view subagent
```

That is the brief the implementer was given, and the only standard you review against. `tm task get <node-id> --yaml` names the node's `target_repo`, `declared_files` and flags. For a plan or spec the brief also lists every child whose own review rejected, with its `:review`: those children landed on this branch unfixed, and this review is where their findings get fixed.

## 3. Read the branch

In each repository the claim's `repos` names, `<base>` is `origin/main` when `base` is `main`, and the container branch otherwise:

```
git -C <repo> log --oneline <base>..<branch>
git -C <repo> diff <base>...<branch>
git -C <repo> show <branch>:<path>
```

For a review claimed from `LANDED`, `<base>...<branch>` is empty: the node's code is what landed on its target, `<base>`. That is its own landing merge, and the landing merge of every node under it that landed on that target itself rather than on the node's branch; a node whose children all landed that way has no landing merge of its own. List the nodes under it, a plan's with `tm task list --plan <node-id> --json`, a spec's with `tm plan list --spec <node-id> --json` and `tm task list --spec <node-id> --json`. Then read every one of those merges against its first parent, the target as it stood before it landed:

```
git -C <repo> log -p --diff-merges=first-parent -E --grep '^merge[(](<node-id>|<id under it>|...)[)]: land [^ ]+ on <base>$' <branch> --
```

A repository where that prints nothing had nothing land.

Read only. Do not check the branch out in the project's own checkout, do not edit a file, do not run a formatter. If you must execute the code, cut a detached worktree where the workflow's prompt says (on your own, under your session's scratch directory), never inside the project, and remove it before you close the step:

```
git -C <repo> worktree add --detach <scratch>/<repo>-<node-id>-review <branch>
git -C <repo> worktree remove <scratch>/<repo>-<node-id>-review
```

Say in the review that you executed it, and where.

- **A task's review** reads the whole diff against the brief.
- **A plan's or spec's review** runs once, on its landed target. It reads the whole landing against the brief, and for what is true only between its children: a producer nobody calls, a column only ever written as null, two halves that do not join.
- **A re-review** is scoped to the open findings of a sensitive fix: each finding in `:review` not yet recorded as closed, checked against the fix commits and the fixer's latest `:report` entry, and, when the last landing failed, the failure the latest `:merge` entry names. Establish each closure by making it fail. It never widens: no fresh read of the rest of the diff and no new finding outside those; anything else you notice goes in the report.

## 4. Run the checks

```
tm verify run <node-id> --ref <branch>
```

The path checks read that ref directly, with no fetch, so a check against the unmerged branch is real evidence. Each `test_command` sees the same ref as `TM_VERIFY_REF`. Exit 1 names each failing row; `No verifications to run.` exits 2 and proves nothing — a task with no checks is itself a finding.

A test that fails on the branch rejects the node, whichever task declared the test's file; another task's ownership of a file never excuses a failure this diff caused.

Run every check the acceptance lists. A check you could not run is named in the findings as not run, and the node is not approved over it. A test that only searches source text is not evidence for an acceptance line about behaviour; name it in the findings.

A UI node's behaviour lines are checked by driving them in the running app: the write and its feedback, the focus after it, the keyboard route, a live update mid-edit, a reload. A screenshot beside the frame shows the look and proves none of them.

## 5. Write the findings

Append them to the node before you close the step:

```
tm section get <node-id>:review > <path>
tm section set <node-id>:review --file <path> --header "## Review"
```

`tm section get` writes the header to stderr and the content alone to stdout, so `> <path>` captures content only and the round trip above never folds the header back in.

One line per defect: the file, the symbol or line, and what breaks. No summary, no praise, no restatement of the task, no severity essay. Number them, because the fix answers them by number, and record each earlier finding as closed or still open. Cite a symbol rather than a line number wherever you can. Nothing to say is a valid review: say it in one line.

## 6. Close the step

```
tm task review <node-id> --agent <name> --token <token> --approve
tm task review <node-id> --agent <name> --token <token> --reject --verdict "<one line>"
```

tm refuses either one while the `:review` section is unchanged since your claim: the findings are the record, and a verdict without them leaves a fixer nothing to fix. It refuses it too when the live lease is not `<name>`'s or not `<token>`'s: the step is no longer yours, so stop and report. `--verdict` is a free-text line shown beside the status; it never decides anything.

- **Approved**: a task, or a re-reviewed fix, goes on to its landing; a plan or spec reviewed at `LANDED` is `COMPLETED`.
- **Rejected, and the node fixes its own rejections**: it gets one fix, which lands without another review unless the node is sensitive. A rejected re-review makes the node `FAILED` with a decision for the owner, never a second fix.
- **Rejected, and the node does not fix** (`fix` off): a task lands its branch on its parent unfixed, and the parent's review is where the findings are fixed. A plan or spec at `LANDED` is already on its target, so a rejection it does not fix makes it `FAILED`, and the owner decides.

A judgement call the brief itself cannot settle — not a defect, a genuine open question — is raised rather than left in prose: `tm task release <node-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`. A finding that turns on "the brief doesn't say" or "which of these is correct" is that same call: a reviewer raises it instead of rejecting on it. Run `tm task heartbeat <node-id>` if the read runs long.

## 7. Report

The verdict, the numbered findings, the `tm verify run` exit code with the rows that failed, and what you did not cover.

## Never

- Never edit code, tests, fixtures or configuration — not even a one-line fix you can see.
- Never approve with a finding still open, and never merge or push anything.
- Never widen a re-review past the findings still open.
- Never close the step without writing `:review` first.
- Never leave a worktree you cut behind you.
- Never file a finding you have not read in the branch's own content.
