import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Condition, Job, Lease, Node, NodeRelation, NodeSection
from taskmanager.core.status import Action, JobKind, JobState, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.web.bodies import BodyRepos, body_items, build_bodies, refresh_relations
from taskmanager.web.rows import build_rows


class Estate:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.node_repo = NodeRepository(db)
        self.runtime_repo = RuntimeRepository(db)
        self.job_repo = JobRepository(db)
        self.cache = CacheRepository(db)
        self.builder = SnapshotBuilder(self.node_repo, self.runtime_repo, self.job_repo)
        self.assets_dir = root / ".taskmanager" / "assets"

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

    def depend(self, dependent: str, dependency: str) -> None:
        self.node_repo.add_relation(
            NodeRelation(
                source_id=dependent, target_id=dependency, relation_type=RelationType.DEPENDS_ON
            )
        )

    def view(self) -> DisplayView:
        return DisplayView(self.builder, self.cache, 300)

    def repos(self, condition_ttl: int = 300) -> BodyRepos:
        return BodyRepos(
            node_repo=self.node_repo,
            job_repo=self.job_repo,
            cache=self.cache,
            condition_ttl=condition_ttl,
            assets_dir=self.assets_dir,
        )


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def test_build_bodies_carries_lifecycle_relations_sections_and_lease(estate: Estate) -> None:
    estate.add("BLOCKER", status=Status.READY)
    estate.add("T1", status=Status.READY)
    estate.depend("T1", "BLOCKER")
    estate.node_repo.save_section(
        NodeSection(node_id="T1", section_key="notes", ordinal=1, header="## Notes", content="hi")
    )
    estate.runtime_repo.acquire_lease(
        Lease(task_id="T1", agent_id="agent-a", session_id="s1", branch_name="tm/T1"), []
    )

    body = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]

    assert body["node"]["id"] == "T1"
    assert [d["id"] for d in body["dependency_details"]] == ["BLOCKER"]
    assert body["sections"] == [
        {"key": "notes", "header": "## Notes", "content": "hi", "ordinal": 1}
    ]
    assert body["lease"] is not None
    assert body["lease"]["agent_id"] == "agent-a"


def test_build_bodies_carries_the_rows_display_and_phase_for_a_node_awaiting_a_decision(
    estate: Estate,
) -> None:
    estate.add("D1", NodeKind.DECISION)
    estate.add("T1", status=Status.READY)
    estate.depend("T1", "D1")
    view = estate.view()

    body = build_bodies(view, ["T1"], repos=estate.repos())["T1"]
    row = build_rows(view)["T1"]

    assert body["display"] == row["display"] == "AWAITING_DECISION"
    assert body["phase"] == row["phase"] == "QUEUED"


def test_build_bodies_carries_a_live_leases_acquired_at_and_last_heartbeat(
    estate: Estate,
) -> None:
    acquired = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    heartbeat = datetime.now(tz=UTC)
    estate.add("T1", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.runtime_repo.acquire_lease(
        Lease(
            task_id="T1",
            agent_id="agent-a",
            session_id="s1",
            branch_name="tm/T1",
            action=Action.IMPLEMENT,
            acquired_at=acquired,
            last_heartbeat=heartbeat,
            ttl_seconds=3600,
        ),
        [],
    )

    lease = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]["lease"]

    assert datetime.fromisoformat(lease["acquired_at"]) == acquired
    assert datetime.fromisoformat(lease["last_heartbeat"]) == heartbeat
    assert lease["ttl_seconds"] == 3600


def test_build_bodies_carries_a_finished_landing_jobs_heartbeat(estate: Estate) -> None:
    landed_at = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)
    estate.add("T1", status=Status.COMPLETED)
    estate.job_repo.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="core",
            target="main",
            state=JobState.SUCCEEDED,
            heartbeat=landed_at,
        )
    )

    jobs = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]["jobs"]

    assert datetime.fromisoformat(jobs[0]["heartbeat"]) == landed_at


def test_refresh_relations_moves_a_stale_bodys_display_once_its_dependency_completes(
    estate: Estate,
) -> None:
    estate.add("A", status=Status.READY)
    estate.add("B", status=Status.READY)
    estate.depend("B", "A")
    stale = build_bodies(estate.view(), ["B"], repos=estate.repos())
    assert stale["B"]["display"] != "READY"

    a = estate.node_repo.get_node("A")
    assert a is not None
    estate.node_repo.save_node(a.model_copy(update={"status": Status.COMPLETED}))
    view = estate.view()

    refreshed = refresh_relations(view, view.snapshot.graph_data(), stale)["B"]

    assert refreshed["display"] == refreshed["node"]["display"] == "READY"
    assert refreshed["phase"] == refreshed["node"]["phase"] == "QUEUED"


def test_build_bodies_lists_a_dependent_the_dependency_finds(estate: Estate) -> None:
    estate.add("BLOCKER", status=Status.READY)
    estate.add("T1", status=Status.READY)
    estate.depend("T1", "BLOCKER")

    body = build_bodies(estate.view(), ["BLOCKER"], repos=estate.repos())["BLOCKER"]

    assert [d["id"] for d in body["dependent_details"]] == ["T1"]


def test_build_bodies_reads_every_job_state_not_only_the_live_ones(estate: Estate) -> None:
    estate.add("T1")
    estate.job_repo.create(
        Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main", state=JobState.SUCCEEDED)
    )

    body = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]

    assert [j["state"] for j in body["jobs"]] == ["succeeded"]


