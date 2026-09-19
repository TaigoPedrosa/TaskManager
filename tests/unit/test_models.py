from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
    VirtualStatus,
)
from taskmanager.core.models import (
    FileLock,
    Lease,
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)


def test_enums_values() -> None:
    assert NodeKind.SPEC.value == "spec"
    assert NodeKind.PLAN.value == "plan"
    assert NodeKind.TASK.value == "task"
    assert NodeKind.REVIEW_GATE.value == "review_gate"

    assert NodeStatus.NOT_STARTED.value == "NOT_STARTED"
    assert NodeStatus.IMPLEMENTING.value == "IMPLEMENTING"
    assert NodeStatus.WAITING_REVIEW.value == "WAITING_REVIEW"
    assert NodeStatus.REVIEWING.value == "REVIEWING"
    assert NodeStatus.WAITING_FIXES.value == "WAITING_FIXES"
    assert NodeStatus.FIXING.value == "FIXING"
    assert NodeStatus.WAITING_MERGE.value == "WAITING_MERGE"
    assert NodeStatus.COMPLETED.value == "COMPLETED"
    assert NodeStatus.SUPERSEDED.value == "SUPERSEDED"
    assert NodeStatus.ABANDONED.value == "ABANDONED"
    assert NodeStatus.DEFERRED.value == "DEFERRED"

    assert VirtualStatus.BLOCKED.value == "BLOCKED"
    assert VirtualStatus.READY.value == "READY"
    assert VirtualStatus.IN_FLIGHT.value == "IN_FLIGHT"

    assert RelationType.CONTAINS.value == "contains"
    assert RelationType.DEPENDS_ON.value == "depends_on"
    assert RelationType.BLOCKS.value == "blocks"
    assert RelationType.SUPERSEDES.value == "supersedes"

    assert VerificationType.FILE_EXISTS.value == "file_exists"
    assert VerificationType.FILE_ABSENT.value == "file_absent"
    assert VerificationType.SYMBOL_SIGNATURE.value == "symbol_signature"
    assert VerificationType.AST_EXPORT.value == "ast_export"
    assert VerificationType.TEST_COMMAND.value == "test_command"
    assert VerificationType.CODEGRAPH_QUERY.value == "codegraph_query"


def test_node_instantiation_defaults() -> None:
    node = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Test Node")
    assert node.id == "AUTH-T01"
    assert node.kind == NodeKind.TASK
    assert node.title == "Test Node"
    assert node.status == NodeStatus.NOT_STARTED
    assert node.priority == 50
    assert node.target_repo is None
    assert node.acceptable_models == []
    assert node.frontmatter == {}
    assert isinstance(node.created_at, datetime)
    assert isinstance(node.updated_at, datetime)


def test_node_instantiation_explicit() -> None:
    now = datetime.now(tz=UTC)
    node = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="Implement JWT token verification",
        status=NodeStatus.IMPLEMENTING,
        priority=80,
        target_repo="auth-service",
        acceptable_models=["claude-3-7-sonnet", "gemini-3.8-flash"],
        frontmatter={"risk": "high"},
        created_at=now,
        updated_at=now,
    )
    assert node.id == "AUTH-T01"
    assert node.kind == NodeKind.TASK
    assert node.priority == 80
    assert node.status == NodeStatus.IMPLEMENTING
    assert node.target_repo == "auth-service"
    assert len(node.acceptable_models) == 2
    assert node.frontmatter == {"risk": "high"}
    assert node.created_at == now
    assert node.updated_at == now


def test_node_priority_bounds() -> None:
    Node(id="AUTH-T01", kind=NodeKind.TASK, title="Valid min", priority=1)
    Node(id="AUTH-T02", kind=NodeKind.TASK, title="Valid max", priority=100)

    with pytest.raises(ValidationError):
        Node(id="AUTH-T03", kind=NodeKind.TASK, title="Too low", priority=0)

    with pytest.raises(ValidationError):
        Node(id="AUTH-T04", kind=NodeKind.TASK, title="Too high", priority=101)


def test_node_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Node(id="AUTH-T01", kind=NodeKind.TASK, title="Extra", unknown_field="invalid")  # type: ignore[call-arg]


def test_node_section() -> None:
    section = NodeSection(
        node_id="AUTH-T01",
        section_key="steps",
        ordinal=1,
        header="### Implementation Steps",
        content="- [ ] Step 1",
    )
    assert section.node_id == "AUTH-T01"
    assert section.section_key == "steps"
    assert section.ordinal == 1
    assert section.header == "### Implementation Steps"
    assert section.content == "- [ ] Step 1"


def test_node_relation() -> None:
    rel = NodeRelation(
        source_id="AUTH-P01",
        target_id="AUTH-T01",
        relation_type=RelationType.CONTAINS,
        metadata={"strict": True},
    )
    assert rel.source_id == "AUTH-P01"
    assert rel.target_id == "AUTH-T01"
    assert rel.relation_type == RelationType.CONTAINS
    assert rel.metadata == {"strict": True}


def test_node_relation_defaults() -> None:
    rel = NodeRelation(
        source_id="AUTH-P01",
        target_id="AUTH-T01",
        relation_type=RelationType.DEPENDS_ON,
    )
    assert rel.metadata == {}


def test_node_verification() -> None:
    verification = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="src/auth/jwt.py",
        expected_pattern="def verify_jwt",
        codegraph_query_json='{"symbol": "verify_jwt"}',
    )
    assert verification.id is None
    assert verification.node_id == "AUTH-T01"
    assert verification.verification_type == VerificationType.FILE_EXISTS
    assert verification.target_path == "src/auth/jwt.py"
    assert verification.expected_pattern == "def verify_jwt"
    assert verification.codegraph_query_json == '{"symbol": "verify_jwt"}'


def test_lease() -> None:
    now = datetime.now(tz=UTC)
    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-123",
        session_id="session-456",
        account_id="acc-1",
        worktree_path="/path/to/worktree",
        branch_name="tm/AUTH-T01",
        acquired_at=now,
        last_heartbeat=now,
        ttl_seconds=300,
    )
    assert lease.task_id == "AUTH-T01"
    assert lease.agent_id == "agent-123"
    assert lease.session_id == "session-456"
    assert lease.account_id == "acc-1"
    assert lease.worktree_path == "/path/to/worktree"
    assert lease.branch_name == "tm/AUTH-T01"
    assert lease.acquired_at == now
    assert lease.last_heartbeat == now
    assert lease.ttl_seconds == 300


def test_file_lock() -> None:
    lock = FileLock(file_path="src/auth/jwt.py", task_id="AUTH-T01")
    assert lock.file_path == "src/auth/jwt.py"
    assert lock.task_id == "AUTH-T01"
    assert lock.lock_type == "write"

    read_lock = FileLock(file_path="src/auth/jwt.py", task_id="AUTH-T01", lock_type="read")
    assert read_lock.lock_type == "read"


def test_ledger_event() -> None:
    event = LedgerEvent(
        actor_id="agent-123",
        command="node.create",
        target_id="AUTH-T01",
        payload={"title": "Test"},
        diff={"status": ["NOT_STARTED", "IMPLEMENTING"]},
    )
    assert event.id is None
    assert event.actor_id == "agent-123"
    assert event.command == "node.create"
    assert event.target_id == "AUTH-T01"
    assert event.payload == {"title": "Test"}
    assert event.diff == {"status": ["NOT_STARTED", "IMPLEMENTING"]}
    assert isinstance(event.timestamp, datetime)
