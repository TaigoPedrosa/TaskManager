"""A reviewed plan lands before its review: from its first succeeded landing on, its code is on
its target through every review and fix status after it."""

from pathlib import Path

import pytest
from lifecycle_estate import add, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Job
from taskmanager.core.status import Action, JobKind, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.stepgraph import migration_writers

# The plan's stored fields at each status its post-landing review and fix pass through.
AFTER_LANDING = [
    pytest.param({"status": Status.LANDED}, id="landed"),
    pytest.param({"status": Status.REVIEWING, "claimed_from": Status.LANDED}, id="reviewing"),
    pytest.param({"status": Status.REVIEWED, "outcome": Outcome.REJECT}, id="reviewed"),
    pytest.param(
        {"status": Status.FIXING, "claimed_from": Status.REVIEWED, "fix_for": Outcome.REJECT},
        id="fixing",
    ),
    pytest.param({"status": Status.FIXED, "fix_for": Outcome.REJECT}, id="fixed"),
]
REVIEWING_AFTER_FAILED_LANDING = {
    "status": Status.REVIEWING,
    "claimed_from": Status.FIXED,
    "fix_for": Outcome.MERGE_FAILED,
}
FIXING = {"status": Status.FIXING, "claimed_from": Status.REVIEWED, "fix_for": Outcome.REJECT}
MIGRATION = ["api/migrations/versions/001_add.py"]


def plan_estate(
    tmp_path: Path,
    plan: dict[str, object],
    landings: dict[str, JobState],
    children: dict[str, tuple[str, Status]] | None = None,
) -> Claims:
    """Spec S over plan P (review on), whose children land on P's branch; task X depends on P's
    child C. P ran one land job per repository in `landings`."""
    under = children or {"C": ("api", Status.COMPLETED)}
    claims = make_estate(tmp_path, tuple(sorted({repo for repo, _ in under.values()})))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S", review=True, **plan)
    for child, (repo, status) in under.items():
        files = MIGRATION if child == "W" else None
        add(claims, child, parent="P", repo=repo, merge=Merge.PARENT, status=status, files=files)
    add(claims, "X", depends=("C",))
    for repo, state in landings.items():
        claims.jobs.create(
            Job(kind=JobKind.LAND, node_id="P", repo=repo, target="main", state=state)
        )
    return claims


def blocked(claims: Claims) -> str | None:
    return claims.blocked_reason(stored(claims, "X"), claims.snapshots.build(), Action.IMPLEMENT)


@pytest.mark.parametrize("plan", AFTER_LANDING)
def test_a_dependent_is_claimable_while_the_landed_plan_is_reviewed_and_fixed(
    tmp_path: Path, plan: dict[str, object]
) -> None:
    claims = plan_estate(tmp_path, plan, {"api": JobState.SUCCEEDED})

    assert blocked(claims) is None


def test_a_dependent_waits_while_the_plan_is_reviewed_after_a_failed_landing(
    tmp_path: Path,
) -> None:
    claims = plan_estate(tmp_path, REVIEWING_AFTER_FAILED_LANDING, {"api": JobState.OWN_DEFECT})

    assert blocked(claims) == "waits on C"


def test_a_dependent_waits_while_the_plan_has_landed_in_only_some_of_its_repositories(
    tmp_path: Path,
) -> None:
    children = {"C": ("api", Status.COMPLETED), "D": ("web", Status.COMPLETED)}
    landings = {"api": JobState.SUCCEEDED, "web": JobState.OWN_DEFECT}
    claims = plan_estate(tmp_path, FIXING, landings, children)

    assert blocked(claims) == "waits on C"


def test_a_set_aside_child_names_no_repository_the_plan_must_land_in(tmp_path: Path) -> None:
    children = {"C": ("api", Status.COMPLETED), "D": ("web", Status.DEFERRED)}
    claims = plan_estate(tmp_path, FIXING, {"api": JobState.SUCCEEDED}, children)

    assert blocked(claims) is None


@pytest.mark.parametrize("plan", AFTER_LANDING)
def test_a_migration_writer_leaves_the_chain_once_its_plan_has_landed(
    tmp_path: Path, plan: dict[str, object]
) -> None:
    children = {"C": ("api", Status.COMPLETED), "W": ("api", Status.COMPLETED)}
    claims = plan_estate(tmp_path, plan, {"api": JobState.SUCCEEDED}, children)

    assert [n.id for n in migration_writers(claims.snapshots.build(), "api")] == []


def test_a_migration_writer_holds_the_chain_while_its_plan_has_not_landed(
    tmp_path: Path,
) -> None:
    children = {"C": ("api", Status.COMPLETED), "W": ("api", Status.COMPLETED)}
    landings = {"api": JobState.OWN_DEFECT}
    claims = plan_estate(tmp_path, REVIEWING_AFTER_FAILED_LANDING, landings, children)

    assert [n.id for n in migration_writers(claims.snapshots.build(), "api")] == ["W"]


def test_a_succeeded_sync_of_the_plan_is_no_landing(tmp_path: Path) -> None:
    claims = plan_estate(tmp_path, REVIEWING_AFTER_FAILED_LANDING, {"api": JobState.OWN_DEFECT})
    claims.jobs.create(
        Job(kind=JobKind.SYNC, node_id="P", repo="api", target="tm/S", state=JobState.SUCCEEDED)
    )

    assert blocked(claims) == "waits on C"
