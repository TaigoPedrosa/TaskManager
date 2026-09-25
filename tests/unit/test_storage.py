import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import taskmanager
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Condition, Lease, Node
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager, PreLifecycleEstate
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import SCHEMA_VERSION

PRE_LIFECYCLE = (
    "this directory holds a pre-lifecycle estate: run `tm init --archive` to move it to "
    "`.taskmanager/archive-<timestamp>/` and start fresh, then re-import the ongoing work"
)


def _fresh(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    return db


def _old_estate(tm_dir: Path) -> dict[str, bytes]:
    """A directory as a pre-lifecycle tm left it: three SQLite files, one row each."""
    tm_dir.mkdir(parents=True)
    for name in ("spec.db", "runtime.db", "ledger.db"):
        conn = sqlite3.connect(tm_dir / name)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return {name: (tm_dir / name).read_bytes() for name in ("spec.db", "runtime.db", "ledger.db")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_init_creates_state_and_ledger_at_the_current_schema_version(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    with db.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert {
            "nodes",
            "node_sections",
            "node_relations",
            "node_verifications",
            "node_conditions",
            "nodes_fts",
            "embedding_metadata",
            "vec_nodes",
            "index_state",
            "leases",
            "file_locks",
        } <= _tables(conn)
    with db.get_ledger_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert "ledger_events" in _tables(conn)


@pytest.mark.parametrize("name", ["spec.db", "runtime.db"])
def test_init_leaves_a_tombstone_an_old_binary_fails_to_open(tmp_path: Path, name: str) -> None:
    _fresh(tmp_path)
    tombstone = tmp_path / ".taskmanager" / name
    assert f"taskmanager {taskmanager.__version__}" in tombstone.read_text(encoding="utf-8")
    old_binary = sqlite3.connect(tombstone)
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        old_binary.execute("PRAGMA journal_mode = WAL;")
    old_binary.close()


def test_a_tombstoned_directory_opens_as_the_new_estate(tmp_path: Path) -> None:
    _fresh(tmp_path)
    again = DatabaseManager(tmp_path / ".taskmanager")
    assert not again.is_pre_lifecycle()
    assert again.is_initialized()


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.init_all(),
        lambda db: NodeRepository(db).get_node("any"),
        lambda db: RuntimeRepository(db).get_lease("any"),
        lambda db: db.get_ledger_connection().__enter__(),
    ],
    ids=["init", "node-read", "lease-read", "ledger"],
)
def test_a_pre_lifecycle_estate_refuses_every_open_with_the_archive_instruction(
    tmp_path: Path, call: Callable[[DatabaseManager], object]
) -> None:
    tm_dir = tmp_path / ".taskmanager"
    before = _old_estate(tm_dir)
    db = DatabaseManager(tm_dir)
    with pytest.raises(PreLifecycleEstate) as refused:
        call(db)
    assert str(refused.value) == PRE_LIFECYCLE
    assert {n: (tm_dir / n).read_bytes() for n in before} == before
    assert not (tm_dir / "state.db").exists()


def test_archive_moves_the_old_estate_intact_and_a_fresh_init_follows(tmp_path: Path) -> None:
    tm_dir = tmp_path / ".taskmanager"
    before = _old_estate(tm_dir)
    (tm_dir / "config.yaml").write_text("lease_ttl: 60\n", encoding="utf-8")

    archive = DatabaseManager.archive_pre_lifecycle(tmp_path)

    assert archive.parent == tm_dir
    assert re.fullmatch(r"archive-\d{8}T\d{12}Z", archive.name)
    assert {n: (archive / n).read_bytes() for n in before} == before
    assert (tm_dir / "config.yaml").read_text(encoding="utf-8") == "lease_ttl: 60\n"
    db = DatabaseManager(tm_dir)
    db.init_all()
    assert NodeRepository(db).list_nodes() == []


def test_archive_refuses_a_directory_holding_no_pre_lifecycle_estate(tmp_path: Path) -> None:
    _fresh(tmp_path)
    with pytest.raises(ValueError, match="no pre-lifecycle estate"):
        DatabaseManager.archive_pre_lifecycle(tmp_path)
    assert [p.name for p in (tmp_path / ".taskmanager").iterdir() if p.is_dir()] == []


def test_every_lifecycle_column_round_trips(tmp_path: Path) -> None:
    repo = NodeRepository(_fresh(tmp_path))
    node = Node(
        id="T1",
        kind=NodeKind.TASK,
        title="t",
        status=Status.REVIEWING,
        claimed_from=Status.FIXED,
        review=True,
        fix=False,
        merge=Merge.PARENT,
        outcome=Outcome.MERGE_FAILED,
        verdict="rebuilt the index",
        fix_for=Outcome.REJECT,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
        branch="tm/T1@1",
        requires=["figma"],
        land_order=["core", "web"],
    )
    repo.save_node(node)
    loaded = repo.get_node("T1")
    assert loaded is not None
    assert loaded.model_dump(exclude={"created_at", "updated_at"}) == node.model_dump(
        exclude={"created_at", "updated_at"}
    )


@pytest.mark.parametrize(
    ("kind", "review", "fix"),
    [
        (NodeKind.TASK, True, True),
        (NodeKind.PLAN, False, False),
        (NodeKind.SPEC, False, False),
        (NodeKind.DECISION, True, True),
    ],
)
def test_a_container_defaults_to_no_review_and_a_task_to_review_and_fix(
    kind: NodeKind, review: bool, fix: bool
) -> None:
    node = Node(id="N", kind=kind, title="n")
    assert (node.review, node.fix) == (review, fix)


def test_a_container_keeps_the_flags_its_planner_set() -> None:
    node = Node(id="P", kind=NodeKind.PLAN, title="p", review=True, fix=True)
    assert (node.review, node.fix) == (True, True)


@pytest.mark.parametrize(
    ("status", "vocabulary"),
    [
        (Status.READY, Status),
        (Status.IMPLEMENTED, Status),
        (Status.FAILED, Status),
        (DecisionStatus.OPEN, DecisionStatus),
        (DecisionStatus.ANSWERED, DecisionStatus),
    ],
)
def test_a_stored_status_reads_back_in_its_own_vocabulary(
    tmp_path: Path, status: Status | DecisionStatus, vocabulary: type
) -> None:
    kind = NodeKind.DECISION if vocabulary is DecisionStatus else NodeKind.TASK
    repo = NodeRepository(_fresh(tmp_path))
    repo.save_node(Node(id="N", kind=kind, title="n", status=status))
    loaded = repo.get_node("N")
    assert loaded is not None
    assert loaded.status == status
    assert isinstance(loaded.status, vocabulary)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fix", "1, review = 0"),
        ("merge", "'sideways'"),
        ("outcome", "'maybe'"),
        ("fix_for", "'maybe'"),
        ("claimed_from", "'MERGING'"),
        ("review", "2"),
        ("review_cycles", "-1"),
        ("merge_attempts", "-1"),
        ("step_failures", "-1"),
    ],
)
def test_the_schema_refuses_an_out_of_range_lifecycle_value(
    tmp_path: Path, column: str, value: str
) -> None:
    db = _fresh(tmp_path)
    NodeRepository(db).save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE nodes SET {column} = {value} WHERE id = 'T1'")


