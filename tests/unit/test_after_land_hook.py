from pathlib import Path

from lifecycle_estate import add, attach_landing, git, make_estate, on_branch, section, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigStore, Gate, ProjectConfig, RepoConfig

TRUE = Gate(command="true", timeout=60)


def estate(tmp_path: Path, hook: str | None) -> Claims:
    config = ProjectConfig(repos={"api": RepoConfig(gates={"main": TRUE}, after_land=hook)})
    return make_estate(tmp_path, config=config)


def reviewed_plan(claims: Claims) -> None:
    """Plan P lands its branch on main: the push to its spec's target a hook follows."""
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
    )
    add(claims, "A", parent="P", merge=Merge.PARENT, status=Status.COMPLETED)
    on_branch(claims.root / "api", "tm/P", "feature.py", "x = 1\n")


def land(claims: Claims, node_id: str) -> JobState:
    landing = attach_landing(claims)
    result = claims.start(node_id, "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    return landing.run(result.job)


def landed_on_main(claims: Claims, node_id: str) -> bool:
    subjects = git(claims.root / "api", "log", "--first-parent", "--format=%s", "origin/main")
    return f"merge({node_id}): land tm/{node_id} on main" in subjects.splitlines()


def test_after_land_on_a_container_push_runs_in_the_landing_worktree_with_placeholders_filled(
    tmp_path: Path,
) -> None:
    log = tmp_path / "hook.log"
    claims = estate(
        tmp_path, f'printf "%s\\n" {{target}} {{branch}} {{node}} {{repo}} "$PWD" > {log}'
    )
    reviewed_plan(claims)

    assert land(claims, "P") == JobState.SUCCEEDED

    target, branch, node, repo, cwd = log.read_text().splitlines()
    assert (target, branch, node, repo) == ("main", "tm/P", "P", "api")
    assert Path(cwd).parent == claims.root / claims.config.worktree_dir / "land"
    assert "api: after_land `printf" in section(claims, "P", "merge")
    assert "exited 0" in section(claims, "P", "merge")


def test_after_land_that_fails_is_recorded_and_the_landing_kept(tmp_path: Path) -> None:
    claims = estate(tmp_path, "echo no forge reachable; exit 3")
    reviewed_plan(claims)

    assert land(claims, "P") == JobState.SUCCEEDED

    merge = section(claims, "P", "merge")
    assert "exited 3" in merge
    assert "no forge reachable" in merge
    assert landed_on_main(claims, "P")
    assert stored(claims, "P").status == Status.COMPLETED


def test_after_land_not_configured_runs_nothing(tmp_path: Path) -> None:
    claims = estate(tmp_path, None)
    reviewed_plan(claims)

    assert land(claims, "P") == JobState.SUCCEEDED

    assert landed_on_main(claims, "P")
    assert "after_land" not in section(claims, "P", "merge")


def test_after_land_on_a_task_landing_on_its_spec_s_target_never_runs(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    claims = estate(tmp_path, f"touch {log}")
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(claims.root / "api", "tm/T1", "feature.py", "x = 1\n")

    assert land(claims, "T1") == JobState.SUCCEEDED

    assert landed_on_main(claims, "T1")
    assert not log.exists()
    assert "after_land" not in section(claims, "T1", "merge")


def test_config_set_after_land_stores_the_command_under_its_repository(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)

    store.set("repos.api.after_land", "<your forge's command> {branch} {target}")

    assert store.project().repos["api"].after_land == "<your forge's command> {branch} {target}"
