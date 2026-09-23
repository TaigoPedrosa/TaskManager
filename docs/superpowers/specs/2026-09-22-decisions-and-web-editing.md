# Decisions, tri-state filters and an editable web view

Date: 2026-09-22. Status: approved for implementation. Plan:
`docs/superpowers/plans/2026-09-22-decisions-and-web-editing.md`.

## 1. Goals

1. **Decisions are first-class.** A question an agent cannot answer is raised as a *decision* with
   agent-defined options, not as a task nobody can claim. The owner answers it in the web view by
   picking an option or writing a custom answer. Tasks can be marked as waiting on a decision, and
   read `AWAITING_DECISION` until it is answered.
2. **Evidence renders.** Any node can carry attachments (screenshots, diagrams, logs). Images render
   inline in the web view, and markdown image links inside sections render too.
3. **Filters are tri-state everywhere.** One gesture grammar for status chips and for the model,
   spec and repo filters: click includes, double-click excludes, clicking an active item clears it.
4. **The web view edits.** Anything the CLI can do to a single node — create, update, link, unlink,
   add or remove sections and verifications, change status, supersede, move, release a lease — the
   web view can do too, through the same code path.

Out of scope: migrating the existing `owner-decisions` tasks (ruled 2026-09-22: no migration; they
stay as they are), import/export/config/index from the web, hard-deleting nodes (a node leaves by
`ABANDONED` or `SUPERSEDED`, as in the CLI), multi-user auth.

## 2. One write path: `Operations`

Every mutation the CLI performs today lives inside a typer command function. The web cannot call
those, so the logic moves into `taskmanager/engine/operations.py`:

```python
class OperationError(ValueError):
    """A refusal a user can act on; its message is shown verbatim by the CLI and the web."""
    status_code: int  # 400 invalid input, 404 unknown node, 409 conflict with current state

class Operations:
    def __init__(self, node_repo, runtime_repo, graph, coordinator, ledger_repo,
                 verification_engine, actor: str = "cli") -> None: ...
    def with_actor(self, actor: str) -> "Operations": ...
```

Each method validates everything first and writes nothing when any part is refused (the rule
`tm task depends` already follows), writes, appends exactly one ledger event with `actor_id=actor`,
and returns the resulting node id or a small result dataclass. The CLI commands become thin:
parse options, call one method, print. **CLI output text and exit codes do not change**; the
existing tests are the proof.

Generic methods (all existing CLI behaviour, plus four additions marked *new*):

| Method | CLI | Notes |
|:--|:--|:--|
| `add_spec(title, slug, priority)` | `spec add` | |
| `add_plan(title, spec, slug, priority, order, require_review)` | `plan add` | |
| `add_task(title, plan, slug, priority, order, depends_on, models)` | `task add` | |
| `update_node(id, title, priority, models, repo, frontmatter_set, frontmatter_unset)` | `task update` | works on any kind; `frontmatter_unset` is *new* (`--unset key`) |
| `set_dependencies(id, add: list[(dep, gate)], remove)` | `task depends` | cycle and existence checks unchanged |
| `supersede(old, new, transfer_blocks)` | `task supersede` | |
| `move_task(id, plan)` *new* | `task move <id> --plan <plan>` | re-parents: removes the old `contains` edge, adds the new one |
| `set_status(id, status, remove_worktree, section)` | `run stop` | same semantics: releases any lease. `section=(key, content, header)` *new* writes a section in the same transaction (`tm run stop <id> --status COMPLETED --section ruling --section-file F`), so a ruling can never land without its status or the reverse |
| `set_section(id, key, content, header)` | `section set` | |
| `remove_section(id, key)` *new* | `section remove <id>:<key>` | 404 when absent |
| `add_verification(...)`, `remove_verification(id, vid)` | `verify add/remove` | |
| `run_verifications(id)` | `verify run` | returns per-row results |
| `release_lease(id)` *new* | `run release <id>` | drops lease and locks, status unchanged |
| `sweep_leases()` | `run sweep` | |

`Operations` is provided by the dishka container. Leases being *claimed* (`run start`,
`heartbeat`) stay agent-only and are not exposed to the web.

