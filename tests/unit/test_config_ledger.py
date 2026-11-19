import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.config import ConfigStore

runner = CliRunner()

RETIRED = "dispatch.tick_min and dispatch.tick_max are removed: tm monitor wakes the dispatcher"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    return tmp_path


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.output


def config_rows(root: Path) -> list[tuple[str, str | None, dict]]:
    with closing(sqlite3.connect(root / ".taskmanager" / "audit.db")) as conn:
        rows = conn.execute(
            "SELECT command, target_id, payload_json FROM ledger_events "
            "WHERE command LIKE 'config %' ORDER BY id"
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


def test_set_records_one_row_with_the_key_and_no_value(root: Path) -> None:
    assert tm(root, "config", "set", "worktree_dir", "wt-1234567")[0] == 0
    assert config_rows(root) == [("config set", "config", {"key": "worktree_dir"})]


def test_set_of_a_secret_shaped_value_leaves_it_out_of_the_whole_ledger(root: Path) -> None:
    assert tm(root, "config", "set", "name", "hunter2-secret")[0] == 0
    with closing(sqlite3.connect(root / ".taskmanager" / "audit.db")) as conn:
        dump = json.dumps(conn.execute("SELECT * FROM ledger_events").fetchall(), default=str)
    assert "hunter2-secret" not in dump


def test_unset_records_one_row_with_the_key(root: Path) -> None:
    assert tm(root, "config", "set", "name", "x")[0] == 0
    assert tm(root, "config", "unset", "name")[0] == 0
    assert config_rows(root)[1:] == [("config unset", "config", {"key": "name"})]


def test_a_refused_set_records_nothing(root: Path) -> None:
    code, _ = tm(root, "config", "set", "dispatch.wave_size", "50")
    assert code == 1
    assert tm(root, "config", "set", "no.such.key", "1")[0] == 1
    assert config_rows(root) == []


@pytest.mark.parametrize("key", ["dispatch.tick_min", "dispatch.tick_max"])
def test_a_retired_dispatch_key_is_refused_and_writes_nothing(root: Path, key: str) -> None:
    code, out = tm(root, "config", "set", key, "1")
    assert code == 1
    assert RETIRED in " ".join(out.split())
    assert not ConfigStore(root).path.exists()
    assert config_rows(root) == []


@pytest.mark.parametrize("key", ["tick_min", "tick_max"])
def test_a_config_holding_a_retired_key_loads_and_ignores_it(root: Path, key: str) -> None:
    path = ConfigStore(root).path
    path.write_text(yaml.safe_dump({"dispatch": {key: 5, "wave_size": 7}}), encoding="utf-8")
    assert tm(root, "config", "get", "dispatch.wave_size") == (0, "7\n")
    assert tm(root, "config", "list")[0] == 0
    assert f"dispatch.{key}" not in tm(root, "config", "list")[1]
    assert ConfigStore(root).project().dispatch.wave_size == 7


@pytest.mark.parametrize("key", ["tick_min", "tick_max"])
def test_unset_of_a_retired_key_succeeds_and_clears_it(root: Path, key: str) -> None:
    path = ConfigStore(root).path
    path.write_text(yaml.safe_dump({"dispatch": {key: 5, "wave_size": 7}}), encoding="utf-8")
    assert tm(root, "config", "unset", f"dispatch.{key}")[0] == 0
    assert key not in path.read_text(encoding="utf-8")
    assert tm(root, "config", "get", "dispatch.wave_size") == (0, "7\n")
