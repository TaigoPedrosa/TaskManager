from taskmanager.core.status import DecisionStatus, Status

# The schema a fresh `tm init` writes. A later change bumps this and migrates by user_version.
SCHEMA_VERSION = 1

# Built from the enums so the vocabulary SQLite enforces and the one the code writes cannot drift.
_CYCLE_STATUSES = ", ".join(f"'{s.value}'" for s in Status)
_DECISION_STATUSES = ", ".join(f"'{s.value}'" for s in DecisionStatus)
NODE_STATUS_CHECK = (
    f"CHECK ((kind = 'decision' AND status IN ({_DECISION_STATUSES})) "
    f"OR (kind <> 'decision' AND status IN ({_CYCLE_STATUSES})))"
)

STATE_SCHEMA_SQL = (
    """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'READY',
    priority INTEGER NOT NULL DEFAULT 50,
    ordinal INTEGER NOT NULL DEFAULT 0,
    target_repo TEXT,
    acceptable_models TEXT NOT NULL DEFAULT '[]',
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    claimed_from TEXT CHECK (claimed_from IN ('READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED')),
    review INTEGER NOT NULL DEFAULT 1 CHECK (review IN (0, 1)),
    fix INTEGER NOT NULL DEFAULT 1 CHECK (fix IN (0, 1)),
    merge TEXT NOT NULL DEFAULT 'main' CHECK (merge IN ('parent', 'main')),
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (fix <= review),
    """
    + NODE_STATUS_CHECK
    + """
);

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
    model TEXT
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
