"""Import on the lifecycle model: flags, conditions and kind-aware statuses, and a refused
document writes nothing at all."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import EXPORT_FORMAT
from taskmanager.cli.main import app as cli_app
from taskmanager.core.lifecycle import next_action
from taskmanager.core.status import Action, ConditionStage, DecisionStatus, Merge, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.renderers.importers import BulkImporter


@pytest.fixture
def repo(tmp_path: Path) -> NodeRepository:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    return NodeRepository(db)


def doc(*tasks: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "spec": {"id": "S", "title": "S"},
        "plans": [{"id": "S-P", "title": "P", **(plan or {}), "tasks": list(tasks)}],
    }


CHECK = "curl -fsS https://staging.example/health"


def test_import_stores_flags_merge_requires_land_order_and_conditions(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        doc(
            {
                "id": "S-P-a",
                "title": "a",
                "merge": "parent",
                "review": True,
                "fix": False,
                "requires": ["figma"],
                "conditions": [{"needs": "staging up", "command": CHECK, "stage": "landing"}],
            },
            plan={"review": True, "fix": True, "land_order": ["api", "web"]},
        )
    )
    task = repo.get_node("S-P-a")
    plan = repo.get_node("S-P")
    assert task is not None and plan is not None
    assert (task.status, task.review, task.fix, task.merge, task.requires) == (
        Status.READY,
        True,
        False,
        Merge.PARENT,
        ["figma"],
    )
    assert (plan.review, plan.fix, plan.land_order) == (True, True, ["api", "web"])
    assert [(c.needs, c.command, c.stage) for c in repo.get_conditions("S-P-a")] == [
        ("staging up", CHECK, ConditionStage.LANDING)
    ]


REFUSED = [
    pytest.param(doc({"id": "S-P-a", "title": "a", "review": False}), "fix", id="fix-no-review"),
    pytest.param(doc({"id": "S-P-a", "title": "a", "fix": False}), None, id="unfixed-on-main"),
    pytest.param({"spec": {"id": "S", "title": "S", "merge": "parent"}}, None, id="spec-on-parent"),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "merge": "spec"}, plan={"review": True, "fix": True}),
        "S-P-a: lands on its target main with review off, so its code would land there "
        "unreviewed: S-P's review reads only what lands on its branch",
        id="unreviewed-on-main-under-a-reviewed-plan",
    ),
    pytest.param(
        doc(
            {"id": "S-P-a", "title": "a", "depends_on": ["S-P-b"]},
            {"id": "S-P-b", "title": "b", "depends_on": ["S-P-a"]},
        ),
        "S-P-a.start ← S-P-b.landed ← S-P-b.implemented ← S-P-b.start ← S-P-a.landed "
        "← S-P-a.implemented ← S-P-a.start",
        id="cycle",
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "depends_on": [{"id": "S-P", "gate": "REVIEWED"}]}),
        "gate",
        id="gated-edge",
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "status": "NOT_STARTED"}), "NOT_STARTED", id="old-status"
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "status": "IMPLEMENTING", "claimed_from": "READY"}),
        "entered only by a claim",
        id="in-step-status",
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "status": "REVIEWED"}), "outcome", id="no-outcome"
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "status": "FIXED", "outcome": "reject"}),
        "fix_for",
        id="no-fix-for",
    ),
    pytest.param(
        {"decisions": [{"id": "decision-x", "title": "Q", "status": "READY"}]},
        "READY",
        id="decision-with-a-cycle-status",
    ),
    pytest.param(
        doc(
            {
                "id": "S-P-a",
                "title": "a",
                "conditions": [{"needs": "sign-off", "command": "the design is signed off"}],
            }
        ),
        "decision",
        id="prose-condition",
    ),
]


@pytest.mark.parametrize(("document", "words"), REFUSED)
def test_a_refused_import_writes_nothing_and_says_why(
    repo: NodeRepository, document: dict[str, Any], words: str | None
) -> None:
    with pytest.raises(ValueError, match="nothing written") as exc:
        BulkImporter(repo).import_dict(document)
    if words is not None:
        assert words in str(exc.value)
    assert repo.list_nodes() == []


def _via_yaml(data: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(yaml.safe_dump(data)))


def _via_json(data: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(json.dumps(data)))


def _via_markdown(data: dict[str, Any]) -> dict[str, Any]:
    # A markdown import's document lives in the frontmatter block, parsed the same way as yaml.
    content = f"---\n{yaml.safe_dump(data)}---\n\nbody\n"
    _, frontmatter, _ = content.split("---", 2)
    return cast("dict[str, Any]", yaml.safe_load(frontmatter))


@pytest.mark.parametrize(
    "round_trip", [_via_yaml, _via_json, _via_markdown], ids=["yaml", "json", "markdown"]
)
def test_two_nodes_sharing_an_id_are_refused_naming_the_id_and_both_places(
    repo: NodeRepository, round_trip: Any
) -> None:
    document = round_trip(
        {
            "spec": {"id": "S", "title": "spec"},
            "plans": [{"id": "S", "title": "plan", "tasks": [{"id": "S-a", "title": "a"}]}],
        }
    )
    with pytest.raises(ValueError, match="nothing written") as exc:
        BulkImporter(repo).import_dict(document)
    assert "'S'" in str(exc.value)
    assert "spec" in str(exc.value)
    assert "plan S" in str(exc.value)
    assert repo.list_nodes() == []


def test_a_duplicate_id_refusal_never_opens_a_write_transaction(
    repo: NodeRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _forbidden(_self: NodeRepository) -> Any:
        raise AssertionError("a duplicate-id refusal must not open a write transaction")

    monkeypatch.setattr(NodeRepository, "transaction", _forbidden)
    with pytest.raises(ValueError, match="nothing written"):
        BulkImporter(repo).import_dict(
            {"spec": {"id": "S", "title": "spec"}, "plans": [{"id": "S", "title": "plan"}]}
        )


@pytest.mark.parametrize("plan_status", ["COMPLETED", "LANDED"])
def test_importing_a_new_child_under_a_plan_whose_code_landed_is_refused(
    repo: NodeRepository, plan_status: str
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        doc(
            {"id": "S-P-a", "title": "a", "status": "COMPLETED"},
            plan={"status": plan_status, "review": True, "fix": True},
        )
    )
    with pytest.raises(ValueError, match=f"nothing written: S-P-b: S-P is {plan_status}"):
        importer.import_dict(doc({"id": "S-P-b", "title": "b"}))
    assert repo.get_node("S-P-b") is None


def test_bringing_a_landed_plan_back_into_play_under_a_completed_spec_is_refused(
    repo: NodeRepository,
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        {
            "spec": {"id": "S", "title": "S", "status": "COMPLETED"},
            "plans": [
                {
                    "id": "S-P",
                    "title": "P",
                    "status": "LANDED",
                    "review": True,
                    "fix": True,
                    "tasks": [{"id": "S-P-a", "title": "a", "status": "COMPLETED"}],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="nothing written: S-P: S is COMPLETED"):
        importer.import_dict(doc(plan={"status": "READY"}))
    plan = repo.get_node("S-P")
    assert plan is not None and plan.status == Status.LANDED


@pytest.mark.parametrize(
    ("review", "nothing_to_land", "rolled_up"),
    [
        (False, False, Status.IMPLEMENTED),
        (False, True, Status.COMPLETED),
        (True, False, Status.IMPLEMENTED),
        (True, True, Status.LANDED),
    ],
)
def test_an_imported_plan_whose_tasks_are_all_completed_waits_to_land_only_with_code_to_land(
    repo: NodeRepository,
    monkeypatch: pytest.MonkeyPatch,
    review: bool,
    nothing_to_land: bool,
    rolled_up: Status,
) -> None:
    """A plan with nothing left to land is where its landing would leave it: one with review on
    still owes its one review of the target its children's code is on."""
    importer = BulkImporter(repo)
    monkeypatch.setattr(importer.ops, "nothing_to_land", lambda _container: nothing_to_land)
    importer.import_dict(
        doc(
            {"id": "S-P-a", "title": "a", "status": "COMPLETED"},
            plan={"review": review, "fix": review},
        )
    )
    plan = repo.get_node("S-P")
    assert plan is not None and plan.status == rolled_up


