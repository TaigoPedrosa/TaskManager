import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    Check,
    Condition,
    Finding,
    Job,
    Lease,
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
    Note,
)
from taskmanager.core.status import CheckResult, DecisionStatus, JobKind, JobState, Severity, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.archive import (
    ArchiveSchedule,
    archive_due,
    archive_schedule,
    copy_to_archive,
)
from taskmanager.engine.snapshot import SnapshotBuilder

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class Estate:
    def __init__(self, root: Path) -> None:
        self.root = root
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.ledger = LedgerRepository(db)
        self.runtime = RuntimeRepository(db)
        self.jobs = JobRepository(db)
        self.builder = SnapshotBuilder(self.nodes, self.runtime, self.jobs)

    def add(
        self, node_id: str, kind: NodeKind, parent: str | None = None, status: Status = Status.READY
    ) -> None:
        self.nodes.save_node(Node(id=node_id, kind=kind, title=node_id, status=status))
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def spec(self, spec_id: str, status: Status = Status.COMPLETED) -> None:
        self.add(spec_id, NodeKind.SPEC, status=status)
        self.add(f"{spec_id}-plan", NodeKind.PLAN, parent=spec_id, status=status)
        self.add(f"{spec_id}-task", NodeKind.TASK, parent=f"{spec_id}-plan", status=status)

    def moved(self, node_id: str, to: Status, days_ago: float) -> None:
        self.ledger.append(
            LedgerEvent(
                timestamp=NOW - timedelta(days=days_ago),
                actor_id="tm",
                command="rollup",
                target_id=node_id,
                payload={"from": "READY", "to": to.value},
            )
        )

    def updated(self, node_id: str, days_ago: float) -> None:
        node = self.nodes.get_node(node_id)
        assert node is not None
        self.nodes.save_node(node.model_copy(update={"updated_at": NOW - timedelta(days=days_ago)}))

    def archive(self, hours: int = 72) -> ArchiveSchedule:
        specs = self.nodes.list_nodes(kind=NodeKind.SPEC)
        return archive_schedule(specs, self.ledger, NOW, hours)


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def test_archived_specs_completed_past_the_window_archives_and_within_it_does_not(
    estate: Estate,
) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=4)
    estate.spec("NEW")
    estate.moved("NEW", Status.COMPLETED, days_ago=2)

    archive = estate.archive()

    assert archive.due == ["OLD"]
    assert archive.next_boundary == NOW + timedelta(days=1)


def test_archived_specs_later_event_on_a_completed_spec_keeps_its_completion_time(
    estate: Estate,
) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=4)
    estate.ledger.append(
        LedgerEvent(
            timestamp=NOW - timedelta(days=1),
            actor_id="tm",
            command="task update",
            target_id="S",
            payload={"title": "S"},
        )
    )

    assert estate.archive().due == ["S"]


def test_archived_specs_with_zero_hours_archives_nothing(estate: Estate) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=40)

    assert estate.archive(hours=0) == ArchiveSchedule([], None)


def test_archived_specs_reopened_spec_leaves_the_archive(estate: Estate) -> None:
    estate.spec("S", status=Status.READY)
    estate.moved("S", Status.COMPLETED, days_ago=5)
    estate.moved("S", Status.READY, days_ago=1)

    archive = estate.archive()

    assert archive.due == []
    assert archive.next_boundary is None


def test_archived_specs_completed_again_counts_from_the_last_completion(estate: Estate) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=5)
    estate.moved("S", Status.READY, days_ago=4)
    estate.moved("S", Status.COMPLETED, days_ago=1)

    archive = estate.archive()

    assert archive.due == []
    assert archive.next_boundary == NOW + timedelta(days=2)


def test_archived_specs_completed_spec_with_no_ledger_completion_counts_from_its_last_update(
    estate: Estate,
) -> None:
    estate.spec("OLD")
    estate.updated("OLD", days_ago=4)
    estate.spec("NEW")
    estate.updated("NEW", days_ago=1)

    archive = estate.archive()

    assert archive.due == ["OLD"]
    assert archive.next_boundary == NOW + timedelta(days=2)


