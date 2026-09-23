# Plan: decisions, tri-state filters and an editable web view

Spec: `docs/superpowers/specs/2026-09-22-decisions-and-web-editing.md` (cited below as §n).

## Branches and waves

Integration branch `feat/decisions-web`, cut from `main`. Each task cuts its own branch from the
integration branch's tip when it starts. Each wave's branches merge into the integration branch
before the next wave starts, and the whole branch merges to `main` after review.

| Wave | Task | Branch | Depends on | Files it owns |
|:--|:--|:--|:--|:--|
| 1 | T1 Operations layer | `dw/t1-operations` | — | `engine/operations.py` (new), `cli/main.py`, `di/container.py`, `core/enums.py` (ledger commands only), `tests/unit/test_operations.py` (new), CLI tests touched by the new commands |
| 1 | T2 Split the UI into files | `dw/t2-ui-split` | — | `web/ui.py`, `web/static/**` (new), `tests/unit/test_web_ui.py` |
| 2 | T3 Decisions and attachments backend | `dw/t3-decisions` | T1 | `engine/decisions.py`, `engine/assets.py` (new), `engine/operations.py`, `engine/graph.py`, `core/enums.py`, `cli/main.py`, `renderers/markdown.py`, `renderers/importers.py`, `web/enums.py` (the `AWAITING_DECISION` theme only), `guides/*.md`, `skills/**`, new tests |
| 2 | T4 Web write API | `dw/t4-web-api` | T1 | `web/app.py`, `tests/integration/test_web_api.py` (new) |
| 2 | T5 Tri-state filters | `dw/t5-filters` | T2 | `web/static/js/filters.js`, the toolbar filter markup in `web/static/index.html`, `web/static/app.css` |
| 3 | T6 Editing UI | `dw/t6-edit-ui` | T2, T4 | `web/static/js/edit.js` (new), `web/static/js/detail.js`, `web/static/js/core.js`, the `toolbar-actions` and `dialog-root` slots in `index.html` |
| 4 | T7 Decisions UI, decision/asset API, images | `dw/t7-decisions-ui` | T3, T4, T6 | `web/app.py` (decision, attachment, `/assets`, `/api/file` routes), `web/static_export.py`, `web/static/js/decisions.js` (new), `detail.js`, `index.html` (view switcher), tests |
| 3 | T8 CLI ergonomics | `dw/t8-cli-ergonomics` | T3 | `cli/main.py` (`render`, `task list`, `next` help), `guides/overview.md`, tests |
| 5 | Review | — | all | branch review (Opus) and design review (Opus, Playwright), one fix round (Sonnet) |

T3 and T4 both read `Operations` and never edit the same file. T5 and T6 edit different regions of
`index.html`. T2 creates the slots they own (§6.1), so neither touches the other's markup.

## Standing conventions for every task

- Work in your own worktree:
  `git -C /Users/taigo.pedrosa/Documents/TaskManager worktree add -b <branch> <scratchpad>/tm-<branch-leaf> feat/decisions-web`.
  Set up the environment inside it with `(cd <wt> && uv sync --group dev)`. Never edit the primary
  checkout at `/Users/taigo.pedrosa/Documents/TaskManager`. Check every `file_path` starts with
  your worktree path.
- Never `cd` in a Bash command; use `(cd <wt> && ...)` subshells or `git -C`.
- Gates, each run in the foreground as `cmd; echo $?`, never through a pipe:
  `(cd <wt> && uv run --group dev pytest -q -p no:cacheprovider)`,
  `(cd <wt> && uv run --group dev ruff check src tests)`,
  `(cd <wt> && uv run --group dev ruff format --check src tests)`,
  `(cd <wt> && uv run --group dev mypy src --cache-dir <wt>/.mypy_cache)`.
  The baseline at `feat/decisions-web`'s creation is 409 passed.
- Every new test is watched failing before its fix, by reverting or mutating the code it covers,
  and the report pastes that red output.
- Comments say why, never what; never mention this plan, a task id, a wave or a review in code,
  tests, log lines or commit messages.
- Commit with explicit paths (`git -C <wt> add <files> && git -C <wt> commit -m "..." -- <files>`),
  in conventional-commit form, with no AI attribution trailer. Don't push and don't merge; the
  dispatcher integrates.
- Don't ask questions. Where something is ambiguous, take the option that fails closed, mark it
  "provisional" in the report, and continue.
- Report as your final message: branch and head sha, each file changed, each gate's command and
  exit code, pytest's collected/passed counts, the red output of each new test, and anything
  provisional or not done. Cleanup is skip-and-report: leave the worktree in place.

## T1 — Operations layer (§2)

1. Create `engine/operations.py` with `OperationError` (carrying `status_code`) and `Operations`,
   with every method in §2's table. Move the logic out of the CLI commands verbatim, validation
   included. Record the ledger event inside each method (move `_record_ledger`'s behaviour there,
   with `actor_id=self.actor`). Add new `LedgerCommand` members for commands that write free-text
   strings today (`"task depends"`, `"task update"`): `TASK_DEPENDS`, `TASK_UPDATE`,
   `TASK_MOVE`, `SECTION_REMOVE`, `LEASE_RELEASE`. Keep their string values identical to what is
   written today where one exists (`"task depends"`, `"task update"`), so existing ledgers read the
   same.
