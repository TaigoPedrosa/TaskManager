# Writing a plan

For the agent authoring new work: turn an intent into a document `tm import` accepts, so every node is claimable, lands where it should and is provable without asking its author anything.

## 1. Write the document

One YAML file (`--format json` and `markdown` parse the same shape). A `spec` is the standing intent, a `plan` is a shippable slice of it, a `task` is one agent's unit of work.

```
spec:     one; required for any plan not already under a spec
plans:    a list; each carries its own tasks
  tasks:  a list; also allowed at top level, where the tasks hang off the spec
```

To add plans under a spec already in the database, write `spec: {id: <spec-id>}`: a spec named by its id alone keeps its title, its other fields and its sections, and the plans land under it. Without `spec:`, a plan already under a spec stays there, and a document holding any other plan is refused, since that plan would belong to no spec.

Every node takes the fields below; `verifications` and `target_repo` act only on tasks, and `land_order` only on plans and specs.

| Field | What it means |
|:--|:--|
| `id` | Yours to choose and permanent. Prefix a child with its parent (`NOTIFY`, `NOTIFY-EMAIL`, `NOTIFY-EMAIL-SENDER`): it reads as a path and sorts with its siblings. |
| `title` | One line, what the change is. |
| `priority` | 1-100, default 50. Raise it to break a tie in discovery, not to express importance. |
| `ordinal` | Display order; the position in the list when omitted. |
| `target_repo` | The directory, under the tm root, the task's branch is cut in; `.` when the tm root is itself the repository. Per node and **not inherited**: set it on every task. A task without one cannot be implemented. `tm import`, `tm task add --repo` and `tm task update --repo` refuse a directory that is not a git working tree under the tm root, and name the ones that are. |
| `acceptable_models` | Real model ids. See §3. |
| `review`, `fix`, `merge` | How the node reaches its spec's target branch. See §4. |
| `requires` | Capabilities the agent needs, such as `figma`. See §3. |
| `conditions` | States outside the corpus the node waits on, each with its command. See §5. |
| `land_order` | On a plan or spec: the order its repositories land in. See §4. |
| `frontmatter` | Free keys, rendered into the brief's frontmatter. See §2. |
| `sections` | A map of key to text, in the order they appear in the brief. See §2. |
| `depends_on` | Ids this node cannot start before. See §5. |
| `verifications` | Machine checks. See §6. |

Never write `kind`: position decides it. Never write `status`: a new node is `READY`, and only the claim and its verbs move it. Any key a document omits keeps the node's current value on a re-import.

## 2. Sections and frontmatter

Section keys, in this order where they apply: `objective` (what is true when it is done), `acceptance` (the checks a reviewer runs, one per line), `body` (how, where the how is not obvious), `context` (what the agent would otherwise have to go and read), `attestation` (a signed claim for work no assertion can measure). The header is `## <Key>` unless a section is given as `{header, content, ordinal}` instead of plain text. tm writes `:report`, `:review`, `:merge`, `:reopen` and `:deferral` itself, or its agents do.

Work this node deliberately leaves open is never a section: it is its own spec, plan or task, with `depends_on` naming this node so it waits on it instead of hiding in prose nobody discovers. tm refuses a section keyed `owed` and says so.

Put shared context on the **plan**. Only the direct parent's `context` and `overview` reach `tm render <id> --view subagent`; a spec's sections never do.

Frontmatter keys the estate reads:

- `declared_files`: every repo-relative path the task will create or modify. This is what an implement or fix claim locks and what discovery keeps disjoint, so an unlisted file is a collision nobody sees and a listed file nobody touches holds a task out of a wave for nothing. Tests count.
- `review_models`: who reviews this node, as model ids; tm routes the review to that family. A task that is cheap to write can be expensive to check, and a migration, a row-level security policy or a crypto boundary is reviewed on the strongest model whatever wrote it.
- `sensitive`: where a fix of this node is re-reviewed before it lands, as one of the areas `tm config` key `sensitive_areas` lists (`tenant`, `rls`, `crypto` and `migration` unless set), or a list of them: `sensitive: migration`, `sensitive: [tenant, rls]`. tm refuses any other name. A node that writes a migration, a path matching one of its repository's `repos.<repo>.migrations` globs (`**/migrations/**` among them unless set) in its `declared_files` or in a step's commits, is sensitive without the key. A sensitive node's fix gets one re-review, scoped to its open findings; every other fix lands without one.
- `soft_depends_on`: ids this task builds against a stub until they land. It creates no edge and holds nothing back; it tells the implementer what the stub is for.
- `gate_lane`: where this task's own gate can run. It is a claim about this task's files, so its author owns it.
- `land_on`: on a spec only, the branch every node under it lands on, such as `feature/notify`, `release/2.4` or `fix/login-timeout` (§4). tm refuses it on a plan or task, and refuses a name `git check-ref-format --branch` rejects. On a spec already in the database, `tm task update <spec-id> --set land_on=<branch>`; on a new one, `tm spec add "<title>" --land-on <branch>`.

