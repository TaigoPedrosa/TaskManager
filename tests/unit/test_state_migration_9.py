import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    Check,
    Condition,
    FileLock,
    Finding,
    Job,
    Lease,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
    Note,
)
from taskmanager.core.status import CheckResult, JobKind, JobState, Severity
from taskmanager.db import connection
from taskmanager.db.connection import DatabaseManager, StateSchemaTooOld
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import LEDGER_SCHEMA_SQL, STATE_MIGRATIONS, STATE_SCHEMA_VERSION
from taskmanager.engine.archive import archive_path, archived_repo

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "state_v1.sql"
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
runner = CliRunner()


def _schema_8(db_file: Path, extra: str = "") -> None:
    """The file 0.3.0 wrote, carried through every migration up to schema 8."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(_FIXTURE.read_text(encoding="utf-8"))
        for version in range(2, 9):
            conn.executescript(STATE_MIGRATIONS[version])
        conn.executescript(extra)
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
    finally:
        conn.close()


def _seed(db: DatabaseManager) -> None:
    """A row in each table through the repositories that write it in use."""
    nodes = NodeRepository(db)
    for node_id in ("T1", "T2"):
        nodes.save_node(Node(id=node_id, kind=NodeKind.TASK, title=f"task {node_id}"))
    nodes.save_section(
        NodeSection(node_id="T1", section_key="body", ordinal=1, header="## B", content="x")
    )
    nodes.add_relation(
        NodeRelation(source_id="T1", target_id="T2", relation_type=RelationType.DEPENDS_ON)
    )
    nodes.add_verification(
        NodeVerification(
            node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="a.py"
        )
    )
    nodes.add_condition(Condition(node_id="T1", needs="ready", command="true"))
    nodes.add_finding(Finding(node_id="T1", severity=Severity.MINOR, at="a.py:1", breaks="b"))
    nodes.add_check(Check(node_id="T1", deleted="a.py:2", result=CheckResult.RED, test="t"))
    nodes.add_note(Note(node_id="T1", text="n"))
    jobs = JobRepository(db)
    jobs.create(
        Job(kind=JobKind.LAND, node_id="T2", repo="api", target="main", state=JobState.SUCCEEDED)
    )
    assert jobs.acquire_branch("api", "main", "J")
    # Lapsed, since a live lease refuses the migration.
    RuntimeRepository(db).acquire_lease(
        Lease(
            task_id="T1",
            agent_id="a",
            session_id="s",
            branch_name="tm/T1",
            ttl_seconds=1,
            acquired_at=_LONG_AGO,
            last_heartbeat=_LONG_AGO,
        ),
        [FileLock(file_path="api:a.py", task_id="T1")],
    )


def _dump(db_file: Path) -> list[str]:
    conn = sqlite3.connect(db_file)
    try:
        return sorted(conn.iterdump())
    finally:
        conn.close()


def _reservations_sql(db_file: Path) -> str | None:
    conn = sqlite3.connect(db_file)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'reservations'").fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _schema_8_estate(root: Path, monkeypatch: pytest.MonkeyPatch, *, archive: bool = False) -> None:
    tm_dir = root / ".taskmanager"
    _schema_8(tm_dir / "state.db")
    if archive:
        _schema_8(archive_path(root), LEDGER_SCHEMA_SQL)
    with monkeypatch.context() as at_8:
        at_8.setattr(connection, "STATE_SCHEMA_VERSION", 8)
        live = DatabaseManager(tm_dir)
        _seed(live)
        live.close()
        if archive:
            cold = DatabaseManager(tm_dir)
            cold.state_db = cold.ledger_db = archive_path(root)
            NodeRepository(cold).save_node(Node(id="OLD", kind=NodeKind.SPEC, title="old"))
            cold.close()


def test_migration_9_adds_reservations_and_leaves_every_existing_row_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _schema_8_estate(tmp_path, monkeypatch)
    state_db = tmp_path / ".taskmanager" / "state.db"
    before = _dump(state_db)
    assert any(line.startswith('INSERT INTO "file_locks"') for line in before)
    assert _reservations_sql(state_db) is None

    db = DatabaseManager(tmp_path / ".taskmanager")
    assert db.migrate_state() == 8
    db.close()

    created = _reservations_sql(state_db)
    assert created is not None
    assert _dump(state_db) == sorted([*before, f"{created};"])
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION == 9
    finally:
        conn.close()


def test_tm_init_creates_reservations_at_schema_9_as_the_migration_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = runner.invoke(app, ["init", "--path", str(fresh)])
    assert result.exit_code == 0, result.output
    fresh_db = fresh / ".taskmanager" / "state.db"
    conn = sqlite3.connect(fresh_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    finally:
        conn.close()

    old = tmp_path / "old"
    _schema_8_estate(old, monkeypatch)
    assert DatabaseManager(old / ".taskmanager").migrate_state() == 8
    assert _reservations_sql(old / ".taskmanager" / "state.db") == _reservations_sql(fresh_db)


def test_tm_db_migrate_brings_a_schema_8_archive_to_schema_9_so_it_still_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _schema_8_estate(tmp_path, monkeypatch, archive=True)
    state_before = _dump(tmp_path / ".taskmanager" / "state.db")
    with pytest.raises(StateSchemaTooOld):
        archived_repo(tmp_path)

    result = runner.invoke(app, ["db", "migrate", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    backup = tmp_path / ".taskmanager" / "archive.db.schema8.bak"
    said = " ".join(result.output.split())
    assert f"Migrated archive.db from schema 8 to 9; the schema-8 copy is {backup}" in said
    assert _dump(tmp_path / ".taskmanager" / "state.db.schema8.bak") == state_before
    assert [n.id for n in archived_repo(tmp_path).list_nodes()] == ["OLD"]
    again = runner.invoke(app, ["db", "migrate", "--path", str(tmp_path)])
    assert again.exit_code == 0, again.output
    assert " ".join(again.output.split()) == "state.db is current at schema 9: nothing to migrate"
