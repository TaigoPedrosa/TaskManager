"""`LiveHub`: one published model per app, one `Session` per `/ws` socket, diffed against what
that socket already holds. `refresh()` is the whole detect-rebuild-broadcast cycle, called on a
timer in `app.py` and directly by tests."""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from taskmanager.core.models import Job, Lease
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.graph_reader import GraphData
from taskmanager.db.job_repo import _COLUMNS as _JOB_COLUMNS
from taskmanager.db.job_repo import JobRepository, _row_to_job
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.utils import parse_db_datetime
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.web.bodies import (
    BodyRepos,
    _condition_result,
    body_items,
    build_bodies,
    lease_dict,
    refresh_relations,
)
from taskmanager.web.rows import build_rows, decisions_open, row_digest, statuses, statuses_hash
from taskmanager.web.visibility import Filters, facets, parse_filters, project_edges, visible_ids

_MAX_WATCH = 200


def _bulk_jobs(job_repo: JobRepository, ids: Sequence[str]) -> dict[str, list[Job]]:
    # Mirrors `bodies._bulk_jobs`: every job a node ever ran, one query for the whole set.
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with job_repo.db.get_state_connection() as conn:
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE node_id IN ({placeholders}) ORDER BY rowid ASC",
            tuple(ids),
        ).fetchall()
    jobs: dict[str, list[Job]] = {}
    for r in rows:
        job = _row_to_job(r)
        jobs.setdefault(job.node_id, []).append(job)
    return jobs