## 3. Decisions

### 3.1 Storage

A decision is a node with `kind = decision` (new `NodeKind.DECISION`). Reusing the node table gives
sections, relations, full-text search, the ledger and the graph view for free. Its status reuses
`NodeStatus`, presented under decision names everywhere a human reads it:

| Decision state | `NodeStatus` |
|:--|:--|
| Open | `NOT_STARTED` |
| Answered | `COMPLETED` |
| Withdrawn | `ABANDONED` |

The structured part lives in `frontmatter["decision"]`, validated by pydantic models in
`taskmanager/engine/decisions.py` (`extra="forbid"`) on every write and read:

```python
class DecisionOption(BaseModel):
    key: str            # short, unique within the decision: "a", "b", "session-jwks"
    label: str          # one line
    description: str = ""   # markdown
    recommended: bool = False  # at most one option is recommended

class DecisionAnswer(BaseModel):
    option: str | None = None   # an option key, or None for a custom answer
    text: str = ""              # the custom answer, or an optional note on a picked option
    rationale: str = ""
    answered_by: str
    answered_at: datetime

class DecisionData(BaseModel):
    options: list[DecisionOption] = []   # may be empty: then only a custom answer is possible
    allow_custom: bool = True
    raised_by: str | None = None         # the node id that raised it
    answer: DecisionAnswer | None = None
    withdrawn_reason: str = ""
```

The question is the node's `title`. Context, evidence prose and anything else are ordinary sections
(`context` by convention). Ids are `decision-<slug>`, or `decision-D<n>` when no slug is given.

### 3.2 Blocking

"Task T waits on decision D" is a `depends_on` edge from T to D. That keeps the existing cycle
check, `dependency_details`, export and the graph view, with no new relation type.

`GraphEngine.resolve_task_state`, for a `NOT_STARTED` task with unmet dependencies:

- a dependency whose kind is `decision` is **met when it is Answered or Withdrawn** (`COMPLETED` or
  `ABANDONED`). A withdrawn question no longer needs an answer, so it must not hold work forever;
- if any unmet dependency is not a decision → `BLOCKED` (unchanged);
- else, if any unmet dependency is a decision → new `VirtualStatus.AWAITING_DECISION`;
- then the existing `BLOCKED_BY_LEASE` and `READY` checks.

`resolve_plan_status` counts `AWAITING_DECISION` as blocked, exactly as it counts
`BLOCKED_BY_LEASE`. `tm next` offers only `READY`, so a waiting task is never recommended.
Decisions are never contained in plans, never leased and never scored.

### 3.3 CLI

```
tm decision add "<question>" [--slug s] [--priority n] [--context TEXT | --context-file F]
                [--option "key|Label|description"]... [--recommend key] [--no-custom]
                [--raised-by <node>] [--blocks t1,t2]
tm decision list [--status open|answered|withdrawn] [--yaml|--json]
tm decision get <id> [--yaml|--json]
tm decision answer <id> (--option key [--note TEXT] | --custom TEXT) [--rationale TEXT] [--by NAME]
tm decision reopen <id>
tm decision withdraw <id> [--reason TEXT]
tm decision block <id> --tasks t1,t2        # adds depends_on edges from each task
tm decision unblock <id> --tasks t1,t2
```

`answer` refuses an unknown option key, a custom answer when `allow_custom` is false, and a
decision that is not Open (409, "reopen it first"). `reopen` clears `answer` and returns the node to
`NOT_STARTED`, which re-blocks its dependents. `tm task get` gains `awaiting_decisions: [ids]`.
`tm render` of a decision prints the question, context, options (recommended marked) and the answer.
`answer` writes the answer and moves the status in one transaction, removing the two-call gap where
a ruling section could be written while the task stayed `NOT_STARTED`.

### 3.4 Guides

`overview.md`, `plan.md`, `implement.md`, `review.md`, `fix.md` and `dispatch.md` each say, where
they currently tell an agent to stop and ask: raise a decision with `tm decision add` naming the
options you considered and your recommendation, `--blocks` the task that cannot proceed, then stop
the task normally. `dispatch.md` says `tm decision list --status open` is the owner's queue. The
estate addenda that mention an `owner-decisions` plan are the estate's to update, not this plan's.

