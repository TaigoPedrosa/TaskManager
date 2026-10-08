"""Two specs on a scratch estate whose repository defaults to trunk, landed for real: one with
`land_on: release/x` creates that branch on origin through its reviewed plan, is reviewed there,
parks on it while it is red and is verified there by default; the other lands on trunk. Neither
touches origin/main."""

import json
import sys
import time
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.status import Action, JobState, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing

# The estate helpers sit beside the unit tests, with no __init__.py: importable only once their
# directory is on sys.path, which a run of this file alone does not otherwise do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from lifecycle_estate import (
    attach_landing,
    commit,
    git,
    junit_gate,
    make_estate,
    on_branch,
    stored,
)

RELEASE = "release/x"
# Run from the tm root, it records each ref a run hands it.
REF_LOG = "verify-ref.log"
RELEASED: dict[str, Any] = {
    "spec": {"id": "S1", "title": "Released", "frontmatter": {"land_on": RELEASE}},
    "plans": [
        {
            "id": "S1-P",
            "title": "Plan",
            "review": True,
            "fix": True,
            "tasks": [
                {
                    "id": "S1-P-A",
                    "title": "A",
                    "target_repo": "api",
                    "merge": "parent",
                    "verifications": [
                        {"type": "file_exists", "target_path": "a.py"},
                        {
                            "type": "test_command",
                            "target_path": "verify-ref",
                            "expected_pattern": f'echo "$TM_VERIFY_REF" >> {REF_LOG}',
                        },
                    ],
                },
                {"id": "S1-P-B", "title": "B", "target_repo": "api", "merge": "parent"},
            ],
        }
    ],
    "tasks": [{"id": "S1-R", "title": "R", "target_repo": "api", "review": True}],
}
DEFAULTED: dict[str, Any] = {
    "spec": {"id": "S2", "title": "Defaulted"},
    "tasks": [{"id": "S2-T", "title": "T", "target_repo": "api", "review": True}],
}

Estate = tuple[Claims, Landing]


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(cli_app, [*args, "-C", str(root)], input=stdin)
    return result.exit_code, result.output


def estate(tmp_path: Path) -> Estate:
    config = ProjectConfig(
        repos={"api": RepoConfig(default_branch="trunk", gates={"main": junit_gate(tmp_path)})},
        condition_ttl=1,
    )
    claims = make_estate(tmp_path, config=config)
    api = claims.root / "api"
    on_branch(api, "trunk", "trunk.txt", "t\n")
    git(api, "push", "-q", "origin", "trunk:trunk")
    git(api, "fetch", "-q", "origin")
    for document in (RELEASED, DEFAULTED):
        code, out = tm(claims.root, "import", stdin=json.dumps(document))
        assert code == 0, out
    return claims, attach_landing(claims)


def implement(claims: Claims, node_id: str, path: str) -> None:
    result = claims.start(node_id, "implementer", "s1")
    assert result.action == Action.IMPLEMENT, result
    assert result.worktree is not None
    commit(Path(result.worktree), path, f"{node_id}\n", f"{node_id}: {path}")
    assert claims.complete(node_id) == Status.IMPLEMENTED


def review(claims: Claims, node_id: str) -> tuple[str | None, str | None]:
    result = claims.start(node_id, "reviewer", "s1")
    assert result.action == Action.REVIEW, result
    claims.ops.set_section(node_id, "review", "Nothing to fix.")
    claims.review(node_id, approve=True)
    return result.branch, result.base


def land(estate: Estate, node_id: str) -> JobState:
    claims, landing = estate
    result = claims.start(node_id, "lander", "s1")
    assert result.action == Action.MERGE, result
    assert result.job is not None
    return landing.run(result.job)


def subjects(api: Path, ref: str) -> list[str]:
    git(api, "fetch", "-q", "origin")
    return git(api, "log", "--first-parent", "--format=%s", ref).splitlines()


def test_each_spec_lands_on_its_own_target_and_main_never_moves(tmp_path: Path) -> None:
    claims, _ = landed = estate(tmp_path)
    api = claims.root / "api"
    main = git(api, "ls-remote", "origin", "refs/heads/main")

    for task, path in (("S1-P-A", "a.py"), ("S1-P-B", "b.py")):
        implement(claims, task, path)
        assert land(landed, task) == JobState.SUCCEEDED
    assert git(api, "ls-remote", "origin", f"refs/heads/{RELEASE}") == ""
    assert land(landed, "S1-P") == JobState.SUCCEEDED
    assert stored(claims, "S1-P").status == Status.LANDED
    assert git(api, "show", f"origin/{RELEASE}:a.py") == "S1-P-A"
    assert git(api, "show", f"origin/{RELEASE}:trunk.txt") == "t"

    assert review(claims, "S1-P") == (f"origin/{RELEASE}", RELEASE)
    assert f"merge(S1-P): land tm/S1-P on {RELEASE}" in subjects(api, f"origin/{RELEASE}")
    assert stored(claims, "S1-P").status == Status.COMPLETED

    code, out = tm(claims.root, "verify", "run", "S1-P-A")
    assert code == 0, out
    assert f"git ref origin/{RELEASE} in api" in " ".join(out.split())
    assert (claims.root / REF_LOG).read_text().splitlines()[-1] == f"origin/{RELEASE}"

    red = on_branch(api, RELEASE, "failing.txt", "a\n", base=f"origin/{RELEASE}")
    git(api, "push", "-q", "origin", f"{RELEASE}:{RELEASE}")
    implement(claims, "S1-R", "r.py")
    review(claims, "S1-R")
    assert land(landed, "S1-R") == JobState.CONDITION_UNMET
    [parked] = claims.nodes.get_conditions("S1-R")
    assert f"api {RELEASE} at {red[:12]}" in parked.needs
    assert claims.start("S1-R", "lander", "s1").action == Action.BLOCKED
    on_branch(api, RELEASE, "failing.txt", None)
    git(api, "push", "-q", "origin", f"{RELEASE}:{RELEASE}")
    time.sleep(1.1)
    assert land(landed, "S1-R") == JobState.SUCCEEDED
    assert claims.nodes.get_conditions("S1-R") == []
    assert git(api, "show", f"origin/{RELEASE}:r.py") == "S1-R"

    implement(claims, "S2-T", "t.py")
    review(claims, "S2-T")
    assert land(landed, "S2-T") == JobState.SUCCEEDED
    assert subjects(api, "origin/trunk")[0] == "merge(S2-T): land tm/S2-T on trunk"
    assert git(api, "show", "origin/trunk:t.py") == "S2-T"

    assert git(api, "ls-remote", "origin", "refs/heads/main") == main
