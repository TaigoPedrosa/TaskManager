"""A reviewed plan lands before its review. From its landing until a write moves it back before
landing, its code is on its target: its dependents stay claimable and its migration writers
leave the chain, through every review and fix status in between."""

from pathlib import Path

import pytest
from lifecycle_estate import add, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.stepgraph import migration_writers
from taskmanager.renderers.importers import BulkImporter

MIGRATION = ["api/migrations/versions/001_add.py"]


def plan_estate(tmp_path: Path) -> Claims:
    """Spec S over plan P (review on), mid-merge, whose children land on P's branch: C, and W,
    which writes a migration. Task X depends on C."""
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        parent="S",
        review=True,
        status=Status.MERGING,
        claimed_from=Status.IMPLEMENTED,
    )
    add(claims, "C", parent="P", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "W", parent="P", merge=Merge.PARENT, status=Status.COMPLETED, files=MIGRATION)
    add(claims, "X", depends=("C",))
    return claims


def landed_plan(tmp_path: Path) -> Claims:
    claims = plan_estate(tmp_path)
    assert claims.landed("P", "api: landed on main") == Status.LANDED
    return claims


def rejected(claims: Claims) -> None:
    """P's review, claimed on its landed target, rejects it."""
    claims.start("P", "reviewer", "s1")
    claims.ops.set_section("P", "review", "1. open: a finding")
    assert claims.review("P", approve=False) == Status.REVIEWED


def blocked(claims: Claims, node_id: str = "X") -> str | None:
    snap = claims.snapshots.build()
    return claims.blocked_reason(stored(claims, node_id), snap, Action.IMPLEMENT)


def chain(claims: Claims) -> list[str]:
    return [n.id for n in migration_writers(claims.snapshots.build(), "api")]


def state(claims: Claims) -> tuple[Status, str | None, list[str]]:
    return Status(stored(claims, "P").status), blocked(claims), chain(claims)


def test_the_landed_plan_stays_on_its_target_through_its_review_and_fix(tmp_path: Path) -> None:
    claims = landed_plan(tmp_path)
    seen = [state(claims)]
    claims.start("P", "reviewer", "s1")
    seen.append(state(claims))
    claims.ops.set_section("P", "review", "1. open: a finding")
    claims.review("P", approve=False)
    seen.append(state(claims))
    claims.start("P", "fixer", "s1")
    seen.append(state(claims))
    claims.complete("P")
    seen.append(state(claims))
    # W makes P sensitive, so its fix is reviewed once more before it lands.
    claims.start("P", "reviewer", "s2")
    seen.append(state(claims))

    assert seen == [
        (Status.LANDED, None, []),
        (Status.REVIEWING, None, []),
        (Status.REVIEWED, None, []),
        (Status.FIXING, None, []),
        (Status.FIXED, None, []),
        (Status.REVIEWING, None, []),
    ]


def test_a_plan_whose_landing_failed_is_off_its_target_through_its_fix_and_review(
    tmp_path: Path,
) -> None:
    claims = plan_estate(tmp_path)
    claims.landing_failed("P", "api: verifications red on main")
    seen = [state(claims)]
    claims.start("P", "fixer", "s1")
    seen.append(state(claims))
    claims.complete("P")
    seen.append(state(claims))
    claims.start("P", "reviewer", "s1")
    seen.append(state(claims))

    waits = ("waits on C", ["W"])
    assert seen == [
        (Status.REVIEWED, *waits),
        (Status.FIXING, *waits),
        (Status.FIXED, *waits),
        (Status.REVIEWING, *waits),
    ]


def test_a_child_added_under_the_landed_plan_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    rejected(claims)

    added = claims.ops.add_task(
        "C2", plan="P", slug="C2", merge="parent", frontmatter={"declared_files": MIGRATION}
    )
    claims.ops.update_node(added, repo="api")
    add(claims, "X2", depends=(added,))

    assert stored(claims, "P").status == Status.READY
    assert (blocked(claims, "X2"), blocked(claims), chain(claims)) == (
        f"waits on {added}",
        "waits on C",
        [added, "W"],
    )


def test_a_child_moved_in_under_the_landed_plan_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    add(claims, "Q", NodeKind.PLAN, parent="S")
    add(claims, "M", parent="Q", merge=Merge.PARENT)
    rejected(claims)

    claims.ops.move_task("M", "P")

    assert state(claims) == (Status.READY, "waits on C", ["W"])


def test_a_completed_child_moved_in_under_the_landed_plan_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    add(claims, "Q", NodeKind.PLAN, parent="S")
    add(claims, "M", parent="Q", merge=Merge.PARENT, status=Status.COMPLETED, files=MIGRATION)
    add(claims, "X2", depends=("M",))
    rejected(claims)

    claims.ops.move_task("C", "P")
    kept = state(claims)
    claims.ops.move_task("M", "P")

    assert kept == (Status.REVIEWED, None, ["M"])
    assert (state(claims), blocked(claims, "X2")) == (
        (Status.REVIEWED, "waits on C", ["M", "W"]),
        "waits on M",
    )