## 4. Attachments and image rendering

`tm attach <node-id> <file> [--caption TEXT]` copies the file into
`.taskmanager/assets/<sha256[:16]><ext>` (content-addressed, so re-attaching is idempotent) and
appends `{"asset": name, "name": original filename, "caption": ..., "mime": ...}` to
`frontmatter["attachments"]`. `tm detach <node-id> <asset>` removes the entry; the file is removed
only when no node references it any more. Limit 20 MB per file; `mime` comes from `mimetypes`.
Evidence survives the scratch directory it came from, because the file is copied.

**Provenance.** An attachment is usually a capture of something live (a Figma frame, a page, a
file another agent keeps editing), and a capture silently goes stale when its source changes. Each
attachment entry therefore also carries:

```python
class AttachmentSource(BaseModel):
    uri: str | None          # "path/in/project.png", "figma:<fileKey>:<nodeId>", "https://..."
    sha256: str | None       # of the source file at capture, for project-file sources only
    captured_at: datetime
    checked_at: datetime | None = None
    state: Literal["fresh", "stale", "missing", "unverifiable"] = "unverifiable"
```

`tm attach --source <uri>` records it; when the attached file itself lies inside the project root
and no `--source` is given, the source defaults to that project-relative path. `tm attachments
<node-id> [--check]` lists attachments. `--check` re-hashes every project-file source and sets
`fresh`, `stale` or `missing` plus `checked_at`. Figma and URL sources stay `unverifiable` and
report their capture age. `tm attach <node-id> <file> --replace <asset>` re-captures an attachment
from its source, keeping its caption and source.

`tm export` also copies every referenced asset into `<dir>/assets/` and writes every decision into
`<dir>/_decisions.json` (sorted by id, same byte-identical guarantee). `tm restore` copies
`assets/` back and imports decisions after plans and specs, so their `depends_on` edges from tasks
resolve. The importer accepts `kind: decision` nodes under a top-level `decisions` key.

Web serving (read-only routes):

- `GET /assets/{name}` serves only names matching `^[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$` from
  `.taskmanager/assets/`.
- `GET /api/file?path=...` serves an image (`image/*` by `mimetypes`) whose resolved real path is
  inside the project root, for markdown like `![shot](web/e2e/out/home.png)`. Anything else is 404.

In the page, a section's rendered markdown has every `<img src>` that is not `http(s):`, `data:` or
`/assets/` rewritten to `/api/file?path=<src>`. Attachments render as a gallery (image thumbnails,
click to open full size in a lightbox; other types as download links with name and size). All
markdown is sanitised with DOMPurify before insertion, because the page now writes as well as reads.
The static export embeds image attachments up to 2 MB as `data:` URIs and shows the rest as names.
Every attachment in the page shows its source and capture age, with a badge for `stale` (amber),
`missing` (red) or `unverifiable` ("unverified since <age>"), plus a **Re-check** action
(`POST /api/nodes/{id}/attachments/check`).

## 4a. CLI ergonomics

- `tm render <id> [<id>...] [--view v] [-r]` renders several nodes in one call, separated by a rule.
- `tm task list [filters] --render <view>` renders every listed task instead of the table.
- `tm next --help` and `overview.md` say plainly: what can be worked on now, scored, is
  `tm next [--model <id>] -n <N> --yaml`, and the owner's queue is `tm decision list --status open`.

## 5. Web write API

All routes live in `web/app.py`, call `Operations.with_actor(actor)`, and map `OperationError` to
its `status_code` with `{"detail": message}`. The ledger watcher already broadcasts every write, so
every open page refreshes.

**Write guard** (a FastAPI dependency on every non-GET route): the request's `Content-Type` is
`application/json`, and if an `Origin` header is present its host:port equals the request's `Host`.
Otherwise 403. JSON bodies force a CORS preflight a foreign page cannot pass, and the origin check
covers the rest. The actor is the `X-TM-Actor` header when present, else `web`.