2. Provide `Operations` from `di/container.py`.
3. Make each CLI command a thin wrapper: parse, call, print. **Every existing CLI message and exit
   code stays byte-identical**; the existing suite is the proof, and no existing test's assertions
   may change.
4. Add the new CLI surface: `tm task update --unset key` (repeatable), `tm task move <id> --plan <p>`,
   `tm section remove <id>:<key>`, `tm run release <id>`. `task update` now accepts any node kind.
5. `tests/unit/test_operations.py`: one test per method covering success, the ledger event (actor
   included), and each refusal writing nothing (assert the node, relations and ledger are
   unchanged after a refused call), plus CLI tests for the four new commands.

Model: Sonnet (`python-dev` agent). Ponytail full.

## T2 — Split the UI into files (§6.1)

Pure refactor. The rendered page must behave identically.

1. Move the HTML, CSS and JS out of `get_web_html`'s f-string into `web/static/index.html`,
   `web/static/app.css` and `web/static/js/{core,filters,tree,graph,detail,main}.js`, splitting the
   script along its existing function groups. Un-double every `{{`/`}}`. The values `ui.py`
   injects today (status themes, groups, CSS, the icon sprite, `STATIC_DATA`) become JSON in one
   generated `<script>` block or `<!--slot:name-->` replacements. Don't template inside JS.
2. `get_web_html(initial_data=None)` reads the files with `importlib.resources` and inlines them in
   a fixed order, returning one self-contained document.
3. Add empty slots for later waves: `<div id="toolbar-actions"></div>` after the filters in the
   toolbar, `<div id="dialog-root"></div>` and `<div id="toast-root"></div>` before `</body>`, and
   `<div id="view-extra-buttons"></div>` beside the Document/Graph view buttons. Add `canEdit()` to
   `core.js` (false when `window.STATIC_DATA` is set).
4. Update `tests/unit/test_web_ui.py`'s `_function_body` helper so it does not depend on the old
   4-space indentation. Every existing assertion keeps its meaning.
5. Prove behaviour parity: start `tm web run -C /Users/taigo.pedrosa/Documents/SocialSrc --port
   <free port>` from the main checkout and from your worktree (record both PIDs, kill both after).
   Take a Playwright screenshot of both at 1440×900 in document and graph view, and report whether
   the pairs match. Paste the `lsof` and `ps` proof that both servers stopped.

Model: Sonnet (general-purpose). Stop ponytail: this is frontend.

## T3 — Decisions and attachments backend (§3, §4)

1. `NodeKind.DECISION`, `VirtualStatus.AWAITING_DECISION`, and `LedgerCommand` members
   `DECISION_ADD`, `DECISION_ANSWER`, `DECISION_REOPEN`, `DECISION_WITHDRAW`, `DECISION_LINK`,
   `ATTACH`, `DETACH`.
2. `engine/decisions.py`: the pydantic models of §3.1 plus `read(node)` and `write(node, data)`
   helpers; `Operations` methods `add_decision`, `answer_decision`, `reopen_decision`,
   `withdraw_decision`, `link_decision(id, add, remove)`, each with §3.3's refusals.
3. `engine/graph.py`: §3.2 exactly, including decisions met when `COMPLETED` or `ABANDONED` and the
   plan rollup. Tests: a task waiting on an open decision reads `AWAITING_DECISION`; answering,
   then withdrawing, each make it `READY`; reopening makes it wait again; a task with both an unmet
   task dependency and an open decision reads `BLOCKED`; a plan whose only open tasks await
   decisions reads `BLOCKED`; `tm next` never offers an awaiting task.
4. `engine/assets.py` plus `Operations.attach`/`detach` (§4), with `tm attach` and `tm detach`.
   Test idempotent re-attach, the 20 MB refusal, and that the file stays while any node still
   references it.
5. CLI group `tm decision` (§3.3); `tm task get` gains `awaiting_decisions`; `tm render` handles
   decisions (§3.3). Make sure `get_tree`, `score_every_task`, `tm task list`, `tm next` and the
   plan/spec rollups ignore decisions rather than crashing on them. Add a test that seeds one
   decision and runs each of them.
6. Export/restore/import (§4): `_decisions.json`, `assets/`, the importer's `decisions` key. Test a
   round trip: export, restore into a fresh root, export again, and require byte-identical output
   including assets.
7. `web/enums.py`: the `AWAITING_DECISION` theme (§6.4) and `help-circle` in the sprite, so the page
   renders the new status.
8. The atomic `set_status(..., section=)` and its `tm run stop --section/--section-file` flags (§2),
   tested by forcing the status write to fail and asserting the section was not written either.
9. Attachment provenance (§4): `AttachmentSource`, `--source`, `--replace`, and `tm attachments
   [--check]` with the fresh/stale/missing/unverifiable states. Test each state: edit the source
   file and see `stale`, delete it and see `missing`.
