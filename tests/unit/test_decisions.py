from pathlib import Path

import pytest

from taskmanager.core.status import DecisionStatus
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.decisions import DecisionData, read_decision
from taskmanager.engine.operations import Operations
from taskmanager.engine.verification import VerificationEngine


@pytest.fixture
def ops_setup(tmp_path: Path) -> tuple[NodeRepository, Operations]:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    ops = Operations(
        node_repo,
        RuntimeRepository(db),
        LedgerRepository(db),
        VerificationEngine(tmp_path),
        JobRepository(db),
        actor="owner",
    )
    return node_repo, ops


def test_withdraw_decision_records_who_and_when(
    ops_setup: tuple[NodeRepository, Operations],
) -> None:
    node_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1")
    ops.withdraw_decision(decision_id, reason="no longer relevant")
    node = node_repo.get_node(decision_id)
    assert node is not None
    data = read_decision(node)
    assert data.withdrawn_by == "owner"
    assert data.withdrawn_at is not None


def test_reopen_decision_clears_withdrawn_by_and_at(
    ops_setup: tuple[NodeRepository, Operations],
) -> None:
    node_repo, ops = ops_setup
    decision_id = ops.add_decision("Q", slug="q1")
    ops.withdraw_decision(decision_id, reason="no longer relevant")
    ops.reopen_decision(decision_id)
    node = node_repo.get_node(decision_id)
    assert node is not None
    assert node.status == DecisionStatus.OPEN
    data = read_decision(node)
    assert data.withdrawn_by is None
    assert data.withdrawn_at is None
    assert data.withdrawn_reason == ""


def test_decision_data_without_withdrawn_by_and_at_still_validates() -> None:
    data = DecisionData.model_validate({"withdrawn_reason": "moot"})
    assert data.withdrawn_by is None
    assert data.withdrawn_at is None
