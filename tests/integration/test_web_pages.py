"""Non-live HTTP reads: `/api/statuses`, `/api/nodes` (filtered, keyset-paged, `include=body`)
and `/api/decisions` (paginated, newest first)."""

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Node, NodeRelation
from taskmanager.core.status import DecisionStatus, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.web.app import create_app, paginate_nodes
from taskmanager.web.bodies import BodyRepos
from taskmanager.web.live import LiveHub
from taskmanager.web.rows import build_rows

# tests/perf/estate.py has no __init__.py alongside it, so it is only importable once its own
# directory is on sys.path -- true already when a full run collects it first, not when this
# file's own suite runs alone (as `pages-suite`'s verification does).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "perf"))
from estate import seed as seed_estate

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "visibility_cases.json").read_text()
)


def _seed_basic(tmp_path: Path) -> NodeRepository:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="S1", kind=NodeKind.SPEC, title="Spec One"))
    node_repo.save_node(Node(id="S1-P1", kind=NodeKind.PLAN, title="Plan One"))
    node_repo.add_relation(
        NodeRelation(source_id="S1", target_id="S1-P1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(
        Node(id="S1-P1-T1", kind=NodeKind.TASK, title="Task One", priority=70, status=Status.READY)
    )
    node_repo.add_relation(
        NodeRelation(source_id="S1-P1", target_id="S1-P1-T1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(
        Node(id="S1-P1-T2", kind=NodeKind.TASK, title="Task Two", priority=60, status=Status.READY)
    )
    node_repo.add_relation(
        NodeRelation(source_id="S1-P1", target_id="S1-P1-T2", relation_type=RelationType.CONTAINS)
    )
    return node_repo


def _seed_decisions(node_repo: NodeRepository) -> None:
    for suffix, day in (("a", 1), ("b", 2), ("c", 3)):
        node_repo.save_node(
            Node(
                id=f"decision-{suffix}",
                kind=NodeKind.DECISION,
                title=f"Decision {suffix.upper()}?",
                status=DecisionStatus.OPEN,
                created_at=datetime(2024, 1, day, tzinfo=UTC),
            )
        )


# -- /api/nodes: paging walks the tree one level per call, exactly as visibility.py filters it --


def test_paging_from_root_through_every_container_visits_each_node_once_in_order() -> None:
    rows = FIXTURE["rows"]
    visited: list[str] = []

    def page_all(parent: str | None) -> None:
        cursor: str | None = None
        last_key: tuple[int, str] | None = None
        while True:
            page = paginate_nodes(
                rows,
                view=None,
                repos=None,
                parent=parent,
                ids=None,
                filters_raw={},
                include_body=False,
                cursor=cursor,
                limit=2,
            )
            for item in page["items"]:
                key = (item["ordinal"], item["id"])
                assert last_key is None or key > last_key
                last_key = key
                visited.append(item["id"])
                page_all(item["id"])
            cursor = page["next"]
            if cursor is None:
                break

    page_all(None)
    assert sorted(visited) == sorted(rows)
    assert len(visited) == len(rows)


@pytest.mark.parametrize(
    "case_name",
    [
        "open_spec",
        "model_filter_matches_a_task_with_several_models",
        "spec_filter",
        "a_plan_in_its_own_review_shows_under_its_status_without_its_tasks",
    ],
)
def test_visibility_case_reconstructs_through_nodes_pagination(case_name: str) -> None:
    case = next(c for c in FIXTURE["cases"] if c["name"] == case_name)
    rows = case.get("estate", FIXTURE)["rows"]
    open_ids = set(case["open"])
    collected: list[str] = []

    def walk(parent: str | None) -> None:
        page = paginate_nodes(
            rows,
            view=None,
            repos=None,
            parent=parent,
            ids=None,
            filters_raw=case["filters"],
            include_body=False,
            cursor=None,
            limit=200,
        )
        for item in page["items"]:
            collected.append(item["id"])
            if item["id"] in open_ids:
                walk(item["id"])

    walk(None)
    assert collected == case["visible"]


def test_nodes_page_include_body_equals_node_detail(tmp_path: Path) -> None:
    _seed_basic(tmp_path)
    client = TestClient(create_app(tmp_path))

    detail = client.get("/api/nodes/S1-P1-T1").json()
    page = client.get("/api/nodes", params={"parent": "S1-P1", "include": "body"}).json()

    item = next(i for i in page["items"] if i["id"] == "S1-P1-T1")
    assert item["body"] == detail


def test_nodes_more_than_200_ids_is_400(tmp_path: Path) -> None:
    _seed_basic(tmp_path)
    client = TestClient(create_app(tmp_path))
    ids = ",".join(f"x{i}" for i in range(201))
    res = client.get("/api/nodes", params={"ids": ids})
    assert res.status_code == 400


def test_nodes_limit_out_of_range_is_400(tmp_path: Path) -> None:
    _seed_basic(tmp_path)
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/nodes", params={"limit": "0"}).status_code == 400
    assert client.get("/api/nodes", params={"limit": "201"}).status_code == 400


def test_nodes_parent_and_ids_together_is_400(tmp_path: Path) -> None:
    _seed_basic(tmp_path)
    client = TestClient(create_app(tmp_path))
    res = client.get("/api/nodes", params={"parent": "S1", "ids": "S1-P1"})
    assert res.status_code == 400


def test_nodes_cursor_from_a_different_query_is_400(tmp_path: Path) -> None:
    _seed_basic(tmp_path)
    client = TestClient(create_app(tmp_path))
    page = client.get("/api/nodes", params={"parent": "S1-P1", "limit": "1"}).json()
    cursor = page["next"]
    assert cursor is not None

    res = client.get("/api/nodes", params={"parent": "S1", "cursor": cursor})
    assert res.status_code == 400


# -- /api/statuses: exactly what a websocket snapshot carries, for the same estate ---------------


def test_statuses_endpoint_equals_a_live_snapshot(tmp_path: Path) -> None:
    node_repo = _seed_basic(tmp_path)
    db = node_repo.db
    builder = SnapshotBuilder(node_repo, RuntimeRepository(db), JobRepository(db))
    cache = CacheRepository(db)
    hub = LiveHub(
        snapshots=builder,
        cache=cache,
        node_repo=node_repo,
        job_repo=JobRepository(db),
        assets_dir=tmp_path / ".taskmanager" / "assets",
        condition_ttl=lambda: 0,
        state_db=db.state_db,
        cache_db=db.cache_db,
    )
    asyncio.run(hub.refresh())

    res = TestClient(create_app(tmp_path)).get("/api/statuses").json()

    assert res["statuses"] == hub.statuses
    assert res["hash"] == hub.hash
    assert res["decisions_open"] == hub.decisions_open


# -- /api/decisions: newest first, paginated, today's item shape kept ----------------------------


def test_decisions_page_newest_first_and_page_through_with_a_stable_cursor(
    tmp_path: Path,
) -> None:
    node_repo = _seed_basic(tmp_path)
    _seed_decisions(node_repo)
    client = TestClient(create_app(tmp_path))

    page1 = client.get("/api/decisions", params={"limit": "2"}).json()
    assert [d["id"] for d in page1["items"]] == ["decision-c", "decision-b"]
    assert page1["next"] is not None
    assert set(page1["items"][0]) == {
        "id",
        "title",
        "status",
        "priority",
        "created_at",
        "waiting_count",
        "blocks",
        "decision",
        "attachments",
    }

    page2 = client.get("/api/decisions", params={"limit": "2", "cursor": page1["next"]}).json()
    assert [d["id"] for d in page2["items"]] == ["decision-a"]
    assert page2["next"] is None


def test_decisions_status_filter_keeps_only_that_tab(tmp_path: Path) -> None:
    node_repo = _seed_basic(tmp_path)
    _seed_decisions(node_repo)
    client = TestClient(create_app(tmp_path))
    client.post("/api/decisions/decision-b/answer", json={"text": "because"})

    open_page = client.get("/api/decisions", params={"status": "open"}).json()
    assert [d["id"] for d in open_page["items"]] == ["decision-c", "decision-a"]

    answered_page = client.get("/api/decisions", params={"status": "answered"}).json()
    assert [d["id"] for d in answered_page["items"]] == ["decision-b"]


def test_decisions_cursor_from_a_different_status_filter_is_400(tmp_path: Path) -> None:
    node_repo = _seed_basic(tmp_path)
    _seed_decisions(node_repo)
    client = TestClient(create_app(tmp_path))

    page = client.get("/api/decisions", params={"status": "open", "limit": "1"}).json()
    cursor = page["next"]
    assert cursor is not None

    res = client.get("/api/decisions", params={"cursor": cursor})
    assert res.status_code == 400


# -- statement budget: one page costs the same at 100 and 1,000 nodes ----------------------------


@contextmanager
def _count_statements(db: DatabaseManager) -> Iterator[list[int]]:
    count = [0]

    def trace(_: str) -> None:
        count[0] += 1

    with db.get_state_connection() as conn:
        conn.set_trace_callback(trace)
    with db.get_cache_connection() as conn:
        conn.set_trace_callback(trace)
    try:
        yield count
    finally:
        with db.get_state_connection() as conn:
            conn.set_trace_callback(None)
        with db.get_cache_connection() as conn:
            conn.set_trace_callback(None)


_ESTATE_SIZES = {100: 11, 1000: 111}


def _one_page_statements(root: Path, plans: int) -> int:
    node_repo = seed_estate(root, plans)
    db = node_repo.db
    builder = SnapshotBuilder(node_repo, RuntimeRepository(db), JobRepository(db))
    repos = BodyRepos(
        node_repo=node_repo,
        job_repo=JobRepository(db),
        cache=CacheRepository(db),
        condition_ttl=0,
        assets_dir=root / ".taskmanager" / "assets",
    )
    with _count_statements(db) as count:
        view = DisplayView(builder, CacheRepository(db), max_age=0)
        rows = build_rows(view)
        paginate_nodes(
            rows,
            view=view,
            repos=repos,
            parent="SPEC",
            ids=None,
            filters_raw={},
            include_body=True,
            cursor=None,
            limit=5,
        )
    return count[0]


def test_a_nodes_page_costs_the_same_number_of_statements_at_100_and_1000_nodes(
    tmp_path: Path,
) -> None:
    counts = {
        size: _one_page_statements(tmp_path / str(size), plans)
        for size, plans in _ESTATE_SIZES.items()
    }
    assert counts[100] == counts[1000]
