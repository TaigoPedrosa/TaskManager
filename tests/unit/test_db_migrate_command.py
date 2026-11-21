import hashlib
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_state_migration import (
    _as_schema_5,
    _build_v1_estate,
    _build_v2_estate,
    _raw_rows,
    _seed_every_v2_status,
    _user_version,
)
from test_state_migration_10 import schema_9_estate

from taskmanager.cli.main import main
from taskmanager.db import connection
from taskmanager.db.schema import STATE_MIGRATIONS, STATE_SCHEMA_VERSION
from taskmanager.engine.config import ConfigStore


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
    code = exited.value.code
    # Success speaks on stdout, a refusal on stderr, and neither writes to the other stream.
    said, silent = (out, err) if code == 0 else (err, out)
    assert silent == ""
    return code, " ".join(said.split())


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
    assert _raw_rows(state_db) == _as_schema_5(before_rows)
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


def _lease(state_db: Path, node_id: str, agent: str, ttl: int | None, beat: datetime) -> None:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "INSERT INTO leases (task_id, agent_id, session_id, branch_name, last_heartbeat, "
            "ttl_seconds, token) VALUES (?, ?, 's1', ?, ?, ?, ?)",
            (node_id, agent, f"tm/{node_id}", beat.isoformat(), ttl, f"tok-{node_id}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_refuses_while_a_lease_is_live_naming_each_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    v2_estate = _v2_estate(tmp_path)
    state_db = v2_estate / ".taskmanager" / "state.db"
    now = datetime.now(tz=UTC)
    _lease(state_db, "T01", "implementer-1", 300, now)
    _lease(state_db, "T02", "lander", None, now - timedelta(days=1))
    _lease(state_db, "T03", "gone", 300, now - timedelta(hours=1))
    before = _sha256(state_db.read_bytes())

    code, out = _tm(["db", "migrate", "-C", str(v2_estate)], monkeypatch, capsys)

    assert code == 1
    assert out == (
        "state.db was not migrated: T01 (implementer-1), T02 (lander) still hold a lease, and a "
        "step in flight would lose its next tm command to the new schema. Migrate once each "
        "closes, is released, or lapses past its TTL"
    )
    assert _user_version(state_db) == 2
    assert _sha256(state_db.read_bytes()) == before
    assert sorted(p.name for p in state_db.parent.iterdir()) == ["state.db"]

    conn = sqlite3.connect(state_db)
    try:
        conn.execute("DELETE FROM leases WHERE task_id IN ('T01', 'T02')")
        conn.commit()
    finally:
        conn.close()
    assert _tm(["db", "migrate", "-C", str(v2_estate)], monkeypatch, capsys)[0] == 0
    assert _user_version(state_db) == STATE_SCHEMA_VERSION


_CLAUDE_CONFIG = (
    "# Routed by hand\n"
    "models:\n"
    "    default: claude-sonnet-5-5\n"
    "    merge: claude-haiku-4-5-20251001\n"
    "    strong: [claude-opus-5-5, claude-fable-5-1, gpt-6.1-sol]\n"
    "worktree_dir: ../wt\n"
    "name: claude-sonnet-5-5\n"
)


def test_migrate_across_schema_10_rewrites_config_models_into_tiers_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text(_CLAUDE_CONFIG, encoding="utf-8")

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 0
    backup = tmp_path / ".taskmanager" / "state.db.schema9.bak"
    assert out == (
        f"Migrated state.db from schema 9 to 10; the schema-9 copy is {backup} "
        "Rewrote models.default, models.merge, models.strong in config.yaml from Claude ids "
        "into tiers"
    )
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == {
        "models": {"default": "standard", "merge": "light", "strong": ["strong", "gpt-6.1-sol"]},
        "worktree_dir": "../wt",
        "name": "claude-sonnet-5-5",
    }


@pytest.mark.parametrize(
    "stored",
    [
        "# Routed by hand\nworktree_dir: ../wt\n",
        "# Routed by hand\nmodels:\n    default: gpt-6.1-sol\n    strong: [strong, opus]\n",
    ],
    ids=["no models", "no claude id"],
)
def test_migrate_across_schema_10_leaves_a_config_without_claude_ids_byte_identical(
    stored: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text(stored, encoding="utf-8")
    before = _sha256(config.read_bytes())

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 0
    assert "config.yaml" not in out
    assert _sha256(config.read_bytes()) == before


def test_migrate_past_schema_10_keeps_a_claude_id_config_set_since_as_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    assert _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)[0] == 0
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text(_CLAUDE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(connection, "STATE_SCHEMA_VERSION", 11)
    monkeypatch.setitem(STATE_MIGRATIONS, 11, "")

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 0
    assert out.startswith("Migrated state.db from schema 10 to")
    assert "config.yaml" not in out
    assert config.read_text(encoding="utf-8") == _CLAUDE_CONFIG


def test_migrate_rerun_after_a_failed_config_write_rewrites_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    state_db = tmp_path / ".taskmanager" / "state.db"
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text(_CLAUDE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["tm", "db", "migrate", "-C", str(tmp_path)])

    def full_disk(self: ConfigStore, flat: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as failing, pytest.raises(OSError, match="No space left"):
        failing.setattr(ConfigStore, "_write", full_disk)
        main()

    assert _user_version(state_db) == 9
    assert config.read_text(encoding="utf-8") == _CLAUDE_CONFIG

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 0
    assert out.endswith(
        "Rewrote models.default, models.merge, models.strong in config.yaml from Claude ids "
        "into tiers"
    )
    assert _user_version(state_db) == STATE_SCHEMA_VERSION
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["models"] == {
        "default": "standard",
        "merge": "light",
        "strong": ["strong", "gpt-6.1-sol"],
    }


def test_migrate_refused_by_a_live_lease_leaves_the_config_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_9_estate(tmp_path, monkeypatch, live_lease=True)
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text(_CLAUDE_CONFIG, encoding="utf-8")

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 1
    assert out.startswith("state.db was not migrated: ONE (implementer) still hold a lease")
    assert config.read_text(encoding="utf-8") == _CLAUDE_CONFIG


def test_migrate_with_an_unreadable_config_refuses_before_changing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_9_estate(tmp_path, monkeypatch)
    state_db = tmp_path / ".taskmanager" / "state.db"
    config = tmp_path / ".taskmanager" / "config.yaml"
    config.write_text("models: [\n", encoding="utf-8")
    before = _dump(state_db)

    code, out = _tm(["db", "migrate", "-C", str(tmp_path)], monkeypatch, capsys)

    assert code == 1
    assert out == f"{config} is not valid YAML"
    assert _dump(state_db) == before
    assert _user_version(state_db) == 9
    assert not (tmp_path / ".taskmanager" / "state.db.schema9.bak").exists()


def test_config_rewrite_reads_and_writes_under_the_config_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".taskmanager").mkdir()
    (tmp_path / ".taskmanager" / "config.yaml").write_text(_CLAUDE_CONFIG, encoding="utf-8")
    store = ConfigStore(tmp_path)
    held: list[bool] = []
    seen: list[str] = []
    locked, read, write = ConfigStore._locked, ConfigStore.read, ConfigStore._write

    @contextmanager
    def spy_locked(self: ConfigStore) -> Iterator[None]:
        with locked(self):
            held.append(True)
            yield
            held.pop()

    def spy_read(self: ConfigStore) -> dict[str, Any]:
        seen.append(f"read locked={bool(held)}")
        return read(self)

    def spy_write(self: ConfigStore, flat: dict[str, Any]) -> None:
        seen.append(f"write locked={bool(held)}")
        write(self, flat)

    monkeypatch.setattr(ConfigStore, "_locked", spy_locked)
    monkeypatch.setattr(ConfigStore, "read", spy_read)
    monkeypatch.setattr(ConfigStore, "_write", spy_write)

    assert store.claude_ids_as_tiers() == ["models.default", "models.merge", "models.strong"]
    assert seen == ["read locked=True", "write locked=True"]
