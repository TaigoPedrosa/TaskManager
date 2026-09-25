"""Import on the lifecycle model: flags, conditions and kind-aware statuses, and a refused
document writes nothing at all."""

from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Status
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


def test_importing_a_new_child_under_a_completed_plan_is_refused(repo: NodeRepository) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "status": "COMPLETED"}, plan={"status": "COMPLETED"})
    )
    with pytest.raises(ValueError, match="nothing written"):
        importer.import_dict(doc({"id": "S-P-b", "title": "b"}))
    assert repo.get_node("S-P-b") is None


@pytest.mark.parametrize(
    ("nothing_to_land", "rolled_up"), [(False, Status.IMPLEMENTED), (True, Status.COMPLETED)]
)
def test_an_imported_plan_whose_tasks_are_all_completed_waits_to_land_only_with_code_to_land(
    repo: NodeRepository,
    monkeypatch: pytest.MonkeyPatch,
    nothing_to_land: bool,
    rolled_up: Status,
) -> None:
    importer = BulkImporter(repo)
    monkeypatch.setattr(importer.ops, "nothing_to_land", lambda _container: nothing_to_land)
    importer.import_dict(doc({"id": "S-P-a", "title": "a", "status": "COMPLETED"}))
    plan = repo.get_node("S-P")
    assert plan is not None and plan.status == rolled_up


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