def test_archived_specs_ledger_completion_wins_over_a_later_update(estate: Estate) -> None:
    estate.spec("S")
    estate.moved("S", Status.COMPLETED, days_ago=4)
    estate.updated("S", days_ago=1)

    assert estate.archive().due == ["S"]


def test_an_abandoned_spec_sets_the_next_boundary(estate: Estate) -> None:
    estate.spec("GONE", status=Status.ABANDONED)
    estate.moved("GONE", Status.ABANDONED, days_ago=1)

    assert estate.archive().next_boundary == NOW + timedelta(days=2)


runner = CliRunner()
HOT_TABLES = (
    "nodes",
    "node_sections",
    "node_relations",
    "node_verifications",
    "node_conditions",
    "node_findings",
    "node_checks",
    "node_notes",
    "jobs",
)


def tm(root: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*args, "-C", str(root)])


def rich_spec(estate: Estate, spec_id: str = "S") -> list[str]:
    """A completed spec holding one of every kind of record a subtree carries, and the ids of
    its nodes."""
    estate.add(spec_id, NodeKind.SPEC, status=Status.COMPLETED)
    estate.add(f"{spec_id}-P", NodeKind.PLAN, parent=spec_id, status=Status.COMPLETED)
    for task in ("T1", "T2"):
        estate.add(
            f"{spec_id}-{task}", NodeKind.TASK, parent=f"{spec_id}-P", status=Status.COMPLETED
        )
    t1, t2 = f"{spec_id}-T1", f"{spec_id}-T2"
    estate.nodes.save_section(
        NodeSection(node_id=t1, section_key="brief", ordinal=1, header="## Brief", content="body")
    )
    estate.nodes.add_finding(
        Finding(node_id=t1, severity=Severity.BLOCKING, at="a.py:1", breaks="it breaks")
    )
    estate.nodes.add_check(Check(node_id=t1, deleted="a guard", result=CheckResult.GREEN))
    estate.nodes.add_note(Note(node_id=t1, text="a note"))
    estate.nodes.add_verification(
        NodeVerification(
            node_id=t1, verification_type=VerificationType.FILE_EXISTS, target_path="a.py"
        )
    )
    estate.nodes.add_condition(Condition(node_id=t2, needs="x", command="true"))
    estate.nodes.save_node(
        Node(id=f"{spec_id}-D", kind=NodeKind.DECISION, title="D", status=DecisionStatus.ANSWERED)
    )
    estate.nodes.add_relation(
        NodeRelation(source_id=t2, target_id=f"{spec_id}-D", relation_type=RelationType.DEPENDS_ON)
    )
    estate.jobs.create(
        Job(kind=JobKind.LAND, node_id=t1, repo="r", target="main", state=JobState.SUCCEEDED)
    )
    ids = [spec_id, f"{spec_id}-P", t1, t2, f"{spec_id}-D"]
    for node_id in ids:
        for command in ("one", "two"):
            estate.ledger.append(
                LedgerEvent(
                    timestamp=NOW - timedelta(hours=100),
                    actor_id="tm",
                    command=command,
                    target_id=node_id,
                    payload={"to": "COMPLETED"} if command == "two" else {},
                )
            )
    return ids


def rows(db_path: Path, table: str) -> list[tuple[object, ...]]:
    """Every row of `table` but its `rev`, which moves with each insert beneath a node, and a
    verification's `id`, which each store numbers for itself."""
    skipped = {"rev", "id"} if table == "node_verifications" else {"rev"}
    with closing(sqlite3.connect(db_path)) as conn:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})") if c[1] not in skipped]
        return conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1, 2").fetchall()


def events(root: Path, db_name: str) -> list[tuple[object, ...]]:
    """Each event but its `id`, which each store numbers for itself."""
    with closing(sqlite3.connect(root / ".taskmanager" / db_name)) as conn:
        return conn.execute(
            "SELECT timestamp, actor_id, command, target_id, payload_json, diff_json "
            "FROM ledger_events ORDER BY id"
        ).fetchall()