def test_build_bodies_reads_a_conditions_cached_result(estate: Estate) -> None:
    estate.add("T1")
    estate.node_repo.add_condition(Condition(node_id="T1", needs="x", command="true"))
    estate.cache.put_condition("T1", 1, "true", 0)

    body = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]

    assert body["conditions"] == [
        {"idx": 1, "needs": "x", "command": "true", "stage": "claim", "last_result": 0}
    ]


def test_build_bodies_reads_no_result_once_the_conditions_command_changed(estate: Estate) -> None:
    estate.add("T1")
    estate.node_repo.add_condition(Condition(node_id="T1", needs="x", command="true"))
    estate.cache.put_condition("T1", 1, "false", 1)

    body = build_bodies(estate.view(), ["T1"], repos=estate.repos())["T1"]

    assert body["conditions"][0]["last_result"] is None


def test_build_bodies_skips_an_id_the_snapshot_does_not_carry(estate: Estate) -> None:
    estate.add("T1")

    bodies = build_bodies(estate.view(), ["T1", "GHOST"], repos=estate.repos())

    assert set(bodies) == {"T1"}


class _CountingConnection:
    """Forwards every call to the real connection except `execute`, which it also tallies --
    `sqlite3.Connection` itself refuses attribute assignment, so counting wraps the connection
    instead of patching it."""

    def __init__(self, conn: sqlite3.Connection, calls: dict[str, int]) -> None:
        self._conn = conn
        self._calls = calls

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._calls["n"] += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_build_bodies_issues_the_same_statement_count_for_200_ids_as_for_2(
    estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"T{i}" for i in range(200)]
    for node_id in ids:
        estate.add(node_id)
    view = estate.view()
    repos = estate.repos()
    calls = {"n": 0}
    db_mgr = estate.node_repo.db
    real_state_conn = db_mgr.get_state_connection
    real_cache_conn = db_mgr.get_cache_connection

    @contextmanager
    def counting_state_conn() -> Any:
        with real_state_conn() as conn:
            yield _CountingConnection(conn, calls)

    @contextmanager
    def counting_cache_conn() -> Any:
        with real_cache_conn() as conn:
            yield _CountingConnection(conn, calls)

    monkeypatch.setattr(db_mgr, "get_state_connection", counting_state_conn)
    monkeypatch.setattr(db_mgr, "get_cache_connection", counting_cache_conn)

    calls["n"] = 0
    build_bodies(view, ids[:2], repos=repos)
    few = calls["n"]

    calls["n"] = 0
    build_bodies(view, ids, repos=repos)
    many = calls["n"]

    assert many == few > 0


def _body(sections: list[dict[str, Any]], **parts: Any) -> dict[str, Any]:
    body = {
        "node": {"id": "T1"},
        "display": "READY",
        "phase": "QUEUED",
        "dependency_details": [],
        "dependent_details": [],
        "verifications": [],
        "conditions": [],
        "jobs": [],
        "lease": None,
        "sections": sections,
    }
    body.update(parts)
    return body


def test_body_items_is_empty_for_an_unchanged_body() -> None:
    body = _body([{"key": "a", "header": "H", "content": "c", "ordinal": 1}])

    assert body_items("T1", body, body) == []


def test_body_items_with_no_prior_body_reports_every_section_and_part() -> None:
    body = _body([{"key": "a", "header": "H", "content": "c", "ordinal": 1}])

    items = body_items("T1", None, body)

    assert {
        "op": "section",
        "id": "T1",
        "key": "a",
        "section": {"header": "H", "content": "c", "ordinal": 1},
    } in items
    assert sum(1 for i in items if i["op"] == "section") == 1
    assert {i["part"] for i in items if i["op"] == "body"} == {
        "node",
        "display",
        "phase",
        "dependency_details",
        "dependent_details",
        "verifications",
        "conditions",
        "jobs",
        "lease",
    }


def test_body_items_reports_an_added_a_changed_and_a_removed_section_each_once() -> None:
    old = _body(
        [
            {"key": "keep", "header": "H", "content": "same", "ordinal": 1},
            {"key": "gone", "header": "H", "content": "bye", "ordinal": 2},
            {"key": "changed", "header": "H", "content": "before", "ordinal": 3},
        ]
    )
    new = _body(
        [
            {"key": "keep", "header": "H", "content": "same", "ordinal": 1},
            {"key": "changed", "header": "H", "content": "after", "ordinal": 3},
            {"key": "new", "header": "H", "content": "hi", "ordinal": 4},
        ]
    )

    items = [i for i in body_items("T1", old, new) if i["op"] == "section"]

    assert {i["key"] for i in items} == {"gone", "changed", "new"}
    gone = next(i for i in items if i["key"] == "gone")
    assert gone["section"] is None
    changed = next(i for i in items if i["key"] == "changed")
    assert changed["section"] == {"header": "H", "content": "after", "ordinal": 3}


def test_body_items_reports_one_item_for_a_changed_part() -> None:
    old = _body([], lease={"agent_id": "a"})
    new = _body([], lease={"agent_id": "b"})

    items = body_items("T1", old, new)

    assert items == [{"op": "body", "id": "T1", "part": "lease", "value": {"agent_id": "b"}}]
