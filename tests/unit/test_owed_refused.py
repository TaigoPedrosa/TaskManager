"""`owed` names work a node leaves open; tm refuses to keep it as a section and points at
registering it as its own node with `depends_on` on the node that owed it."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.operations import OperationError, Operations, owed_refusal
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.importers import BulkImporter
from taskmanager.web.app import create_app


@pytest.fixture
def ops_setup(tmp_path: Path) -> tuple[NodeRepository, Operations, str]:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    ops = Operations(
        node_repo,
        RuntimeRepository(db),
        LedgerRepository(db),
        VerificationEngine(tmp_path),
        JobRepository(db),
        actor="tester",
    )
    task_id = ops.add_task("Task", ops.add_plan("Plan", ops.add_spec("Spec", slug="S"), slug="P"))
    return node_repo, ops, task_id


@pytest.mark.parametrize("key", ["owed", "Owed", " OWED ", "  owed"])
def test_section_set_refuses_owed_case_and_space_insensitively(
    ops_setup: tuple[NodeRepository, Operations, str], key: str
) -> None:
    node_repo, ops, task_id = ops_setup
    with pytest.raises(OperationError) as exc:
        ops.set_section(task_id, key, "Rate limiting is not in this task.")
    assert str(exc.value) == owed_refusal(task_id)
    assert exc.value.status_code == 400
    assert node_repo.get_all_sections(task_id) == []


def test_section_set_of_a_plain_key_is_unaffected(
    ops_setup: tuple[NodeRepository, Operations, str],
) -> None:
    node_repo, ops, task_id = ops_setup
    ops.set_section(task_id, "context", "some context")
    assert node_repo.get_section(task_id, "context") is not None


def test_append_section_also_refuses_owed(
    ops_setup: tuple[NodeRepository, Operations, str],
) -> None:
    node_repo, ops, task_id = ops_setup
    with pytest.raises(OperationError):
        ops.append_section(task_id, "owed", "more scope cut")
    assert node_repo.get_all_sections(task_id) == []


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, NodeRepository]:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="Task", status=Status.READY))
    return TestClient(create_app(tmp_path)), node_repo


def test_put_section_owed_answers_400_and_writes_nothing(
    api: tuple[TestClient, NodeRepository],
) -> None:
    client, node_repo = api
    res = client.put("/api/nodes/T1/sections/owed", json={"content": "left for later"})
    assert res.status_code == 400
    assert res.json()["detail"] == owed_refusal("T1")
    assert node_repo.get_all_sections("T1") == []


def _doc(*, plan_owed: bool = False, task_owed: bool = False) -> dict[str, Any]:
    return {
        "spec": {"id": "S", "title": "S"},
        "plans": [
            {
                "id": "S-P",
                "title": "P",
                "sections": {"owed": "later"} if plan_owed else {},
                "tasks": [
                    {
                        "id": "S-P-a",
                        "title": "a",
                        "sections": {"owed": "later"} if task_owed else {},
                    }
                ],
            }
        ],
    }


def test_import_refuses_a_document_with_an_owed_section_before_writing_anything(
    tmp_path: Path,
) -> None:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    with pytest.raises(ValueError, match="nothing written") as exc:
        BulkImporter(node_repo).import_dict(_doc(task_owed=True))
    assert "S-P-a" in str(exc.value)
    assert node_repo.list_nodes() == []


def test_import_names_every_node_carrying_an_owed_section(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    with pytest.raises(ValueError) as exc:
        BulkImporter(node_repo).import_dict(_doc(plan_owed=True, task_owed=True))
    assert "S-P" in str(exc.value)
    assert "S-P-a" in str(exc.value)
    assert node_repo.list_nodes() == []


def test_import_without_an_owed_section_is_unaffected(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    BulkImporter(node_repo).import_dict(_doc())
    assert {n.id for n in node_repo.list_nodes()} == {"S", "S-P", "S-P-a"}