def state(root: Path) -> Path:
    return root / ".taskmanager" / "state.db"


def archive_db(root: Path) -> Path:
    return root / ".taskmanager" / "archive.db"


def test_archive_moves_the_whole_subtree_out_of_the_hot_estate(estate: Estate) -> None:
    ids = rich_spec(estate)
    estate.spec("LIVE", status=Status.READY)

    res = tm(estate.root, "spec", "archive", "S")

    assert res.exit_code == 0, res.output
    assert tm(estate.root, "task", "get", "S-T1").exit_code == 1
    hot = json.loads(tm(estate.root, "spec", "list", "--json").stdout)
    cold = json.loads(tm(estate.root, "spec", "list", "--archived", "--json").stdout)
    assert [row["id"] for row in hot] == ["LIVE"]
    assert [row["id"] for row in cold] == ["S"]
    assert "S-T1" in tm(estate.root, "render", "S-T1", "--archived").stdout
    assert tm(estate.root, "render", "S-T1").exit_code == 1
    with closing(sqlite3.connect(state(estate.root))) as conn:
        assert conn.execute("SELECT id FROM nodes WHERE id LIKE 'S-%' OR id = 'S'").fetchall() == []
        assert (
            conn.execute(
                "SELECT node_id FROM nodes_fts WHERE node_id IN (?,?,?,?,?)", ids
            ).fetchall()
            == []
        )
    hot_events = {row[3] for row in events(estate.root, "audit.db")}
    assert hot_events.isdisjoint(ids)
    assert {row[3] for row in events(estate.root, "archive.db")} == set(ids)


def test_unarchive_restores_every_row_once(estate: Estate) -> None:
    ids = rich_spec(estate)
    before = {t: rows(state(estate.root), t) for t in HOT_TABLES}
    before_events = events(estate.root, "audit.db")
    shown = {i: tm(estate.root, "render", i).stdout for i in ids}
    got = {i: tm(estate.root, "task", "get", i, "--json").stdout for i in ("S-T1", "S-T2")}

    assert tm(estate.root, "spec", "archive", "S").exit_code == 0
    res = tm(estate.root, "spec", "unarchive", "S")

    assert res.exit_code == 0, res.output
    assert {t: rows(state(estate.root), t) for t in HOT_TABLES} == before
    assert events(estate.root, "audit.db") == before_events
    assert {i: tm(estate.root, "render", i).stdout for i in ids} == shown
    assert {i: tm(estate.root, "task", "get", i, "--json").stdout for i in got} == got
    assert all(rows(archive_db(estate.root), t) == [] for t in HOT_TABLES)
    assert events(estate.root, "archive.db") == []
    with closing(sqlite3.connect(state(estate.root))) as conn:
        found = conn.execute("SELECT node_id FROM nodes_fts WHERE node_id IN (?,?,?,?,?)", ids)
        assert sorted(r[0] for r in found) == sorted(ids)


def blocked_by_unfinished_child(estate: Estate) -> str:
    estate.add("S", NodeKind.SPEC, status=Status.ABANDONED)
    estate.add("S-T", NodeKind.TASK, parent="S", status=Status.READY)
    return "S-T is READY, not finished"


def blocked_by_a_live_lease(estate: Estate) -> str:
    estate.spec("S")
    estate.runtime.acquire_lease(
        Lease(task_id="S-task", agent_id="a", session_id="s", branch_name="tm/S-task", token="t"),
        [],
    )
    return "S-task holds a live lease"


def blocked_by_a_running_job(estate: Estate) -> str:
    estate.spec("S")
    estate.jobs.create(
        Job(kind=JobKind.LAND, node_id="S-task", repo="r", target="main", state=JobState.RUNNING)
    )
    return "S-task has a running job"


