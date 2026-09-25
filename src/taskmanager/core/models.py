from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from taskmanager.core.enums import (
    LedgerCommand,
    LockType,
    NodeKind,
    RelationType,
    VerificationType,
)
from taskmanager.core.status import (
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Status,
)

_CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})

# `blocked` exits `tm task start` with nothing written (spec §5.2): a lease never holds it.
LeaseAction = Literal[Action.IMPLEMENT, Action.REVIEW, Action.FIX, Action.MERGE, Action.SYNC]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    title: str
    status: Status | DecisionStatus = Status.READY
    priority: int = Field(default=50, ge=1, le=100)
    ordinal: int = 0
    target_repo: str | None = None
    acceptable_models: list[str] = Field(default_factory=list)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    claimed_from: Status | None = None
    review: bool = True
    fix: bool = True
    merge: Merge = Merge.MAIN
    outcome: Outcome | None = None
    verdict: str | None = None
    fix_for: Outcome | None = None
    review_cycles: int = Field(default=0, ge=0)
    merge_attempts: int = Field(default=0, ge=0)
    step_failures: int = Field(default=0, ge=0)
    branch: str | None = None
    requires: list[str] = Field(default_factory=list)
    land_order: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @model_validator(mode="before")
    @classmethod
    def _containers_default_to_no_review(cls, data: Any) -> Any:
        # A plan or spec reviews and fixes only when its planner asks for it; a task does unless
        # its planner opts out.
        if isinstance(data, dict) and data.get("kind") in _CONTAINERS:
            return {"review": False, "fix": False, **data}
        return data

    @model_validator(mode="after")
    def _status_fits_kind(self) -> Self:
        is_decision = self.kind == NodeKind.DECISION
        if is_decision and "status" not in self.model_fields_set:
            self.status = DecisionStatus.OPEN
        if isinstance(self.status, DecisionStatus) != is_decision:
            raise ValueError(f"a {self.kind.value} cannot hold status {self.status.value}")
        return self


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    idx: int = 0
    needs: str
    command: str
    stage: ConditionStage = ConditionStage.CLAIM


class NodeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    section_key: str
    ordinal: int
    header: str
    content: str


class NodeRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    target_id: str
    relation_type: RelationType
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    node_id: str
    verification_type: VerificationType
    target_path: str
    expected_pattern: str | None = None
    codegraph_query_json: str | None = None


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    agent_id: str
    session_id: str
    account_id: str | None = None
    worktree_path: str | None = None
    branch_name: str
    acquired_at: datetime = Field(default_factory=datetime.now)
    last_heartbeat: datetime = Field(default_factory=datetime.now)
    ttl_seconds: int | None = 300
    action: LeaseAction | None = None
    review_hash: str | None = None
    model: str | None = None


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    kind: JobKind
    node_id: str
    repo: str
    target: str
    state: JobState = JobState.RUNNING
    step: str | None = None
    worktree: str | None = None
    pid: int | None = None
    heartbeat: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    result: dict[str, Any] = Field(default_factory=dict)


class BranchLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    branch: str
    holder: str
    heartbeat: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True)
class GateRun:
    exit_code: int
    failing: frozenset[str] | None
    tail: str


class FileLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    task_id: str
    lock_type: LockType = LockType.WRITE


class LedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    actor_id: str
    command: LedgerCommand | str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