## 3. Models and capabilities

`acceptable_models` decides the implement route: tm takes the cheapest family listed. An **empty list means every model**, not the strongest one. tm routes on the Claude families `haiku`, `sonnet`, `opus` and `fable`, read from each id: an id naming none of them is ignored, so a list of only such ids routes as an empty one. Reviews, fixes and landings follow from it and from `review_models`, as `tm guide dispatch` lists; tm prints the model family with every claim.

`requires` names what the agent must be able to reach — `figma` for a node read against a design frame, say. The dispatcher routes the node to an agent type that serves it, or to the default agent, which reaches every connected tool.

## 4. Where a node lands: `review`, `fix`, `merge`

Every spec lands on a target branch, and so in the end does every node under it. Its `land_on` (§2) names that branch, and that is the normal case: a spec's work belongs on a feature, release or fix branch of its own, and tm lands it there and goes no further. Taking that branch on to environments and to `main` is the developer's. A spec with no `land_on` lands on `repos.<repo>.default_branch` in each repository it touches, `main` unless the project sets it. A target origin does not have yet is cut from that default branch, and the first landing on it creates it with its push. `tm task get <id> --yaml` prints the branch a node's chain lands on as `lands_on`.

- `merge: spec` (the default, except under a reviewed plan or spec) cuts the node's branch from `origin/<target>` and lands it on its spec's target. A spec's own `merge` is `spec`, and `main` is a branch, never a `merge` value.
- `merge: parent` cuts it from the branch of the plan or spec above it, `tm/<parent-id>`, and lands it there. It reaches the spec's target only when that parent lands. A spec cannot land on a parent.
- `review` puts a review after implement, or for a plan or spec, after its landing; `fix` makes this node fix its own rejections, and needs `review`. A task has both on unless the document says otherwise, and a plan or spec has both off. Children under a reviewed plan or spec take `review: false` and `fix: false` by default, and `merge: parent`, because its one review covers what lands on its branch; a sensitive child (§2) keeps `review` and `fix` on, and an explicit flag still wins. A child there with `review` off and `merge: spec` is refused: its code would reach the target unreviewed.

One wait never joins two targets. A `depends_on` edge from a node whose spec lands on one branch to a node whose spec lands on another is refused where it is written (an import, `tm task depends`, the web), naming both branches, and so are two open migration writers in one repository landing on different targets. Remove the edge, land one first, or land both on one target.

Two shapes cover most work:

- **Each task reviewed and landed alone.** Tasks keep the defaults and land on the spec's target; the plan is a grouping only.
- **One review for the whole plan.** Tasks carry `merge: parent` and no review of their own, and the plan carries `review: true` and `fix: true`. Once every task has landed on the plan's branch, the plan lands on its own target and reads `LANDED`: its code is there, its one review owed. That review reads the landing; an approval completes the plan, and a rejection is fixed on a branch cut from the target, which lands without a second review unless the plan is sensitive. A task may keep its own review with `review: true` and `fix: false`: a rejection then lands on the plan's branch unfixed, and the plan's review is where it gets fixed. tm refuses `review` without `fix` anywhere else, because a rejection nobody below fixes must land where a review above will see it.

