import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import FileLock, Lease, Node
from taskmanager.db import connection
from taskmanager.db.connection import DatabaseManager, StateSchemaTooOld
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import LEDGER_SCHEMA_SQL, STATE_MIGRATIONS, STATE_SCHEMA_VERSION
from taskmanager.engine.archive import archive_path, archived_repo

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "state_v1.sql"
runner = CliRunner()

# Each node as 0.3.23 stores it, with what schema 10 reads back: (acceptable_models,
# frontmatter) before, then after.
NODES: dict[str, tuple[list[str], dict[str, object], list[str], dict[str, object]]] = {
    "EMPTY": ([], {}, [], {}),
    "ONE": (
        ["claude-sonnet-5-5"],
        {"sensitive": "migration"},
        ["standard"],
        {"sensitive": "migration"},
    ),
    "EVERY": (
        ["claude-haiku-4-5-20251001", "claude-sonnet-5-5", "claude-opus-5-5", "gpt-6.1-sol"],
        {"review_models": ["claude-opus-5-5", "claude-fable-5-1"], "requires": ["gpu"]},
        ["light", "standard", "strong", "gpt-6.1-sol"],
        {"review_models": ["strong"], "requires": ["gpu"]},
    ),
    "TWINS": (
        ["claude-opus-5-5", "claude-fable-5-1"],
        {"review_models": []},
        ["strong"],
        {"review_models": []},
    ),
    "REVIEWED": (
        ["gpt-6.1-sol"],
        {"review_models": ["claude-haiku-4-5-20251001"]},
        ["gpt-6.1-sol"],
        {"review_models": ["light"]},
    ),
    # A number reads as its string, as routing reads it.
    "NUMBERED": (
        ["claude-opus-5-5"],
        {"review_models": [5, "claude-opus-5-5"]},
        ["strong"],
        {"review_models": ["5", "strong"]},
    ),
    # `--set review_models=<id>` stores a bare string: not a list of ids, so not rewritten.
    "BARE": (
        ["gpt-6.1-sol"],
        {"review_models": "claude-opus-5-5"},
        ["gpt-6.1-sol"],
        {"review_models": "claude-opus-5-5"},
    ),
    "PINNED": (
        ["gpt-6.1-sol", "sonnet", "claude-sonnet", "standard"],
        {"review_models": ["gpt-6.1-sol"]},
        ["gpt-6.1-sol", "sonnet", "claude-sonnet", "standard"],
        {"review_models": ["gpt-6.1-sol"]},
    ),
}
UNCHANGED = ("EMPTY", "BARE", "PINNED")


def _schema_9(db_file: Path, extra: str = "") -> None:
    """The file 0.3.0 wrote, carried through every migration up to schema 9."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(_FIXTURE.read_text(encoding="utf-8"))
        for version in range(2, 10):
            script = STATE_MIGRATIONS[version]
            assert isinstance(script, str)
            conn.executescript(script)
        conn.executescript(extra)
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
    finally:
        conn.close()


def _save(db: DatabaseManager, nodes: dict[str, tuple[list[str], dict[str, object]]]) -> None:
    repo = NodeRepository(db)
    for node_id, (models, frontmatter) in nodes.items():
        repo.save_node(
            Node(
                id=node_id,
                kind=NodeKind.TASK,
                title=node_id,
                acceptable_models=models,
                frontmatter=frontmatter,
            )
        )


def schema_9_estate(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, archive: bool = False, live_lease: bool = False
) -> None:
    """state.db, and archive.db with `archive`, at schema 9, holding `NODES` as written then."""
    tm_dir = root / ".taskmanager"
    _schema_9(tm_dir / "state.db")
    if archive:
        _schema_9(archive_path(root), LEDGER_SCHEMA_SQL)
    before = {node_id: row[:2] for node_id, row in NODES.items()}
    with monkeypatch.context() as at_9:
        at_9.setattr(connection, "STATE_SCHEMA_VERSION", 9)
        live = DatabaseManager(tm_dir)
        _save(live, before)
        if live_lease:
            now = datetime.now(tz=UTC)
            RuntimeRepository(live).acquire_lease(
                Lease(
                    task_id="ONE",
                    agent_id="implementer",
                    session_id="s",
                    branch_name="tm/ONE",
                    ttl_seconds=3600,
                    acquired_at=now,
                    last_heartbeat=now,
                ),
                [FileLock(file_path="api:a.py", task_id="ONE")],
            )
        live.close()
        if archive:
            cold = DatabaseManager(tm_dir)
            cold.state_db = cold.ledger_db = archive_path(root)
            _save(cold, {f"OLD-{node_id}": row for node_id, row in before.items()})
            cold.close()


def _rows(db_file: Path) -> dict[str, tuple[str, str, str, int]]:
    """Each node's stored models, frontmatter, search text and rev, as the columns hold them."""
    conn = sqlite3.connect(db_file)
    try:
        return {
            node_id: (models, frontmatter, text, rev)
            for node_id, models, frontmatter, text, rev in conn.execute(
                "SELECT id, acceptable_models, frontmatter_json, frontmatter_text, rev "
                "FROM nodes JOIN nodes_fts ON nodes_fts.node_id = nodes.id"
            )
        }
    finally:
        conn.close()