def flags(repo: NodeRepository, node_id: str) -> tuple[bool, bool]:
    node = repo.get_node(node_id)
    assert node is not None
    return node.review, node.fix


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ({}, (False, False)),
        ({"merge": "parent", "review": True}, (True, False)),
        ({"review": True, "fix": True}, (True, True)),
    ],
    ids=["defaults-off", "explicit-review-wins", "explicit-review-and-fix-win"],
)
def test_a_task_imported_under_a_reviewed_plan_takes_no_review_of_its_own_unless_it_says_so(
    repo: NodeRepository, task: dict[str, Any], expected: tuple[bool, bool]
) -> None:
    BulkImporter(repo).import_dict(
        doc({"id": "S-P-a", "title": "a", **task}, plan={"review": True, "fix": True})
    )
    assert flags(repo, "S-P-a") == expected


def test_a_task_imported_under_a_plan_without_review_keeps_its_own_review_and_fix(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(doc({"id": "S-P-a", "title": "a"}))
    assert flags(repo, "S-P-a") == (True, True)


def test_a_task_imported_straight_under_a_reviewed_spec_takes_no_review_of_its_own(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        {
            "spec": {"id": "S", "title": "S", "review": True, "fix": True},
            "tasks": [{"id": "S-a", "title": "a"}],
        }
    )
    assert flags(repo, "S-a") == (False, False)


def test_reimporting_a_task_under_a_plan_now_reviewed_keeps_the_flags_it_has(
    repo: NodeRepository,
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}))
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}, plan={"review": True, "fix": True}))
    assert flags(repo, "S-P-a") == (True, True)


