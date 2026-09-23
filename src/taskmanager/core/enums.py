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
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"
    ABANDONED = "ABANDONED"
    DEFERRED = "DEFERRED"


class VirtualStatus(StrEnum):
    BLOCKED = "BLOCKED"
    # Every depends_on gate is satisfied, but a file this task declares is locked by another
    # task's active lease -- ready by the dependency graph, not claimable right now. Distinct
    # from BLOCKED so a reader isn't sent to check dependencies that are, in fact, all clear.
    BLOCKED_BY_LEASE = "BLOCKED_BY_LEASE"
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


class LockType(StrEnum):
    WRITE = "write"
    READ = "read"


class TransferMode(StrEnum):
    ALL = "all"
    NONE = "none"
    CUSTOM = "custom"


class RecommendationStrategy(StrEnum):
    BALANCED = "balanced"
    UNBLOCK_FIRST = "unblock-first"
    FINISH_PLANS = "finish-plans"
    PRIORITY_STRICT = "priority-strict"


class RenderView(StrEnum):
    SUMMARY = "summary"
    SUBAGENT = "subagent"
    FULL = "full"


class ImportFormat(StrEnum):
    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "markdown"


class LedgerCommand(StrEnum):
    INIT = "init"
    SPEC_ADD = "spec_add"
    PLAN_ADD = "plan_add"
    PLAN_REVIEW_GATE = "plan_review_gate"
    TASK_ADD = "task_add"
    TASK_SUPERSEDE = "task_supersede"
    SECTION_SET = "section_set"
    VERIFICATION_ADD = "verification_add"
    VERIFICATION_RUN = "verification_run"
    TASK_START = "task_start"
    TASK_HEARTBEAT = "task_heartbeat"
    TASK_STOP = "task_stop"
    LEASE_SWEEP = "lease_sweep"
    IMPORT = "import"
    # Free-text strings before Operations existed ("task depends", "task update"); kept
    # identical so a ledger written by an older build still reads the same command.
    TASK_DEPENDS = "task depends"
    TASK_UPDATE = "task update"
    TASK_MOVE = "task_move"
    SECTION_REMOVE = "section_remove"
    LEASE_RELEASE = "lease_release"


class SearchTargetType(StrEnum):
    TITLE = "title"
    SECTION = "section"
    OVERVIEW = "overview"


class EmbeddingProviderType(StrEnum):
    NONE = "none"
    LOCAL = "local"
    MOCK = "mock"
    OPENAI = "openai"


class SearchMode(StrEnum):
    AUTO = "auto"
    FTS = "fts"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