def test_conditions_are_numbered_listed_in_order_and_removed(tmp_path: Path) -> None:
    repo = NodeRepository(_fresh(tmp_path))
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    first = repo.add_condition(Condition(node_id="T1", needs="staging up", command="true"))
    second = repo.add_condition(
        Condition(node_id="T1", needs="gate green", command="exit 0", stage=ConditionStage.LANDING)
    )
    assert (first.idx, second.idx) == (1, 2)
    assert repo.get_conditions("T1") == [first, second]
    assert repo.remove_condition("T1", 1) is True
    assert repo.remove_condition("T1", 1) is False
    assert repo.get_conditions("T1") == [second]
    assert repo.add_condition(Condition(node_id="T1", needs="n", command="true")).idx == 3


def test_a_nodes_conditions_go_with_it(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    repo = NodeRepository(db)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    repo.add_condition(Condition(node_id="T1", needs="n", command="true"))
    with db.get_state_connection() as conn:
        conn.execute("DELETE FROM nodes WHERE id = 'T1'")
        conn.commit()
    assert repo.get_conditions("T1") == []


@pytest.mark.parametrize(
    ("command", "stage"), [("   ", "claim"), ("true", "later")], ids=["blank", "stage"]
)
def test_the_schema_refuses_a_blank_command_or_an_unknown_stage(
    tmp_path: Path, command: str, stage: str
) -> None:
    db = _fresh(tmp_path)
    NodeRepository(db).save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO node_conditions (node_id, idx, needs, command, stage) "
            "VALUES ('T1', 1, 'n', ?, ?)",
            (command, stage),
        )


def test_a_lease_write_joins_an_open_node_transaction(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    nodes, leases = NodeRepository(db), RuntimeRepository(db)
    with pytest.raises(RuntimeError), nodes.transaction():
        assert db.in_transaction
        nodes.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
        leases.acquire_lease(
            Lease(task_id="T1", agent_id="a", session_id="s", branch_name="tm/T1"), []
        )
        raise RuntimeError("the step failed after its lease was written")
    assert not db.in_transaction
    assert nodes.get_node("T1") is None
    assert leases.get_lease("T1") is None
