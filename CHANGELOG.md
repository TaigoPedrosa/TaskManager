# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CI on GitHub Actions runs the lint, format, type and test gates on every push and on pull
  requests to `main`.
- `CONTRIBUTING.md`, `SECURITY.md`, this changelog, issue templates and a pull request template.
- The Gemini CLI extension loads `GEMINI.md`, which tells an agent how to get `tm` and which
  `tm guide` topic to read.
- The README opens with Requirements, Install and Quickstart sections, a CI badge and a screenshot
  of `tm web`.
- The plugin's skills give the command that installs `tm`, and tell an agent to stop when `tm` is
  not on PATH.

### Changed

- TaskManager is released under the MIT license, declared in the wheel metadata and in every
  plugin manifest.
- `/taskmanager:tm` only runs a `tm` command: invoking it no longer opts the session into the
  Workflow tool, a self-paced loop or any other tool.

### Removed

- The design notes and implementation plans under `docs/superpowers/`.
- The `/docs` and `/redoc` API pages on `tm web`.
- `tm install`, the empty `tm plugin` group and `install.sh`: the README's install commands
  replace them.

### Fixed

- A plan or spec reviewed after its landing keeps its dependents claimable, and its migration
  writers out of the chain, from its landing until a write moves it back before landing.
  `state.db` moves to schema 5: run `tm db migrate`.
- `tm web --host` takes an IPv6 address: it found no free port for one, and printed its URL
  without brackets.

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

[Unreleased]: https://github.com/TaigoPedrosa/TaskManager/compare/v0.3.7...HEAD
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
