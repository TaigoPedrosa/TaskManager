import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from test_state_migration import (
    _build_v1_estate,
    _build_v2_estate,
    _merge_as_spec,
    _raw_rows,
    _seed_every_v2_status,
    _user_version,
)

from taskmanager.cli.main import main
from taskmanager.db.schema import STATE_SCHEMA_VERSION


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(db: Path) -> list[str]:
    # The backup API rewrites header fields on its copy, so the file's bytes are not the measure.
    conn = sqlite3.connect(db)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _tm(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int | str | None, str]:
    monkeypatch.setattr(sys, "argv", ["tm", *argv])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        main()
    out, err = capsys.readouterr()
    assert err == ""
    return exited.value.code, " ".join(out.split())


def _v2_estate(root: Path) -> Path:
    _build_v2_estate(root / ".taskmanager")
    _seed_every_v2_status(root / ".taskmanager")
    return root


def test_migrate_schema_2_estate_backs_it_up_then_keeps_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    v2_estate = _v2_estate(tmp_path)
    state_db = v2_estate / ".taskmanager" / "state.db"
    backup = v2_estate / ".taskmanager" / "state.db.schema2.bak"
    before_dump = _dump(state_db)
    before_rows = _raw_rows(state_db)

    code, out = _tm(["db", "migrate", "-C", str(v2_estate)], monkeypatch, capsys)

    assert code == 0
    assert out == (
        f"Migrated state.db from schema 2 to {STATE_SCHEMA_VERSION}; the schema-2 copy is {backup}"
    )
    assert _user_version(backup) == 2
    assert _dump(backup) == before_dump
    assert _raw_rows(backup) == before_rows
    assert _user_version(state_db) == STATE_SCHEMA_VERSION
    assert _raw_rows(state_db) == _merge_as_spec(before_rows)
    assert all(before_rows.values())


def test_migrate_a_current_estate_says_so_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    v2_estate = _v2_estate(tmp_path)
    state_db = v2_estate / ".taskmanager" / "state.db"
    backup = v2_estate / ".taskmanager" / "state.db.schema2.bak"
    assert _tm(["db", "migrate", "-C", str(v2_estate)], monkeypatch, capsys)[0] == 0
    migrated, backed_up = _sha256(state_db.read_bytes()), _sha256(backup.read_bytes())

    code, out = _tm(["db", "migrate", "-C", str(v2_estate)], monkeypatch, capsys)

    assert code == 0
    assert out == f"state.db is current at schema {STATE_SCHEMA_VERSION}: nothing to migrate"
    assert _sha256(state_db.read_bytes()) == migrated
    assert _sha256(backup.read_bytes()) == backed_up
    assert sorted(p.name for p in state_db.parent.iterdir()) == ["state.db", backup.name]


def test_migrate_schema_1_estate_runs_every_step_through_the_same_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_v1_estate(tmp_path / ".taskmanager")
    state_db = tmp_path / ".taskmanager" / "state.db"
    backup = tmp_path / ".taskmanager" / "state.db.schema1.bak"

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 0
    assert out == (
        f"Migrated state.db from schema 1 to {STATE_SCHEMA_VERSION}; the schema-1 copy is {backup}"
    )
    assert _user_version(backup) == 1
    assert _user_version(state_db) == STATE_SCHEMA_VERSION
    conn = sqlite3.connect(state_db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()
    assert "rev" in columns


def test_migrate_without_state_db_refuses_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".taskmanager").mkdir()

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 1
    assert out == f"Error: TaskManager is not initialized in {tmp_path}. Run 'tm init' first."
    assert not any((tmp_path / ".taskmanager").iterdir())


def test_migrate_an_empty_state_db_refuses_and_leaves_it_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_db = tmp_path / ".taskmanager" / "state.db"
    state_db.parent.mkdir()
    state_db.touch()

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 1
    assert out == f"Error: TaskManager is not initialized in {tmp_path}. Run 'tm init' first."
    assert state_db.read_bytes() == b""
    assert [p.name for p in state_db.parent.iterdir()] == ["state.db"]


def test_migrate_help_names_the_backup_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _tm(["db", "migrate", "--help"], monkeypatch, capsys)

    assert code == 0
    assert ".taskmanager/state.db.schema<n>.bak" in out
