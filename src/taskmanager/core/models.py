from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    title: str
    status: NodeStatus = NodeStatus.NOT_STARTED
    priority: int = Field(default=50, ge=1, le=100)
    target_repo: str | None = None
    acceptable_models: list[str] = Field(default_factory=list)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


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
    ttl_seconds: int = 300


class FileLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    task_id: str
    lock_type: str = "write"


class LedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    actor_id: str
    command: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