def _dump(db_file: Path) -> list[str]:
    conn = sqlite3.connect(db_file)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_migration_10_rewrites_each_claude_id_into_its_tier_as_the_node_writer_stores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    state_db = tmp_path / ".taskmanager" / "state.db"
    before = _rows(state_db)

    db = DatabaseManager(tmp_path / ".taskmanager")
    assert db.migrate_state() == 9
    nodes = {n.id: n for n in NodeRepository(db).list_nodes()}
    db.close()

    after = _rows(state_db)
    for node_id, (_, _, models, frontmatter) in NODES.items():
        assert nodes[node_id].acceptable_models == models
        assert nodes[node_id].frontmatter == frontmatter
        if node_id in UNCHANGED:
            assert after[node_id] == before[node_id]
        else:
            # The search text follows the frontmatter it indexes; the rev tells readers it moved.
            assert after[node_id] == (
                json.dumps(models),
                json.dumps(frontmatter),
                json.dumps(frontmatter),
                before[node_id][3] + 1,
            )
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == STATE_SCHEMA_VERSION == 10
    finally:
        conn.close()


def test_a_schema_9_estate_is_refused_until_tm_db_migrate_backs_it_up_and_migrates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    tm_dir = tmp_path / ".taskmanager"
    before = _dump(tm_dir / "state.db")
    with pytest.raises(StateSchemaTooOld, match="state.db is schema 9; run `tm db migrate`"):
        NodeRepository(DatabaseManager(tm_dir)).list_nodes()
    assert _dump(tm_dir / "state.db") == before

    result = runner.invoke(app, ["db", "migrate", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    backup = tm_dir / "state.db.schema9.bak"
    said = " ".join(result.output.split())
    assert f"Migrated state.db from schema 9 to 10; the schema-9 copy is {backup}" in said
    assert _dump(backup) == before
    assert NodeRepository(DatabaseManager(tm_dir)).get_node("EVERY").acceptable_models == [
        "light",
        "standard",
        "strong",
        "gpt-6.1-sol",
    ]


def test_tm_db_migrate_rewrites_the_archive_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_9_estate(tmp_path, monkeypatch, archive=True)

    result = runner.invoke(app, ["db", "migrate", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    backup = tmp_path / ".taskmanager" / "archive.db.schema9.bak"
    said = " ".join(result.output.split())
    assert f"Migrated archive.db from schema 9 to 10; the schema-9 copy is {backup}" in said
    archived = {n.id: n for n in archived_repo(tmp_path).list_nodes()}
    assert {
        node_id: (
            archived[f"OLD-{node_id}"].acceptable_models,
            archived[f"OLD-{node_id}"].frontmatter,
        )
        for node_id in NODES
    } == {node_id: row[2:] for node_id, row in NODES.items()}


def test_a_schema_9_estate_with_a_live_lease_is_refused_and_left_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_9_estate(tmp_path, monkeypatch, live_lease=True)
    tm_dir = tmp_path / ".taskmanager"
    state_db = tm_dir / "state.db"
    config = tm_dir / "config.yaml"
    config.write_text("models:\n  default: claude-sonnet-5-5\n", encoding="utf-8")
    before = _dump(state_db)

    result = runner.invoke(app, ["db", "migrate", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "ONE (implementer) still hold a lease" in " ".join(result.output.split())
    assert _dump(state_db) == before
    assert config.read_text(encoding="utf-8") == "models:\n  default: claude-sonnet-5-5\n"
    assert not (tm_dir / "state.db.schema9.bak").exists()


def test_a_failure_after_the_rewrite_rolls_it_back_with_the_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    state_db = tmp_path / ".taskmanager" / "state.db"
    before = _dump(state_db)

    def schema_11(conn: sqlite3.Connection) -> None:
        raise RuntimeError("schema 11 failed")

    monkeypatch.setattr(connection, "STATE_SCHEMA_VERSION", 11)
    monkeypatch.setitem(STATE_MIGRATIONS, 11, schema_11)
    with pytest.raises(RuntimeError, match="schema 11 failed"):
        DatabaseManager(tmp_path / ".taskmanager").migrate_state()

    assert _dump(state_db) == before
    conn = sqlite3.connect(state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        conn.close()
