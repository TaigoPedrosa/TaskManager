from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from taskmanager.core.enums import NodeStatus
from taskmanager.core.models import Node

# §3.1: a decision is stored as a node reusing NodeStatus, but presented under its own names
# everywhere a human reads it -- the CLI table, `tm decision get`, the JSON/YAML rows.
DECISION_STATUS_LABELS: dict[str, str] = {
    NodeStatus.NOT_STARTED: "Open",
    NodeStatus.COMPLETED: "Answered",
    NodeStatus.ABANDONED: "Withdrawn",
}


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    description: str = ""
    recommended: bool = False


class DecisionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str | None = None
    text: str = ""
    rationale: str = ""
    answered_by: str
    answered_at: datetime


class DecisionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[DecisionOption] = Field(default_factory=list)
    allow_custom: bool = True
    raised_by: str | None = None
    answer: DecisionAnswer | None = None
    withdrawn_reason: str = ""


def read_decision(node: Node) -> DecisionData:
    raw: Any = node.frontmatter.get("decision") or {}
    return DecisionData.model_validate(raw)


def write_decision(node: Node, data: DecisionData) -> None:
    node.frontmatter["decision"] = data.model_dump(mode="json")
