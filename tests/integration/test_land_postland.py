"""A reviewed plan on a scratch estate, driven end to end through `tm import`, `tm task add` and
`POST /api/tasks`: a child added under it lands on its branch with no review of its own unless it
is sensitive, and a child that would land on main unreviewed is refused with its message. A plan
with a migration under it, landed for real, has the fix of its one review re-reviewed once before
that fix lands. A landed plan with review off is never at LANDED: `tm task reset` and the web reset
refuse to put it there, and `tm task update`, the web update and `tm import` refuse to turn review off
on one that is, until it is reset to COMPLETED, or while its review after landing is in step, so a
release or a sweep of that review returns it to LANDED with its review still next."""

import copy
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.models import Node
from taskmanager.core.status import Action, JobState, Merge, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.claims import ClaimResult, Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.renderers.importers import BulkImporter
from taskmanager.web.app import create_app

# The estate helpers sit beside the unit tests, with no __init__.py: importable only once their
# directory is on sys.path, which a run of this file alone does not otherwise do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from lifecycle_estate import attach_landing, commit, make_estate, stored

SPEC = {"id": "S", "title": "Spec"}
REVIEWED_PLAN = {
    "spec": SPEC,
    "plans": [{"id": "S-P", "title": "Plan", "review": True, "fix": True}],
}
MIGRATION = "api/migrations/versions/0002_keys.py"
UNREVIEWED_ON_MAIN = (
    "S-P-c: lands on its target main with review off, so its code would land there "
    "unreviewed: S-P's review reads only what lands on its branch; set merge=parent, or turn "
    "review on"
)

Create = Callable[[Path, dict[str, Any]], tuple[int, str]]