A plan or spec that touched several repositories lands them one at a time, in `land_order` (else the project's `repo_order`). A container whose tasks changed nothing has its code on its target already: it completes, or with `review` on reads `LANDED` and still takes its one review. One whose counted tasks name no `target_repo` has no repository to show that in, so it stays `IMPLEMENTED` rather than complete on a claim nothing proves; once its verification passes, complete it by hand with `tm task reset <id> --to COMPLETED --note "<why nothing lands>"`.

## 5. What a node waits on

`depends_on` holds real blockers as explicit ids, each a node or a decision. There is no wildcard and no status gate: an edge to a node is satisfied once that node's code has landed on a branch this node builds on, and an edge on a plan holds every task in it. An id that is neither in the document nor already in the database refuses the whole import, and nothing is written:

```
import refused, nothing written: unknown ids ['DOES-NOT-EXIST']
```

A question that gates several unrelated nodes at once is a decision (`tm decision add "<question>" --option "a|Do X" --recommend a --blocks t1,t2`) that each of them depends on, never a transcript of its terms in a section: an edge onto a decision lifts the moment it is answered, and every node waiting on it reads `AWAITING_DECISION` meanwhile.

A state outside the corpus — an approval, a credential, a tag another repository must cut — is a **condition**: `{needs, command, stage}`, where `command` exits 0 once the state holds and `stage` is `claim` (the default: nothing starts until it holds) or `landing` (the work proceeds, and only the landing waits). A condition with no runnable command is refused: that is a decision.

Every write is checked against a graph of each node's start, implement and landing, and refused when it would close a cycle, with the cycle printed as a path:

```
A.start ← P.landed ← P.implemented ← A.landed ← A.start
```

The usual cause is an edge from a container to its own child, or between two children that land on each other's parent.

## 6. Verifications

A verification is the node's own proof. `file_exists`, `file_absent`, `symbol_signature` and `ast_export` take a repo-relative path in `target_path` and count towards `declared_files`. `symbol_signature` and `ast_export` parse the file as Python and fail on any other language; for a symbol in another language, use a `test_command` or a `codegraph_query`. `test_command` puts a label in `target_path` and the command in `expected_pattern`, and counts towards nothing. `codegraph_query` puts a search in `target_path` and a regex in `expected_pattern`, and counts towards nothing: tm runs `codegraph query --json` for that search and passes when the output matches the regex. `codegraph_query_json` optionally carries the query's other flags as a JSON object, such as `{"kind": "function", "limit": 20}`, set with `--query-json` on `tm verify add`. tm refuses a `codegraph_query` whose `expected_pattern` is missing or is not a regex, and query flags that are not a JSON object. It fails where it cannot run: without the `codegraph` CLI, or when the target repo's checkout has no `.codegraph/` index, which `codegraph init` there creates.

A good check exits 0 exactly when this task's own deliverable exists: content this change makes true, never a path another task creates and never the whole suite. Write it, then run it once against the open task and watch it fail:

```
$ tm verify run NOTIFY-EMAIL-SENDER --ref tm/NOTIFY-EMAIL-SENDER
symbol_signature  src/notify/email/sender.py  FAILED  File src/notify/email/sender.py missing
```

The path checks and `codegraph_query` read a ref of the task's `target_repo` and never a working tree: with no `--ref`, `origin/<the branch its spec lands on>`, fetched first; `--ref` names another. codegraph indexes a directory, so tm exports the ref's commit to `.taskmanager/cache/codegraph/<sha>`, indexes it there once, and every later query at that commit reuses it. The cache keeps the `codegraph.cache_commits` (3) most recently queried commits, and indexing another deletes the least recently queried. `test_command` runs from the tm root with that ref in `TM_VERIFY_REF`, and a landing sets it to the landing target — the parent's branch for `merge: parent` — so a command reads `"${TM_VERIFY_REF:-origin/main}"` instead of naming a branch; tm refuses one that names `origin/main` itself on a task read anywhere else: one landing on its parent, or under a spec whose target is not `main`. The target repository is a directory under the tm root, and the ref is not checked out anywhere, so a command that runs code checks the ref out itself first, as the worked example's do.

A plan with `review: true` carries a verification of its own that runs its children's joined behaviour, the one test that exercises them together, so its review has a check beyond the children's suites.

## 7. Size a task to one agent

A task's deliverable is code or an artifact that must land. A question whose answer is a ruling is a `tm decision add`. A measurement is one read-only agent whose result goes into that decision's context or a section, with no implement, review or fix cycle. Before filing either, look for the answer where it may already be: an agent's report, a section, an earlier decision. When it exists, raise the decision with that data in its context.

Scale review and fix to what the deliverable risks: a document or research deliverable is `review: false`, or not a task at all.

Two small changes to one file from one finding are one task, not two tasks serialized on that file with a review cycle each.

One agent, one sitting, one branch: an objective of a sentence, acceptance of a handful of lines, and a `declared_files` list short enough that no sibling wants any of it. Two objectives joined by "and" are two tasks.

## 8. Write the review into the node

- **`acceptance` is the review's brief.** One check per line, each one a reviewer can actually run, and each about *this* node's deliverable. "The suite is green" is not a check.
- **`review_models` is who runs it.** Set it wherever checking is harder than writing.
- **A landing precondition is an edge or a condition, never a sentence.** Written into `acceptance` it reads as a review check, passes review, and is found only when the landing is already under way.
- An acceptance that something is left unchanged names its measurement: the file's bytes or sha256 before and after, never a field or two read back.
- An invariant or a refusal in acceptance names where it holds: the one function every write or command passes through, or each path by name, with a check for each. An invariant over two fields (a status and a flag, a default and every path that creates the node) names the writes of both fields, not only the one the task touches. A changed shape names every caller.
- A limit or default a spec states that a tm config key covers is written as the project's key, never a number.
- An acceptance that one thing matches another ("matches `tm wave discover`", "byte-identical to before") lists every input the reference reads (conditions, locks, config keys such as `repo_order`) and has a test holding each.
- A task that replaces or removes something names each thing that goes (the implementations one now replaces; a feature's markup, handlers, styles and the state it reset) and everything that loses its last reader with it (components, props, model fields, imports, and computations or loads whose only consumer was the removed code), and deletes each one in the same task, or moves it to where it is still used; its acceptance lists them, each with a check that fails when it comes back.
- Tasks that write into one directory each declare their own paths, and none deletes a path it did not declare.
- A task whose own code runs work concurrently names every file or row two workers write, and how those writes serialize. Concurrency across tasks is what `declared_files` and discovery already keep disjoint.
- A task that consumes another task's derived structure (ids, an ordering, a mapping) names that task and reads its output. It never re-derives the structure. A format one task writes and another reads (an id, a URL key, a file or message shape) is listed in the plan's `context` with every writer and every reader by file, and each reader's acceptance carries its round trip: write it, reload or re-read it, and get back the same thing.
- A brief that has the implementer step or search over a value names that value's allowed range.
- A UI task's acceptance names each interaction's behaviour, not only its look: the feedback for every write, where focus lands after it, the keyboard route to every pointer action, what a live update does to a field mid-edit, what survives a reload, and how a reviewer reaches each state (a route, a fixture). Each line has a check that drives it. A panel over lazily loaded data names each of its states (loading, partial, empty, error, ready) and the reads each state depends on.
- A brief never copies a figure from a source still under review or still being measured; it names the source, and the implementer reads the current value.
- A brief that places work relative to a third-party artifact (a basemap style's layers, an API's ordering, a vendor file's structure) states that artifact's actual contents, read before the brief is written, never an assumed order.
- A test of behaviour runs the code it tests: it calls the function, drives the page's scripts, or runs the command, and asserts what comes out. Searching the source for a call or a string is not a test of behaviour, even when it fails once the line is deleted.
- A test that checks a generated artifact against its generator fails, never skips, when the generator is missing: a skipped check reads as a pass in every gate that runs it.

## 9. Import it, then read it back

```
tm import --format yaml -f plan.yaml
tm task list --yaml
tm wave discover --session <id> --slots 5 --max-strong 2
tm render <task-id> --view subagent
tm export docs/tm/
```

Read the brief before dispatching anyone: a section you meant to write and did not is invisible in the database and obvious here.

## 10. Amend it

While you are still authoring, re-importing the same document is safe: it refuses before writing when an id is unknown, a flag rule breaks or a cycle closes, and adds no verification twice. A task whose `verifications` a document states has exactly those afterwards. Once work has started, change it in place:

```
tm task update <id> --title "<title>" --priority 60 --models a,b --repo <dir>
tm task update <id> --review --no-fix --merge parent --requires figma
tm task update <plan-id> --land-order core,web
tm task depends <id> --add a,b --remove c
tm task condition add <id> --needs "<what must hold>" --command "<check>" --stage landing
tm task condition remove <id> <idx>
tm section set <id>:<key> --file <path> --header "## Context"
tm verify add <id> --type test_command --target api-suite --pattern "<command>"
tm verify list <id>
tm verify remove <id> <verification-id>
```

A section is rewritten in place, never extended with a second generation: `tm section get <id>:<key>`, edit it, set the whole back, with every sentence the change overrides gone. Only `:report`, `:review` and `:merge` grow by appended entries.

A ruling (a decision's answer, or a fix round's instruction) that changes a landed task's approach updates that task's verifications in the same step: `tm verify list <id>`, then `tm verify remove` for each row that checks the old approach, then `tm verify add` for the new one. A verification still checking the old approach fails the plan's landing on a green fix.

A change to where a node lands once its branch exists, through its `merge` or its spec's target, is refused unless that branch was cut from the new target: code cut from a plan's branch must never land on the spec's target carrying the plan's unreviewed work. Set the branch aside and start a new one first; `reopen` takes only a deferred, abandoned or failed node, so defer it before reopening, and wait for (or stop) a step in progress before deferring. A completed node has landed and a superseded one is carried by its replacement: neither is set aside, so file a new task, or change where the replacement lands.

```
tm task defer <id> --note "<why>"
tm task reopen <id> --note "<why>" --new-branch
tm task update <id> --merge spec
```

## Worked example

Imports as written: every task lands on the plan's branch with no review of its own, the plan lands on the spec's `land_on`, `feature/notify`, and is reviewed there once, and `tm wave discover` offers `NOTIFY-EMAIL-SENDER` and `NOTIFY-EMAIL-TEMPLATES` first.

<!-- tm:example -->

```yaml
spec:
  id: NOTIFY
  title: Outbound notifications
  frontmatter:
    land_on: feature/notify
  sections:
    context: One service sends every outbound message; delivery and retries are its own concern.

plans:
  - id: NOTIFY-EMAIL
    title: Email channel
    priority: 60
    review: true
    fix: true
    sections:
      context: SMTP only. The provider client is injected, so no test opens a socket.
    tasks:
      - id: NOTIFY-EMAIL-SENDER
        title: SMTP sender with retry
        priority: 80
        target_repo: backend
        merge: parent
        acceptable_models: [claude-sonnet-5-5]
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
            expected_pattern: >-
              d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT &&
              git -C backend archive "${TM_VERIFY_REF:-origin/main}" | tar -x -C "$d" &&
              cd "$d" && pytest tests/notify/test_sender.py -q

      - id: NOTIFY-EMAIL-TEMPLATES
        title: Template rendering
        target_repo: backend
        merge: parent
        acceptable_models: [claude-sonnet-5-5]
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
            expected_pattern: >-
              d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT &&
              git -C backend archive "${TM_VERIFY_REF:-origin/main}" | tar -x -C "$d" &&
              cd "$d" && pytest tests/notify/test_templates.py -q

      - id: NOTIFY-EMAIL-API
        title: POST /notifications/email
        target_repo: backend
        merge: parent
        depends_on: [NOTIFY-EMAIL-SENDER, NOTIFY-EMAIL-TEMPLATES]
        acceptable_models: [claude-sonnet-5-5]
        conditions:
          - needs: the provider account is approved for outbound SMTP
            command: test -f /etc/notify/smtp-approved
            stage: landing
        frontmatter:
          declared_files: [src/notify/api/routes.py, tests/notify/test_api.py]
        sections:
          objective: The route renders a template and hands the message to the sender.
          acceptance: A body missing `template_id` answers 422; an unknown template answers 404.
        verifications:
          - type: test_command
            target_path: api-suite
            expected_pattern: >-
              d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT &&
              git -C backend archive "${TM_VERIFY_REF:-origin/main}" | tar -x -C "$d" &&
              cd "$d" && pytest tests/notify/test_api.py -q
```

## Never

- Never import the same document twice to change a landed node; amend it in place.
- Never leave a task's `declared_files` unwritten, and never list a file two open tasks both claim.
- Never write a verification that passes before the work starts.
- Never state a dependency or a precondition in prose: an id in `depends_on`, a decision, or a condition with its command.
- Never write `status`.
- Never give a node `review` without `fix` unless it lands on a parent that reviews and fixes.
- Never write an `owed` section: owed work is its own node, with `depends_on` naming the node
  that owed it; tm refuses the section.
