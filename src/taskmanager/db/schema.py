from taskmanager.core.status import DecisionStatus, Status

# audit.db and cache.db: neither has moved past its first shape yet.
SCHEMA_VERSION = 1

# state.db: bumped and migrated separately, since it changes far more often than the ledger
# or the cache.
STATE_SCHEMA_VERSION = 5

# Built from the enums so the vocabulary SQLite enforces and the one the code writes cannot drift.
_CYCLE_STATUSES = ", ".join(f"'{s.value}'" for s in Status)
_DECISION_STATUSES = ", ".join(f"'{s.value}'" for s in DecisionStatus)
NODE_STATUS_CHECK = (
    f"CHECK ((kind = 'decision' AND status IN ({_DECISION_STATUSES})) "
    f"OR (kind <> 'decision' AND status IN ({_CYCLE_STATUSES})))"
)

# `ALTER TABLE ... ADD COLUMN` writes a new column's text straight after the last column's, so
# the fresh `nodes` below carries it there too and both store the same `sqlite_master.sql`.
_ON_TARGET_COLUMN = "on_target INTEGER NOT NULL DEFAULT 0 CHECK (on_target IN (0, 1))"

_NODES_BODY_SQL = (
    """
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'READY',
    priority INTEGER NOT NULL DEFAULT 50,
    ordinal INTEGER NOT NULL DEFAULT 0,
    target_repo TEXT,
    acceptable_models TEXT NOT NULL DEFAULT '[]',
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    claimed_from TEXT CHECK (
        claimed_from IN ('READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'LANDED')
    ),
    review INTEGER NOT NULL DEFAULT 1 CHECK (review IN (0, 1)),
    fix INTEGER NOT NULL DEFAULT 1 CHECK (fix IN (0, 1)),
    merge TEXT NOT NULL DEFAULT 'spec' CHECK (merge IN ('parent', 'spec')),
    outcome TEXT CHECK (outcome IN ('approve', 'reject', 'merge_failed')),
    verdict TEXT,
    fix_for TEXT CHECK (fix_for IN ('approve', 'reject', 'merge_failed')),
    review_cycles INTEGER NOT NULL DEFAULT 0 CHECK (review_cycles >= 0),
    merge_attempts INTEGER NOT NULL DEFAULT 0 CHECK (merge_attempts >= 0),
    step_failures INTEGER NOT NULL DEFAULT 0 CHECK (step_failures >= 0),
    branch TEXT,
    requires TEXT NOT NULL DEFAULT '[]',
    land_order TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, rev INTEGER NOT NULL DEFAULT 0, """
    + _ON_TARGET_COLUMN
    + """,
    CHECK (fix <= review),
    """
    + NODE_STATUS_CHECK
    + "\n)"
)

