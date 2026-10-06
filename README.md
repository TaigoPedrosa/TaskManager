# TaskManager

A local task tracker for agents: a SQLite graph of specs, plans and tasks that claims each step of a node, lands finished work on its parent's branch or on `main`, and verifies it there.

## Features

- One stored status per node, moved only by `tm task start` and the verb that closes each step; blocked, waiting and stale are worked out, never stored
- `review`, `fix` and `merge` flags on every node in place of separate review and fix tasks, on plans and specs too
- Landings as detached jobs: merge, gate against a cached baseline of the target, push, verify
- Edges, decisions and conditions as the only things a node waits on, with a cycle check on every write
- `state.db`, `cache.db` and `ledger.db` under `.taskmanager/`, SQLite in WAL mode, with `sqlite-vec` search
- The `tm-wave` workflow (`workflows/tm-wave.js`): one step per node per tick, on the model family tm names, with a dispatching session looping itself to carry a node the rest of the way

## Web

`tm web` serves the visualizer over one FastAPI app: `GET /` and static assets, the `/ws`
subscribe protocol, and a handful of paginated HTTP reads.

- `/ws` takes `{"type": "subscribe", "id", "filters", "open", "watch", "reset"}` and answers
  `snapshot` (full state, on `reset: true` or a hash mismatch) or `update` (a diff since the
  last message this session saw). A `LiveHub` (`src/taskmanager/web/live.py`) rebuilds its
  model at most four times a second, on `PRAGMA data_version` moving or a lease/condition
  deadline passing, and diffs it per session.
- `GET /api/statuses`, `GET /api/nodes` (`parent=` or `ids=`, `include=body`, cursor-paginated)
  and `GET /api/nodes/{id}` cover what a client reads over plain HTTP instead of the socket;
  `/api/tree`, `/api/graph` and `/api/stats` are gone.
- The wire contract — every message shape, the visible-set and facet rules, the row and body
  fields — is the plan `DATA-WEB` in this project's own `tm` estate (`tm render DATA-WEB`),
  not a doc here: `src/taskmanager/web/live.py`, `rows.py`, `visibility.py` and `bodies.py` are
  its implementation, and `tests/fixtures/statuses_hash.json` /
  `tests/fixtures/visibility_cases.json` are its golden vectors.
- The page opens on one of three views (`WebViewMode`, `src/taskmanager/web/enums.py`): `waves`
  (the default for `tm web` and for a live connection), `graph`, the node graph and inspector,
  and `document`, one card per spec/plan/task walked from the roots down — the view a static
  export (`tm web export`) opens on, since it renders straight from the embedded data.
- Waves shows what `tm wave discover` would claim next, simulated forward from live state
  without claiming anything: a wave-size input, the same spec include filter as the graph view,
  a "Compute next wave" button that adds one wave on top of the last, and "Reset" back to wave
  1. Each card lists its claimable entries (action, model, repos, status before/after) and a
  collapsible "Held" list of what the wave skipped and why.
- `GET /api/waves?depth=&size=&spec=` (`src/taskmanager/web/app.py`) runs that same simulation
  server-side over one snapshot of `state.db` and the cached conditions, and returns
  `{"waves": [...], "max_depth": n}`; `depth` and `size` are bounds-checked server-side
  regardless of what the client sends, and the page stops "Compute next wave" at `max_depth`.
- The fourth view, Decisions (`web/static/js/decisions.js`), pages `GET /api/decisions`
  (`status=open|answered|withdrawn`, cursor-paginated) into three tabs; the Open badge tracks
  the live `decisions_open` count from every snapshot/update. Opening a decision shows its
  question, context and options in the same drawer the Waves and Graph views use
  (`showGraphInspector`, `detail.js`). An open decision answers with a picked option or a
  custom answer, plus an optional rationale, over `POST /api/decisions/{id}/answer`; withdrawing
  takes a reason over `POST /api/decisions/{id}/withdraw`, and either can be reopened.

## Upgrading from 0.2

0.3.0 does not open an estate written by 0.2: it refuses with the command to run. Export the old estate with the old version first; `tm init --archive` then moves the old files aside and starts fresh, and the work still in flight is re-imported. `tm guide overview` carries the whole runbook.

## Upgrading to 0.3.1

- `state.db` moves to schema 2 on first open (adds `nodes.rev` and its update triggers); a
  0.3.0 checkout still opens a 0.3.1 `state.db` unchanged, since 0.3.0 never reads that column.
- An `owed` section is refused on write. Register the owed work as its own spec, plan or task,
  depending on the node that owed it, instead of writing it into a section.
- The `tm-wave` workflow script and the `tm` binary upgrade together: the 0.3.1 script reads
  with `tm task get --fields`, which a 0.3.0 `tm` refuses as an unknown option, so a checkout
  pulled without reinstalling `tm` fails every read.

## Development

```bash
uv sync
uv run pytest
node --test tests/
```

### Rebuilding the web stylesheet

`src/taskmanager/web/static/tailwind.css` is committed, built from `tailwind.config.js` and
`src/taskmanager/web/static/tailwind.input.css` by Tailwind's standalone CLI (no Node package,
pinned at v3.4.19). Fetch the binary for your platform from the release page and rebuild after
touching `tailwind.config.js`, the input stylesheet, or any file its `content` globs cover:

```bash
curl -sSL -o tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.19/tailwindcss-macos-arm64
chmod +x tailwindcss
./tailwindcss -i src/taskmanager/web/static/tailwind.input.css -c tailwind.config.js \
  -o src/taskmanager/web/static/tailwind.css
```

Swap `tailwindcss-macos-arm64` for `tailwindcss-linux-x64`, `tailwindcss-linux-arm64`,
`tailwindcss-macos-x64` or a Windows build to match your platform.

`tests/web/stylesheet.test.mjs` rebuilds the sheet and fails when it differs from the committed
one. Without the CLI it fails too, never skips, so `node --test tests/` needs it. The test looks
at `TAILWINDCSS_BIN`, then `./tailwindcss` at the repository root, then `PATH`. To keep the
binary elsewhere, point the variable at it:

```bash
TAILWINDCSS_BIN="$HOME/.local/share/tm-tools/tailwindcss" node --test tests/
```

Run the build from the repository root: the `content` globs resolve against the working
directory, and from anywhere else the CLI finds no classes and writes a sheet without utilities.