10. Guides and skills (§3.4). `tests/unit/test_guides.py` must stay green, which proves every
   `tm decision ...` line the guides print resolves to a real command and flag.

Model: Sonnet (`python-dev` agent). Ponytail full.

## T8 — CLI ergonomics (§4a)

`tm render` with several ids, `tm task list --render`, and `tm next` discoverability in its
`--help` text and in `overview.md`. Tests pin multi-id output order and the `--render` view.
Model: Sonnet (`python-dev` agent). Ponytail full.

## T4 — Web write API (§5, generic routes only)

1. The write-guard dependency and actor resolution (§5).
2. `GET /api/meta` and every generic route in §5's table: specs, plans, tasks, node PATCH,
   status, dependencies, supersede, move, sections, verifications, verify, lease, sweep. Not the
   decision or attachment routes; T7 adds those. Each calls `Operations`, and `OperationError`
   maps to its status code.
3. `tests/integration/test_web_api.py` with `TestClient`: each route's success and at least one
   refusal (asserting nothing changed); the guard's 403 for a missing JSON content type and for a
   foreign `Origin`, and its pass for a same-host `Origin`; the ledger actor from `X-TM-Actor` and
   the `web` default.

Model: Sonnet (`python-dev` agent). Ponytail full.

## T5 — Tri-state filters (§6.2)

1. `triStateHandlers` in `filters.js`: click/double-click disambiguation at 220 ms, keyboard, and
   accessible names.
2. Status chips move to the gesture grammar.
3. Replace the model and spec multiselects and the repo `<select>` with the tri-state popover of
   §6.2 (repo becomes multi-valued), including counts, the button summary text, the hash parameters
   with backward compatibility, and Clear resetting every dimension.
4. Python-side tests in `test_web_ui.py` pinning the presence of the grammar (`dblclick` handler,
   the 220 ms timer, the `x`-prefixed hash keys). Behaviour is proven in the browser: with Playwright
   against `tm web run -C /Users/taigo.pedrosa/Documents/SocialSrc`, script include, exclude and
   clear on one status chip and one model value, and report the visible task counts after each
   step. Stop every server you start by PID and paste the proof.

Model: Sonnet (general-purpose). Stop ponytail.

## T6 — Editing UI (§6.3)

1. `core.js`: `api(method, path, body)` (JSON, surfaces `detail` on error) and a toast helper in
   `#toast-root`; add DOMPurify from jsDelivr and sanitise every `marked.parse` result.
2. `edit.js`: a dialog primitive (focus trap, Esc, focus return, confirm variant), the `+ New` menu
   in `#toolbar-actions`, and every §6.3 form and action except attachments and decisions (T7).
   Pickers read `/api/meta`.
3. `detail.js`: the action bar, dependency add/remove, section edit/add/delete with a preview tab,
   verification add/remove/run, and lease release, all rendered only when `canEdit()`.
4. Prove it in the browser against a **scratch copy** of a database, never the estate's live one:
   `tm restore <scratch export>` into `<scratchpad>/tm-t6-root`, then `tm web run -C` that root.
   Script creating a task, editing its title, adding and removing a dependency, adding, editing and
   deleting a section, and abandoning it. Report each resulting ledger line
   (`tm audit list -C <root>`). Stop the server by PID.

Model: Sonnet (general-purpose). Stop ponytail.

## T7 — Decisions UI, decision and attachment API, image rendering (§3, §4, §5, §6.4)

1. `web/app.py`: the decision routes, the attachment routes, `GET /assets/{name}` and
   `GET /api/file` (§4), with tests for path traversal (`../`, absolute paths outside the root,
   symlinks that escape), non-images, malformed asset names, and every decision route's success and
   refusal.
1a. `POST /api/nodes/{id}/attachments/check`, and the attachment `source` on upload (§4).
2. `decisions.js`: the view, list, detail, answer flow, reopen, withdraw, the new-decision dialog
   with its options editor, and blocked-task linking. Wire the open-count badge button into
   `#view-extra-buttons`.
3. `detail.js`: the attachment gallery with lightbox, source/age/staleness badges and Re-check, the "Attach file" action, `<img>` src
   rewriting (§4), and the `AWAITING_DECISION` banner on tasks.
4. `web/static_export.py`: embed image attachments of 2 MB or less as `data:` URIs.
5. Prove it in the browser against a scratch root (as in T6): raise a decision by CLI with three
   options and one recommended, `--blocks` a task, attach a PNG, answer it in the page, and confirm
   the task turned `READY` in `tm next`. Then reopen and withdraw it. Screenshot the decisions view
   in the open, answered and empty states at 375, 768 and 1440. Stop the server by PID.

Model: Sonnet (general-purpose). Stop ponytail.

## Review (wave 5)

- **Branch review** (Opus, `branch-reviewer` behaviour): the whole `main..feat/decisions-web`
  diff against the spec, the gates at the tip, and the write guard and file-serving routes read as
  security boundaries.
- **Design review** (Opus): render every §6 surface through Playwright at 375/768/1440 against
  the §6.5 conventions and the §7 checklist.
- One Sonnet fix round over both reviews' findings, then the merge to `main` and the push.