```
GET    /api/meta                         statuses, decision states, verification types, known
                                         models, repos, specs and plans (for form pickers)
POST   /api/specs                        {title, slug?, priority?}
POST   /api/plans                        {title, spec, slug?, priority?, order?, require_review?}
POST   /api/tasks                        {title, plan, slug?, priority?, order?, depends_on?, models?}
PATCH  /api/nodes/{id}                   {title?, priority?, acceptable_models?, target_repo?,
                                          frontmatter_set?: {k: v}, frontmatter_unset?: [k]}
POST   /api/nodes/{id}/status            {status, remove_worktree?}
POST   /api/nodes/{id}/dependencies      {add?: [{id, gate?}], remove?: [id]}
POST   /api/nodes/{id}/supersede         {by, transfer_blocks?: "all"|"none"|[ids]}
POST   /api/nodes/{id}/move              {plan}
PUT    /api/nodes/{id}/sections/{key}    {content, header?}
DELETE /api/nodes/{id}/sections/{key}
POST   /api/nodes/{id}/verifications     {type, target_path, expected_pattern?}
DELETE /api/nodes/{id}/verifications/{vid}
POST   /api/nodes/{id}/verify            -> [{id, type, target, passed, detail}]
DELETE /api/nodes/{id}/lease
POST   /api/leases/sweep
POST   /api/nodes/{id}/attachments       {filename, content_base64, caption?, source?}
POST   /api/nodes/{id}/attachments/check
DELETE /api/nodes/{id}/attachments/{asset}
GET    /api/decisions?status=open|answered|withdrawn
POST   /api/decisions                    {question, slug?, priority?, context?, options?,
                                          recommend?, allow_custom?, raised_by?, blocks?}
POST   /api/decisions/{id}/answer        {option?, text?, rationale?}
POST   /api/decisions/{id}/reopen
POST   /api/decisions/{id}/withdraw      {reason?}
POST   /api/decisions/{id}/blocks        {add?: [task ids], remove?: [task ids]}
```

Attachments upload as base64 JSON so the server needs no multipart dependency.

## 6. Web UI

### 6.1 Structure

`web/ui.py` is one 1,600-line Python f-string, so every brace in the JavaScript is doubled. It is
split into package files served inline: `web/static/index.html` (the page with a few
`<!--slot-->` markers), `web/static/app.css`, and `web/static/js/*.js` (`core.js` state, fetch and
helpers; `filters.js`; `tree.js` for the document view; `graph.js`; `detail.js`; `edit.js`;
`decisions.js`; `main.js` wiring). `get_web_html(initial_data)` reads them with
`importlib.resources` and inlines them in a fixed order, so the page stays a single self-contained
document and the static export keeps working. No build step and no new runtime dependency beyond
DOMPurify from the jsDelivr CDN, beside the existing Tailwind and marked.

The page is read-only when `window.STATIC_DATA` is set: `core.js` exposes `canEdit()`, and every
edit control renders only when it returns true.

### 6.2 Tri-state filters

One gesture grammar, implemented once in `filters.js` as `triStateHandlers(el, getMode, setMode)`:

- **click** on a neutral item → include; **click** on an included or excluded item → neutral;
- **double-click** on any item → exclude.

A double-click also fires two clicks, so a click waits 220 ms for a second one before applying.
Keyboard: `Enter`/`Space` acts as a click, `Shift+Enter` excludes.

Where it applies:

- **Status chips** (row 2). Same rule, replacing today's click-to-cycle.
- **Model, spec and repo filters** (repo becomes multi-valued). Each is a toolbar button opening a
  popover list. Each row shows the value, its task count, and **exactly two icon toggle buttons,
  `plus` and `minus`, with no neutral button**. `plus` is green when that value is included and
  gray otherwise; `minus` is red when it is excluded and gray otherwise. Clicking the unselected
  one selects it and deselects the other, and clicking the selected one returns the value to
  neutral (both gray). The icons never change shape; only their colour shows the state. Each
  button is `aria-pressed`, named "Include <value>" / "Exclude <value>". Clicking the row's label
  follows the gesture grammar above. The button reads `Model` when neutral, and `Model +2 −1` when
  two values are included and one excluded.