def blocked_by_a_hot_edge_onto_an_abandoned_node(estate: Estate) -> str:
    estate.add("S", NodeKind.SPEC, status=Status.ABANDONED)
    estate.add("S-T", NodeKind.TASK, parent="S", status=Status.ABANDONED)
    estate.add("HOT", NodeKind.TASK)
    estate.nodes.add_relation(
        NodeRelation(source_id="HOT", target_id="S-T", relation_type=RelationType.DEPENDS_ON)
    )
    return "HOT depends on S-T, which is ABANDONED"


def blocked_by_a_hot_supersede(estate: Estate) -> str:
    estate.spec("S")
    estate.add("HOT", NodeKind.TASK)
    estate.nodes.add_relation(
        NodeRelation(source_id="HOT", target_id="S-task", relation_type=RelationType.SUPERSEDES)
    )
    return "HOT supersedes S-task"


def blocked_by_a_shared_open_decision(estate: Estate) -> str:
    estate.spec("S")
    estate.add("HOT", NodeKind.TASK)
    estate.nodes.save_node(
        Node(id="D", kind=NodeKind.DECISION, title="D", status=DecisionStatus.OPEN)
    )
    for source in ("HOT", "S-task"):
        estate.nodes.add_relation(
            NodeRelation(source_id=source, target_id="D", relation_type=RelationType.DEPENDS_ON)
        )
    return "decision D is open and also blocks nodes outside the spec"


@pytest.mark.parametrize(
    "blocker",
    [
        blocked_by_unfinished_child,
        blocked_by_a_live_lease,
        blocked_by_a_running_job,
        blocked_by_a_hot_edge_onto_an_abandoned_node,
        blocked_by_a_hot_supersede,
        blocked_by_a_shared_open_decision,
    ],
)
def test_archive_refuses_and_names_each_blocker(estate: Estate, blocker) -> None:  # type: ignore[no-untyped-def]
    named = blocker(estate)

    res = tm(estate.root, "spec", "archive", "S")

    assert res.exit_code == 1
    assert named in res.stderr
    assert estate.nodes.get_node("S") is not None
    assert rows(archive_db(estate.root), "nodes") == []


def test_archive_allows_a_hot_edge_onto_a_completed_node(estate: Estate) -> None:
    estate.spec("S")
    estate.add("HOT", NodeKind.TASK)
    estate.nodes.add_relation(
        NodeRelation(source_id="HOT", target_id="S-task", relation_type=RelationType.DEPENDS_ON)
    )

    assert tm(estate.root, "spec", "archive", "S").exit_code == 0
    assert estate.nodes.get_dependencies("HOT") == []


def test_archive_due_honours_the_hours(estate: Estate) -> None:
    for spec_id, status, hours in (
        ("OLD", Status.COMPLETED, 73),
        ("NEW", Status.COMPLETED, 71),
        ("GONE", Status.ABANDONED, 80),
    ):
        estate.spec(spec_id, status=status)
        estate.moved(spec_id, status, days_ago=hours / 24)

    archived = archive_due(estate.root, NOW)

    assert archived == ["GONE", "OLD"]
    cold = json.loads(tm(estate.root, "spec", "list", "--archived", "--json").stdout)
    assert sorted(row["id"] for row in cold) == ["GONE", "OLD"]
    assert archive_due(estate.root, NOW) == []


def test_archive_due_skips_a_spec_with_a_blocker_and_archives_the_rest(estate: Estate) -> None:
    estate.spec("HELD")
    estate.moved("HELD", Status.COMPLETED, days_ago=5)
    estate.runtime.acquire_lease(
        Lease(task_id="HELD-task", agent_id="a", session_id="s", branch_name="b", token="t"), []
    )
    estate.spec("FREE")
    estate.moved("FREE", Status.COMPLETED, days_ago=5)

    assert archive_due(estate.root, NOW) == ["FREE"]


def test_archive_due_with_zero_hours_archives_nothing(estate: Estate) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=40)
    assert tm(estate.root, "config", "set", "archive.automatically_after_hours", "0").exit_code == 0

    assert archive_due(estate.root, NOW) == []


