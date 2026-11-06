# Reviewing a node

For the agent that reads a node's work after `tm task start` claimed its `review`, records its findings on the node, and approves or rejects it.

## 1. The claim

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to step 2. On your own:

```
tm task start <node-id> --agent <name> --session <id> --yaml
```

`action: review` sets the node to `REVIEWING`, names the `model` id, the `repos` it touched (several for a plan or spec), its `branch` and `base`, and locks nothing: a review writes no code, so it never holds a sibling out. It cuts no worktree. The lease is held under `<name>` and the claim prints its `token`; the verbs closing the step pass both back with `--agent <name> --token <token>`, and a workflow's prompt carries them. `action: blocked` (exit 3) claimed nothing; report its `reason`.

What this review covers follows from the status it was claimed from, which `tm task get <node-id> --yaml` prints as `claimed_from`:

- `IMPLEMENTED`: a task's review, of its own branch before it lands.
- `LANDED`: a plan's or spec's one review. Its branch has already landed, so the claim's `branch` names the target it landed on, not its own.
- `FIXED`: the re-review of a sensitive node's fix (`tm guide plan`, §2).

## 2. Check out what you review

You judge the code as it stands. You read neither the node's brief, the implementer's or fixer's notes, the spec, the design nor the plan, and you run no `tm` command but `tm guide review`, `tm finding add`, `tm finding list`, `tm finding reopen`, `tm task heartbeat`, `tm task list` and `tm plan list` (to name the nodes under a container landed without a squash) and the one that closes the step or raises a decision. Whether the change meets its acceptance is proven by the node's verifications, the landing gate and the spec's test plan, not by you.

In each repository the claim's `repos` names, `<base>` is that repository's entry in the claim's `bases`, since a container's repositories can land on different default branches (`base` is the first repository's): the branch the node's spec lands on, or the container branch the node lands on. `<base-ref>` is where it is read: `<remote>/<base>` for the spec's target in a repository whose entry in the claim's `remotes` names a remote (it pushes), the local `<base>` where that entry is null, and `<base>` itself for a container branch, which is local to the clone. A repository the claim does not name is not yours to check out: at its default branch it could lack, or contradict, work still in flight.

Cut a detached worktree of each repository at the state under review, where the workflow's prompt says (on your own, under your session's scratch directory), never inside the project, and remove it before you close the step:

```
git -C <repo> worktree add --detach <scratch>/<repo>-<node-id>-review <state>
git -C <repo> worktree remove <scratch>/<repo>-<node-id>-review
```

`<state>` is `<branch>` for a task and for a re-review. For a review claimed from `LANDED` it is `<base-ref>`, since the claim's `branch` already names the target the node landed on. Read only: do not check anything out in the project's own checkout, do not edit a file, do not run a formatter. Say in the review that you executed code, and where.

The range you read, in each worktree:

- **A task's review**: `git -C <worktree> diff <base-ref>...HEAD`, the whole diff.
- **A review claimed from `LANDED`**: in each repository, `git -C <worktree> show <squash>`, where `<squash>` is that repository's entry in the claim's `squashes`: the commit the node's latest landing there squashed to, which holds everything it landed however far the target has moved since. A repository with no entry in `squashes` was landed as merges: list the nodes under the node (a plan's with `tm task list --plan <node-id> --json`, a spec's with `tm plan list --spec <node-id> --json` and `tm task list --spec <node-id> --json`), then read every landing merge of the node and of each node under it against its first parent, the target as it stood before it landed: `git -C <worktree> log -p --diff-merges=first-parent -E --grep '^merge[(](<node-id>|<id under it>|...)[)]: land [^ ]+ on <base>$' HEAD --`. A repository where that prints nothing had nothing land.
- **A re-review** is scoped to the findings a sensitive fix answered: each one in `tm finding list <node-id>` with its answer, the commit that closed it or the evidence that disputes it, a landing failure's included. You read the branch's whole diff (`git -C <worktree> diff <base-ref>...HEAD`) and judge only those findings and their answers. Establish each closure by reading the fix; `tm finding reopen <node-id> <n> --agent <name> --token <token>` opens one that still holds. It never widens: no new finding outside those; anything else you notice goes in the report.

A spec review across several repositories is split under its one claim. One reviewer per repository reviews that repository's range in parallel, records its findings with `tm finding add` and does not close the step. Then a seam reviewer sees every reviewed repository at its target and the findings already recorded (`tm finding list <node-id>`), reads what is true only between repositories (a producer and its consumer that do not agree, a field written on one side and never read on the other), adds its own findings and closes the step. A spec with one repository gets one reviewer, which also closes. Your prompt says which of them you are; on your own with several repositories you are each in turn, the seam last.

## 3. Review the range

Run the `code-review` skill at `high` effort over the range, then record every defect it confirms (§4). Where the harness has no `code-review` skill, this recipe applies. Read the whole range once, then for each change ask:

- **Behaviour**: what does it do on an empty, null, repeated, out-of-order or boundary input, and on the failure of each call it makes?
- **Trust**: does an id, key or text that arrives from outside get checked against the caller's tenant before it reads or writes? Can secrets or personal data reach a log or an error?
- **Seams**: who calls each changed signature (grep them), what consumes each changed output, what else writes the same state? A producer nobody calls, a column only ever written as null and two halves that do not join are each a finding.
- **Limits**: does every call that waits have a timeout, every pool a bound and every shutdown a drain?
- **Tests**: does each run the code it names, or only search its source text?

