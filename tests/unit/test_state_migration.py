import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app, main
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    Condition,
    Job,
    Lease,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import DecisionStatus, JobKind, Status
from taskmanager.db.connection import DatabaseManager, StateSchemaTooNew
from taskmanager.db.graph_reader import read_graph
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import SCHEMA_VERSION, STATE_MIGRATIONS, STATE_SCHEMA_VERSION

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "state_v1.sql"
runner = CliRunner()


def _build_v1_estate(taskmanager_dir: Path) -> None:
    """A `state.db` shaped exactly like 0.3.0 wrote it: the frozen schema, at its own version,
    with no `rev` column and no trigger this version added."""
    taskmanager_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(taskmanager_dir / "state.db")
    try:
        conn.executescript(_FIXTURE.read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def _repo(tmp_path: Path) -> NodeRepository:
    db = DatabaseManager(tmp_path)
    db.init_all()
    return NodeRepository(db)


def _rev(repo: NodeRepository, node_id: str) -> int:
    with repo.db.get_state_connection() as conn:
        row = conn.execute("SELECT rev FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None
    return row[0]


def _sqlite_master_rows(db: DatabaseManager) -> list[tuple[str, str, str, str]]:
    with db.get_state_connection() as conn:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name = 'nodes' OR name LIKE 'trg_%rev%' ORDER BY name"
        ).fetchall()


def test_migrated_v1_estate_matches_a_fresh_init(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _build_v1_estate(old)
    migrated = DatabaseManager(old)
    with migrated.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION == 3

    fresh = DatabaseManager(tmp_path / "fresh")
    fresh.init_all()
    with fresh.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION

    assert _sqlite_master_rows(migrated) == _sqlite_master_rows(fresh)


def _build_v2_estate(taskmanager_dir: Path) -> None:
    """A `state.db` at schema 2, as 0.3.3 wrote it: its `nodes` CHECK has no LANDED."""
    _build_v1_estate(taskmanager_dir)
    conn = sqlite3.connect(taskmanager_dir / "state.db")
    try:
        conn.executescript(STATE_MIGRATIONS[2])
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _seed_every_v2_status(taskmanager_dir: Path) -> None:
    conn = sqlite3.connect(taskmanager_dir / "state.db")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # A deleted first row leaves a rowid gap, so a copy that renumbers rowids shows.
        conn.execute("INSERT INTO nodes (id, kind, title) VALUES ('GAP', 'task', 'gap')")
        cycle = [s for s in Status if s is not Status.LANDED]
        for i, status in enumerate(cycle):
            conn.execute(
                "INSERT INTO nodes (id, kind, title, status, priority, ordinal, target_repo, "
                "frontmatter_json, claimed_from, review, fix, merge, outcome, verdict, fix_for, "
                "review_cycles, merge_attempts, step_failures, branch, requires, land_order, "
                "created_at, updated_at) VALUES (?, 'task', ?, ?, ?, ?, 'backend', '{\"k\": 1}', "
                "'READY', 1, 0, 'parent', 'reject', 'needs work', 'reject', 2, 1, 1, ?, "
                "'[\"gpu\"]', '[\"a\"]', '2026-01-01 00:00:00', '2026-01-02 00:00:00')",
                (f"T{i:02}", f"task {status}", status.value, 10 + i, i, f"tm/T{i:02}"),
            )
        for status in DecisionStatus:
            conn.execute(
                "INSERT INTO nodes (id, kind, title, status) VALUES (?, 'decision', ?, ?)",
                (f"D-{status}", f"decision {status}", status.value),
            )
        conn.execute("DELETE FROM nodes WHERE id = 'GAP'")
        conn.execute("UPDATE nodes SET title = title || '!' WHERE id = 'T00'")
        conn.execute(
            "INSERT INTO node_sections (node_id, section_key, ordinal, header, content) "
            "VALUES ('T00', 'body', 1, '## Body', 'x')"
        )
        conn.execute(
            "INSERT INTO node_relations (source_id, target_id, relation_type) "
            "VALUES ('T00', 'T01', 'depends_on')"
        )
        conn.execute("INSERT INTO nodes_fts (rowid, node_id, title) VALUES (1, 'T00', 'task')")
        conn.commit()
    finally:
        conn.close()


def _raw_rows(state_db: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = sqlite3.connect(state_db)
    try:
        return {
            "nodes": conn.execute("SELECT rowid, * FROM nodes ORDER BY id").fetchall(),
            "node_sections": conn.execute("SELECT * FROM node_sections").fetchall(),
            "node_relations": conn.execute("SELECT * FROM node_relations").fetchall(),
        }
    finally:
        conn.close()


def _write_landed(state_db: Path, status: str, claimed_from: str | None) -> None:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "UPDATE nodes SET status = ?, claimed_from = ? WHERE id = 'T00'",
            (status, claimed_from),
        )
        conn.commit()
    finally:
        conn.close()


def test_open_schema_2_estate_keeps_every_row_and_column_unchanged(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _build_v2_estate(old)
    _seed_every_v2_status(old)
    before = _raw_rows(old / "state.db")
    seeded = {row[4] for row in before["nodes"]}
    assert seeded == {s.value for s in Status if s is not Status.LANDED} | set(DecisionStatus)

    db = DatabaseManager(old)
    with db.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    db.close()

    assert _raw_rows(old / "state.db") == before
    assert before["node_sections"] != []
    assert before["node_relations"] != []


def test_open_schema_2_estate_stores_the_fresh_nodes_sql_byte_for_byte(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _build_v2_estate(old)
    migrated = DatabaseManager(old)
    fresh = DatabaseManager(tmp_path / "fresh")
    fresh.init_all()

    assert _sqlite_master_rows(migrated) == _sqlite_master_rows(fresh)
    with migrated.get_state_connection() as conn:
        (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'nodes'").fetchone()
    assert "'LANDED'" in sql


@pytest.mark.parametrize(
    ("status", "claimed_from"),
    [("LANDED", None), ("REVIEWING", "LANDED")],
    ids=["landed-node", "review-claimed-from-landed"],
)
def test_open_schema_2_estate_then_write_landed_succeeds(
    tmp_path: Path, status: str, claimed_from: str | None
) -> None:
    old = tmp_path / "old"
    _build_v2_estate(old)
    _seed_every_v2_status(old)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _write_landed(old / "state.db", status, claimed_from)

    db = DatabaseManager(old)
    with db.get_state_connection():
        pass
    db.close()

    _write_landed(old / "state.db", status, claimed_from)
    conn = sqlite3.connect(old / "state.db")
    try:
        row = conn.execute("SELECT status, claimed_from FROM nodes WHERE id = 'T00'").fetchone()
    finally:
        conn.close()
    assert row == (status, claimed_from)


def test_open_schema_3_estate_leaves_the_file_bytes_untouched(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    first = DatabaseManager(estate)
    first.init_all()
    first.close()
    before = (estate / "state.db").read_bytes()

    again = DatabaseManager(estate)
    with again.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION == 3
    again.close()

    assert (estate / "state.db").read_bytes() == before


def test_uninitialised_directory_is_left_to_tm_init(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    with db.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        has_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
    assert has_nodes is None

    db.init_all()
    with db.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION


def test_two_database_managers_migrate_the_same_estate_exactly_once(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _build_v1_estate(estate)

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def open_it() -> None:
        try:
            barrier.wait(timeout=5)
            with DatabaseManager(estate).get_state_connection():
                pass
        except BaseException as exc:  # noqa: BLE001 -- captured across the thread boundary
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    conn = sqlite3.connect(estate / "state.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION
    finally:
        conn.close()


def test_newer_schema_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    future = tmp_path / "future"
    _build_v1_estate(future)
    conn = sqlite3.connect(future / "state.db")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    before = (future / "state.db").read_bytes()

    with (
        pytest.raises(StateSchemaTooNew, match=r"schema 4.*\(3\)"),
        DatabaseManager(future).get_state_connection(),
    ):
        pass

    # Not just user_version and the rev column: the file's bytes, so a WAL-mode switch (which
    # rewrites the header and adds -wal/-shm) counts as a write too.
    assert (future / "state.db").read_bytes() == before
    assert not (future / "state.db-wal").exists()
    assert not (future / "state.db-shm").exists()


def test_a_refused_connection_is_closed_not_leaked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    future = tmp_path / "future"
    _build_v1_estate(future)
    conn = sqlite3.connect(future / "state.db")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: opened.append(real_connect(*a, **kw)) or opened[-1]
    )

    with (
        pytest.raises(StateSchemaTooNew),
        DatabaseManager(future).get_state_connection(),
    ):
        pass

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


# One command per registered group, plus the top-level ones that read the estate, each run
# through the console entry so the refusal is what a user sees, not what CliRunner catches.
_ESTATE_COMMANDS: dict[str, list[str]] = {
    "spec": ["spec", "list"],
    "plan": ["plan", "list"],
    "task": ["task", "list"],
    "section": ["section", "get", "S1:body"],
    "run": ["run", "list"],
    "wave": ["wave", "discover", "--session", "s", "--slots", "1", "--max-strong", "1"],
    "verify": ["verify", "list", "S1"],
    "audit": ["audit", "list"],
    "web run": ["web", "run", "--no-open"],
    "web export": ["web", "export", "-o", "out.html"],
    "decision": ["decision", "list"],
    "job": ["job", "status", "J1"],
    "land": ["land", "start", "S1"],
    "next": ["next"],
    "render": ["render", "S1"],
    "export": ["export", "exported"],
}


def test_every_registered_group_is_covered_by_the_too_new_schema_check() -> None:
    covered = {argv[0] for argv in _ESTATE_COMMANDS.values()}
    groups = {g.typer_instance.info.name for g in app.registered_groups if g.typer_instance}
    assert groups - covered <= {"plugin", "config"}


@pytest.mark.parametrize("argv", _ESTATE_COMMANDS.values(), ids=_ESTATE_COMMANDS.keys())
def test_a_too_new_schema_is_only_its_refusal_from_every_command(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "estate"
    res = runner.invoke(app, ["init", "-C", str(root)])
    assert res.exit_code == 0, res.output
    conn = sqlite3.connect(root / ".taskmanager" / "state.db")
    conn.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["tm", *argv, "-C", str(root)])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exited:
        main()

    out, err = capsys.readouterr()
    assert exited.value.code == 1
    assert out.strip() == (
        f"state.db is schema {STATE_SCHEMA_VERSION + 1}, newer than this tm "
        f"({STATE_SCHEMA_VERSION}): upgrade tm"
    ), out + err
    assert err == ""


def test_rev_increases_on_status_and_title_change_through_save_node(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    node = Node(id="T1", kind=NodeKind.TASK, title="Original", status=Status.READY)
    repo.save_node(node)
    rev0 = _rev(repo, "T1")

    repo.save_node(node.model_copy(update={"status": Status.DEFERRED}))
    rev1 = _rev(repo, "T1")
    assert rev1 > rev0

    repo.save_node(node.model_copy(update={"status": Status.DEFERRED, "title": "Renamed"}))
    assert _rev(repo, "T1") > rev1


def test_rev_increases_on_set_section_and_remove_section(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    rev0 = _rev(repo, "T1")

    repo.save_section(
        NodeSection(node_id="T1", section_key="body", ordinal=1, header="## Body", content="x")
    )
    rev1 = _rev(repo, "T1")
    assert rev1 > rev0

    assert repo.remove_section("T1", "body")
    assert _rev(repo, "T1") > rev1


def test_rev_increases_on_add_and_remove_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    rev0 = _rev(repo, "T1")

    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="a.py"
    )
    repo.add_verification(ver)
    rev1 = _rev(repo, "T1")
    assert rev1 > rev0
    assert ver.id is not None

    assert repo.remove_verification("T1", ver.id)
    assert _rev(repo, "T1") > rev1


def test_rev_increases_on_add_and_remove_condition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    rev0 = _rev(repo, "T1")

    added = repo.add_condition(Condition(node_id="T1", needs="X", command="true"))
    rev1 = _rev(repo, "T1")
    assert rev1 > rev0

    assert repo.remove_condition("T1", added.idx)
    assert _rev(repo, "T1") > rev1


def test_rev_increases_on_add_and_remove_dependency_on_both_ends(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T1", status=Status.READY))
    repo.save_node(Node(id="T2", kind=NodeKind.TASK, title="T2", status=Status.READY))
    source_rev0, target_rev0 = _rev(repo, "T1"), _rev(repo, "T2")

    repo.add_relation(
        NodeRelation(source_id="T1", target_id="T2", relation_type=RelationType.DEPENDS_ON)
    )
    source_rev1, target_rev1 = _rev(repo, "T1"), _rev(repo, "T2")
    assert source_rev1 > source_rev0
    assert target_rev1 > target_rev0

    repo.remove_relation("T1", "T2", RelationType.DEPENDS_ON)
    assert _rev(repo, "T1") > source_rev1
    assert _rev(repo, "T2") > target_rev1


def test_rev_increases_on_moving_a_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save_node(Node(id="P1", kind=NodeKind.PLAN, title="P1", status=Status.READY))
    repo.save_node(Node(id="P2", kind=NodeKind.PLAN, title="P2", status=Status.READY))
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T1", status=Status.READY))
    repo.add_relation(
        NodeRelation(source_id="P1", target_id="T1", relation_type=RelationType.CONTAINS)
    )
    rev0 = _rev(repo, "T1")

    repo.remove_relation("P1", "T1", RelationType.CONTAINS)
    repo.add_relation(
        NodeRelation(source_id="P2", target_id="T1", relation_type=RelationType.CONTAINS)
    )
    assert _rev(repo, "T1") > rev0


def test_rev_unchanged_by_acquiring_heartbeating_and_releasing_a_lease(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    runtime = RuntimeRepository(db)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    rev0 = _rev(repo, "T1")

    runtime.acquire_lease(
        Lease(task_id="T1", agent_id="a", session_id="s", branch_name="tm/T1"), []
    )
    assert _rev(repo, "T1") == rev0

    assert runtime.heartbeat("T1")
    assert _rev(repo, "T1") == rev0

    runtime.release_lease("T1")
    assert _rev(repo, "T1") == rev0


def test_rev_unchanged_by_job_writes(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    jobs = JobRepository(db)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    rev0 = _rev(repo, "T1")

    jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="backend",
            target="main",
            heartbeat=datetime.now(tz=UTC),
        )
    )
    assert _rev(repo, "T1") == rev0


def test_graph_data_exposes_each_nodes_rev(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T", status=Status.READY))
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="T renamed", status=Status.READY))

    data = read_graph(db)
    assert data.revs["T1"] == _rev(repo, "T1")
    assert data.revs["T1"] > 0


def test_audit_and_cache_dbs_keep_schema_1(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    assert SCHEMA_VERSION == 1
    with db.get_ledger_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    with db.get_cache_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_export_then_restore_round_trips_and_never_exports_rev(tmp_path: Path) -> None:
    source = tmp_path / "source"
    setup = [
        ["init", "-C", str(source)],
        ["spec", "add", "S", "--slug", "S1", "-C", str(source)],
        ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(source)],
        ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "-C", str(source)],
        ["section", "set", "S1-P1-t1:body", "line one", "-C", str(source)],
    ]
    for args in setup:
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.output

    export_dir = tmp_path / "export"
    res = runner.invoke(app, ["export", str(export_dir), "-C", str(source)])
    assert res.exit_code == 0, res.output
    for f in export_dir.glob("*.json"):
        assert '"rev"' not in f.read_text(encoding="utf-8"), f.name

    restored = tmp_path / "restored"
    restored.mkdir()
    res = runner.invoke(app, ["restore", str(export_dir), "-C", str(restored)])
    assert res.exit_code == 0, res.output

    export_dir2 = tmp_path / "export2"
    res = runner.invoke(app, ["export", str(export_dir2), "-C", str(restored)])
    assert res.exit_code == 0, res.output
    for f in sorted(export_dir.glob("*.json")):
        assert f.read_bytes() == (export_dir2 / f.name).read_bytes(), f.name
