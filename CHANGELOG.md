# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- An implement is not claimed while another started node that declares one of its files has code
  that has not reached a branch it builds on. A lease locks files only while its step runs, so a
  waiter claimed during the holder's review or landing was cut without the holder's change.

## [0.3.13] - 2026-10-16

### Fixed

- tm-wave accepts a checked transcript whose runner added blank lines before `__CHECK` or
  `__EXIT`, instead of releasing a valid claim as unreadable and counting a step failure.

## [0.3.12] - 2026-10-16

### Changed

- The implement and fix guides have the agent delete each guard, bound, filter and escape in its diff
  one at a time before closing and confirm a test turns red, with null, empty and whitespace as
  separate inputs and a filter a mock stands in for tested through the real component.

## [0.3.11] - 2026-10-16

### Changed

- An implement or fix claim on a repository with no codegraph index runs `codegraph init -y` on it
  first (once, even when claims race), and a failure or timeout prints `unavailable (...)` without
  blocking the claim.
- A review claim prints `codegraph: ready <path>` for an index of the commit under review, from
  the cache `codegraph_query` verifications use, and `tm-wave` forwards it in the review brief.
- `tm guide implement|fix|review|plan` carries its codegraph block whenever the `codegraph` CLI is
  on PATH, indexed or not. The blocks and `tm guide codegraph` teach the CLI only: `explore`
  first, `callers` and `impact` before a change, `affected` for the tests to run first, and when
  to `sync`.
- `tm doctor` recommends `codegraph init -y <repo>` for a missing index, as do the init and design
  skills, and the dispatcher and taskmanager skills point agents to `tm guide codegraph`.
- The README says tm uses codegraph as a CLI only and never wires its MCP server.

## [0.3.10] - 2026-10-15

### Upgrading

- An embedding key is sent only from a variable whose name starts with `TASKMANAGER_`, over https
  or to localhost, and only to api.openai.com, localhost or the URL `TASKMANAGER_OPENAI_BASE_URL`
  names. A config whose `embeddings.api_key_env` or `embeddings.base_url` breaks this still loads,
  but a `tm index` or `tm search` that would send the key refuses with the reason: rename the
  variable, or set `TASKMANAGER_OPENAI_BASE_URL` in your own environment.
- `tm spec|plan|task|decision add` and `tm import` refuse a new slug or id that is not one plain
  path component (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `..`). A stored id is still read and updated.

### Changed

- `tm decision withdraw` refuses a decision that is not open (409).
- `tm task add` and `tm plan add` refuse a parent of the wrong kind.
- `tm supersede --transfer-blocks` refuses a value that is not `all`, `none` or nodes waiting on
  the superseded one.
- Files added with `tm task update --set declared_files=...` on a claimed node are locked, or the
  update is refused when another live node holds them.
- `tm config unset repos.<repo>.gates.<gate>.command` removes a gate that has no other field.
- A `test_command` verification is killed with its child processes after 600 seconds and fails.
- `tm verify run` with no `--ref` reads the repository's default branch while the spec's
  `land_on` branch does not exist yet, for path checks, codegraph queries and test commands.
- `install.sh` repoints or removes only a `~/.gemini/extensions/taskmanager` link it made; an older
  install's link to a `--from` checkout is left for you to remove.

### Fixed

- Concurrent adds pick their id inside the write transaction, so one can no longer overwrite the
  other, and an auto-numbered task skips the id of a task moved out of its plan.
- Answering, reopening or withdrawing a decision, attaching and detaching files, `tm task update`,
  `supersede` and `move` read the node under the write lock, so concurrent changes are no longer
  lost.
- Adding a condition and sweeping expired leases hold the write lock from their read to their
  write, and a repository write that raises rolls back its open transaction, so a web worker
  thread no longer holds the SQLite write lock.
- `tm task complete` and `tm task review` re-check the lease under the write lock, so a lease that
  lapsed and was claimed again is never overwritten or deleted.
- `config.yaml` is replaced atomically, and `tm config set`/`unset` and the config `tm restore`
  writes serialize, so a concurrent reader never sees defaults and no writer loses another's
  change.
- `tm detach` never deletes a file outside the assets directory, and `tm attach` names a file with
  no usable extension `<hash>.bin`.