def test_a_completed_child_imported_under_the_landed_plan_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    rejected(claims)
    importer = BulkImporter(claims.nodes)

    importer.import_dict(
        {
            "plans": [{"id": "P", "title": "P", "tasks": [{"id": "C", "title": "C"}]}],
            "tasks": [{"id": "X", "title": "X", "depends_on": ["C"]}],
        }
    )
    kept = state(claims)
    importer.import_dict(
        {
            "plans": [
                {
                    "id": "P",
                    "title": "P",
                    "tasks": [
                        {
                            "id": "N",
                            "title": "N",
                            "status": "COMPLETED",
                            "merge": "parent",
                            "target_repo": "api",
                        }
                    ],
                }
            ]
        }
    )
    add(claims, "X3", depends=("N",))

    assert kept == (Status.REVIEWED, None, [])
    assert (state(claims), blocked(claims, "X3")) == (
        (Status.REVIEWED, "waits on C", ["W"]),
        "waits on N",
    )


def test_a_child_reopened_under_the_landed_plan_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = plan_estate(tmp_path)
    add(claims, "D", parent="P", merge=Merge.PARENT, status=Status.DEFERRED)
    claims.landed("P", "api: landed on main")
    rejected(claims)

    claims.reopen("D", "back in scope")

    assert state(claims) == (Status.READY, "waits on C", ["W"])


def test_reopening_the_plan_after_it_failed_past_its_landing_moves_it_back_before_landing(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    claims.nodes.save_node(
        stored(claims, "P").model_copy(update={"status": Status.FAILED, "outcome": Outcome.REJECT})
    )
    assert blocked(claims) is None

    claims.reopen("P", "land it again")

    assert state(claims) == (Status.IMPLEMENTED, "waits on C", ["W"])


@pytest.mark.parametrize(
    ("to", "outcome"),
    [
        (Status.READY, None),
        (Status.IMPLEMENTED, None),
        (Status.REVIEWED, Outcome.MERGE_FAILED),
        (Status.FIXED, Outcome.REJECT),
    ],
)
def test_a_reset_to_before_landed_moves_the_plan_back_before_landing(
    tmp_path: Path, to: Status, outcome: Outcome | None
) -> None:
    claims = landed_plan(tmp_path)

    claims.reset("P", to, "redo it", outcome)

    assert state(claims) == (to, "waits on C", ["W"])


def test_a_plan_the_rollup_lands_with_nothing_to_land_stays_on_its_target_through_its_review(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S", review=True)
    add(
        claims,
        "C",
        parent="P",
        merge=Merge.PARENT,
        status=Status.MERGING,
        claimed_from=Status.REVIEWED,
        outcome=Outcome.APPROVE,
    )
    add(claims, "X", depends=("C",))

    claims.landed("C", "api: landed on tm/P")
    assert stored(claims, "P").status == Status.LANDED
    claims.start("P", "reviewer", "s1")

    assert (stored(claims, "P").status, blocked(claims)) == (Status.REVIEWING, None)


def test_a_write_from_a_copy_read_before_the_landing_leaves_the_plan_on_its_target(
    tmp_path: Path,
) -> None:
    claims = plan_estate(tmp_path)
    before = stored(claims, "P")
    claims.landed("P", "api: landed on main")

    claims.nodes.save_node(before.model_copy(update={"title": "renamed"}), keep_cycle=True)
    claims.start("P", "reviewer", "s1")

    assert state(claims) == (Status.REVIEWING, None, [])


def test_reimporting_the_landed_plan_keeps_it_on_its_target_unless_it_states_a_status(
    tmp_path: Path,
) -> None:
    claims = landed_plan(tmp_path)
    rejected(claims)
    importer = BulkImporter(claims.nodes)

    importer.import_dict({"spec": {"id": "S", "title": "S"}, "plans": [{"id": "P", "title": "P"}]})
    kept = state(claims)
    importer.import_dict(
        {"plans": [{"id": "P", "title": "P", "status": "REVIEWED", "outcome": "reject"}]}
    )

    assert (kept, state(claims)) == (
        (Status.REVIEWED, None, []),
        (Status.REVIEWED, "waits on C", ["W"]),
    )


def test_a_plan_imported_with_its_children_keeps_the_on_target_it_states(tmp_path: Path) -> None:
    claims = plan_estate(tmp_path)

    BulkImporter(claims.nodes).import_dict(
        {
            "spec": {"id": "S", "title": "S"},
            "plans": [
                {
                    "id": "P2",
                    "title": "P2",
                    "status": "REVIEWED",
                    "outcome": "reject",
                    "on_target": True,
                    "tasks": [
                        {"id": "N", "title": "N", "status": "COMPLETED", "merge": "parent"},
                    ],
                }
            ],
        }
    )

    assert stored(claims, "P2").on_target
