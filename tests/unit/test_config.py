import json
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.config import KEYS, ConfigError, ConfigStore, Resolved

runner = CliRunner()

ENV_VARS = (
    "TM_WORKTREES",
    "TM_LEASE_TTL",
    "TASKMANAGER_OPENAI_BASE_URL",
    "TASKMANAGER_OPENAI_MODEL",
    "TASKMANAGER_OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    return tmp_path


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.stdout


@pytest.mark.parametrize(
    ("key", "env", "flag", "from_env", "from_file", "expected"),
    [
        ("lease_ttl", "TM_LEASE_TTL", 900, "700", "500", (900, 700, 500, 300)),
        (
            "worktree_dir",
            "TM_WORKTREES",
            "/flag",
            "/env",
            "in-file",
            ("/flag", "/env", "in-file", ".worktrees"),
        ),
    ],
)
def test_a_flag_beats_the_environment_beats_the_file_beats_the_default(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    env: str,
    flag: object,
    from_env: str,
    from_file: str,
    expected: tuple[object, object, object, object],
) -> None:
    store = ConfigStore(root)
    flagged, enved, filed, default = expected
    assert store.resolve(key) == Resolved(default, "default")

    store.set(key, from_file)
    assert store.resolve(key) == Resolved(filed, "config")

    monkeypatch.setenv(env, from_env)
    assert store.resolve(key) == Resolved(enved, "env")

    assert store.resolve(key, flag) == Resolved(flagged, "flag")

    monkeypatch.delenv(env)
    store.unset(key)
    assert store.resolve(key) == Resolved(default, "default")
    assert not store.path.exists()


def test_config_list_says_where_every_value_came_from(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm(root, "config", "set", "lease_ttl", "600")
    monkeypatch.setenv("TM_WORKTREES", "/somewhere")
    code, out = tm(root, "config", "list", "--json")
    assert code == 0
    rows = {r["key"]: r for r in json.loads(out)}
    assert list(rows) == list(KEYS)
    assert rows["lease_ttl"] == {"key": "lease_ttl", "value": 600, "source": "config"}
    assert rows["worktree_dir"] == {"key": "worktree_dir", "value": "/somewhere", "source": "env"}
    assert rows["embeddings.provider"] == {
        "key": "embeddings.provider",
        "value": "none",
        "source": "default",
    }
    assert "lease_ttl = 600  (config)" in tm(root, "config", "list")[1]
    assert "source: config" in tm(root, "config", "list", "--yaml")[1]


def test_get_prints_the_effective_value(root: Path) -> None:
    assert tm(root, "config", "get", "lease_ttl") == (0, "300\n")
    tm(root, "config", "set", "lease_ttl", "45")
    assert tm(root, "config", "get", "lease_ttl") == (0, "45\n")


@pytest.mark.parametrize(
    "args",
    [
        ("set", "nope", "1"),
        ("get", "nope"),
        ("unset", "nope"),
        ("set", "lease_ttl", "soon"),
        ("set", "lease_ttl", "0"),
        ("set", "embeddings.provider", "gemini"),
        ("set", "embeddings.dimensions", "-3"),
        ("set", "embeddings.api_key_env", "sk-proj-abc123"),
    ],
)
def test_an_unknown_key_or_a_bad_value_is_one_line_and_exit_1(
    root: Path, args: tuple[str, ...]
) -> None:
    code, out = tm(root, "config", *args)
    assert code == 1
    assert "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert "valid keys: embeddings.provider" in out
    assert not ConfigStore(root).path.exists()


def test_the_key_itself_can_never_be_written_to_the_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-live-0123456789"
    monkeypatch.setenv("MY_EMBED_KEY", secret)
    assert tm(root, "config", "set", "embeddings.provider", "openai")[0] == 0
    assert tm(root, "config", "set", "embeddings.api_key_env", "MY_EMBED_KEY")[0] == 0
    code, out = tm(root, "config", "set", "embeddings.api_key_env", secret)
    assert code == 1 and secret not in out
    text = ConfigStore(root).path.read_text()
    assert secret not in text
    assert yaml.safe_load(text) == {
        "embeddings": {"api_key_env": "MY_EMBED_KEY", "provider": "openai"}
    }


def test_a_hand_edited_file_with_a_bad_value_is_a_one_line_error(root: Path) -> None:
    ConfigStore(root).path.write_text("lease_ttl: soon\n")
    code, out = tm(root, "config", "list")
    assert code == 1 and "Traceback" not in out and "lease_ttl" in out


def test_a_bad_environment_value_names_the_variable(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TM_LEASE_TTL", "abc")
    code, out = tm(root, "config", "get", "lease_ttl")
    assert code == 1 and "TM_LEASE_TTL" in out and "Traceback" not in out


def test_unset_of_the_last_key_removes_the_file(root: Path) -> None:
    tm(root, "config", "set", "lease_ttl", "10")
    assert ConfigStore(root).path.exists()
    assert tm(root, "config", "unset", "lease_ttl")[0] == 0
    assert not ConfigStore(root).path.exists()
    with pytest.raises(ConfigError):
        ConfigStore(root).unset("nope")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "ci@example.com")
    _git(path, "config", "user.name", "CI")
    _git(path, "commit", "--allow-empty", "-m", "init")


def test_run_start_takes_its_worktree_directory_and_ttl_through_the_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "estate"
    _repo(root)
    _repo(root / "web")
    _git(root / "web", "update-ref", "refs/remotes/origin/main", "HEAD")
    assert tm(root, "init")[0] == 0
    nodes = NodeRepository(DatabaseManager(root / ".taskmanager"))
    for n in range(1, 6):
        nodes.save_node(Node(id=f"T-{n}", kind=NodeKind.TASK, title="t", target_repo="web"))

    def start(task: str, *extra: str) -> tuple[Path, int]:
        code, out = tm(root, "run", "start", task, "--worktree", *extra)
        assert code == 0, out
        lease = next(
            lease
            for lease in json.loads(tm(root, "run", "list", "--json")[1])["leases"]
            if lease["task_id"] == task
        )
        return Path(lease["worktree_path"]).parent, lease["ttl_seconds"]

    assert start("T-1") == (root / ".worktrees", 300)

    tm(root, "config", "set", "worktree_dir", "in-file")
    tm(root, "config", "set", "lease_ttl", "500")
    assert start("T-2") == (root / "in-file", 500)

    monkeypatch.setenv("TM_WORKTREES", str(tmp_path / "from-env"))
    monkeypatch.setenv("TM_LEASE_TTL", "700")
    assert start("T-3") == (tmp_path / "from-env", 700)

    flagged = tmp_path / "from-flag"
    assert start("T-4", "--worktree-dir", str(flagged), "--ttl", "900") == (flagged, 900)

    monkeypatch.delenv("TM_WORKTREES")
    monkeypatch.delenv("TM_LEASE_TTL")
    tm(root, "config", "unset", "worktree_dir")
    tm(root, "config", "unset", "lease_ttl")
    assert start("T-5") == (root / ".worktrees", 300)
