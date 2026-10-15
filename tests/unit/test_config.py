import fcntl
import json
import os
import threading
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.status import Action
from taskmanager.engine.config import (
    KEYS,
    LEASE_TTL_DEFAULTS,
    ConfigError,
    ConfigStore,
    FixRounds,
    Gate,
    ProjectConfig,
    RepoConfig,
    Resolved,
)

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
    return res.exit_code, res.output


@pytest.mark.parametrize(
    ("key", "env", "flag", "from_env", "from_file", "expected"),
    [
        ("lease_ttl", "TM_LEASE_TTL", 900, "700", "500", (900, 700, 500, LEASE_TTL_DEFAULTS)),
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
    assert tm(root, "config", "get", "condition_ttl") == (0, "300\n")
    tm(root, "config", "set", "condition_ttl", "45")
    assert tm(root, "config", "get", "condition_ttl") == (0, "45\n")


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
        ("set", "lease_ttl", "{deploy: 60}"),
        ("set", "lease_ttl", "{review: 0}"),
        ("set", "lease_ttl", "{review: ["),
        ("set", "max_fix_rounds.task", "-1"),
        ("set", "max_merge_attempts", "0"),
        ("set", "max_step_failures", "0"),
        ("set", "condition_ttl", "-5"),
        ("set", "condition_timeout", "0"),
        ("set", "red_target_decision_after", "0"),
        ("set", "repo_order", "core"),
        ("set", "repos", "{core: {gates: {staging: {command: make}}}}"),
        ("set", "repos", "{core: {gates: {main: {timeout: 60}}}}"),
        ("set", "repos", "{core: {gates: {main: {command: make, retries: 2}}}}"),
        ("set", "repos", "{core: {default_branch: 'a..b'}}"),
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


def test_the_lifecycle_settings_default_to_the_documented_values() -> None:
    config = ProjectConfig()
    assert config.max_fix_rounds == FixRounds(task=2, container=3)
    assert (config.max_merge_attempts, config.max_step_failures) == (3, 3)
    assert (config.condition_ttl, config.condition_timeout) == (300, 60)
    assert config.red_target_decision_after == 3600
    assert config.lease_ttl == {
        "implement": 10800,
        "review": 3600,
        "fix": 7200,
        "merge": 3600,
        "sync": 3600,
    }
    assert (config.repo_order, config.repos) == ([], {})


@pytest.mark.parametrize(
    ("lease_ttl", "action", "seconds"),
    [
        (None, Action.IMPLEMENT, 10800),
        (None, Action.REVIEW, 3600),
        (None, Action.FIX, 7200),
        (None, Action.MERGE, 3600),
        (None, Action.SYNC, 3600),
        (500, Action.IMPLEMENT, 500),
        (500, Action.REVIEW, 3600),
        ({"review": 1800}, Action.REVIEW, 1800),
        ({"review": 1800}, Action.IMPLEMENT, 10800),
    ],
)
def test_each_action_has_its_lease_ttl_and_a_single_number_is_the_implementers(
    lease_ttl: dict[str, int] | int | None, action: Action, seconds: int
) -> None:
    config = ProjectConfig() if lease_ttl is None else ProjectConfig(lease_ttl=lease_ttl)
    assert config.lease_ttl_for(action) == seconds


def test_a_lease_ttl_flag_beats_every_stored_value(root: Path) -> None:
    store = ConfigStore(root)
    store.set("lease_ttl", "{implement: 100, review: 200}")
    assert store.lease_ttl(Action.REVIEW) == 200
    assert store.lease_ttl(Action.REVIEW, 50) == 50


def test_whole_valued_keys_are_set_as_yaml_and_stored_nested(root: Path) -> None:
    store = ConfigStore(root)
    store.set("lease_ttl", "{review: 1800}")
    store.set("repo_order", "[core, api, web]")
    store.set("repos", "{core: {gates: {main: {command: 'make ci', junit: 'out/*.xml'}}}}")
    store.set("max_fix_rounds.container", "4")
    assert yaml.safe_load(store.path.read_text()) == {
        "lease_ttl": {"review": 1800},
        "max_fix_rounds": {"container": 4},
        "repo_order": ["core", "api", "web"],
        "repos": {"core": {"gates": {"main": {"command": "make ci", "junit": "out/*.xml"}}}},
    }
    project = store.project()
    assert project.lease_ttl_for(Action.REVIEW) == 1800
    assert project.max_fix_rounds == FixRounds(task=2, container=4)
    assert project.repo_order == ["core", "api", "web"]
    assert project.repos == {
        "core": RepoConfig(gates={"main": Gate(command="make ci", junit="out/*.xml")})
    }


def test_set_under_a_repo_stores_no_defaults_and_reads_them_back(root: Path) -> None:
    store = ConfigStore(root)
    store.set("repos.core.gates.main.command", "make ci")

    assert yaml.safe_load(store.path.read_text()) == {
        "repos": {"core": {"gates": {"main": {"command": "make ci"}}}}
    }
    assert store.resolve("repos.core.default_branch") == Resolved("main", "config")
    assert store.resolve("repos.core.gates.main.timeout") == Resolved(3600, "config")


@pytest.mark.parametrize(
    ("repos", "repo", "branch"),
    [(None, "core", "main"), ("{core: {default_branch: trunk}}", "core", "trunk")]
    + [("{core: {default_branch: trunk}}", other, "main") for other in ("web", None)],
)
def test_default_branch_reads_the_repository_s_key_else_main(
    root: Path, repos: str | None, repo: str | None, branch: str
) -> None:
    store = ConfigStore(root)
    if repos is not None:
        store.set("repos", repos)
    assert store.branches().default_branch(repo) == branch
    assert store.project().default_branch(repo) == branch


def test_a_hand_written_repos_block_reads_back_whole(root: Path) -> None:
    ConfigStore(root).path.write_text(
        "repos:\n"
        "  web:\n"
        "    gates:\n"
        "      main: {command: 'npm test', timeout: 900}\n"
        "      parent: {command: 'npm run lint'}\n",
        encoding="utf-8",
    )
    code, out = tm(root, "config", "get", "repos")
    assert code == 0 and "npm run lint" in out
    assert ConfigStore(root).project().repos["web"].gates == {
        "main": Gate(command="npm test", timeout=900),
        "parent": Gate(command="npm run lint"),
    }


@pytest.mark.parametrize("remote", ["up+stream", "a(b)", "up@stream"])
def test_a_remote_name_tm_wave_cannot_quote_is_refused(remote: str) -> None:
    with pytest.raises(ValueError, match="holds a character tm does not quote"):
        RepoConfig(remote=remote)


@pytest.mark.parametrize("remote", ["origin", "up.stream_2", "team/fork", None])
def test_a_remote_name_of_quotable_characters_or_none_is_kept(remote: str | None) -> None:
    assert RepoConfig(remote=remote).remote == remote


def test_a_reader_never_sees_the_file_empty_while_it_is_rewritten(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConfigStore(root)
    store.set("lease_ttl", "10")
    seen: list[str] = []
    replace = os.replace

    def replace_after_checking(src: Path, dst: Path) -> None:
        seen.append(store.path.read_text(encoding="utf-8"))
        replace(src, dst)

    monkeypatch.setattr("taskmanager.engine.config.os.replace", replace_after_checking)

    store.set("lease_ttl", "20")

    assert seen == ["lease_ttl: 10\n"]
    assert store.read()["lease_ttl"] == 20


def test_two_writers_at_once_both_keep_their_change(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConfigStore(root)
    first_read, second_arrived, go = threading.Event(), threading.Event(), threading.Event()
    read, flock = ConfigStore.read, fcntl.flock

    def read_pausing_the_first(self: ConfigStore) -> dict[str, object]:
        loaded = read(self)
        if threading.current_thread().name == "first":
            first_read.set()
            go.wait(timeout=30)
        elif threading.current_thread().name == "second":
            second_arrived.set()
        return loaded

    # Locked, the second announces itself before it blocks on the first's lock; unlocked, it
    # announces itself by reading the file the first has not written yet.
    def flock_announcing_the_second(fd: int, op: int) -> None:
        if threading.current_thread().name == "second" and op == fcntl.LOCK_EX:
            second_arrived.set()
        flock(fd, op)

    monkeypatch.setattr(ConfigStore, "read", read_pausing_the_first)
    monkeypatch.setattr("taskmanager.engine.config.fcntl.flock", flock_announcing_the_second)
    first = threading.Thread(target=store.set, args=("lease_ttl", "10"), name="first")
    second = threading.Thread(target=store.set, args=("worktree_dir", "wt"), name="second")
    first.start()
    assert first_read.wait(timeout=30)
    second.start()
    assert second_arrived.wait(timeout=30)
    go.set()
    first.join(timeout=30)
    second.join(timeout=30)

    monkeypatch.undo()
    assert (store.read()["lease_ttl"], store.read()["worktree_dir"]) == (10, "wt")