- Abandoning or deferring a decision's blocked nodes no longer opens a stranded-dependents
  decision on a node that same answer rules.
- A refused `tm task release --blocked` no longer expires or stops the node's landing job.
- A refused `tm task reopen --new-branch`, or one whose later repository fails, puts back the
  branches and worktrees it retired.
- A claim or a reset to landed is refused when `git fetch` fails and only a stale tracking ref
  remains; a `land_on` branch the remote deleted reads as the default branch again, and its stale
  tracking ref is dropped. A refused container claim across several repositories leaves no
  worktree.
- A note appended to a section keeps the section's custom header.
- Wave selection holds a node whose files overlap a container fix, using the claim's own lock set.
- A landing that conflicts while merging a moved target records the moved sha as its baseline, and
  a baseline gate run that timed out or failed outside the tests is no longer cached.
- A landing merge that fails without a conflict stops for an agent with git's error.
- A resume that loses the job swap no longer deletes the winner's worktree; a landing stopped from
  outside removes its worktrees and kills its gate's process group; a landing recovers from a
  worktree deleted by hand; one failed heartbeat no longer stops the later ones; gate reports are
  found under a worktree path holding `[`, `*` or `?`.
- `tm init` refuses a stored repo whose clone is gone, and migration globs match non-ASCII paths.
- `tm export` names plan and spec files by percent-encoded id, writes nothing until every document
  is built, and removes an earlier export's json files from its directory; `tm restore` is all or
  nothing and copies assets only after the import commits.
- `tm section set <id>:<key>` with no content refuses instead of writing an empty section, and
  `tm section get` prints an id holding `[...]` verbatim.
- `tm verify run --json` and `--yaml` keep stdout clean for a task with no verifications.
- A markdown `tm import` refuses a file with text below its frontmatter rather than dropping it.
- tm-wave briefs a single-repository container claim with that repository's worktree, reads the
  landings of nested plans when reviewing a container, never re-sends a claim a runner already ran,
  releases a step whose brief or agent throws, and names a review's scratch worktree after the
  claim.
- The web records the `X-TM-Actor` actor for reopen, reset, defer, abandon and lease release and
  sweep, keeps live updates going after a failed refresh, stops rebuilding on every tick once a
  condition or lease has expired, and delivers a broadcast and a concurrent subscribe reply in
  order.
- A served file's `Content-Disposition` carries non-ASCII and quoted filenames, and an attachment
  filename holding a NUL byte or over 255 bytes is refused with a 400.
- The static export escapes every `<` in its embedded data, and draws the Decisions badge on load.
- A cancelled or failed confirm dialog no longer runs its action against the next node opened with
  the same title, and a reopened edit dialog shows the node as it is now.
- The edit dialog sends only the frontmatter fields you changed, and Flags on a plan or spec read
  the full node first, so a save no longer clears `land_order` or `land_on`.
- Creating a node selects it through its location and never navigates later on its own.
- API validation failures show their messages instead of `[object Object]`, and a bar for a node
  with only set-aside tasks draws.
- A refused decision answer, withdrawal or effect confirm no longer leaves the decision marked as
  your own write, and a late "Load more" no longer overwrites another tab's list.
- A live status push no longer discards the wave size you are typing, and Enter and Space on a
  filter row's Include and Exclude buttons reach the button.
- An expanded row the server drops and sends back is watched again, and collapsing a node in the
  tree no longer freezes the drawer showing it.
- The init skill reads each repository's remote from `repos.<repo>.remote` and single-quotes
  tracker text in the commands it runs; `uninstall` removes tm however long `uv tool list`'s output
  is; the README, plan skill, merge guide and plan guide match the code.

## [0.3.9] - 2026-10-15

### Upgrading

- `repos.<repo>.remote` accepts only letters, digits, `.`, `_`, `/` and `-`, the characters
  tm-wave quotes into a git command. A config naming any other remote refuses to load: rename the
  remote with `git remote rename` and set the key to the new name.

### Added

- A claim prints `remotes`, naming each repository's `repos.<repo>.remote`, null for one with no
  remote.

### Changed

- `tm decision add --slug` and the web's New decision refuse a slug holding `/`.

