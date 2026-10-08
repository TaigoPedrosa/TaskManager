# Web protocol

`tm web` serves the visualizer over one FastAPI app (`src/taskmanager/web/app.py`): the page at
`/` and each view path, static assets, the `/ws` subscribe protocol, and a handful of paginated
HTTP reads. This is the contract a client of that server builds against.

## The live socket

- `/ws` takes `{"type": "subscribe", "id", "filters", "open", "watch", "reset"}` and answers
  `snapshot` (full state, on `reset: true` or a hash mismatch) or `update` (a diff since the
  last message this session saw). A `LiveHub` (`src/taskmanager/web/live.py`) rebuilds its
  model at most four times a second, on `PRAGMA data_version` moving or a lease/condition
  deadline passing, and diffs it per session.
- Every message shape, the visible-set and facet rules, and the row and body fields are
  implemented in `src/taskmanager/web/live.py`, `rows.py`, `visibility.py` and `bodies.py`.
  `tests/fixtures/statuses_hash.json` and `tests/fixtures/visibility_cases.json` are their
  golden vectors.

## HTTP reads

- `GET /api/statuses`, `GET /api/nodes` (`parent=` or `ids=`, `include=body`, cursor-paginated)
  and `GET /api/nodes/{id}` cover what a client reads over plain HTTP instead of the socket.
- `GET /api/waves?depth=&size=&spec=` runs the Waves simulation server-side over one snapshot of
  `state.db` and the cached conditions, and returns `{"waves": [...], "max_depth": n}`; `depth`
  and `size` are bounds-checked server-side regardless of what the client sends, and the page
  stops "Compute next wave" at `max_depth`.
- `GET /api/decisions` (`status=open|answered|withdrawn`, cursor-paginated) pages the Decisions
  view (`web/static/js/decisions.js`); the Open badge tracks the live `decisions_open` count
  from every snapshot and update.

## Writes

The other routes under `/api` serve the board's own controls: creating specs, plans, tasks and
decisions, and changing a node's dependencies, sections, verifications, conditions, lease,
attachments or status, or a decision's answer, blocks or status. `src/taskmanager/web/app.py`
lists them. Answering a decision is `POST /api/decisions/{id}/answer`, with a picked option or a
custom answer plus an optional rationale; withdrawing is `POST /api/decisions/{id}/withdraw`,
with a reason; either is undone by `POST /api/decisions/{id}/reopen`.

## Host and origin

Every request and socket is refused when its `Host` is not one the server admits, or when it
carries an `Origin` other than that host: an HTTP request with 403, a socket by closing it with
1008 before it is accepted, so it never receives a frame.
