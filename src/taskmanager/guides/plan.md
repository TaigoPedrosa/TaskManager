# Writing a plan

For the agent authoring new work: turn an intent into a document `tm import` accepts, so every task is claimable, disjoint and provable without asking its author anything.

## 1. Write the document

One YAML file (`--format json` and `markdown` parse the same shape). A `spec` is the standing intent, a `plan` is a shippable slice of it, a `task` is one agent's unit of work.

```
spec:     one, optional; omit it to add plans under a spec already in the database
plans:    a list; each carries its own tasks
  tasks:  a list; also allowed at top level, where the tasks hang off the spec
```

Every node takes `id`, `title`, `priority`, `ordinal`, `target_repo`, `acceptable_models`, `frontmatter`, `sections` and `depends_on`. `verifications` is read on tasks only, and only a task's `target_repo`, `acceptable_models` and `frontmatter` are ever acted on — on a plan they are recorded and nothing reads them.

| Field | What it means |
|:--|:--|
| `id` | Yours to choose and permanent. Prefix a child with its parent (`NOTIFY`, `NOTIFY-EMAIL`, `NOTIFY-EMAIL-SENDER`): it reads as a path and sorts with its siblings. |
| `title` | One line, what the change is. It is the whole of the task in a `tm next` listing. |
| `priority` | 1-100, default 50. Raise it to break a tie in `tm next`, not to express importance. |
| `ordinal` | Display order; the position in the list when omitted. |
| `target_repo` | The directory, under the tm root, the task's worktree is cut from. Per node and **not inherited**: set it on every task, not on the plan. |
| `acceptable_models` | Real model ids. See §3. |
| `frontmatter` | Free keys, rendered into the brief's frontmatter. See §2. |
| `sections` | A map of key to text. The key names the section; the order is the order they appear in the brief. See §2. |
| `depends_on` | Ids this node cannot start before. See §4. |
| `verifications` | Machine checks. See §5. |

Never write `kind`: position decides it. Write `status` only to record work that already exists (`WAITING_MERGE`, `COMPLETED`, `DEFERRED`); otherwise omit it and the run commands own it. Any key a document omits keeps the node's current value on a re-import.

## 2. Sections and frontmatter

Section keys, in this order where they apply: `objective` (what is true when it is done), `acceptance` (the checks a reviewer runs, one per line), `body` (how, where the how is not obvious), `context` (what the agent would otherwise have to go and read), `deferral` (why it is parked), `owed` (what this task deliberately leaves open), `attestation` (a signed claim for work no assertion can measure). The header is `## <Key>` unless a section is given as `{header, content, ordinal}` instead of plain text.

Put shared context on the **plan**. Only the direct parent's `context` and `overview` reach `tm render <id> --view subagent`; a spec's sections never do.

Frontmatter keys the estate reads:

- `declared_files`: every repo-relative path the task will create or modify. This is what the lease locks and what `tm next` filters on, so an unlisted file is a collision nobody sees and a listed file nobody touches is a task needlessly held out of a wave. Tests count.
- `soft_depends_on`: ids this task builds against a stub or a mock until they land. It creates no edge and holds nothing back; it tells the implementer what the stub is for.
- `external_blockers`: prose conditions outside the corpus (an approval, a credential, a third party). Nothing enforces them; they are what the dispatcher checks before claiming.
- `review_models`: who reviews this task, as model ids. Distinct from `acceptable_models`, which is who *implements* it: a task that is cheap to write can be expensive to check, and a migration, an RLS policy or a crypto boundary is reviewed on the strongest model whatever wrote it. Nothing in `tm` reads this; the dispatcher and the rendered brief do.
- `gate_lane`: where this task's own gate can run — a database or a real browser needs the estate's shared machine, anything CPU-light runs beside the session. It is a claim about this task's files, so its author owns it: a dispatcher re-deriving it per wave from the file list gets it wrong on exactly the tasks where it matters, and a gate in the wrong lane is either a wedged container or an hour of queueing for a two-file diff.

## 3. Models

`acceptable_models` is the whole of the routing decision: a task is delegable to a cheaper family exactly when a model of that family is listed. There is no separate flag.

An **empty list means every model**, not the strongest one: `tm next --model <any-id>` returns it. Where a task needs one family, list only that family.

## 4. Dependencies