### Fixed

- A tm-wave review or fix brief reads the spec's target on each repository's own remote, and the
  local branch where `repos.<repo>.remote` is null, instead of `origin/<target>`. The plan skill
  proves a task with `tm verify run <task-id>`, which reads the target the same way.
  ([#1](https://github.com/TaigoPedrosa/TaskManager/issues/1))
- A red-target decision's id replaces the `/` of its repository's path, and the web pane opens,
  answers, reopens, withdraws and links a decision whose stored id holds one. An open red-target
  decision stored that way keeps its parked landings rather than opening a second one.
  ([#1](https://github.com/TaigoPedrosa/TaskManager/issues/1))
- A claim of a child landing on its parent's existing branch never reads the remote from git, so
  a remote the clone lacks no longer refuses it after its worktree is cut.
- `install.sh` creates `~/.gemini/extensions` when it is missing and links into it, and
  `uninstall` removes that folder only when install created it.
- `install.sh` updates a marketplace already added from this repository at the default branch in
  place, and warns before a change of source resets the plugin's data.

## [0.3.8] - 2026-10-09

### Upgrading

- 0.3.8 adds state migrations 4 and 5: `state.db` moves from schema 3 to schema 5, and every
  command that opens an older estate refuses, naming `tm db migrate`. After installing 0.3.8, run
  `tm db migrate` once in each existing project; it backs `state.db` up first.
- Migration 4 rewrites a node's `merge: main` to `merge: spec`. Migration 5 adds
  `nodes.on_target` and puts on its target every plan or spec past its landing.
- `tm export` writes format 5, which an older tm does not restore. `tm restore` still reads the
  older formats, and a format 1 or 2 export's `merge: main` as `spec`.
- A verification of type `symbol_signature` or `ast_export` now fails: rewrite it as a
  `test_command`.

### Added

- `install.sh` installs, reports on and uninstalls `tm` and the Claude Code plugin at one version,
  from a git ref or a checkout; `install.sh status` exits non-zero when they differ. The README,
  the skills, `tm doctor` and the guides send every user to it and name no version tag.
- `tm init` walks repositories, default branches, the worktree directory, gates and estate
  tracking at a terminal, re-runnably, with `--yes` and flags for scripts, and ends with
  `tm doctor`.
- `tm doctor` reports what `tm` needs and recommends codegraph.
- `/taskmanager:init` turns a tracker item, a file or the user's words into a spec ready for
  design; `/taskmanager:design` settles its open questions as decisions and ends on an approval
  decision; `/taskmanager:plan` imports an approved design as plans and tasks whose verifications
  fail before the work.
- `tm guide intake` turns an outside request, or a defect against landed work, into specs, tasks
  and decisions.
- A spec's `land_on` names the branch its chain lands on, else `repos.<repo>.default_branch`.
  The CLI and the web set it, every Lands line names the branch, and a top landing merges, gates,
  pushes and verifies on that branch, creating it on the remote when absent.
- `repos.<repo>.remote` names the remote every fetch, push and tracking ref reads; `null` lands
  on the local branch.
- A claim names the model id it runs on, from the node's own lists or `models.default` and
  `models.merge`; `--max-strong` counts `models.strong`, and `tm-wave` runs the claimed id as
  given.
- A gate's `failing_pattern` names its failing tests, so attribution works with any runner, and
  a gate that exits 0 with no test report, zero tests or no `tests_ran` count stops the landing as
  no tests.
- `repos.<repo>.after_land` runs once a container's landing pushes to its spec's target, and
  `:merge` records it.
- `repos.<repo>.unlocked_files`, `repos.<repo>.migrations` and `sensitive_areas` come from config,
  with defaults across ecosystems.
- A claim seeds its worktree's codegraph index, prints its codegraph lines under a `codegraph`
  key, and names the declared symbols that reach another lease's files; implement and fix briefs
  carry them, and the implement, fix, review and plan guides say how to use codegraph when it is
  on PATH and the repository is indexed.
- A `codegraph_query` verification runs a real query over the ref's tree and fails when it cannot
  run. `tm verify add` takes `--query-json`, and the cache keeps `codegraph.cache_commits`
  commits.
- Web: specs completed longer ago than `web.archive_after_days` leave every count and the Waves
  view; the toolbar's Archive toggle shows only them, kept in the URL as `?archive=1`.
- A review brief leaves out the implementer's report unless `review.blind` is false.
- Every listing command and `get` take `--json` and `--yaml`, and `tm job list` and
  `tm --version` exist.
- A superseded node names its replacement in `tm task get`, `tm render` and the web, and counts as
  completed once that is.
- `tm task add` takes `--repo`.
- CI on GitHub Actions runs the lint, format, type and test gates on every push and on pull
  requests to `main`, and an install job runs `install.sh` install, status, re-install and
  uninstall in a scratch `HOME`.
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, issue templates and a pull request template.
- The Gemini CLI extension loads `GEMINI.md`, which tells an agent how to get `tm` and which
  `tm guide` topic to read.
- The README opens with Requirements, Install and Quickstart sections, a CI badge and a screenshot
  of `tm web`.
- The plugin's skills give the command that installs `tm`, and tell an agent to stop when `tm` is
  not on PATH.
- `tm config set`, `get` and `unset` take dotted keys under `repos`:
  `repos.<repo>.gates.<main|parent>[.command|.junit|.tests_ran|.failing_pattern|.timeout]`,
  `repos.<repo>.gates`, `repos.<repo>.default_branch`, `.after_land`, `.unlocked_files`,
  `.migrations`, `.remote` and `repos.<repo>`. Each merges into the stored mapping; the
  repository at the tm root is `.`, as in `repos...gates.main.command`.
- A lease records the worktree its claim cut, so `tm run list` shows it.

### Changed

- TaskManager is released under the MIT license, declared in the wheel metadata and in every
  plugin manifest, which also link the repository and name the license holder.
- `/taskmanager:tm` only runs a `tm` command: invoking it no longer opts the session into the
  Workflow tool, a self-paced loop or any other tool.
- The plugin lives under `plugin/`, so installing it copies its skills, commands, agent and
  workflow rather than the whole repository.
- The dispatch guide and the dispatcher skill run the workflow as `taskmanager:tm-wave`, or by
  `scriptPath` at the plugin's `workflows/tm-wave.js`.
- `tm-wave` runs its commands on the plugin's `taskmanager:tm-op` agent, then on `tm-op`, before
  the generic agent. Its `models` argument aliases the id a claim names, and the family names
  `haiku`, `sonnet`, `opus` and `fable` still resolve to `claude-haiku-4-5-20251001`,
  `claude-sonnet-5-5`, `claude-opus-5-5` and `claude-fable-5-1`.
- The shipped guides, the worked example, CLI help and the `tm-wave` briefs name no stack and
  carry no house style: a dispatcher reports and stops when nothing is claimable or in flight, a
  question that holds work is a decision, and comment, test-name and review-format rules belong in
  a project's own guide addendum (`tm section set guide:<topic>`).
- `merge` is `parent` or `spec`; every path refuses `main`.
- Every chain ends at its target branch and meets within it. A dependency or migration link across
  targets is refused where it is written, while both ends still land, and `tm config` refuses
  moving a repository's default branch while that would split a chain.
- A claim names each repository's base, the default branch until the remote has the target, and
  a run with no `--ref` reads the branch the task lands on.
- `tm import`, `tm task update --repo` and the web refuse a `target_repo` that is not a git
  working tree under the tm root, and name the repositories that are, `.` for the root itself.
  A node already stored with one, as `tm restore` brings back, is left as it is.
- `tm wave discover`, the web's waves and a claim hold a node whose repository has no main gate,
  naming the `tm config set repos.<repo>.gates.main.command` that clears it, instead of running
  its implement and review only to stop at its landing.
- Discovery and claims hold a node under a deferred, abandoned or superseded ancestor.
- Sections other than `:report`, `:review` and `:merge` are rewritten whole, and a finding seen on
  two nodes becomes a guide rule.
- Reviews prove behaviour by breaking it and name their searches, and a fix watches its test go red
  first. `tm guide plan` states the bug-fix reproduction rule with its revert half.
- CLI errors go to stderr naming their fix, and every command has help. A heartbeat checks its
  lease's agent and token.

### Removed

- The design notes and implementation plans under `docs/superpowers/`.
- The `/docs` and `/redoc` API pages on `tm web`.
- `tm install` and the empty `tm plugin` group: `install.sh` replaces them.
- The unused copy of the skills under `src/taskmanager/skills`.
- The `symbol_signature` and `ast_export` verification types: every write refuses them, naming the
  `test_command` grep idiom.

### Fixed

- A plan or spec reviewed after its landing keeps its dependents claimable, and its migration
  writers out of the chain, from its landing until a write moves it back before landing. A child
  moved or imported under a container takes every ancestor off its target. `tm export` and
  `tm restore` carry that fact.
- `tm export` and `tm restore` round-trip an estate exactly: in-step nodes at their
  `claimed_from`, supersedes edges, ordinals, a completed spec's completion time, and the config
  without machine-local values.
- A landing job's `SUCCEEDED` commits with its node's landed status or merge note, so no reader
  sees one without the other.
- A landing stopped for no tests resumes at the gate, and a handed-over landing resumes once its
  merge is committed, whatever untracked files its worktree holds.
- Every excluded node reserves its declared files, whatever spec the discover is scoped to, so a
  parallel run cannot claim over them.
- `tm import` refuses bad input without a traceback, and refuses plans it cannot place under a
  spec.
- `tm task add` and `update` refuse a `--set` key that names a node field, naming only options the
  running command accepts.
- The config store saves only the values someone set, so `tm init` sets an unset default branch.
- A gate's cached baseline reruns when its `failing_pattern` changes.
- `tm web --host` takes an IPv6 address: it found no free port for one, and printed its URL
  without brackets.
- A gate command may hold braces of its own (`${HOME}`, `awk '{print $1}'`, `find -exec {} +`):
  only `{worktree}`, `{node}`, `{repo}` and `{target}` are replaced.
- A landing job stopped by any error waits for an agent with the error named, instead of staying
  `running` with no process behind it.
- `tm config set repos` refuses a value that would drop a repository's stored settings, and names
  the repository.
- A claim in a repository with no `origin/<default branch>` is refused, naming it, instead of
  failing at landing. A landing with no main gate names the command that sets one.
- An implement or fix worktree is removed once its node is `COMPLETED` by any write, unless it
  holds uncommitted work. The `tm/<id>` branch stays.
- A task whose `target_repo` is `.` gets the worktree `<worktree_dir>/<id>`, not `.-<id>`.
- Web: a refused dialog write puts focus back on the field to correct, its refusal toast stays in
  the dialog's Tab cycle and cuts its text at three lines.
- The CI script-test gate runs the quoted test glob, which Node 22 reads as files.

### Security

- `tm web` refuses a foreign `Host` or `Origin` on every route and on `/ws`, so a page on another
  site can no longer read the estate through the socket or through a DNS-rebound hostname.
- The web page and `tm web export` load marked, vis-network and DOMPurify from pinned copies
  that ship with tm, not from unpinned CDN URLs, and the export opens offline.
- `tm web` refuses a `--host` other than loopback unless `--expose` is passed: it has no
  authentication, and any client that reaches the port can run commands through a verification.
  An exposed server answers a client that reaches it by any of the machine's addresses, or by the
  name `--host` gives.

## [0.3.7] - 2026-10-08

### Changed

- The plan, implement and review guides require a removal to also remove everything that loses
  its last reader: a payload field's producer, a computation, a file.

## [0.3.6] - 2026-10-08

### Added

- Web: an Expand all toggle in the toolbar and on every spec, plan and task opens or closes
  everything under it, and holds through live updates.

### Changed

- `tm guide` states the brief-writing rules the skills defer to it.
- The plan and implement guides carry the round trip for a format two tasks share, and have a
  third-party artifact read before a brief about its structure is followed.
- A ruling or fix that changes a landed node's approach updates its verifications in the same
  step.

### Fixed

- The contract a report adds to a plan overview is appended instead of replacing it.
- Web: a long fact value wraps inside the facts strip, and revealing a node opens each
  ancestor's children group.

## [0.3.5] - 2026-10-07

### Added

- `tm wave discover --lines` prints one line per offer.
- Web: the page opens on the Document view and keeps the view, its selection and the filters in
  the browser path. Siblings sort by progress, with a Priority toggle kept in `?sort`.
- Web: shared renderers draw status, ids, kind, priority, lease, disclosure and pane states the
  same way in every view, and one detail renderer serves the expanded Document card and the
  drawer, which is now a dialog.
- Web: a started plan's or spec's own step is the last row of its list and counts in its n/m.
- Web decisions: answering stays on Open and moves to the next decision, keeps typed text,
  confirms abandon and defer, and runs from the keyboard. Below `sm` a decision is a page with
  the list in a drawer. Every held node reaches its decision through one id link, and closed
  decisions say whether they were blocking.
- Node bodies carry display status, phase, lease heartbeat and job times. A page write without
  an actor header is recorded under the git `user.name`.

### Changed

- A claim never locks `pyproject.toml` or `uv.lock`.

### Fixed

- `tm section get` piped into `tm section set --file -` round-trips the header and content byte
  for byte.
- Web: a write spins its control, a re-render keeps focus and an open menu, and section rows are
  buttons.

## [0.3.4] - 2026-10-07

### Added

- The land-first lifecycle and its `LANDED` status: a reviewed container lands before its one
  review, a fix to it lands without a re-review, and the review reads the landed code on its
  target.
- `tm db migrate` copies `state.db` to `state.db.schema<n>.bak` and moves it to schema 3. Every
  other command refuses an older `state.db` until it has run.
- A landing pauses between push tries and records each failed try in `push_errors`.
- Web: `LANDED` chips, and `LANDED` wherever the page picks a status.

### Changed

- The web serves a committed, built Tailwind stylesheet instead of the CDN script.
- A reviewed container's children default to `merge: parent`, and a child that would reach
  `main` unreviewed is refused, through one set of child defaults for import, `task add`,
  `plan add` and the API.
- A container is sensitive when any node under it is, so its post-landing fix is re-reviewed
  once.
- The guides teach the land-first order, and a generated file is rebuilt, never hand-merged.

### Fixed

- An import whose nodes share an id is refused, and the cycle check is linear.
- A withdrawn decision records who withdrew it and when, and the static export embeds decision
  bodies.
- Sibling claims that race to create a container branch create it once.
- Turning review off on a node at `LANDED` or under review is refused on every write path.

## [0.3.3] - 2026-09-26

### Added

- Web: Decisions is the fourth segment of the view switcher, and the Document view is back as
  the third. Decisions read context first and live, recover on failure, and offer their options
  as a radio group with answer and withdraw on a sticky bar.

### Fixed

- Web: the Waves view stays out of the static export, refetches on a spec filter, and refuses an
  out-of-range or non-integer wave size.
- `/api/decisions` counts every status in one grouped query.
- The page carries an inline favicon, so `/` logs no 404.

## [0.3.2] - 2026-09-26

### Added

- Web: a Waves view simulates `tm wave discover` forward, served by `GET /api/waves`.
- Discovery's candidate and wave choice is pure over a snapshot, which lets it simulate waves.

### Changed

- File locks and disjointness checks are keyed on `(target_repo, path)`.
- The guides pin acceptance measurement, invariant scope, limits read from config, plan
  verification, and one test per branch of a fix.

### Removed

- Web: the Document (tree) view, restored in 0.3.3.

### Fixed

- Web: watched bodies build and refresh without a write and resync after a dropped watch.
- Web: a container's own review, fix and merge steps count in the statuses.

## [0.3.1] - 2026-09-26

### Added

- Web: a live-view protocol over `/ws` (subscribe, snapshot, update) and paginated HTTP reads for
  statuses, nodes and decisions. Every view draws from one client store, and the static export
  mirrors it.
- `tm task get --fields`, and `tm job status` bounded to the keys asked for.
- Configurable dispatch tick and wave targets, printed by `tm guide dispatch`.

### Changed

- `state.db` moves to schema 2, with node revisions kept by trigger. A `state.db` newer than the
  installed `tm` is refused once, at the console entry.
- A snapshot reads `state.db` once, and every task is scored from it.
- The `tm-wave` workflow claims and runs one step per node per tick.
- An `owed` section is refused, pointing at registering the work as its own node.

### Removed

- `/api/tree`, `/api/graph` and `/api/stats`.

## [0.3.0] - 2026-09-25

### Added

- The lifecycle: one stored status per node with a derived phase and display status, a state
  machine for claims, step closes, caps and repair verbs, and containers rolled up from their
  children.
- `tm task start` claims a node, its lease and its file locks in one transaction and names the
  next step and its model. Each claim carries a lease token, and only that token closes or
  releases the step.
- Landing: tm lands a node as a detached job that merges, gates against a cached baseline,
  pushes and verifies. Container branches sync at claim and land repository by repository.
- Discovery offers every claimable node with its next step. A decision can block any node and
  applies its answer's effect in one transaction. Node conditions run in a cached, timed-out
  shell.
- The `tm-wave` workflow runs each node through one loop of `tm task start` and the step it
  names, and the guides teach every role its claim, its closing verb and tm's landing.

### Changed

- 0.3.0 starts a fresh `state.db` and refuses an estate written by 0.2; the README's "Upgrading
  from 0.2" has the steps. The ledger lives in `audit.db`.
- A cyclic write is refused with the cycle printed, and every write is checked against the flag,
  target, placement and busy-node rules.
- Web: nodes show stored status, display and phase, and edits go through verbs and flags instead
  of a status setter.

### Removed

- The pre-lifecycle status vocabulary.

## [0.2.0] - 2026-09-23

### Added

- Decisions and attachments: `tm decision add`, `answer`, `reopen`, `withdraw`, `block` and
  `unblock`. Web: a Decisions view, an attachment gallery and image serving; the static export
  embeds small images.
- Web editing: create and edit nodes, their status, dependencies, sections and verifications,
  through a write API guarded by Content-Type and Origin.
- Web: tri-state filters for status, model, spec and repository, a legend, per-status progress
  and each task's dispatch score.
- A claim covers every lifecycle stage, reads print JSON or YAML, and `tm export` and
  `tm restore` move an estate.
- `tm guide` serves the role guides, and the plugin skills point at it.
- `tm search`, `tm index` and `tm config`, with full-text, semantic and hybrid lookup.
- `tm render --recursive`, several nodes per `tm render`, and `tm task list --render`.
- Dependency edges on an existing task can be added and removed, and a task's verifications
  listed and removed.
- `tm wave discover` chooses a batch, and the plugin ships the `tm-wave` workflow.
- `test_command` verifications receive the ref under test as `TM_VERIFY_REF`.

### Changed

- Every CLI mutation goes through one operations layer, and multi-write operations are atomic.
- A ready task whose declared file is leased reads `BLOCKED_BY_LEASE`.
- Plan and spec listings show the rolled-up state.

### Fixed

- A re-import keeps the node values it does not state, and a key nothing reads is refused
  instead of dropped.
- One connection and one transaction depth per thread, so web routes never share a cursor or
  leak file descriptors.

## [0.1.0] - 2026-09-19

### Added

- A local SQLite store for specs, plans, tasks and their sections, addressed by qualified slugs
  under a configurable naming scheme.
- Hybrid search with sqlite-vec.
- Verifications that check a symbol in the code, and an optional codegraph runner.
- A dependency graph with cycle detection and a recommendation score per task.
- Git worktree management for the tasks in flight.
- Markdown projections of any node and a bulk importer.
- The `tm` CLI.
- A web visualizer with a live socket, and a static exporter.
- The taskmanager and dispatcher skills, packaged as a Claude Code plugin.

[Unreleased]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.13...HEAD
[0.3.13]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.11...v0.3.12
[0.3.11]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.10...v0.3.11
[0.3.10]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/TaigoPedrosa/TaskManager/compare/6b6dd21...v0.3.4
[0.3.3]: https://github.com/TaigoPedrosa/TaskManager/compare/7a5c2c6...6b6dd21
[0.3.2]: https://github.com/TaigoPedrosa/TaskManager/compare/8e2f7de...7a5c2c6
[0.3.1]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.0...8e2f7de
[0.3.0]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/TaigoPedrosa/TaskManager/compare/86f4278...v0.2.0
[0.1.0]: https://github.com/TaigoPedrosa/TaskManager/tree/86f4278