Anything the diff left without a reader (a file, symbol, field, or a computation or load whose only consumer it removed) is a finding. So is a defect in code the diff leaves unchanged but makes reachable; mark it as reached through the diff. A UI test that reaches a control another file owns by its implementation rather than by what it shows the user is a finding.

A claim that something is absent (no caller, no other reader, no test) is made only after the search that would have found it. Run the code only to establish a finding you cannot settle by reading, in your worktree. A test that fails on the range is a blocking finding, whichever task owned its file.

A UI node's behaviour is checked by driving it in the running app: the write and its feedback, the focus after it, the keyboard route, a live update mid-edit, a reload. A screenshot beside the frame shows the look and proves none of them.

## Waiting on something that takes time

A gate, a build, an external state change — pick by duration, because duration is what you actually know:

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: the tool's own blocking `wait` where one exists, else `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | it is a condition, not a wait: `tm task release <node-id> --agent <name> --token <token> --blocked --needs "<what must hold>" --command "<a command that exits 0 once it holds>"` |

Never end your turn to wait on a background run "until notified." A background command's completion notification reaches you only while you are still working — ending your turn is what loses it, and nothing resumes you afterward.

A command that waits inside itself (a build queued on a slot, a long test run) runs as `tm task heartbeat <node-id> -- <command>`, so the lease outlives the wait.

## 4. Record the findings

Record each defect on the node, one per command, before you close the step:

```
tm finding add <node-id> --agent <name> --token <token> --blocking --at "<file>:<symbol>" --breaks "<what breaks>"
tm finding add <node-id> --agent <name> --token <token> --minor --at "<file>:<symbol>" --breaks "<what breaks>" --rule "<the rule that would have prevented it>"
```

Each prints the finding's number, which the fix answers it by. `--at` names where it is, a symbol rather than a line number wherever you can; `--breaks` says what breaks, in one line. A finding states what was read; a derived claim is not a finding until it is read. A claim that something is absent (no caller, no other reader, no test) names in `--breaks` the search that would have found it: the command, the path and pattern, and the range it covered. A finding a written rule would have prevented quotes that rule in `--rule`, or proposes the missing line and the guide it belongs in.

Every finding carries a severity, and every defect is recorded, however small: the severity decides only whether it holds the landing.

- `--blocking`: the node must not land as it is. It is any of these: a behaviour defect; a tenant, security or data-loss risk; a seam that does not join; a break of the stack's skill or the profile `CLAUDE.md`, with the line cited in `--breaks`.
- `--minor`: landing as it is breaks nothing. Dead code, naming, a comment, test tidiness. A minor finding never holds the landing, and it is still fixed: after the node lands, its fix answers every minor finding left open.
- When in doubt whether a defect holds the landing, it is blocking.

A question the code cannot answer is a decision, not a finding. A re-review files neither kind outside the findings it re-reads (§2).

`tm finding list <node-id>` shows earlier rounds' findings with their answers. A closed or disputed one that still holds is opened again with `tm finding reopen`, never recorded a second time.

## 5. Close the step

```
tm task review <node-id> --agent <name> --token <token> --approve
tm task review <node-id> --agent <name> --token <token> --reject
tm task review <node-id> --agent <name> --token <token> --approve --clean
```

tm refuses a verdict when no finding was recorded or reopened since your claim, unless `--clean` says nothing was found; it refuses `--clean` when one was. It refuses `--approve` while a blocking finding is open, and `--reject` while none is: minor findings alone never reject. It refuses either one too when the live lease is not `<name>`'s or not `<token>`'s: the step is no longer yours, so stop and report. tm writes the verdict line shown beside the status from the findings left open.

- **Approved**: a task, or a re-reviewed fix, goes on to its landing; a plan or spec reviewed at `LANDED` is `COMPLETED`. With minor findings open and fix on, the node lands first and then gets one fix for them, which is never re-reviewed.
- **Rejected, and the node fixes its own rejections**: it gets one fix, which lands without another review unless the node is sensitive. A rejected re-review makes the node `FAILED` with a decision for the owner, never a second fix.
- **Rejected, and the node does not fix** (`fix` off): a task lands its branch on its parent unfixed, and the parent's review is where the findings are fixed. A plan or spec at `LANDED` is already on its target, so a rejection it does not fix makes it `FAILED`, and the owner decides.

A judgement call the code cannot settle — not a defect, a genuine open question — is raised rather than left in prose: `tm task release <node-id> --agent <name> --token <token> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`. A finding that turns on "the code doesn't say" or "which of these is correct" is that same call: a reviewer raises it instead of rejecting on it. Run `tm task heartbeat <node-id>` if the read runs long.

## 6. Report

The verdict, the findings' numbers and severities, what you executed and where, and what you did not cover.

## Never

- Never edit code, tests, fixtures or configuration — not even a one-line fix you can see.
- Never approve with a blocking finding open, never mark a defect minor to approve over it, and never merge or push anything.
- Never widen a re-review past the findings still open.
- Never close the step without recording its findings first, or `--clean` when there are none. A repository reviewer in a split spec review never closes it.
- Never read the brief, the notes, the spec, the design or the plan.
- Never end your turn with the step open, or while a command you started in the background still runs: wait for it, read its result, then close.
- Never leave a worktree you cut behind you.
- Never file a finding you have not read in the range's own content.