`depends_on` holds real blockers as explicit ids, and a dependency is satisfied when it reaches `COMPLETED` or `SUPERSEDED`. There is no wildcard: a set is expanded and every member named. An id that is neither in the document nor already in the database refuses the whole import, and nothing is written:

```
import refused, nothing written: unknown ids ['DOES-NOT-EXIST']
```

Where the dependency is only about ordering an agent can work around, it is `soft_depends_on`, not an edge.

A cross-cutting hold — a question that gates several unrelated tasks at once, not a missing dependency — is a decision (`tm decision add "<question>" --option "a|Do X" --recommend a --blocks t1,t2`), and every task it holds `depends_on` it, never a transcript of its terms in a section. A transcript cannot lift when the decision does, and every agent already dispatched under it is still carrying the stale copy; a `depends_on` edge onto a decision lifts the moment it is answered or withdrawn, with no message to anybody, and the waiting tasks read `AWAITING_DECISION` in the meantime rather than a plain `BLOCKED` that gives no hint who moves it.

## 5. Verifications

A verification is the task's own proof. `file_exists`, `file_absent`, `symbol_signature` and `ast_export` take a repo-relative path in `target_path` and count towards `declared_files`. `test_command` puts a label in `target_path` and the command in `expected_pattern`, and counts towards nothing. `codegraph_query` **passes when `codegraph` is not installed**, so it never proves anything on its own.

A good check exits 0 exactly when this task's own deliverable exists: content this change makes true, never a path another task creates and never the whole suite. Write it, then run it once against the open task and watch it fail:

```
$ tm verify run NOTIFY-EMAIL-SENDER
symbol_signature  src/notify/email/sender.py  FAILED  File src/notify/email/sender.py missing
```

A check that passes before the work starts is not a check. `file_exists`, `file_absent`, `symbol_signature` and `ast_export` read the task's `target_repo` at `origin/main` (fetched first), never the working tree; a failed fetch fails the check rather than falling back to disk. A task with no `target_repo` still reads a tm-root-relative path off the working tree. Before the merge, check the unmerged branch with `tm verify run <id> --ref tm/<id>`, which reads that ref with no fetch. `test_command` still runs from the tm root's working tree, so write the command to run from there.

## 6. Size a task to one agent

One agent, one sitting, one branch: an objective of a sentence, acceptance of a handful of lines, and a `declared_files` list short enough that no sibling wants any of it. Two objectives joined by "and" are two tasks.

## 7. Provision the review and the merge on the task, not on the wave

A task's review, its fix rounds and its merge are stages of **that task**. Nothing in `tm` couples them to a sibling: the lease is per task, the locks are per task, and `depends_on` is the only thing that makes one task wait for another. So a plan is authored to let each task advance alone — one task merging while another is on its first fix round is the normal shape, not a special case.

What that costs the author is four lines per task:

- **`acceptance` is the review's brief.** One check per line, each one a reviewer can actually run, and each one about *this* task's own deliverable. "The suite is green" is not a check: it is equally true of every task in the plan, so it tells a reviewer nothing and cannot fail for this task's reasons.
- **`review_models` is who runs it.** Set it wherever checking is harder than writing, which is most migrations, every RLS or tenant-isolation change, and anything holding a key.
- **A merge precondition is an edge or an `external_blocker`, never a sentence.** A task that cannot merge until another lands says so in `depends_on`. One that cannot merge until something outside the corpus happens — a release tag cut, an approval, a credential — says so in `external_blockers`. Written into `acceptance` instead it reads as a review check, passes review, and is then discovered by the merge agent with the branch already built and the gate already spent.
- **Do not write the merge's outward effect on the task.** Whether the target repo deploys, applies or publishes on push is two live measurements — the workflow's job list and the current value of whatever variable gates it — and a copy on the task is wrong the day either changes. The merge agent measures it at merge time.

A task provisioned this way needs nothing from its siblings to move, which is exactly what lets a dispatcher run implement, review, fix and merge concurrently across a wave instead of in lockstep. A task that hides a precondition in prose forces the whole wave back into lockstep, because the only safe thing to do with it is wait.

## 8. Import it, then read it back

```
tm import --format yaml -f plan.yaml     # stdin when there is no -f
tm task list --yaml                      # every task with its resolved state
tm next -n 5 --yaml                      # what is claimable now, in order
tm render <task-id> --view subagent      # exactly what the implementer will be handed
tm export docs/tm/                       # sorted, timestamp-free text, for version control
```

Read the brief before dispatching anyone: a section you meant to write and did not is invisible in the database and obvious here.

