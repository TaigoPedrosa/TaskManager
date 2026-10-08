"""Gate config set key by key, and landing and claim failures that name what to do."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from lifecycle_estate import add, attach_landing, commit, git, make_estate, make_repo, stored
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.status import Action, JobState, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine import gates
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigStore, Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing

TRUE = Gate(command="true", timeout=60)


def gate(command: str, junit: str | None = None, timeout: int = 3600) -> dict[str, Any]:
    return {"command": command, "junit": junit, "timeout": timeout}


WEB = {"web": {"default_branch": "main", "gates": {"main": gate("make web")}}}


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = CliRunner().invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.output


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init")[0] == 0
    assert tm(tmp_path, "config", "set", "repos.web.gates.main.command", "make web")[0] == 0
    return tmp_path


def stored_repos(root: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(ConfigStore(root).path.read_text())["repos"])


@pytest.mark.parametrize(
    ("key", "value", "entry"),
    [
        (
            "repos.app.gates.main.command",
            "true",
            {"default_branch": "main", "gates": {"main": gate("true")}},
        ),
        (
            "repos.app.gates.parent.command",
            "make lint",
            {"default_branch": "main", "gates": {"parent": gate("make lint")}},
        ),
        (
            "repos.app.gates.main",
            "{command: 'cd {worktree} && make', timeout: 60}",
            {"default_branch": "main", "gates": {"main": gate("cd {worktree} && make", None, 60)}},
        ),
        (
            "repos.app.gates",
            "{parent: {command: lint}}",
            {"default_branch": "main", "gates": {"parent": gate("lint")}},
        ),
        ("repos.app.default_branch", "trunk", {"default_branch": "trunk", "gates": {}}),
        ("repos.app", "{default_branch: trunk}", {"default_branch": "trunk", "gates": {}}),
    ],
)
def test_a_dotted_repos_key_merges_into_the_stored_mapping(
    root: Path, key: str, value: str, entry: dict[str, Any]
) -> None:
    code, out = tm(root, "config", "set", key, value)

    assert code == 0, out
    assert stored_repos(root) == {**WEB, "app": entry}


def test_every_gate_field_is_set_on_its_own_and_read_back(root: Path) -> None:
    for gate_name in ("main", "parent"):
        prefix = f"repos.app.gates.{gate_name}"
        for field, value in (("command", "make ci"), ("junit", "out/*.xml"), ("timeout", "90")):
            assert tm(root, "config", "set", f"{prefix}.{field}", value)[0] == 0
            assert tm(root, "config", "get", f"{prefix}.{field}") == (0, f"{value}\n")

    full = gate("make ci", "out/*.xml", 90)
    assert stored_repos(root) == {
        **WEB,
        "app": {"default_branch": "main", "gates": {"main": full, "parent": full}},
    }
    assert ConfigStore(root).project().repos["app"].gates["main"] == Gate(
        command="make ci", junit="out/*.xml", timeout=90
    )


def test_the_repository_at_the_tm_root_is_named_dot_in_a_dotted_key(root: Path) -> None:
    assert tm(root, "config", "set", "repos...gates.main.command", "true")[0] == 0

    assert stored_repos(root)["."] == {"default_branch": "main", "gates": {"main": gate("true")}}
    assert tm(root, "config", "get", "repos...gates.main.command") == (0, "true\n")
    assert tm(root, "config", "unset", "repos..")[0] == 0
    assert stored_repos(root) == WEB


def test_a_dotted_key_reads_a_repository_with_no_entry_as_the_defaults(root: Path) -> None:
    assert tm(root, "config", "get", "repos.app.default_branch") == (0, "main\n")
    assert tm(root, "config", "get", "repos.app.gates.main.command") == (0, "\n")


APP = {"app": {"default_branch": "main", "gates": {"main": gate("x")}}}
WEB_GATELESS = {"web": {"default_branch": "main", "gates": {}}}


@pytest.mark.parametrize(
    ("key", "left"),
    [
        ("repos.web.gates.main.junit", {**WEB, **APP}),
        ("repos.web.gates.main", {**WEB_GATELESS, **APP}),
        ("repos.web.gates", {**WEB_GATELESS, **APP}),
        (
            "repos.app",
            {"web": {"default_branch": "main", "gates": {"main": gate("make web", "j")}}},
        ),
    ],
)
def test_a_dotted_unset_removes_only_what_it_names(
    root: Path, key: str, left: dict[str, Any]
) -> None:
    assert tm(root, "config", "set", "repos.web.gates.main.junit", "j")[0] == 0
    assert tm(root, "config", "set", "repos.app.gates.main.command", "x")[0] == 0

    code, out = tm(root, "config", "unset", key)

    assert code == 0, out
    assert stored_repos(root) == left


def test_unsetting_the_last_repository_removes_the_repos_key(root: Path) -> None:
    assert tm(root, "config", "set", "lease_ttl", "600")[0] == 0

    assert tm(root, "config", "unset", "repos.web")[0] == 0

    assert yaml.safe_load(ConfigStore(root).path.read_text()) == {"lease_ttl": 600}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("set", "repos.app.gates.main.junit", "out.xml"), "repos.app.gates.main.command: Field"),
        (("set", "repos.web.gates.main.timeout", "soon"), "repos.web.gates.main.timeout: Input"),
        (("set", "repos.app.default_branch", "a..b"), "repos.app.default_branch: 'a..b'"),
        (("set", "repos.app.gates.main", "{command: ["), "repos.app.gates.main: not valid YAML"),
        (("set", "repos.app.gates.staging.command", "x"), "unknown key 'repos.app.gates.staging"),
        (("unset", "repos.web.gates.main.command"), "repos.web.gates.main.command: Field"),
    ],
)
def test_a_bad_dotted_write_is_one_line_naming_the_key_and_writes_nothing(
    root: Path, args: tuple[str, ...], message: str
) -> None:
    before = ConfigStore(root).path.read_text()

    code, out = tm(root, "config", *args)

    assert code == 1
    assert message in out and "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert "repos.<repo>[.default_branch|.gates[.<main|parent>" in out
    assert ConfigStore(root).path.read_text() == before


def test_a_whole_repos_value_that_drops_a_repository_is_refused_naming_it(root: Path) -> None:
    before = ConfigStore(root).path.read_text()

    code, out = tm(root, "config", "set", "repos", "{app: {gates: {main: {command: x}}}}")

    assert code == 1
    assert "would drop the settings stored for web" in out
    assert "tm config unset repos.<repo>" in out
    assert ConfigStore(root).path.read_text() == before


def test_a_whole_repos_value_that_keeps_every_repository_is_written(root: Path) -> None:
    code, out = tm(root, "config", "set", "repos", "{web: {}, app: {gates: {main: {command: x}}}}")

    assert code == 0, out
    assert set(stored_repos(root)) == {"web", "app"}
    assert stored_repos(root)["web"]["gates"] == {}


def test_a_gate_command_keeps_every_brace_that_names_no_placeholder() -> None:
    template = "test -n ${HOME} && awk '{print $1}' f && find . -exec ls {} + && cd {worktree}"

    rendered = gates.render(template, worktree="/w t", node="T1", repo="api", target="main")

    assert rendered == (
        "test -n ${HOME} && awk '{print $1}' f && find . -exec ls {} + && cd '/w t'"
    )


def landing_estate(tmp_path: Path, gate_config: Gate | None = TRUE) -> tuple[Claims, Landing]:
    repos = {"api": RepoConfig(gates={"main": gate_config})} if gate_config is not None else {}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    return claims, attach_landing(claims)


def implemented(claims: Claims, node_id: str = "T1") -> Path:
    """`node_id` claimed, built in the worktree its claim cut, and closed, with review off."""
    add(claims, node_id, review=False)
    claimed = claims.start(node_id, "builder", "s1")
    assert claimed.action == Action.IMPLEMENT and claimed.worktree is not None
    worktree = Path(claimed.worktree)
    commit(worktree, "feature.py", "x = 1\n", "feature")
    claims.complete(node_id, token=claimed.token)
    return worktree


def land(claims: Claims, landing: Landing, node_id: str = "T1") -> tuple[str, JobState]:
    merge = claims.start(node_id, "merger", "s1")
    assert merge.action == Action.MERGE and merge.job is not None, merge.reason
    return merge.job, landing.run(merge.job)


def test_a_gate_command_with_shell_braces_lands(tmp_path: Path) -> None:
    command = "test -n \"${HOME}\" && echo a | awk '{print $1}' && test -d {worktree}"
    claims, landing = landing_estate(tmp_path, Gate(command=command, timeout=60))
    implemented(claims)

    _, state = land(claims, landing)

    assert state == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED


def test_an_unexpected_error_in_a_step_stops_the_job_for_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims, landing = landing_estate(tmp_path)
    implemented(claims)

    def boom(*_args: object) -> gates.GateRun:
        raise RuntimeError("the gate runner broke")

    monkeypatch.setattr(gates, "run_gate", boom)

    job_id, state = land(claims, landing)

    job = claims.jobs.get(job_id)
    assert state == JobState.NEEDS_AGENT
    assert job is not None and job.result["reason"] == "error"
    assert job.result["error"] == "RuntimeError: the gate runner broke"


def test_a_landing_with_no_main_gate_names_the_command_that_sets_one(tmp_path: Path) -> None:
    claims, landing = landing_estate(tmp_path, gate_config=None)
    implemented(claims)

    job_id, state = land(claims, landing)

    job = claims.jobs.get(job_id)
    assert state == JobState.NEEDS_AGENT and job is not None
    assert job.result["reason"] == "no gate"
    assert 'tm config set repos.api.gates.main.command "<command>"' in job.result["detail"]
    assert f"tm job resume {job_id}" in job.result["detail"]


@pytest.mark.parametrize(
    ("setup", "branch"),
    [(("remote", "remove", "origin"), "main"), (None, "trunk")],
    ids=["no-origin", "default-branch-not-on-origin"],
)
def test_a_claim_in_a_repository_with_no_origin_branch_is_refused_naming_it(
    tmp_path: Path, setup: tuple[str, ...] | None, branch: str
) -> None:
    repos = {"api": RepoConfig(default_branch=branch)}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    add(claims, "T1")
    api = claims.root / "api"
    if setup is not None:
        git(api, *setup)

    code, out = tm(claims.root, "task", "start", "T1", "--agent", "a", "--session", "s")

    assert code == 1
    assert (
        f"api has no origin/{branch}: tm cuts branches from origin/{branch} and lands by "
        "pushing to origin"
    ) in out
    assert stored(claims, "T1").status == Status.READY
    assert claims.runtime.get_lease("T1") is None
    assert not git(api, "branch", "--list", "tm/T1")


def test_a_lease_records_the_worktree_its_claim_cut(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    code, out = tm(claims.root, "task", "start", "T1", "--agent", "a", "--session", "s")
    assert code == 0, out
    worktree = yaml.safe_load(out)["worktree"]

    code, out = tm(claims.root, "run", "list", "--yaml")
    assert code == 0, out
    assert [lease["worktree_path"] for lease in yaml.safe_load(out)["leases"]] == [worktree]
    assert worktree == str(claims.root / ".worktrees" / "api-T1")


def test_a_task_in_the_repository_at_the_tm_root_is_cut_at_its_id(tmp_path: Path) -> None:
    (tmp_path / "estate").mkdir()
    root = make_repo(tmp_path / "estate", "solo")
    tm_dir = root / ".taskmanager"
    tm_dir.mkdir()
    DatabaseManager(tm_dir).init_all()
    add(Claims.open(root, ProjectConfig()), "SOLO-A", repo=".")

    code, out = tm(root, "task", "start", "SOLO-A", "--agent", "a", "--session", "s")

    assert code == 0, out
    assert yaml.safe_load(out)["worktree"] == str(root / ".worktrees" / "SOLO-A")
    assert (root / ".worktrees" / "SOLO-A" / "README.md").exists()


def test_a_completed_landing_removes_the_worktree_and_keeps_the_branch(tmp_path: Path) -> None:
    claims, landing = landing_estate(tmp_path)
    worktree = implemented(claims)

    _, state = land(claims, landing)

    assert state == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert not worktree.exists()
    assert str(worktree) not in git(claims.root / "api", "worktree", "list")
    assert git(claims.root / "api", "branch", "--list", "tm/T1")


def reset_to_completed(claims: Claims) -> tuple[int, str]:
    git(claims.root / "api", "push", "-q", "origin", "tm/T1:main")
    return tm(claims.root, "task", "reset", "T1", "--to", "COMPLETED", "--note", "landed by hand")


def test_a_reset_to_completed_removes_the_worktree(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    worktree = implemented(claims)

    code, out = reset_to_completed(claims)

    assert code == 0, out
    assert not worktree.exists()


@pytest.mark.parametrize("through", ["landing", "reset"])
def test_a_worktree_holding_uncommitted_work_outlives_its_node_s_completion(
    tmp_path: Path, through: str
) -> None:
    claims, landing = landing_estate(tmp_path)
    worktree = implemented(claims)
    (worktree / "notes.txt").write_text("keep me\n")

    if through == "landing":
        assert land(claims, landing)[1] == JobState.SUCCEEDED
    else:
        code, out = reset_to_completed(claims)
        assert (code, "Traceback" in out) == (0, False), out

    assert stored(claims, "T1").status == Status.COMPLETED
    assert (worktree / "notes.txt").read_text() == "keep me\n"