@pytest.mark.parametrize(
    ("sensitive", "named"),
    [("pii", "'pii'"), (["rls", "billing"], "'billing'"), (True, "'True'")],
)
def test_an_import_naming_a_sensitive_area_outside_the_four_is_refused_with_its_name(
    repo: NodeRepository, sensitive: object, named: str
) -> None:
    document = doc({"id": "S-P-a", "title": "a", "frontmatter": {"sensitive": sensitive}})
    with pytest.raises(ValueError, match="nothing written") as exc:
        BulkImporter(repo).import_dict(document)
    assert f"S-P-a: sensitive names {named}" in str(exc.value)
    assert repo.list_nodes() == []


@pytest.mark.parametrize("sensitive", ["migration", ["tenant", "rls", "crypto", "migration"]])
def test_an_import_naming_known_sensitive_areas_is_stored(
    repo: NodeRepository, sensitive: object
) -> None:
    BulkImporter(repo).import_dict(
        doc({"id": "S-P-a", "title": "a", "frontmatter": {"sensitive": sensitive}})
    )
    node = repo.get_node("S-P-a")
    assert node is not None and node.frontmatter["sensitive"] == sensitive


FIXED = {"status": "FIXED", "outcome": "reject", "fix_for": "reject"}
MIGRATION = "core/migrations/versions/0042_add_tenant.py"


@pytest.mark.parametrize(
    ("task", "action"),
    [
        ({}, Action.MERGE),
        ({"frontmatter": {"sensitive": ["rls"]}}, Action.REVIEW),
        ({"frontmatter": {"declared_files": [MIGRATION]}}, Action.REVIEW),
        (
            {"verifications": [{"type": "file_exists", "target_path": MIGRATION}]},
            Action.REVIEW,
        ),
    ],
    ids=["plain", "sensitive-key", "declared-migration", "verified-migration"],
)
def test_an_imported_fix_is_reviewed_before_it_lands_only_when_the_node_is_sensitive(
    repo: NodeRepository, task: dict[str, Any], action: Action
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(doc({"id": "S-P-a", "title": "a", **FIXED, **task}))
    node = repo.get_node("S-P-a")
    assert node is not None
    assert next_action(importer.snapshots.cycle(node)) == action


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ({}, (False, False, Merge.PARENT)),
        ({"frontmatter": {"sensitive": "tenant"}}, (True, True, Merge.PARENT)),
        ({"frontmatter": {"declared_files": [MIGRATION]}}, (True, True, Merge.PARENT)),
        (
            {"verifications": [{"type": "file_exists", "target_path": MIGRATION}]},
            (True, True, Merge.PARENT),
        ),
        ({"frontmatter": {"sensitive": "rls"}, "fix": False}, (True, False, Merge.PARENT)),
        ({"review": True, "fix": True, "merge": "spec"}, (True, True, Merge.SPEC)),
    ],
    ids=[
        "plain",
        "sensitive-key",
        "declared-migration",
        "verified-migration",
        "explicit-fix-wins",
        "explicit-spec-with-review",
    ],
)
def test_a_child_imported_under_a_reviewed_plan_lands_on_its_branch_and_reviews_only_if_sensitive(
    repo: NodeRepository, task: dict[str, Any], expected: tuple[bool, bool, Merge]
) -> None:
    BulkImporter(repo).import_dict(
        doc({"id": "S-P-a", "title": "a", **task}, plan={"review": True, "fix": True})
    )
    node = repo.get_node("S-P-a")
    assert node is not None and (node.review, node.fix, node.merge) == expected


