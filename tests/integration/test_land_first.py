"""A reviewed plan driven end to end on a scratch estate with real landings: it lands before its
one review, which reads main; the fix of what that review found lands without a second; a
sensitive fix takes one review, and a rejection there fails it to its owner; and work that
depends on the plan starts as soon as it lands."""

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, DecisionStatus, JobState, Status
from taskmanager.engine.claims import ClaimResult, Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.renderers.importers import BulkImporter
from taskmanager.web.app import create_app

# The estate helpers sit beside the unit tests, with no __init__.py: importable only once their
# directory is on sys.path, which a run of this file alone does not otherwise do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from lifecycle_estate import attach_landing, commit, git, make_estate, stored

MAIN_GATE = ProjectConfig(
    repos={"api": RepoConfig(gates={"main": Gate(command="true", junit=None, timeout=60)})}
)
MIGRATION = "api/migrations/versions/0001_tenants.py"
DOCUMENT: dict[str, Any] = {
    "spec": {"id": "S", "title": "Spec"},
    "plans": [
        {
            "id": "S-P",
            "title": "Plan",
            "review": True,
            "fix": True,
            "tasks": [
                {"id": "S-P-A", "title": "A", "target_repo": "api", "merge": "parent"},
                {"id": "S-P-B", "title": "B", "target_repo": "api", "merge": "parent"},
            ],
        }
    ],
    "tasks": [
        {"id": "S-NEXT", "title": "Next", "target_repo": "api", "depends_on": ["S-P"]},
        {
            "id": "S-MIG",
            "title": "Migration",
            "target_repo": "api",
            "frontmatter": {"declared_files": [MIGRATION]},
        },
    ],
}

Estate = tuple[Claims, Landing]


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    claims = make_estate(tmp_path, config=MAIN_GATE)
    BulkImporter(claims.nodes, claims.ops).import_dict(copy.deepcopy(DOCUMENT))
    return claims, attach_landing(claims)


def implement(claims: Claims, node_id: str, path: str, content: str) -> None:
    result = claims.start(node_id, "implementer", "s1")
    assert result.action == Action.IMPLEMENT, result
    assert result.worktree is not None
    commit(Path(result.worktree), path, content, f"{node_id}: {path}")
    assert claims.complete(node_id) == Status.IMPLEMENTED


def land(estate: Estate, node_id: str) -> Status:
    claims, landing = estate
    result = claims.start(node_id, "lander", "s1")
    assert result.action == Action.MERGE, result
    assert result.job is not None
    assert landing.run(result.job) == JobState.SUCCEEDED
    return Status(stored(claims, node_id).status)


def review(
    claims: Claims, node_id: str, findings: str, *, approve: bool
) -> tuple[ClaimResult, Status]:
    result = claims.start(node_id, "reviewer", "s1")
    assert result.action == Action.REVIEW, result
    claims.ops.set_section(node_id, "review", findings)
    return result, claims.review(node_id, approve=approve)


def landed_plan(estate: Estate) -> None:
    """Both tasks implemented and landed on tm/S-P with no review of their own, then the plan
    landed on main."""
    claims, _landing = estate
    for task, path in (("S-P-A", "a.py"), ("S-P-B", "b.py")):
        assert (stored(claims, task).review, stored(claims, task).fix) == (False, False)
        implement(claims, task, path, f"{task}\n")
        assert land(estate, task) == Status.COMPLETED
    assert stored(claims, "S-P").status == Status.IMPLEMENTED
    assert land(estate, "S-P") == Status.LANDED


def next_step(claims: Claims, node_id: str) -> tuple[Action | None, str | None]:
    return claims.next_step(stored(claims, node_id), claims.snapshots.build())


def test_a_reviewed_plan_lands_first_and_its_one_review_on_main_completes_it(
    estate: Estate,
) -> None:
    claims, _landing = estate
    api = claims.root / "api"
    landed_plan(estate)
    assert git(api, "show", "origin/main:a.py") == "S-P-A"

    claim, status = review(claims, "S-P", "Nothing to fix.", approve=True)

    assert (claim.branch, claim.base, claim.repos) == ("origin/main", "main", ["api"])
    assert status == Status.COMPLETED
    assert stored(claims, "S-P").review_cycles == 1


