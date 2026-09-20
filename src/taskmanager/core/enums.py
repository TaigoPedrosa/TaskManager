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