# Quoted because `ALTER TABLE ... RENAME TO nodes` writes the new name quoted, so a fresh estate
# and one the migration below rebuilt store byte-identical `sqlite_master.sql` for `nodes`.
STATE_SCHEMA_SQL = (
    '\nCREATE TABLE IF NOT EXISTS "nodes" ('
    + _NODES_BODY_SQL
    + """;

CREATE TABLE IF NOT EXISTS node_sections (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    header TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (node_id, section_key)
);

CREATE TABLE IF NOT EXISTS node_relations (
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, relation_type)
);

CREATE TABLE IF NOT EXISTS node_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    verification_type TEXT NOT NULL,
    target_path TEXT NOT NULL,
    expected_pattern TEXT,
    codegraph_query_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_node_verifications ON node_verifications (
    node_id, verification_type, target_path, COALESCE(expected_pattern, '')
);

CREATE TABLE IF NOT EXISTS node_conditions (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    needs TEXT NOT NULL,
    command TEXT NOT NULL CHECK (trim(command) <> ''),
    stage TEXT NOT NULL DEFAULT 'claim' CHECK (stage IN ('claim', 'landing')),
    PRIMARY KEY (node_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    frontmatter_text,
    content_text,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embedding_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per embedded (node, target, section): the hash of the text its vectors were made from.
CREATE TABLE IF NOT EXISTS index_state (
    node_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    section_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (node_id, target_type, section_key)
);

-- Leases live beside the nodes so a claim checks and writes both in one transaction.
CREATE TABLE IF NOT EXISTS leases (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    account_id TEXT,
    worktree_path TEXT,
    branch_name TEXT NOT NULL,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds INTEGER DEFAULT 300 CHECK (ttl_seconds IS NULL OR ttl_seconds > 0),
    action TEXT CHECK (action IN ('implement', 'review', 'fix', 'merge', 'sync')),
    review_hash TEXT,
    model TEXT,
    token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_locks (
    file_path TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES leases(task_id) ON DELETE CASCADE,
    lock_type TEXT NOT NULL DEFAULT 'write'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('land', 'sync')),
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'running', 'needs_agent', 'succeeded', 'own_defect', 'condition_unmet', 'expired'
        )
    ),
    step TEXT,
    worktree TEXT,
    pid INTEGER,
    heartbeat TIMESTAMP NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_node ON jobs(node_id);

CREATE TABLE IF NOT EXISTS branch_locks (
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    holder TEXT NOT NULL,
    heartbeat TIMESTAMP NOT NULL,
    PRIMARY KEY (repo, branch)
);
"""
)

# One node's `rev` moves on any change to the node itself or to a section, verification,
# condition or relation naming it. Shared by the fresh schema and the migration below so the
# two write sets can never drift apart.
NODE_REV_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_nodes_rev
AFTER UPDATE ON nodes
WHEN NEW.rev = OLD.rev
BEGIN
    UPDATE nodes SET rev = OLD.rev + 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_sections_rev_ins