def test_an_archive_interrupted_after_its_copy_finishes_on_rerun(estate: Estate) -> None:
    rich_spec(estate)
    before = {t: rows(state(estate.root), t) for t in HOT_TABLES}
    before_events = events(estate.root, "audit.db")
    copy_to_archive(estate.root, "S")

    res = tm(estate.root, "spec", "archive", "S")

    assert res.exit_code == 0, res.output
    assert {t: rows(archive_db(estate.root), t) for t in HOT_TABLES} == before
    assert all(rows(state(estate.root), t) == [] for t in HOT_TABLES)
    assert events(estate.root, "archive.db") == before_events
    assert events(estate.root, "audit.db") == []
    assert tm(estate.root, "spec", "unarchive", "S").exit_code == 0
    assert {t: rows(state(estate.root), t) for t in HOT_TABLES} == before


def test_archiving_over_a_different_spec_with_the_same_id_is_refused(estate: Estate) -> None:
    estate.spec("S")
    assert tm(estate.root, "spec", "archive", "S").exit_code == 0
    estate.spec("S")
    estate.nodes.save_node(
        Node(id="S", kind=NodeKind.SPEC, title="new", status=Status.COMPLETED, created_at=NOW)
    )

    res = tm(estate.root, "spec", "archive", "S")

    assert res.exit_code == 1
    assert "names a different spec" in res.stderr
    assert estate.nodes.get_node("S") is not None


def depends_on(root: Path, node_id: str) -> list[str]:
    doc = json.loads(tm(root, "task", "get", node_id, "--json").stdout)
    return [dep["id"] for dep in doc["depends_on"]]


def test_unarchive_restores_edges_that_cross_the_subtree(estate: Estate) -> None:
    estate.spec("S")
    estate.add("HOT", NodeKind.TASK)
    estate.add("OTHER", NodeKind.TASK)
    for source, target in (("HOT", "S-task"), ("S-task", "OTHER")):
        estate.nodes.add_relation(
            NodeRelation(source_id=source, target_id=target, relation_type=RelationType.DEPENDS_ON)
        )

    assert tm(estate.root, "spec", "archive", "S").exit_code == 0
    assert depends_on(estate.root, "HOT") == []
    assert tm(estate.root, "spec", "unarchive", "S").exit_code == 0

    assert depends_on(estate.root, "HOT") == ["S-task"]
    assert depends_on(estate.root, "S-task") == ["OTHER"]
    with closing(sqlite3.connect(archive_db(estate.root))) as conn:
        assert conn.execute("SELECT * FROM external_relations").fetchall() == []


def test_unarchive_leaves_an_edge_to_a_node_that_is_still_archived(estate: Estate) -> None:
    estate.spec("A")
    estate.spec("B")
    estate.nodes.add_relation(
        NodeRelation(source_id="A-task", target_id="B-task", relation_type=RelationType.DEPENDS_ON)
    )
    assert tm(estate.root, "spec", "archive", "A").exit_code == 0
    assert tm(estate.root, "spec", "archive", "B").exit_code == 0

    assert tm(estate.root, "spec", "unarchive", "B").exit_code == 0
    assert depends_on(estate.root, "B-task") == []
    assert tm(estate.root, "spec", "unarchive", "A").exit_code == 0

    assert depends_on(estate.root, "A-task") == ["B-task"]


def test_an_edge_onto_an_archived_completed_node_is_satisfied(estate: Estate) -> None:
    estate.spec("S")
    estate.add("HOT", NodeKind.TASK)
    estate.nodes.add_relation(
        NodeRelation(source_id="HOT", target_id="S-task", relation_type=RelationType.DEPENDS_ON)
    )
    assert tm(estate.root, "spec", "archive", "S").exit_code == 0

    ranked = json.loads(tm(estate.root, "next", "--json").stdout)

    assert [task["task_id"] for task in ranked] == ["HOT"]