def tm(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(cli_app, [*args, "-C", str(root)], input=stdin)
    # Rich wraps a long refusal at the terminal width.
    return result.exit_code, " ".join(result.output.split())


def by_import(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    plan = {"id": "S-P", "title": "Plan", "tasks": [{"id": "S-P-c", "title": "c", **fields}]}
    return tm(root, "import", stdin=json.dumps({"spec": SPEC, "plans": [plan]}))


def by_task_add(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    args: list[str] = []
    for key, value in fields.items():
        if key == "frontmatter":
            args += [arg for k, v in value.items() for arg in ("--set", f"{k}={json.dumps(v)}")]
        elif key == "merge":
            args += ["--merge", value]
        else:
            args.append(f"--{key}" if value else f"--no-{key}")
    return tm(root, "task", "add", "c", "--plan", "S-P", "--slug", "c", *args)


def by_api(root: Path, fields: dict[str, Any]) -> tuple[int, str]:
    response = TestClient(create_app(root)).post(
        "/api/tasks", json={"title": "c", "plan": "S-P", "slug": "c", **fields}
    )
    return response.status_code, response.json().get("detail", "")


def child(root: Path) -> Node | None:
    return NodeRepository(DatabaseManager(root / ".taskmanager")).get_node("S-P-c")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert tm(tmp_path, "init")[0] == 0
    code, output = tm(tmp_path, "import", stdin=json.dumps(REVIEWED_PLAN))
    assert code == 0, output
    return tmp_path


@pytest.mark.parametrize(("create", "created"), [(by_task_add, 0), (by_api, 201)])
@pytest.mark.parametrize(
    ("fields", "flags"),
    [
        ({}, (False, False, Merge.PARENT)),
        ({"frontmatter": {"sensitive": "migration"}}, (True, True, Merge.PARENT)),
        ({"frontmatter": {"declared_files": [MIGRATION]}}, (True, True, Merge.PARENT)),
    ],
    ids=["plain", "sensitive-key", "writes-a-migration"],
)
def test_a_child_added_under_a_reviewed_plan_takes_its_own_review_only_when_sensitive(
    root: Path,
    create: Create,
    created: int,
    fields: dict[str, Any],
    flags: tuple[bool, bool, Merge],
) -> None:
    code, output = create(root, fields)
    assert code == created, output
    node = child(root)
    assert node is not None and (node.review, node.fix, node.merge) == flags


@pytest.mark.parametrize(("create", "refused"), [(by_import, 1), (by_task_add, 1), (by_api, 400)])
@pytest.mark.parametrize(
    "fields",
    [{"merge": "spec"}, {"merge": "spec", "review": False, "fix": False}],
    ids=["review-by-default", "review-stated-off"],
)
def test_a_child_landing_on_main_unreviewed_under_a_reviewed_plan_is_refused_and_not_written(
    root: Path, create: Create, refused: int, fields: dict[str, Any]
) -> None:
    code, output = create(root, fields)
    assert code == refused, output
    assert UNREVIEWED_ON_MAIN in output
    assert child(root) is None


MAIN_GATE = ProjectConfig(
    repos={"api": RepoConfig(gates={"main": Gate(command="true", junit=None, timeout=60)})}
)
MIGRATION_PLAN: dict[str, Any] = {
    "spec": SPEC,
    "plans": [
        {
            "id": "S-P",
            "title": "Plan",
            "review": True,
            "fix": True,
            "tasks": [
                {
                    "id": "S-P-M",
                    "title": "M",
                    "target_repo": "api",
                    "frontmatter": {"declared_files": [MIGRATION]},
                }
            ],
        }
    ],
}


def start(claims: Claims, node_id: str, action: Action) -> ClaimResult:
    result = claims.start(node_id, "agent", "s1")
    assert result.action == action, result
    return result


def review(claims: Claims, node_id: str, findings: str, *, approve: bool) -> Status:
    start(claims, node_id, Action.REVIEW)
    claims.ops.set_section(node_id, "review", findings)
    return claims.review(node_id, approve=approve)


def land(claims: Claims, landing: Landing, node_id: str) -> Status:
    result = start(claims, node_id, Action.MERGE)
    assert result.job is not None
    assert landing.run(result.job) == JobState.SUCCEEDED
    return Status(stored(claims, node_id).status)


def test_a_landed_plan_with_a_migration_under_it_has_its_fix_re_reviewed_once_before_it_lands(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=MAIN_GATE)
    BulkImporter(claims.nodes, claims.ops).import_dict(copy.deepcopy(MIGRATION_PLAN))
    landing = attach_landing(claims)
    built = start(claims, "S-P-M", Action.IMPLEMENT)
    assert built.worktree is not None
    commit(Path(built.worktree), MIGRATION, "create table keys();\n", "S-P-M: keys")
    assert claims.complete("S-P-M") == Status.IMPLEMENTED
    assert review(claims, "S-P-M", "Nothing to fix.", approve=True) == Status.REVIEWED
    assert land(claims, landing, "S-P-M") == Status.COMPLETED
    assert land(claims, landing, "S-P") == Status.LANDED
    findings = "1. keys has no primary key."
    assert review(claims, "S-P", findings, approve=False) == Status.REVIEWED
    fix = start(claims, "S-P", Action.FIX)
    commit(Path(fix.worktrees["api"]), MIGRATION, "create table keys(id int primary key);\n", "fix")
    assert claims.complete("S-P") == Status.FIXED

    assert claims.next_step(stored(claims, "S-P"), claims.snapshots.build())[0] == Action.REVIEW
    rereview = start(claims, "S-P", Action.REVIEW)
    assert (rereview.branch, stored(claims, "S-P").status) == ("tm/S-P", Status.REVIEWING)
    claims.ops.set_section("S-P", "review", f"{findings}\n\nClosed: keys.id is its primary key.")
    assert claims.review("S-P", approve=True) == Status.REVIEWED
    assert land(claims, landing, "S-P") == Status.COMPLETED


REVIEW_OFF_AT_LANDED = (
    "this node has review off: nothing reviews it at LANDED; reset it to COMPLETED, or turn "
    "review on first"
)
UNREVIEWED = {"review": False, "fix": False}


def landed_plan(tmp_path: Path, *, reviewed: bool) -> tuple[Claims, Path]:
    """S-P with one unreviewed child landed on its branch, then S-P landed on main."""
    claims = make_estate(tmp_path, config=MAIN_GATE)
    plain = {"id": "S-P-A", "title": "A", "target_repo": "api", "merge": "parent", **UNREVIEWED}
    plan = {"id": "S-P", "title": "Plan", "review": reviewed, "fix": reviewed, "tasks": [plain]}
    BulkImporter(claims.nodes, claims.ops).import_dict({"spec": SPEC, "plans": [plan]})
    landing = attach_landing(claims)
    built = start(claims, "S-P-A", Action.IMPLEMENT)
    assert built.worktree is not None
    commit(Path(built.worktree), "a.py", "a\n", "S-P-A: a.py")
    assert claims.complete("S-P-A") == Status.IMPLEMENTED
    assert land(claims, landing, "S-P-A") == Status.COMPLETED
    assert land(claims, landing, "S-P") == (Status.LANDED if reviewed else Status.COMPLETED)
    return claims, claims.root


def test_a_landed_plan_with_review_off_refuses_a_reset_to_landed_on_the_cli_and_the_web(
    tmp_path: Path,
) -> None:
    claims, estate = landed_plan(tmp_path, reviewed=False)

    code, output = tm(estate, "task", "reset", "S-P", "--to", "LANDED", "--note", "repair")
    response = TestClient(create_app(estate)).post(
        "/api/nodes/S-P/reset", json={"note": "repair", "to": "LANDED"}
    )

    assert (code, REVIEW_OFF_AT_LANDED in output) == (1, True), output
    assert (response.status_code, response.json()["detail"]) == (400, REVIEW_OFF_AT_LANDED)
    assert stored(claims, "S-P").status == Status.COMPLETED


def by_update(estate: Path) -> tuple[int, str]:
    return tm(estate, "task", "update", "S-P", "--no-review", "--no-fix")


def by_patch(estate: Path) -> tuple[int, str]:
    response = TestClient(create_app(estate)).patch("/api/nodes/S-P", json=UNREVIEWED)
    return response.status_code, response.json().get("detail", "")


def by_reimport(estate: Path) -> tuple[int, str]:
    plan = {"id": "S-P", "title": "Plan", **UNREVIEWED}
    return tm(estate, "import", stdin=json.dumps({"spec": SPEC, "plans": [plan]}))


REVIEW_TURNED_OFF_AT_LANDED = (
    "S-P: review is off at LANDED, so nothing reviews or completes it; reset it to COMPLETED "
    "first, or turn review on"
)


@pytest.mark.parametrize(
    ("turn_off", "refused", "accepted"),
    [(by_update, 1, 0), (by_patch, 400, 200), (by_reimport, 1, 0)],
)
def test_turning_review_off_on_a_landed_plan_is_refused_until_it_is_reset_to_completed(
    tmp_path: Path, turn_off: Callable[[Path], tuple[int, str]], refused: int, accepted: int
) -> None:
    claims, estate = landed_plan(tmp_path, reviewed=True)

    code, output = turn_off(estate)

    assert (code, REVIEW_TURNED_OFF_AT_LANDED in output) == (refused, True), output
    landed = stored(claims, "S-P")
    assert (landed.status, landed.review, landed.fix) == (Status.LANDED, True, True)
    assert claims.next_step(landed, claims.snapshots.build())[0] == Action.REVIEW
    code, output = tm(estate, "task", "reset", "S-P", "--to", "COMPLETED", "--note", "no review")
    assert code == 0, output
    code, output = turn_off(estate)
    assert code == accepted, output
    completed = stored(claims, "S-P")
    assert (completed.status, completed.review, completed.fix) == (Status.COMPLETED, False, False)


REVIEW_TURNED_OFF_IN_ITS_REVIEW = (
    "S-P: review is off while its review after landing is in step, and a release or a sweep "
    "returns it to LANDED, where nothing reviews or completes it; let that review end, or release "
    "it and reset it to COMPLETED first"
)


@pytest.mark.parametrize(
    ("turn_off", "refused"), [(by_update, 1), (by_patch, 400), (by_reimport, 1)]
)
@pytest.mark.parametrize("swept", [False, True], ids=["released", "swept"])
def test_turning_review_off_during_a_landed_plans_review_is_refused_so_its_end_returns_it_to_review(
    tmp_path: Path, turn_off: Callable[[Path], tuple[int, str]], refused: int, swept: bool
) -> None:
    claims, estate = landed_plan(tmp_path, reviewed=True)
    start(claims, "S-P", Action.REVIEW)
    if swept:
        # A lapsed lease leaves the node in its step with nothing holding it, as a sweep finds it.
        claims.runtime.release_lease("S-P")

    code, output = turn_off(estate)

    assert (code, REVIEW_TURNED_OFF_IN_ITS_REVIEW in output) == (refused, True), output
    if swept:
        assert claims.sweep() == ["S-P"]
    else:
        claims.release("S-P")
    back = stored(claims, "S-P")
    assert (back.status, back.review, back.fix) == (Status.LANDED, True, True)
    assert claims.next_step(back, claims.snapshots.build())[0] == Action.REVIEW
