"""`tm task complete` on an implement or fix step refuses while a guard its diff adds or changes
has no red-check record."""

from pathlib import Path

import pytest
from lifecycle_estate import add, commit, gated, make_estate, on_branch

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import CheckResult, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.operations import OperationError

GUARDED = "export function f(a, b) {\n  if (a && b) {\n    return 1;\n  }\n  return 0;\n}\n"


def implementing(tmp_path: Path, path: str, content: str) -> Claims:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    worktree = claims.start("T1", "implementer", "s1").worktree
    assert worktree is not None
    commit(Path(worktree), path, content, "f")
    return claims


def test_a_guard_with_no_record_refuses_the_complete_and_lists_each_site(tmp_path: Path) -> None:
    claims = implementing(tmp_path, "src/f.ts", GUARDED)

    with pytest.raises(OperationError) as refused:
        claims.complete("T1")

    assert refused.value.status_code == 409
    assert str(refused.value) == (
        "T1 adds or changes guards no check records: src/f.ts:2 `if (a && b) {` (2 operands); "
        "src/f.ts:3 `return 1;`. Delete each, run the suite, and record it, once per operand: "
        "`tm check add T1 --agent implementer --token <token> --deleted src/f.ts:2 --red <test>`;"
        " a line that is no guard: `tm check add T1 --agent implementer --token <token> --deleted"
        ' src/f.ts:2 --waive "<why>"`. Pass --agent and --token as separate words, never through'
        " one unsplit variable"
    )
    assert claims.nodes.get_node("T1").status == Status.IMPLEMENTING


def test_a_guard_is_refused_on_a_target_its_first_landing_has_not_created(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "T1", parent="S")
    claims.ops.update_node("S", frontmatter_set={"land_on": "feature/notify"})
    worktree = claims.start("T1", "implementer", "s1").worktree
    assert worktree is not None
    commit(Path(worktree), "src/f.ts", GUARDED, "f")

    with pytest.raises(OperationError, match=r"^T1 adds or changes guards no check records"):
        claims.complete("T1")


def test_a_guard_whose_deletions_are_recorded_red_closes(tmp_path: Path) -> None:
    claims = implementing(tmp_path, "src/f.ts", GUARDED)
    claims.add_check("T1", "src/f.ts:2 a", "test_a")
    claims.add_check("T1", "src/f.ts:3", "test_one")

    with pytest.raises(OperationError, match=r"src/f\.ts:2 `if \(a && b\) \{` \(2 operands\)\."):
        claims.complete("T1")

    claims.add_check("T1", "src/f.ts:2 b", "test_b")
    assert claims.complete("T1") == Status.IMPLEMENTED


def test_a_guard_only_in_a_test_file_does_not_count(tmp_path: Path) -> None:
    claims = implementing(tmp_path, "src/f.test.ts", GUARDED)

    assert claims.complete("T1") == Status.IMPLEMENTED


def test_a_container_fix_leaves_its_children_s_guards_to_their_own_steps(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
    )
    claims.ops.update_node("P", repo="api")
    add(claims, "T1", parent="P", status=Status.COMPLETED)
    on_branch(claims.root / "api", "tm/P", "src/f.ts", GUARDED)
    claims.start("P", "fixer", "s1")

    assert claims.complete("P") == Status.FIXED


def test_a_waived_site_closes_and_its_reason_is_recorded(tmp_path: Path) -> None:
    claims = implementing(tmp_path, "src/f.ts", "export const LEVEL = 'info';\nlevel = LEVEL;\n")
    claims.add_check("T1", "src/f.ts:2", None, waived="a module default, read once at start")

    assert claims.complete("T1") == Status.IMPLEMENTED
    (waiver,) = claims.nodes.get_checks("T1")
    assert (waiver.result, waiver.test) == (
        CheckResult.WAIVED,
        "a module default, read once at start",
    )


@pytest.mark.parametrize("why", ["", " "])
def test_a_waiver_names_why(tmp_path: Path, why: str) -> None:
    claims = implementing(tmp_path, "src/f.ts", "level = 1;\n")

    with pytest.raises(OperationError, match="--waive <why>"):
        claims.add_check("T1", "src/f.ts:1", None, waived=why)
    assert claims.nodes.get_checks("T1") == []