def test_next_archives_due_specs_first(estate: Estate) -> None:
    estate.spec("OLD")
    estate.spec("NEW")
    for spec_id, hours in (("OLD", 73), ("NEW", 71)):
        estate.ledger.append(
            LedgerEvent(
                timestamp=datetime.now(tz=UTC) - timedelta(hours=hours),
                actor_id="tm",
                command="rollup",
                target_id=spec_id,
                payload={"from": "READY", "to": "COMPLETED"},
            )
        )

    assert tm(estate.root, "next").exit_code == 0

    listed = json.loads(tm(estate.root, "spec", "list", "--json").stdout)
    assert [row["id"] for row in listed] == ["NEW"]


def test_sweep_archives_due_specs_first(estate: Estate) -> None:
    estate.spec("OLD")
    estate.moved("OLD", Status.COMPLETED, days_ago=30)

    assert tm(estate.root, "run", "sweep").exit_code == 0

    assert json.loads(tm(estate.root, "spec", "list", "--json").stdout) == []


def clashing_spec(estate: Estate, spec_id: str) -> None:
    """A finished spec that cannot move: the archive already holds a different spec of its id."""
    estate.spec(spec_id)
    assert tm(estate.root, "spec", "archive", spec_id).exit_code == 0
    estate.spec(spec_id)
    estate.nodes.save_node(
        Node(id=spec_id, kind=NodeKind.SPEC, title="new", status=Status.COMPLETED, created_at=NOW)
    )
    estate.moved(spec_id, Status.COMPLETED, days_ago=30)


@pytest.mark.parametrize("command", [("next",), ("run", "sweep")])
def test_a_spec_that_cannot_be_archived_warns_once_and_never_stops_the_command(
    estate: Estate, command: tuple[str, ...]
) -> None:
    clashing_spec(estate, "BAD")
    estate.spec("GOOD")
    estate.moved("GOOD", Status.COMPLETED, days_ago=30)

    res = tm(estate.root, *command)

    assert res.exit_code == 0, res.output
    assert res.stderr.count("warning: spec BAD was not archived: OperationError\n") == 1
    assert [r["id"] for r in json.loads(tm(estate.root, "spec", "list", "--json").stdout)] == [
        "BAD"
    ]
    cold = json.loads(tm(estate.root, "spec", "list", "--archived", "--json").stdout)
    assert sorted(row["id"] for row in cold) == ["BAD", "GOOD"]


def test_archive_due_hands_each_failure_to_the_caller_and_carries_on(estate: Estate) -> None:
    clashing_spec(estate, "BAD")
    estate.spec("GOOD")
    estate.moved("GOOD", Status.COMPLETED, days_ago=30)
    failed: list[tuple[str, str]] = []

    archived = archive_due(
        estate.root, datetime.now(tz=UTC), lambda spec_id, exc: failed.append((spec_id, str(exc)))
    )

    assert archived == ["GOOD"]
    assert [spec_id for spec_id, _ in failed] == ["BAD"]


def test_a_restored_archive_keeps_its_audit_trail_and_verifications_when_a_spec_is_unarchived(
    tmp_path: Path,
) -> None:
    source, dest, export = tmp_path / "a", tmp_path / "b", tmp_path / "export"
    for directory in (source, dest):
        directory.mkdir()
    rich_spec(Estate(source))
    assert tm(source, "spec", "archive", "S").exit_code == 0
    assert tm(source, "export", str(export)).exit_code == 0
    assert tm(dest, "restore", str(export)).exit_code == 0
    assert tm(dest, "spec", "add", "Fresh").exit_code == 0
    fresh = Estate(dest)
    fresh.nodes.add_verification(
        NodeVerification(
            node_id="S1", verification_type=VerificationType.FILE_EXISTS, target_path="mine.py"
        )
    )
    mine = [e for e in events(dest, "audit.db") if e[3] == "S1"]
    assert mine

    assert tm(dest, "spec", "unarchive", "S").exit_code == 0

    after = events(dest, "audit.db")
    assert [e for e in after if e[3] == "S1"] == mine
    assert len([e for e in after if e[3] == "S"]) == 2
    assert [v.target_path for v in Estate(dest).nodes.get_verifications("S1")] == ["mine.py"]
    assert [v.target_path for v in Estate(dest).nodes.get_verifications("S-T1")] == ["a.py"]