Semantics per dimension: a task passes if (no includes, or it matches at least one include) and it
matches no exclude. For multi-valued `acceptable_models`, matching means any overlap. Tasks with no
repo match the value `(none)`. Hash parameters: `status`/`xstatus`, `model`/`xmodel`,
`spec`/`xspec`, `repo`/`xrepo`, comma-separated. Old `repo=<single>` links keep working. Hover text
on every tri-state control: "Click: include · Double-click: exclude · Click again: clear".
Accessible name: "<dimension> <value>: included | excluded | not filtered".

### 6.3 Editing

- **Toolbar `+ New`** menu: Spec, Plan, Task, Decision. Each opens a dialog; pickers come from
  `/api/meta`.
- **Detail panel action bar** for a task: Edit (title, priority, models, repo, frontmatter
  key/values with `declared_files` as a list), Status (Abandon, Defer, Reopen as `NOT_STARTED`,
  Mark completed, or any status via "Other…"), Supersede…, Move to plan…, Release lease (when one
  exists). Plans and specs get Edit and Status.
- **Dependencies**: each dependency row gets a remove ×; "Add dependency" opens a search picker
  over node ids and titles, with a gate select (default COMPLETED). "Wait on decision" is the same
  picker filtered to decisions.
- **Sections**: each section gets Edit (a markdown textarea with a live preview tab) and Delete;
  "+ Add section" asks for a key and header. Content is sent as-is.
- **Verifications**: add form (type, target, pattern), remove ×, and "Run" showing pass/fail per row.
- **Attachments**: "Attach file" (file input, sent base64), gallery with detach ×.
- Destructive actions (abandon, supersede, delete section, remove verification, detach, release
  lease, withdraw decision) confirm in a dialog naming the object. Every write shows a toast with
  the server's message on success or refusal. Dialogs trap focus, close on Esc, and return focus to
  their trigger.

### 6.4 Decisions view

A third view beside Document and Graph, with an open-count badge on its toolbar button.

- **List** (left): tabs Open / Answered / Withdrawn. Open is sorted by priority, then oldest first.
  Each row shows the question, the number of tasks waiting, and age.
- **Detail** (right): question; raised-by link; "Waiting on this" chips for blocked tasks (click
  navigates to the task); context sections rendered with images; attachment gallery; options as
  selectable cards (label, description, a `Recommended` badge on the recommended one); a
  "Custom answer" card with a textarea (hidden when `allow_custom` is false); a rationale textarea;
  an **Answer** button, enabled once a choice is made. An answered decision shows the chosen card
  highlighted, the answer text and rationale, who answered and when, and **Reopen**. Open decisions
  also offer **Withdraw** (with a reason) and editing of blocked tasks.
- **New decision** dialog: question, context, an options editor (add/remove rows of
  key/label/description, a radio for recommended), allow-custom toggle, blocked tasks picker.
- In the document view, a task that is `AWAITING_DECISION` shows a banner linking each open
  decision it waits on. `AWAITING_DECISION` gets its own status theme (icon `help-circle`, amber)
  in `StatusVisual`, in the Blocked group.

### 6.5 Visual conventions

No Figma (ruled 2026-09-22). New UI follows the page's existing language: zinc surfaces,
emerald accent, `rounded-lg` controls at `h-8`, lucide icons via the existing sprite, and existing
chip styles. Every new control has hover, `focus-visible` ring, active and disabled states and
works at 375, 768 and 1440 px.

## 7. Acceptance

- `uv run --group dev pytest`, `ruff check`, `ruff format --check` and `mypy src` are green.
- A decision raised by CLI, answered in the web, unblocks its task (`tm next` offers it); reopening
  re-blocks it; withdrawing unblocks it.
- Every web write lands a ledger event with the actor, and a refused write changes nothing.
- A POST without `Content-Type: application/json`, or with a foreign `Origin`, is 403.
- `/api/file` refuses a path outside the root and a non-image; `/assets/` refuses a malformed name.
- The static export renders with no edit controls, and with image attachments inline.
- Design review at 375/768/1440: tri-state filters, every dialog, the decisions view (open,
  answered, withdrawn, empty, no-options custom-only, error toast), keyboard reachability and focus
  order, AA contrast, no console errors.
