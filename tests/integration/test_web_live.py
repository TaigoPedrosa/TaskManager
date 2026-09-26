"""`LiveHub` and the `/ws` subscription protocol: one detect-rebuild-broadcast cycle per
`refresh()`, diffed per session against what that session already holds."""

import asyncio
import functools
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import FileLock, Lease, Node, NodeRelation, NodeSection
from taskmanager.core.status import Action, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.web.app import create_app
from taskmanager.web.live import LiveHub, Session
from taskmanager.web.rows import statuses_hash

# tests/perf/estate.py has no __init__.py alongside it, so it is only importable once its own
# directory is on sys.path -- true already when a full run collects it first, not when this
# file's own suite runs alone (as `live-suite`'s verification does).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "perf"))
from estate import seed as seed_estate


def run_async[**P](fn: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    """Runs an `async def test_...` on its own event loop: no test in this project uses
    pytest-asyncio, and one function's worth of `asyncio.run` beats adding it."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class Estate:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self.node_repo = NodeRepository(db)
        self.runtime_repo = RuntimeRepository(db)
        self.job_repo = JobRepository(db)
        self.cache = CacheRepository(db)
        self.builder = SnapshotBuilder(self.node_repo, self.runtime_repo, self.job_repo)
        self.assets_dir = db.taskmanager_dir / "assets"

    @classmethod
    def fresh(cls, root: Path) -> Estate:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        return cls(db)

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        **fields: object,
    ) -> None:
        self.node_repo.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def lease(
        self, node_id: str, ttl_seconds: int | None = 300, last_heartbeat: datetime | None = None
    ) -> None:
        self.runtime_repo.acquire_lease(
            Lease(
                task_id=node_id,
                agent_id="agent-1",
                session_id="s1",
                branch_name=f"tm/{node_id}",
                action=Action.IMPLEMENT,
                ttl_seconds=ttl_seconds,
                last_heartbeat=last_heartbeat or datetime.now(tz=UTC),
            ),
            [FileLock(file_path=f"src/{node_id}.py", task_id=node_id)],
        )

    def hub(self, clock: Any = None) -> LiveHub:
        kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
        return LiveHub(
            snapshots=self.builder,
            cache=self.cache,
            node_repo=self.node_repo,
            job_repo=self.job_repo,
            assets_dir=self.assets_dir,
            condition_ttl=lambda: 300,
            state_db=self.db.state_db,
            cache_db=self.db.cache_db,
            **kwargs,
        )


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate.fresh(tmp_path)


def _socket(session: Session) -> FakeSocket:
    assert isinstance(session.websocket, FakeSocket)
    return session.websocket


async def subscribe(
    hub: LiveHub,
    session: Session,
    req_id: int,
    *,
    open: list[str] | None = None,
    watch: list[str] | None = None,
    reset: bool = False,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    frame = {
        "type": "subscribe",
        "id": req_id,
        "filters": filters or {},
        "open": open or [],
        "watch": watch or [],
        "reset": reset,
    }
    await hub.handle_frame(session, json.dumps(frame))
    return _socket(session).sent[-1]


# -- reset snapshot ---------------------------------------------------------------------------


@run_async
async def test_reset_subscribe_with_nothing_open_answers_roots_only(estate: Estate) -> None:
    estate.add("S1", NodeKind.SPEC)
    estate.add("P1", NodeKind.PLAN, parent="S1")
    estate.add("T1", NodeKind.TASK, parent="P1")

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    message = await subscribe(hub, session, 1, reset=True)

    assert message["type"] == "snapshot"
    assert message["re"] == 1
    assert [row["id"] for row in message["rows"]] == ["S1"]
    assert message["bodies"] == {}
    assert message["hash"] == statuses_hash(message["statuses"])


# -- opening and closing a plan ----------------------------------------------------------------


@run_async
async def test_opening_a_plan_sends_rows_and_edges_closing_sends_drops(estate: Estate) -> None:
    estate.add("P1", NodeKind.PLAN)
    estate.add("T1", NodeKind.TASK, parent="P1")
    estate.add("T2", NodeKind.TASK, parent="P1")

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, reset=True)

    opened = await subscribe(hub, session, 2, open=["P1"])
    assert opened["type"] == "update"
    row_ids = {item["row"]["id"] for item in opened["items"] if item["op"] == "row"}
    assert row_ids == {"T1", "T2"}
    edges_items = [item for item in opened["items"] if item["op"] == "edges"]
    assert edges_items
    contained = {tuple(pair[:2]) for pair in edges_items[0]["add"]}
    assert ("P1", "T1") in contained
    assert ("P1", "T2") in contained

    closed = await subscribe(hub, session, 3, open=[])
    assert {item["id"] for item in closed["items"] if item["op"] == "drop"} == {"T1", "T2"}


# -- a status change ---------------------------------------------------------------------------


@run_async
async def test_status_change_in_an_open_plan_sends_row_statuses_and_hash(estate: Estate) -> None:
    estate.add("S1", NodeKind.SPEC)
    estate.add("P1", NodeKind.PLAN, parent="S1")
    estate.add("T1", NodeKind.TASK, parent="P1", status=Status.READY, target_repo="api")

    hub = estate.hub()
    await hub.refresh()

    watcher = hub.open_session(FakeSocket())
    await subscribe(hub, watcher, 1, open=["S1", "P1"], reset=True)

    # A different repo, so T1's status change can never touch this session's facet counts:
    # its own repo filter still applies when computing every dimension but "repo" itself.
    onlooker = hub.open_session(FakeSocket())
    await subscribe(hub, onlooker, 1, open=[], filters={"repo": "other"}, reset=True)

    t1 = estate.node_repo.get_node("T1")
    assert t1 is not None
    estate.node_repo.save_node(
        t1.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": Status.READY})
    )
    await hub.refresh()

    watcher_msg = _socket(watcher).sent[-1]
    assert watcher_msg["re"] is None
    ops = {item["op"] for item in watcher_msg["items"]}
    assert {"row", "plan_counts"} <= ops
    row_ids = {item["row"]["id"] for item in watcher_msg["items"] if item["op"] == "row"}
    assert "T1" in row_ids
    assert watcher_msg["hash"] == hub.hash

    # Both sessions hold the same spec's whole entry already, so the count move travels as
    # its own plan_counts item, never as a re-send of the whole (unchanged, single-plan) entry.
    onlooker_msg = _socket(onlooker).sent[-1]
    assert {item["op"] for item in onlooker_msg["items"]} == {"plan_counts"}


# -- a section write ---------------------------------------------------------------------------


@run_async
async def test_section_write_on_a_watched_node_sends_one_section_item(estate: Estate) -> None:
    # Under a closed plan, so T1's own row is never visible: watching a body and having its
    # row on screen are independent, and this isolates the section diff from a row diff (its
    # `rev` bumps on every section write, whether or not anyone is watching).
    estate.add("P1", NodeKind.PLAN)
    estate.add("T1", NodeKind.TASK, parent="P1")

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, open=[], watch=["T1"], reset=True)
    # A rebuild only re-reads a watched id's body when its `rev` moved, and subscribing
    # itself writes nothing -- so T1's body does not exist yet. Re-saving it unchanged still
    # bumps its `rev` (the trigger fires whenever a write leaves `rev` untouched), which forces
    # the one rebuild that builds it, before the write under test.
    t1 = estate.node_repo.get_node("T1")
    assert t1 is not None
    estate.node_repo.save_node(t1)
    await hub.refresh()

    estate.node_repo.save_section(
        NodeSection(node_id="T1", section_key="body", ordinal=0, header="## Body", content="x")
    )
    await hub.refresh()

    msg = _socket(session).sent[-1]
    assert [item["op"] for item in msg["items"]] == ["section"]
    assert msg["items"][0] == {
        "op": "section",
        "id": "T1",
        "key": "body",
        "section": {"header": "## Body", "content": "x", "ordinal": 0},
    }


@run_async
async def test_section_write_on_an_unwatched_invisible_node_sends_nothing(estate: Estate) -> None:
    estate.add("P1", NodeKind.PLAN)
    estate.add("T1", NodeKind.TASK, parent="P1")

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, open=[], watch=[], reset=True)

    estate.node_repo.save_section(
        NodeSection(node_id="T1", section_key="body", ordinal=0, header="## Body", content="x")
    )
    await hub.refresh()

    assert len(_socket(session).sent) == 1  # only the initial snapshot


# -- leases ---------------------------------------------------------------------------------


@run_async
async def test_a_lease_heartbeat_sends_nothing(estate: Estate) -> None:
    estate.add("T1", NodeKind.TASK, status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.lease("T1", ttl_seconds=300)

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, open=["T1"], reset=True)

    estate.runtime_repo.heartbeat("T1")
    await hub.refresh()

    assert len(_socket(session).sent) == 1


@run_async
async def test_a_lease_expiring_with_no_write_sends_its_stale_row_once_the_deadline_passes(
    estate: Estate,
) -> None:
    estate.add("T1", NodeKind.TASK, status=Status.IMPLEMENTING, claimed_from=Status.READY)
    heartbeat = datetime.now(tz=UTC)
    estate.lease("T1", ttl_seconds=1, last_heartbeat=heartbeat)

    clock_box = {"now": heartbeat}
    hub = estate.hub(clock=lambda: clock_box["now"])
    await hub.refresh()
    assert hub.rows["T1"]["display"] == "IMPLEMENTING"
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, open=["T1"], reset=True)

    # No write happens; only real time (the lease's own liveness check) and the injected
    # clock (the hub's deadline check) move past the lease's TTL.
    await asyncio.sleep(1.2)
    clock_box["now"] = heartbeat + timedelta(seconds=2)
    await hub.refresh()

    msg = _socket(session).sent[-1]
    stale_rows = [
        item["row"] for item in msg["items"] if item["op"] == "row" and item["row"]["id"] == "T1"
    ]
    assert stale_rows and stale_rows[0]["display"] == "STALE"


# -- malformed frames -------------------------------------------------------------------------


@run_async
async def test_a_malformed_frame_answers_error_and_leaves_subscription_unchanged(
    estate: Estate,
) -> None:
    estate.add("S1", NodeKind.SPEC)
    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, reset=True)
    before_rows, before_open = dict(session.sent_rows), set(session.open)

    await hub.handle_frame(session, "not json")
    await hub.handle_frame(session, json.dumps({"type": "ping"}))
    await hub.handle_frame(session, json.dumps({"type": "subscribe", "id": 2, "open": "S1"}))
    await hub.handle_frame(
        session,
        json.dumps(
            {"type": "subscribe", "id": 3, "watch": [f"n{i}" for i in range(_MAX_WATCH_PLUS_ONE)]}
        ),
    )

    assert all(m["type"] == "error" for m in _socket(session).sent[1:])
    assert session.sent_rows == before_rows
    assert session.open == before_open


_MAX_WATCH_PLUS_ONE = 201


# -- filters -----------------------------------------------------------------------------------


@run_async
async def test_a_filter_change_answers_rows_in_and_out_and_a_facets_item(estate: Estate) -> None:
    estate.add("P1", NodeKind.PLAN)
    estate.add("T1", NodeKind.TASK, parent="P1", target_repo="api")
    estate.add("T2", NodeKind.TASK, parent="P1", target_repo="web")

    hub = estate.hub()
    await hub.refresh()
    session = hub.open_session(FakeSocket())
    await subscribe(hub, session, 1, open=["P1"], reset=True)

    changed = await subscribe(hub, session, 2, open=["P1"], filters={"repo": "api"})
    ops = {item["op"] for item in changed["items"]}
    assert "facets" in ops
    drop_ids = {item["id"] for item in changed["items"] if item["op"] == "drop"}
    assert "T2" in drop_ids


# -- ConnectionManager and friends are gone ----------------------------------------------------


def test_ledger_watcher_last_event_id_and_connection_manager_are_gone_from_app_py() -> None:
    import taskmanager.web.app as app_module

    source = Path(app_module.__file__).read_text()
    for gone in ("ledger_watcher", "last_event_id", "ConnectionManager"):
        assert gone not in source


def test_the_real_app_answers_ws_with_the_protocol(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    NodeRepository(db).save_node(Node(id="S1", kind=NodeKind.SPEC, title="S1"))

    # `with` runs the app's lifespan (uvicorn always does; a client built without it never
    # sends the ASGI startup message, so the hub would never have rebuilt at all).
    with TestClient(create_app(tmp_path)) as client, client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "subscribe", "id": 1, "filters": {}, "open": [], "watch": [], "reset": True}
        )
        message = ws.receive_json()

    assert message["type"] == "snapshot"
    assert [row["id"] for row in message["rows"]] == ["S1"]


# -- budgets on the 1,000-node estate -----------------------------------------------------------


def test_budgets_on_the_1000_node_estate(tmp_path: Path) -> None:
    node_repo = seed_estate(tmp_path, plans=100)
    estate = Estate(node_repo.db)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        hub = estate.hub()
        await hub.refresh()

        idle = hub.open_session(FakeSocket())
        idle_snapshot = await subscribe(hub, idle, 1, reset=True)

        watcher = hub.open_session(FakeSocket())
        await subscribe(hub, watcher, 1, open=["SPEC", "P0"], reset=True)

        task = estate.node_repo.get_node("P0-T0")
        assert task is not None
        estate.node_repo.save_node(
            task.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": Status.READY})
        )
        await hub.refresh()

        return idle_snapshot, _socket(watcher).sent[-1]

    idle_snapshot, status_update = asyncio.run(run())

    assert len(json.dumps(idle_snapshot).encode("utf-8")) <= 20_000
    assert len(json.dumps(status_update).encode("utf-8")) <= 3_000