## 9. Amend a landed document, or re-import it

While you are still authoring, re-importing the same document is safe: it refuses before writing when an id is unknown, adds no verification twice, and a node keeps its current status, priority, models and frontmatter for every key the document does not state (a key it does state is overwritten, so state `status` only where you mean it). A task whose `verifications` a document states has exactly those afterwards, so a corrected check replaces the stale one; `tm verify list <id>` shows each check's id and `tm verify remove <id> <verification-id>` deletes one. Once work has landed, change it with:

```
tm task update <id> --title ... --priority ... --models a,b --repo <dir>
tm section set <id>:<key> --file <path> --header "## Context"
tm verify add <id> --type test_command --target api-suite --pattern "pytest tests/notify/test_api.py -q"
```

Change a task's dependencies later with `tm task depends <id> --add a,b --remove c`: it refuses an unknown id or a cycle and then writes nothing. Change `declared_files` later with `tm task update <id> --set 'declared_files=["path", ...]'`; a `file_exists` verification for a path joins the same list.

## Worked example

Imports as written, and `tm next` returns `NOTIFY-EMAIL-SENDER` first.

<!-- tm:example -->

```yaml
spec:
  id: NOTIFY
  title: Outbound notifications
  sections:
    context: One service sends every outbound message; delivery and retries are its own concern.

plans:
  - id: NOTIFY-EMAIL
    title: Email channel
    priority: 60
    sections:
      context: SMTP only. The provider client is injected, so no test opens a socket.
    tasks:
      - id: NOTIFY-EMAIL-SENDER
        title: SMTP sender with retry
        priority: 80
        target_repo: backend
        acceptable_models: [claude-sonnet-4-6, gemini-3.8-flash-high]
        frontmatter:
          declared_files: [src/notify/email/sender.py, tests/notify/test_sender.py]
        sections:
          objective: "`send(message)` delivers through the injected SMTP client."
          acceptance: |
            - A transient failure is retried three times, then raises `SendFailed`.
            - A permanent failure raises `SendFailed` without a retry.
          body: Take the clock as an argument so the retry test does not wait.
        verifications:
          - type: symbol_signature
            target_path: src/notify/email/sender.py
            expected_pattern: "def send(self, message: Message) -> SendResult"
          - type: test_command
            target_path: sender-suite
            expected_pattern: pytest tests/notify/test_sender.py -q

      - id: NOTIFY-EMAIL-TEMPLATES
        title: Template rendering
        target_repo: backend
        acceptable_models: [claude-sonnet-4-6, gemini-3.8-flash-high]
        frontmatter:
          declared_files: [src/notify/email/templates.py, tests/notify/test_templates.py]
          soft_depends_on: [NOTIFY-EMAIL-SENDER]
        sections:
          objective: "`render(template_id, payload)` returns a subject and a body."
          acceptance: An unknown template id raises `UnknownTemplate`, never renders an empty span.
          context: Until the sender lands, type the message against a local stub.
        verifications:
          - type: test_command
            target_path: templates-suite
            expected_pattern: pytest tests/notify/test_templates.py -q

      - id: NOTIFY-EMAIL-API
        title: POST /notifications/email
        target_repo: backend
        depends_on: [NOTIFY-EMAIL-SENDER, NOTIFY-EMAIL-TEMPLATES]
        acceptable_models: [claude-sonnet-4-6]
        frontmatter:
          declared_files: [src/notify/api/routes.py, tests/notify/test_api.py]
          external_blockers: [the provider account is approved for outbound SMTP]
        sections:
          objective: The route renders a template and hands the message to the sender.
          acceptance: A body missing `template_id` answers 422; an unknown template answers 404.
          owed: Rate limiting per recipient is not in this task.
        verifications:
          - type: test_command
            target_path: api-suite
            expected_pattern: pytest tests/notify/test_api.py -q
```

## Never

- Never import the same document twice; amend in place.
- Never leave a task's `declared_files` unwritten, and never list a file two open tasks both claim.
- Never write a verification that passes before the work starts.
- Never state a dependency in prose: an id in `depends_on`, or it does not exist.
- Never write `status` except to record work that already exists, and never describe a set of blockers instead of naming them.
- Never write a merge precondition as prose in `acceptance`: it is a `depends_on` edge or an `external_blocker`.
- Never write a task whose review or merge depends on a sibling finishing first unless that sibling is in `depends_on`.