def test_a_rejected_landed_plan_is_fixed_from_main_and_completes_on_landing_with_no_second_review(
    estate: Estate,
) -> None:
    claims, _landing = estate
    api = claims.root / "api"
    landed_plan(estate)
    assert review(claims, "S-P", "1. a.py: wrong greeting.", approve=False)[1] == Status.REVIEWED

    fix = claims.start("S-P", "fixer", "s1")
    assert fix.action == Action.FIX
    worktree = Path(fix.worktrees["api"])
    assert git(worktree, "rev-parse", "HEAD") == git(api, "rev-parse", "origin/main")
    commit(worktree, "a.py", "fixed\n", "S-P: fix a.py")
    assert claims.complete("S-P") == Status.FIXED

    assert land(estate, "S-P") == Status.COMPLETED
    assert git(api, "show", "origin/main:a.py") == "fixed"
    assert next_step(claims, "S-P") == (None, None)
    assert stored(claims, "S-P").review_cycles == 1


def sensitive_fix(claims: Claims) -> None:
    """S-MIG writes a migration: reviewed, rejected, and fixed once."""
    implement(claims, "S-MIG", MIGRATION, "create table tenants();\n")
    findings = "1. The tenants table has no key."
    assert review(claims, "S-MIG", findings, approve=False)[1] == Status.REVIEWED
    fix = claims.start("S-MIG", "fixer", "s1")
    assert fix.action == Action.FIX and fix.worktree is not None
    commit(Path(fix.worktree), MIGRATION, "create table tenants(id int);\n", "S-MIG: fix")
    assert claims.complete("S-MIG") == Status.FIXED


def test_a_sensitive_fix_takes_one_review_and_its_rejection_fails_it_to_a_decision(
    estate: Estate,
) -> None:
    claims, _landing = estate
    sensitive_fix(claims)

    claim, status = review(
        claims, "S-MIG", "1. Still open: the key is not a primary key.", approve=False
    )

    assert claim.branch == "tm/S-MIG"
    assert status == Status.FAILED
    decisions = [
        dep
        for dep in claims.nodes.get_dependencies("S-MIG")
        if stored(claims, dep).kind == NodeKind.DECISION
    ]
    assert [stored(claims, d).status for d in decisions] == [DecisionStatus.OPEN]


def test_work_depending_on_a_plan_starts_once_the_plan_has_landed_with_its_review_owed(
    estate: Estate,
) -> None:
    claims, _landing = estate
    blocked = claims.start("S-NEXT", "implementer", "s1")
    assert (blocked.action, blocked.reason) == (Action.BLOCKED, "waits on S-P")

    landed_plan(estate)
    started = claims.start("S-NEXT", "implementer", "s1")

    assert started.action == Action.IMPLEMENT
    assert started.worktree is not None
    assert (Path(started.worktree) / "a.py").read_text() == "S-P-A\n"


def test_wave_discover_and_the_waves_view_agree_on_a_landed_plan_s_review_and_a_sensitive_fix(
    estate: Estate,
) -> None:
    claims, _landing = estate
    landed_plan(estate)
    sensitive_fix(claims)
    root = claims.root

    res = CliRunner().invoke(
        cli_app,
        [
            "wave",
            "discover",
            "--session",
            "w",
            "--slots",
            "5",
            "--max-strong",
            "5",
            "-C",
            str(root),
        ],
    )
    assert res.exit_code == 0, res.output
    chosen = [(c["id"], c["action"]) for c in json.loads(res.stdout.splitlines()[0])["chosen"]]
    wave = TestClient(create_app(root)).get("/api/waves", params={"size": 5}).json()["waves"][0]
    simulated = [(e["id"], e["action"]) for e in wave["entries"]]

    assert sorted(chosen) == [("S-MIG", "review"), ("S-NEXT", "implement"), ("S-P", "review")]
    assert simulated == chosen
