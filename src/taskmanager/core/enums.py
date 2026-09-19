from enum import StrEnum


class NodeKind(StrEnum):
    SPEC = "spec"
    PLAN = "plan"
    TASK = "task"
    REVIEW_GATE = "review_gate"


class NodeStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IMPLEMENTING = "IMPLEMENTING"
    WAITING_REVIEW = "WAITING_REVIEW"
    REVIEWING = "REVIEWING"
    WAITING_FIXES = "WAITING_FIXES"
    FIXING = "FIXING"
    WAITING_MERGE = "WAITING_MERGE"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"
    ABANDONED = "ABANDONED"
    DEFERRED = "DEFERRED"


class VirtualStatus(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    IN_FLIGHT = "IN_FLIGHT"


class RelationType(StrEnum):
    CONTAINS = "contains"
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    SUPERSEDES = "supersedes"


class VerificationType(StrEnum):
    FILE_EXISTS = "file_exists"
    FILE_ABSENT = "file_absent"
    SYMBOL_SIGNATURE = "symbol_signature"
    AST_EXPORT = "ast_export"
    TEST_COMMAND = "test_command"
    CODEGRAPH_QUERY = "codegraph_query"
