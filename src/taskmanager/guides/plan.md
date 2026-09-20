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

Never write `kind` or `status`: position decides the first, and the run commands own the second.

## 2. Sections and frontmatter

Section keys, in this order where they apply: `objective` (what is true when it is done), `acceptance` (the checks a reviewer runs, one per line), `body` (how, where the how is not obvious), `context` (what the agent would otherwise have to go and read), `deferral` (why it is parked), `owed` (what this task deliberately leaves open), `attestation` (a signed claim for work no assertion can measure). The header is `## <Key>` unless a section is given as `{header, content, ordinal}` instead of plain text.

Put shared context on the **plan**. Only the direct parent's `context` and `overview` reach `tm render <id> --view subagent`; a spec's sections never do.

Frontmatter keys the estate reads:

- `declared_files`: every repo-relative path the task will create or modify. This is what the lease locks and what `tm next` filters on, so an unlisted file is a collision nobody sees and a listed file nobody touches is a task needlessly held out of a wave. Tests count.
- `soft_depends_on`: ids this task builds against a stub or a mock until they land. It creates no edge and holds nothing back; it tells the implementer what the stub is for.
- `external_blockers`: prose conditions outside the corpus (an approval, a credential, a third party). Nothing enforces them; they are what the dispatcher checks before claiming.

## 3. Models

`acceptable_models` is the whole of the routing decision: a task is delegable to a cheaper family exactly when a model of that family is listed. There is no separate flag.

An **empty list means every model**, not the strongest one: `tm next --model <any-id>` returns it. Where a task needs one family, list only that family.

## 4. Dependencies

`depends_on` holds real blockers as explicit ids, and a dependency is satisfied when it reaches `COMPLETED` or `SUPERSEDED`. There is no wildcard: a set is expanded and every member named. An id that is neither in the document nor already in the database refuses the whole import, and nothing is written:

```
import refused, nothing written: unknown ids ['DOES-NOT-EXIST']
```

Where the dependency is only about ordering an agent can work around, it is `soft_depends_on`, not an edge.

## 5. Verifications

A verification is the task's own proof. `file_exists`, `file_absent`, `symbol_signature` and `ast_export` take a repo-relative path in `target_path` and count towards `declared_files`. `test_command` puts a label in `target_path` and the command in `expected_pattern`, and counts towards nothing. `codegraph_query` **passes when `codegraph` is not installed**, so it never proves anything on its own.

A good check exits 0 exactly when this task's own deliverable exists: content this change makes true, never a path another task creates and never the whole suite. Write it, then run it once against the open task and watch it fail:

```
$ tm verify run NOTIFY-EMAIL-SENDER
symbol_signature  src/notify/email/sender.py  FAILED  File src/notify/email/sender.py missing
```

A check that passes before the work starts is not a check. Every path resolves against the tm root, so write the command to run from there.

## 6. Size a task to one agent

One agent, one sitting, one branch: an objective of a sentence, acceptance of a handful of lines, and a `declared_files` list short enough that no sibling wants any of it. Two objectives joined by "and" are two tasks.

## 7. Import it, then read it back

```
tm import --format yaml -f plan.yaml     # stdin when there is no -f
tm task list --yaml                      # every task with its resolved state
tm next -n 5 --yaml                      # what is claimable now, in order
tm render <task-id> --view subagent      # exactly what the implementer will be handed
tm export docs/tm/                       # sorted, timestamp-free text, for version control
```

Read the brief before dispatching anyone: a section you meant to write and did not is invisible in the database and obvious here.

## 8. Amend, never re-import

**Re-importing a document that already landed resets every node's status to `NOT_STARTED`**, silently discarding the progress under it. Change landed work with:

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
- Never write `status`, and never describe a set of blockers instead of naming them.