def test_an_archived_id_is_taken_for_an_auto_id_and_refused_as_a_slug(estate: Estate) -> None:
    estate.spec("S1")
    assert tm(estate.root, "spec", "archive", "S1").exit_code == 0

    added = tm(estate.root, "spec", "add", "Next")
    refused = tm(estate.root, "spec", "add", "Again", "--slug", "S1-plan")

    assert added.exit_code == 0, added.output
    assert estate.nodes.get_node("S1") is None
    assert estate.nodes.get_node("S2") is not None
    assert refused.exit_code == 1
    assert "tm spec unarchive S1" in refused.stderr


def test_an_import_naming_an_archived_id_is_refused(estate: Estate, tmp_path: Path) -> None:
    estate.spec("S1")
    assert tm(estate.root, "spec", "archive", "S1").exit_code == 0
    document = tmp_path / "again.json"
    document.write_text(json.dumps({"spec": {"id": "S1", "title": "Again"}}), encoding="utf-8")

    res = tm(estate.root, "import", "--file", str(document))

    assert res.exit_code == 1
    assert "'S1' is archived: run tm spec unarchive S1" in res.stderr
    assert estate.nodes.get_node("S1") is None


def test_restore_merges_the_exported_archive_into_the_destination_archive(tmp_path: Path) -> None:
    source, dest, export = tmp_path / "a", tmp_path / "b", tmp_path / "export"
    for directory in (source, dest):
        directory.mkdir()
    rich_spec(Estate(source), "FROM")
    assert tm(source, "spec", "archive", "FROM").exit_code == 0
    assert tm(source, "export", str(export)).exit_code == 0
    rich_spec(Estate(dest), "HERE")
    assert tm(dest, "spec", "archive", "HERE").exit_code == 0

    assert tm(dest, "restore", str(export)).exit_code == 0
    assert tm(dest, "restore", str(export)).exit_code == 0

    cold = json.loads(tm(dest, "spec", "list", "--archived", "--json").stdout)
    assert sorted(row["id"] for row in cold) == ["FROM", "HERE"]
    here = [e for e in events(dest, "archive.db") if e[3] == "HERE"]
    assert len(here) == 2
    assert len([e for e in events(dest, "archive.db") if e[3] == "FROM"]) == 2


def test_restore_refuses_an_archived_node_that_names_a_different_node_of_another_spec(
    tmp_path: Path,
) -> None:
    source, dest, export = tmp_path / "a", tmp_path / "b", tmp_path / "export"
    for directory in (source, dest):
        directory.mkdir()
    rich_spec(Estate(source))
    assert tm(source, "spec", "archive", "S").exit_code == 0
    assert tm(source, "export", str(export)).exit_code == 0
    estate = Estate(dest)
    estate.nodes.save_node(
        Node(
            id="S-D",
            kind=NodeKind.DECISION,
            title="other",
            status=DecisionStatus.OPEN,
            created_at=NOW - timedelta(days=9),
        )
    )

    res = tm(dest, "restore", str(export))

    assert res.exit_code == 1
    assert "'S-D' names a different node in the destination" in res.stderr
    assert estate.nodes.get_node("S") is None


def test_restore_refuses_an_archived_spec_that_names_a_different_spec_before_writing(
    tmp_path: Path,
) -> None:
    source, dest, export = tmp_path / "a", tmp_path / "b", tmp_path / "export"
    for directory in (source, dest):
        directory.mkdir()
    rich_spec(Estate(source))
    assert tm(source, "spec", "archive", "S").exit_code == 0
    assert tm(source, "export", str(export)).exit_code == 0
    estate = Estate(dest)
    estate.nodes.save_node(
        Node(id="S", kind=NodeKind.SPEC, title="other", status=Status.READY, created_at=NOW)
    )

    res = tm(dest, "restore", str(export))

    assert res.exit_code == 1
    assert "'S' names a different node in the destination" in res.stderr
    assert [n.title for n in estate.nodes.list_nodes(kind=NodeKind.SPEC)] == ["other"]