def test_a_plan_imported_under_a_reviewed_spec_lands_on_the_spec_s_branch(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        {
            "spec": {"id": "S", "title": "S", "review": True, "fix": True},
            "plans": [{"id": "S-P", "title": "P"}],
        }
    )
    plan = repo.get_node("S-P")
    assert plan is not None and (plan.review, plan.fix, plan.merge) == (False, False, Merge.PARENT)


def test_reimporting_a_child_already_landing_on_main_unreviewed_leaves_it_as_it_is(
    repo: NodeRepository,
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}, plan={"review": True, "fix": True}))
    stored = repo.get_node("S-P-a")
    assert stored is not None
    repo.save_node(stored.model_copy(update={"merge": Merge.SPEC}))

    importer.import_dict(doc({"id": "S-P-a", "title": "renamed"}))

    node = repo.get_node("S-P-a")
    assert node is not None and (node.title, node.merge) == ("renamed", Merge.SPEC)


def restore(tmp_path: Path, task: dict[str, Any]) -> tuple[int, str, Path]:
    """`tm restore` of an export holding one plan with `task` under it, into a new root."""
    export = tmp_path / "export"
    export.mkdir()
    (export / "_format.json").write_text(json.dumps(EXPORT_FORMAT))
    (export / "S-P.json").write_text(json.dumps(doc({"id": "S-P-a", "title": "a", **task})))
    root = tmp_path / "restored"
    root.mkdir()
    result = CliRunner().invoke(cli_app, ["restore", str(export), "-C", str(root)])
    return result.exit_code, result.output, root


def test_a_restore_naming_an_unknown_sensitive_area_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    code, output, root = restore(tmp_path, {"frontmatter": {"sensitive": "pii"}})
    assert code == 1
    assert "sensitive names 'pii'" in output
    restored = NodeRepository(DatabaseManager(root / ".taskmanager"))
    assert restored.list_nodes() == []


def test_a_restored_node_that_writes_a_migration_is_sensitive(tmp_path: Path) -> None:
    code, output, root = restore(
        tmp_path, {**FIXED, "frontmatter": {"declared_files": [MIGRATION]}}
    )
    assert code == 0, output
    db = DatabaseManager(root / ".taskmanager")
    restored = NodeRepository(db)
    node = restored.get_node("S-P-a")
    assert node is not None
    cycle = BulkImporter(restored).snapshots.cycle(node)
    assert cycle.sensitive and next_action(cycle) == Action.REVIEW


def test_a_document_that_states_conditions_replaces_the_set(repo: NodeRepository) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "conditions": [{"needs": "a", "command": "true"}]})
    )
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "conditions": [{"needs": "b", "command": "true"}]})
    )
    assert [c.needs for c in repo.get_conditions("S-P-a")] == ["b"]
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}))
    assert [c.needs for c in repo.get_conditions("S-P-a")] == ["b"]


def test_a_decision_imports_with_its_own_status_and_defaults_to_open(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        {
            "decisions": [
                {"id": "decision-x", "title": "Q", "status": "ANSWERED"},
                {"id": "decision-y", "title": "Q2"},
            ]
        }
    )
    x, y = repo.get_node("decision-x"), repo.get_node("decision-y")
    assert x is not None and y is not None
    assert (x.status, y.status) == (DecisionStatus.ANSWERED, DecisionStatus.OPEN)


def test_reimporting_a_node_in_a_step_at_a_stable_status_ends_the_step(
    repo: NodeRepository,
) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}))
    node = repo.get_node("S-P-a")
    assert node is not None
    repo.save_node(
        node.model_copy(update={"status": Status.IMPLEMENTING, "claimed_from": Status.READY})
    )
    importer.import_dict(doc({"id": "S-P-a", "title": "a", "status": "READY"}))
    node = repo.get_node("S-P-a")
    assert node is not None and (node.status, node.claimed_from) == (Status.READY, None)