def _runtime_parts(
    repos: BodyRepos, data: GraphData, ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Lease, jobs and conditions' cached results for `ids` -- the body parts a rev bump does
    not cover, since none of them live in the node's own row or in a table a rev trigger
    watches. One bulk query each, regardless of how many ids are asked for."""
    jobs = _bulk_jobs(repos.job_repo, ids)
    cached_conditions = repos.cache.all_conditions(repos.condition_ttl)
    parts: dict[str, dict[str, Any]] = {}
    for node_id in ids:
        parts[node_id] = {
            "lease": lease_dict(data.leases.get(node_id)),
            "jobs": [j.model_dump(mode="json") for j in jobs.get(node_id, [])],
            "conditions": [
                {
                    "idx": c.idx,
                    "needs": c.needs,
                    "command": c.command,
                    "stage": c.stage.value,
                    "last_result": _condition_result(cached_conditions, node_id, c),
                }
                for c in data.conditions.get(node_id, [])
            ],
        }
    return parts


def _next_deadline(
    leases: Mapping[str, Lease], cache_conn: sqlite3.Connection, condition_ttl: int
) -> datetime | None:
    """The earliest instant a display could flip on its own, with no write to trigger a
    rebuild: a live lease going stale, or a cached condition result ageing out of its TTL."""
    deadlines = [
        lease.last_heartbeat + timedelta(seconds=lease.ttl_seconds)
        for lease in leases.values()
        if lease.ttl_seconds is not None
    ]
    rows = cache_conn.execute("SELECT checked_at FROM condition_results").fetchall()
    if rows:
        earliest_checked = min(parse_db_datetime(r[0]) for r in rows)
        deadlines.append(earliest_checked + timedelta(seconds=condition_ttl))
    return min(deadlines) if deadlines else None


class SubscribeFrame(BaseModel):
    id: int
    filters: dict[str, str] = Field(default_factory=dict)
    open: list[str] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    reset: bool = False


class Socket(Protocol):
    """What a session's transport needs to be: `LiveHub` never accepts, receives or closes
    it, so a real `WebSocket` and a test double are the same thing to it."""

    async def send_json(self, data: Any) -> None: ...


@dataclass
class Session:
    websocket: Socket
    filters: Filters = field(default_factory=Filters)
    open: set[str] = field(default_factory=set)
    watch: set[str] = field(default_factory=set)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # What this socket already holds, so the next diff starts from what it was actually sent
    # rather than from the hub's own last-published state.
    sent_rows: dict[str, str] = field(default_factory=dict)
    sent_edges: set[tuple[str, ...]] = field(default_factory=set)
    # Which spec entries this socket holds (statuses items travel only on entry add/remove);
    # per-(spec, plan) counts digest is tracked separately, since a count change travels as
    # its own item instead.
    sent_statuses: set[str | None] = field(default_factory=set)
    sent_plan_counts: dict[tuple[str | None, str | None], str] = field(default_factory=dict)
    sent_facets: str | None = None
    sent_decisions_open: int | None = None
    sent_bodies: dict[str, dict[str, Any]] = field(default_factory=dict)


class LiveHub:
    def __init__(
        self,
        snapshots: SnapshotBuilder,
        cache: CacheRepository,
        node_repo: NodeRepository,
        job_repo: JobRepository,
        assets_dir: Path,
        condition_ttl: Callable[[], int],
        state_db: Path,
        cache_db: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._snapshots = snapshots
        self._cache = cache
        self._node_repo = node_repo
        self._job_repo = job_repo
        self._assets_dir = assets_dir
        self._condition_ttl = condition_ttl
        self._clock = clock
        # Dedicated connections that never write: a connection sees its own commits as no
        # change, so the ones a rebuild reads and writes through could never see the change
        # they exist to detect.
        self._state_conn = sqlite3.connect(str(state_db), check_same_thread=False)
        self._cache_conn = sqlite3.connect(str(cache_db), check_same_thread=False)
        self._last_state_version: int | None = None
        self._last_cache_version: int | None = None
        self._deadline: datetime | None = None
        # Guards a rebuild's diff step against a subscribe answered mid-cycle: both touch a
        # session's `sent_*` fields, and a subscribe must always start from what was last
        # actually sent.
        self._lock = asyncio.Lock()
        # `self.sessions` is read (as a list, for the rebuild's diff pass) from the thread
        # `asyncio.to_thread` runs it on, while a socket connecting or closing mutates it from
        # the event loop thread -- a plain dict is not safe for that on its own.
        self._sessions_lock = threading.Lock()
        self.sessions: dict[int, Session] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.statuses: list[dict[str, Any]] = []
        self.hash: str = statuses_hash([])
        self.decisions_open: int = 0
        self._raw_edges: list[tuple[str, str, str]] = []
        self._bodies: dict[str, dict[str, Any]] = {}

    # -- sessions -----------------------------------------------------------------------------

    def open_session(self, websocket: Socket) -> Session:
        session = Session(websocket=websocket)
        with self._sessions_lock:
            self.sessions[id(session)] = session
        return session

    def close_session(self, session: Session) -> None:
        with self._sessions_lock:
            self.sessions.pop(id(session), None)

    async def _send(self, session: Session, message: dict[str, Any]) -> None:
        async with session.send_lock:
            try:
                await session.websocket.send_json(message)
            except WebSocketDisconnect, RuntimeError, OSError:
                pass

    # -- incoming frames ------------------------------------------------------------------------

    async def handle_frame(self, session: Session, text: str) -> None:
        # Off the event loop, same as a rebuild: building a newly-watched id's body below
        # reads the state and cache dbs, and a subscribe frame must never block other sockets
        # on that any more than `refresh()` blocks them on its own rebuild.
        async with self._lock:
            message = await asyncio.to_thread(self._process_frame, session, text)
        await self._send(session, message)

    def _ensure_bodies(self, ids: Iterable[str]) -> None:
        """A session that starts watching an id must see its body right away, not once some
        unrelated write next triggers a rebuild -- on an idle estate that rebuild may never
        come. `wanted` is filtered to known rows so a bogus or stale id builds nothing."""
        wanted = [i for i in dict.fromkeys(ids) if i not in self._bodies and i in self.rows]
        if not wanted:
            return
        view = DisplayView(self._snapshots, self._cache, self._condition_ttl())
        repos = BodyRepos(
            node_repo=self._node_repo,
            job_repo=self._job_repo,
            cache=self._cache,
            condition_ttl=self._condition_ttl(),
            assets_dir=self._assets_dir,
        )
        self._bodies.update(build_bodies(view, wanted, repos=repos))

    def _process_frame(self, session: Session, text: str) -> dict[str, Any]:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return {"type": "error", "re": None, "detail": "invalid JSON"}
        req_id = raw.get("id") if isinstance(raw, dict) else None
        req_id = req_id if isinstance(req_id, int) and not isinstance(req_id, bool) else None
        if not isinstance(raw, dict) or raw.get("type") != "subscribe":
            return {"type": "error", "re": req_id, "detail": "unknown frame type"}
        try:
            frame = SubscribeFrame.model_validate(raw)
        except ValidationError as exc:
            return {"type": "error", "re": req_id, "detail": str(exc)}
        if len(frame.watch) > _MAX_WATCH:
            return {"type": "error", "re": frame.id, "detail": "watch holds at most 200 ids"}
        try:
            new_filters = parse_filters(frame.filters)
        except ValueError as exc:
            return {"type": "error", "re": frame.id, "detail": str(exc)}

        session.filters = new_filters
        session.open = set(frame.open)
        session.watch = set(frame.watch)
        self._ensure_bodies(session.watch)

        if frame.reset:
            session.sent_rows = {}
            session.sent_edges = set()
            session.sent_statuses = set()
            session.sent_plan_counts = {}
            session.sent_facets = None
            session.sent_decisions_open = None
            session.sent_bodies = {}
            return self._snapshot_message(session, frame.id)
        return self._update_message(session, frame.id)

    # -- per-session views ----------------------------------------------------------------------

    def _visible(self, session: Session) -> tuple[list[str], list[list[str]], dict[str, Any]]:
        visible = visible_ids(self.rows, session.filters, list(session.open))
        edges = project_edges(self._raw_edges, self.rows, visible)
        facet_data = facets(self.rows, session.filters)
        return visible, edges, facet_data

    def _snapshot_message(self, session: Session, req_id: int) -> dict[str, Any]:
        visible, edges, facet_data = self._visible(session)
        bodies = {i: self._bodies[i] for i in session.watch if i in self._bodies}
        session.sent_rows = {i: row_digest(self.rows[i]) for i in visible}
        session.sent_edges = {tuple(e) for e in edges}
        session.sent_statuses = {e["spec"] for e in self.statuses}
        session.sent_plan_counts = {
            (e["spec"], p["plan"]): row_digest(p["counts"])
            for e in self.statuses
            for p in e["plans"]
        }
        session.sent_facets = row_digest(facet_data)
        session.sent_decisions_open = self.decisions_open
        session.sent_bodies = dict(bodies)
        return {
            "type": "snapshot",
            "re": req_id,
            "hash": self.hash,
            "statuses": self.statuses,
            "facets": facet_data,
            "decisions_open": self.decisions_open,
            "rows": [self.rows[i] for i in visible],
            "edges": edges,
            "bodies": bodies,
        }

    def _update_message(self, session: Session, req_id: int | None) -> dict[str, Any]:
        return {
            "type": "update",
            "re": req_id,
            "hash": self.hash,
            "items": self._update_items(session),
        }

    def _update_items(self, session: Session) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        visible, edges, facet_data = self._visible(session)
        visible_set = set(visible)

        new_row_digests = {i: row_digest(self.rows[i]) for i in visible}
        for node_id, digest in new_row_digests.items():
            if session.sent_rows.get(node_id) != digest:
                items.append({"op": "row", "row": self.rows[node_id]})
        for node_id in session.sent_rows:
            if node_id not in visible_set:
                items.append({"op": "drop", "id": node_id})
        session.sent_rows = new_row_digests

        new_edge_set = {tuple(e) for e in edges}
        if new_edge_set != session.sent_edges:
            add = [list(e) for e in new_edge_set - session.sent_edges]
            remove = [list(e) for e in session.sent_edges - new_edge_set]
            items.append({"op": "edges", "add": add, "remove": remove})
        session.sent_edges = new_edge_set

        # A `statuses` item only ever carries a spec entry appearing or disappearing whole
        # (a spec created or deleted, or the first/last plan under spec `null`); a count
        # change inside an entry that stays travels as its own `plan_counts` item instead, so
        # one plan's count moving never resends every other plan in the same spec.
        entry_by_spec = {e["spec"]: e for e in self.statuses}
        new_spec_keys = set(entry_by_spec)
        added_specs = new_spec_keys - session.sent_statuses
        removed_specs = session.sent_statuses - new_spec_keys
        for spec_id in added_specs:
            items.append({"op": "statuses", "spec": spec_id, "entry": entry_by_spec[spec_id]})
        for spec_id in removed_specs:
            items.append({"op": "statuses", "spec": spec_id, "entry": None})
        session.sent_statuses = new_spec_keys

        new_plan_counts = {
            (spec_id, p["plan"]): row_digest(p["counts"])
            for spec_id, entry in entry_by_spec.items()
            for p in entry["plans"]
        }
        for spec_id, entry in entry_by_spec.items():
            if spec_id in added_specs:
                continue  # already carried whole, inside the entry just sent above
            for p in entry["plans"]:
                key = (spec_id, p["plan"])
                if session.sent_plan_counts.get(key) != new_plan_counts[key]:
                    items.append(
                        {
                            "op": "plan_counts",
                            "spec": spec_id,
                            "plan": p["plan"],
                            "counts": p["counts"],
                        }
                    )
        for spec_id, plan_id in session.sent_plan_counts:
            if spec_id in removed_specs:
                continue  # already dropped whole, by the entry: None just sent above
            if (spec_id, plan_id) not in new_plan_counts:
                items.append(
                    {"op": "plan_counts", "spec": spec_id, "plan": plan_id, "counts": None}
                )
        session.sent_plan_counts = new_plan_counts

        facets_digest = row_digest(facet_data)
        if facets_digest != session.sent_facets:
            items.append({"op": "facets", "facets": facet_data})
        session.sent_facets = facets_digest

        if session.sent_decisions_open != self.decisions_open:
            items.append({"op": "decisions_open", "count": self.decisions_open})
        session.sent_decisions_open = self.decisions_open

        for node_id in session.watch:
            new_body = self._bodies.get(node_id)
            if new_body is None:
                continue
            old_body = session.sent_bodies.get(node_id)
            if old_body == new_body:
                continue
            items.extend(body_items(node_id, old_body, new_body))
            session.sent_bodies[node_id] = new_body
        for node_id in list(session.sent_bodies):
            if node_id not in session.watch:
                del session.sent_bodies[node_id]

        return items

    # -- detect, rebuild, broadcast -------------------------------------------------------------

    async def refresh(self) -> None:
        if not self._should_rebuild():
            return
        async with self._lock:
            broadcasts = await asyncio.to_thread(self._rebuild_and_diff)
        for session, items in broadcasts:
            if items:
                await self._send(
                    session, {"type": "update", "re": None, "hash": self.hash, "items": items}
                )

    def _should_rebuild(self) -> bool:
        state_version = self._state_conn.execute("PRAGMA data_version").fetchone()[0]
        cache_version = self._cache_conn.execute("PRAGMA data_version").fetchone()[0]
        changed = (
            state_version != self._last_state_version or cache_version != self._last_cache_version
        )
        due = self._deadline is not None and self._clock() >= self._deadline
        if not changed and not due:
            return False
        self._last_state_version = state_version
        self._last_cache_version = cache_version
        return True

    def _rebuild_and_diff(self) -> list[tuple[Session, list[dict[str, Any]]]]:
        self._rebuild()
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        return [(session, self._update_items(session)) for session in sessions]

    def _rebuild(self) -> None:
        view = DisplayView(self._snapshots, self._cache, self._condition_ttl())
        data = view.snapshot.graph_data()
        new_rows = build_rows(view)
        new_statuses = statuses(new_rows)
        raw_edges = [(source, target, "depends_on") for source, target in view.snapshot.edges]

        with self._sessions_lock:
            watched = {i for session in self.sessions.values() for i in session.watch}

        changed_ids = [
            node_id
            for node_id in watched
            if node_id not in self._bodies
            or self.rows.get(node_id, {}).get("rev") != new_rows.get(node_id, {}).get("rev")
        ]
        repos = BodyRepos(
            node_repo=self._node_repo,
            job_repo=self._job_repo,
            cache=self._cache,
            condition_ttl=self._condition_ttl(),
            assets_dir=self._assets_dir,
        )
        if changed_ids:
            self._bodies.update(build_bodies(view, changed_ids, repos=repos))
        stale = {i: self._bodies[i] for i in watched if i not in changed_ids and i in self._bodies}
        if stale:
            relations = refresh_relations(view, data, stale)
            for node_id, parts in _runtime_parts(repos, data, list(stale)).items():
                self._bodies[node_id] = {
                    **self._bodies[node_id],
                    **parts,
                    **relations.get(node_id, {}),
                }
        for node_id in list(self._bodies):
            if node_id not in watched:
                del self._bodies[node_id]

        self.rows = new_rows
        self.statuses = new_statuses
        self.hash = statuses_hash(new_statuses)
        self.decisions_open = decisions_open(view)
        self._raw_edges = raw_edges
        self._deadline = _next_deadline(data.leases, self._cache_conn, self._condition_ttl())