AFTER INSERT ON node_sections
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_sections_rev_upd
AFTER UPDATE ON node_sections
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id IN (OLD.node_id, NEW.node_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_node_sections_rev_del
AFTER DELETE ON node_sections
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = OLD.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_verifications_rev_ins
AFTER INSERT ON node_verifications
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_verifications_rev_upd
AFTER UPDATE ON node_verifications
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id IN (OLD.node_id, NEW.node_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_node_verifications_rev_del
AFTER DELETE ON node_verifications
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = OLD.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_conditions_rev_ins
AFTER INSERT ON node_conditions
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = NEW.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_conditions_rev_upd
AFTER UPDATE ON node_conditions
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id IN (OLD.node_id, NEW.node_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_node_conditions_rev_del
AFTER DELETE ON node_conditions
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id = OLD.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_node_relations_rev_ins
AFTER INSERT ON node_relations
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id IN (NEW.source_id, NEW.target_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_node_relations_rev_upd
AFTER UPDATE ON node_relations
BEGIN
    UPDATE nodes SET rev = rev + 1
    WHERE id IN (OLD.source_id, OLD.target_id, NEW.source_id, NEW.target_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_node_relations_rev_del
AFTER DELETE ON node_relations
BEGIN
    UPDATE nodes SET rev = rev + 1 WHERE id IN (OLD.source_id, OLD.target_id);
END;
"""

# Applied in order to an estate below STATE_SCHEMA_VERSION, each key the version it produces.
#
# Schemas 3 and 4 rebuild `nodes`, the way SQLite changes a CHECK. `rowid` is copied because
# `nodes_fts` rows are keyed by it. `legacy_alter_table` stops the rename from re-parsing the
# other tables' triggers, which name `nodes` while it is briefly gone. Dropping `nodes` drops
# its own trigger, which the trigger script then recreates. The connection runs this with
# foreign keys off: under them the drop would cascade into every child table.
_NODE_COLUMNS_V2 = (
    "rowid, id, kind, title, status, priority, ordinal, target_repo, acceptable_models, "
    "frontmatter_json, claimed_from, review, fix, merge, outcome, verdict, fix_for, "
    "review_cycles, merge_attempts, step_failures, branch, requires, land_order, created_at, "
    "updated_at, rev"
)


def _rebuild_nodes(body: str, select: str) -> str:
    return (
        "PRAGMA legacy_alter_table = ON;\n"
        + f"CREATE TABLE nodes_new ({body};\n"
        + f"INSERT INTO nodes_new ({_NODE_COLUMNS_V2}) SELECT {select} FROM nodes;\n"
        + "DROP TABLE nodes;\n"
        + "ALTER TABLE nodes_new RENAME TO nodes;\n"
        + "PRAGMA legacy_alter_table = OFF;\n"
        + NODE_REV_TRIGGERS_SQL
    )


# Schema 3 takes the current `Status` with the merge column it stored then, `parent` or `main`.
# Schema 4 takes the current merge column, so every row schema 3 held on `main` reads `spec`.
# Schema 5 adds `on_target`, set for a node landed or completed, or one whose review was claimed
# from LANDED. A container's review is claimed only from LANDED or FIXED and a rejected review of
# a fix fails it, so a plan or spec whose review rejected, or whose fix answers that rejection, is
# past its landing; a succeeded `land` job tells it from one a reset put there. A rollup moving
# a container back keeps its outcome and fix_for, so a merge_failed outcome or a merge claimed
# from IMPLEMENTED can come before a landing as well as after one: those start unset, and the
# node's next landing sets it.
_NODES_BODY_SQL_V4 = _NODES_BODY_SQL.replace(f" {_ON_TARGET_COLUMN},", "")
_NODES_BODY_SQL_V3 = _NODES_BODY_SQL_V4.replace(
    "DEFAULT 'spec' CHECK (merge IN ('parent', 'spec'))",
    "DEFAULT 'main' CHECK (merge IN ('parent', 'main'))",
)
STATE_MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE nodes ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;\n" + NODE_REV_TRIGGERS_SQL,
    3: _rebuild_nodes(_NODES_BODY_SQL_V3, _NODE_COLUMNS_V2),
    4: _rebuild_nodes(
        _NODES_BODY_SQL_V4,
        _NODE_COLUMNS_V2.replace(", merge, ", ", IIF(merge = 'parent', 'parent', 'spec'), "),
    ),
    5: f"ALTER TABLE nodes ADD COLUMN {_ON_TARGET_COLUMN};\n"
    + "UPDATE nodes SET on_target = 1 WHERE status IN ('LANDED', 'COMPLETED') "
    + "OR (status = 'REVIEWING' AND claimed_from = 'LANDED') "
    + "OR (kind IN ('plan', 'spec') "
    + "AND status IN ('REVIEWING', 'REVIEWED', 'FIXING', 'FIXED', 'MERGING') "
    + "AND 'reject' IN (outcome, fix_for) AND outcome IS NOT 'merge_failed' "
    + "AND claimed_from IS NOT 'IMPLEMENTED' "
    + "AND EXISTS (SELECT 1 FROM jobs WHERE jobs.node_id = nodes.id "
    + "AND jobs.kind = 'land' AND jobs.state = 'succeeded'));\n",
}

# Derived results only: dropping this database loses time, never state.
CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gate_baselines (
    repo TEXT NOT NULL,
    target_sha TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    failing_json TEXT,
    tail TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (repo, target_sha, template_hash)
);

CREATE TABLE IF NOT EXISTS condition_results (
    node_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    command_hash TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    checked_at TIMESTAMP NOT NULL,
    PRIMARY KEY (node_id, idx)
);
"""


def vec_nodes_sql(dimensions: int) -> str:
    # No primary key: a node holds one vector per title, section and chunk.
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
        node_id TEXT,
        target_type TEXT,
        section_key TEXT,
        embedding FLOAT[{dimensions}] DISTANCE_METRIC=cosine
    );
    """


LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor_id TEXT NOT NULL,
    command TEXT NOT NULL,
    target_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    diff_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ledger_target ON ledger_events(target_id);
"""
