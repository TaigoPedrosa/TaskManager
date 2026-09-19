# TaskManager CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready, open-source local agentic task tracker CLI (`taskmanager` / `tm`) in Python 3.14 with SQLite WAL multi-database storage, `sqlite-vec` vector indexing, git worktree orchestration, static AST verification, DAG recommendation heuristics, and dynamic markdown projections.

**Architecture:** A domain-driven, dependency-injected Python package using `dishka` and `typer`. Three separated SQLite databases (`spec.db`, `runtime.db`, `ledger.db`) under `.taskmanager/` decouple structural definitions, high-frequency runtime leases/locks, and append-only action auditing. A unified node model with relational edges and structured sections supports composable qualified slug addressing (`AUTH-USER-LOGIN:steps`) and dynamic LLM projections without writing markdown files to disk.

**Tech Stack:** Python 3.14, uv, Typer, Dishka, SQLite3, sqlite-vec, Pydantic v2, Pytest, Ruff, Mypy.

**Spec:** [docs/superpowers/specs/2026-09-19-taskmanager-design.md](file:///Users/taigo.pedrosa/Documents/TaskManager/docs/superpowers/specs/2026-09-19-taskmanager-design.md)

## Global Constraints

* SQLite WAL mode with `busy_timeout=5000` is used for all database connections.
* No AI attribution trailers in git commits (`Co-Authored-By: Claude...` is strictly banned).
* Comment why, never what. Never narrate history, plans, PRs, or tasks in code comments.
* Environment variables must be prefixed with `TASKMANAGER_` (e.g. `TASKMANAGER_OPENAI_BASE_URL`).
* Code must pass `ruff check`, `ruff format --check`, and `mypy --strict src/taskmanager`.

---

### Task 1: Package Scaffolding & Pyproject Setup

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/taskmanager/__init__.py`
- Test: `tests/unit/test_package.py`

**Interfaces:**
- Produces: `taskmanager.__version__: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_package.py
import taskmanager


def test_version_defined():
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_package.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager'`

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[project]
name = "taskmanager"
version = "0.1.0"
description = "Local agentic task tracker system powered by SQLite and DAG heuristics"
authors = [{ name = "Taigo Pedrosa" }]
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
    "typer>=0.15.0",
    "pydantic>=2.10.0",
    "dishka>=1.4.0",
    "sqlite-vec>=0.1.6",
    "rich>=13.9.0",
    "httpx>=0.28.0",
]

[project.scripts]
taskmanager = "taskmanager.cli.main:app"
tm = "taskmanager.cli.main:app"

[project.optional-dependencies]
local-embeddings = [
    "sentence-transformers>=3.4.0",
]
dev = [
    "pytest>=8.3.0",
    "pytest-cov>=6.0.0",
    "mypy>=1.15.0",
    "ruff>=0.9.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.mypy]
python_version = "3.14"
strict = true
packages = ["taskmanager"]
```

```python
# src/taskmanager/__init__.py
__version__ = "0.1.0"
```

```gitignore
# .gitignore
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
build/
*.egg-info/
.taskmanager/worktrees/
.taskmanager/runtime.db*
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv sync && uv run pytest tests/unit/test_package.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore README.md src/taskmanager/__init__.py tests/unit/test_package.py
git commit -m "chore: scaffold taskmanager python package"
```

---

### Task 2: Core Domain Entities, Enums & Value Objects

**Files:**
- Create: `src/taskmanager/core/enums.py`
- Create: `src/taskmanager/core/models.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Produces:
  - `NodeKind(StrEnum)`: `SPEC`, `PLAN`, `TASK`, `REVIEW_GATE`
  - `NodeStatus(StrEnum)`: `NOT_STARTED`, `IMPLEMENTING`, `WAITING_REVIEW`, `REVIEWING`, `WAITING_FIXES`, `FIXING`, `WAITING_MERGE`, `COMPLETED`, `SUPERSEDED`, `ABANDONED`, `DEFERRED`
  - `VirtualStatus(StrEnum)`: `BLOCKED`, `READY`, `IN_FLIGHT`
  - `RelationType(StrEnum)`: `CONTAINS`, `DEPENDS_ON`, `BLOCKS`, `SUPERSEDES`
  - `VerificationType(StrEnum)`: `FILE_EXISTS`, `FILE_ABSENT`, `SYMBOL_SIGNATURE`, `AST_EXPORT`, `TEST_COMMAND`, `CODEGRAPH_QUERY`
  - `Node(BaseModel)`: Entity definition with Pydantic v2 validation
  - `NodeSection(BaseModel)`: Content chunk model
  - `NodeRelation(BaseModel)`: Directed graph edge model
  - `NodeVerification(BaseModel)`: Invariant assertion model

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_models.py
import pytest
from pydantic import ValidationError
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Node, NodeRelation, NodeSection


def test_node_instantiation():
    node = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="Implement JWT token verification",
        status=NodeStatus.NOT_STARTED,
        priority=80,
        acceptable_models=["claude-3-7-sonnet", "gemini-3.8-flash"],
    )
    assert node.id == "AUTH-T01"
    assert node.priority == 80
    assert len(node.acceptable_models) == 2


def test_node_priority_bounds():
    with pytest.raises(ValidationError):
        Node(
            id="AUTH-T01",
            kind=NodeKind.TASK,
            title="Bad priority",
            status=NodeStatus.NOT_STARTED,
            priority=150,
        )


def test_node_section():
    section = NodeSection(
        node_id="AUTH-T01",
        section_key="steps",
        ordinal=1,
        header="### Implementation Steps",
        content="- [ ] Step 1",
    )
    assert section.section_key == "steps"


def test_node_relation():
    rel = NodeRelation(
        source_id="AUTH-P01",
        target_id="AUTH-T01",
        relation_type=RelationType.CONTAINS,
    )
    assert rel.relation_type == RelationType.CONTAINS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_models.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.core'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/core/enums.py
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
```

```python
# src/taskmanager/core/models.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_models.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/core/ tests/unit/test_models.py
git commit -m "feat(core): domain entities, enums and pydantic models"
```

---

### Task 3: Database Manager & Schema Migrations (Multi-DB + sqlite-vec)

**Files:**
- Create: `src/taskmanager/db/connection.py`
- Create: `src/taskmanager/db/schema.py`
- Test: `tests/unit/test_db_schema.py`

**Interfaces:**
- Produces:
  - `DatabaseManager`: Class managing SQLite connections for `spec.db`, `runtime.db`, `ledger.db` with WAL mode and `sqlite-vec` extension loading.
  - `DatabaseManager.init_all(directory: Path, vector_dimensions: int = 384)`: Initializes schemas and virtual tables.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_db_schema.py
from pathlib import Path
import sqlite3
from taskmanager.db.connection import DatabaseManager


def test_database_initialization(tmp_path: Path):
    db_mgr = DatabaseManager(tmp_path)
    db_mgr.init_all(vector_dimensions=384)

    spec_path = tmp_path / "spec.db"
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"

    assert spec_path.exists()
    assert runtime_path.exists()
    assert ledger_path.exists()

    with db_mgr.get_spec_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "nodes" in tables
        assert "node_sections" in tables
        assert "node_relations" in tables
        assert "node_verifications" in tables
        assert "nodes_fts" in tables
        assert "vec_nodes" in tables

    with db_mgr.get_runtime_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "leases" in tables
        assert "file_locks" in tables

    with db_mgr.get_ledger_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "ledger_events" in tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_db_schema.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.db'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/db/schema.py
SPEC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    target_repo TEXT,
    acceptable_models TEXT NOT NULL DEFAULT '[]',
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
"""

RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leases (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    account_id TEXT,
    worktree_path TEXT,
    branch_name TEXT NOT NULL,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds INTEGER NOT NULL DEFAULT 300
);

CREATE TABLE IF NOT EXISTS file_locks (
    file_path TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES leases(task_id) ON DELETE CASCADE,
    lock_type TEXT NOT NULL DEFAULT 'write'
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
```

```python
# src/taskmanager/db/connection.py
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Generator
import sqlite_vec
from taskmanager.db.schema import LEDGER_SCHEMA_SQL, RUNTIME_SCHEMA_SQL, SPEC_SCHEMA_SQL


class DatabaseManager:
    def __init__(self, taskmanager_dir: Path):
        self.dir = taskmanager_dir
        self.spec_db = self.dir / "spec.db"
        self.runtime_db = self.dir / "runtime.db"
        self.ledger_db = self.dir / "ledger.db"

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        self.dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        if load_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        return conn

    @contextmanager
    def get_spec_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._create_connection(self.spec_db, load_vec=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_runtime_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._create_connection(self.runtime_db, load_vec=False)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._create_connection(self.ledger_db, load_vec=False)
        try:
            yield conn
        finally:
            conn.close()

    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_spec_connection() as conn:
            conn.executescript(SPEC_SCHEMA_SQL)
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
                    node_id TEXT PRIMARY KEY,
                    target_type TEXT,
                    section_key TEXT,
                    embedding FLOAT[{vector_dimensions}] DISTANCE_METRIC=cosine
                );
                """
            )
            conn.commit()

        with self.get_runtime_connection() as conn:
            conn.executescript(RUNTIME_SCHEMA_SQL)
            conn.commit()

        with self.get_ledger_connection() as conn:
            conn.executescript(LEDGER_SCHEMA_SQL)
            conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_db_schema.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/db/ tests/unit/test_db_schema.py
git commit -m "feat(db): multi-database connections, schemas and sqlite-vec loading"
```

---

### Task 4: Hierarchical Composable Slug & Section Path Resolver

**Files:**
- Create: `src/taskmanager/core/naming.py`
- Test: `tests/unit/test_naming.py`

**Interfaces:**
- Produces:
  - `QualifiedPath`: Dataclass parsing `<spec>-<plan>-<task>[:<section>]` into `node_id`, `parent_id`, `section_key`.
  - `NamingConfig`: Pydantic model for `.taskmanager/config.json` naming rules.
  - `SlugGenerator`: Generates next composable ID from parent ID and optional slug or autoincrement counter.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_naming.py
import pytest
from taskmanager.core.enums import NodeKind
from taskmanager.core.naming import NamingConfig, QualifiedPath, SlugGenerator


def test_parse_qualified_path():
    p1 = QualifiedPath.parse("AUTH-USER-LOGIN:steps")
    assert p1.node_id == "AUTH-USER-LOGIN"
    assert p1.section_key == "steps"

    p2 = QualifiedPath.parse("AUTH-USER:overview")
    assert p2.node_id == "AUTH-USER"
    assert p2.section_key == "overview"

    p3 = QualifiedPath.parse("AUTH")
    assert p3.node_id == "AUTH"
    assert p3.section_key is None


def test_slug_generator_with_slug():
    config = NamingConfig()
    gen = SlugGenerator(config)

    # Spec
    assert gen.generate_spec_id(slug="AUTH") == "AUTH"
    # Plan under spec
    assert gen.generate_plan_id(parent_spec_id="AUTH", slug="USER") == "AUTH-USER"
    # Task under plan
    assert gen.generate_task_id(parent_plan_id="AUTH-USER", slug="LOGIN") == "AUTH-USER-LOGIN"


def test_slug_generator_autoincrement():
    config = NamingConfig()
    gen = SlugGenerator(config)

    assert gen.generate_spec_id(counter=1) == "S1"
    assert gen.generate_plan_id(parent_spec_id="AUTH", counter=1) == "AUTH-P1"
    assert gen.generate_task_id(parent_plan_id="AUTH-USER", counter=2) == "AUTH-USER-T2"


def test_require_slug_enforcement():
    config = NamingConfig(task={"require_slug": True})
    gen = SlugGenerator(config)

    with pytest.raises(ValueError, match="Slug is required"):
        gen.generate_task_id(parent_plan_id="AUTH-USER", slug=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_naming.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.core.naming'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/core/naming.py
from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class QualifiedPath:
    node_id: str
    section_key: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "QualifiedPath":
        cleaned = raw.strip()
        if ":" in cleaned:
            parts = cleaned.split(":", 1)
            return cls(node_id=parts[0].strip(), section_key=parts[1].strip() or None)
        return cls(node_id=cleaned, section_key=None)


class LevelNamingRule(BaseModel):
    prefix: str = "T"
    require_slug: bool = False
    pad: int = 1


class NamingConfig(BaseModel):
    separator: str = "-"
    spec: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="S"))
    plan: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="P"))
    task: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="T"))

    def __init__(self, **data: Any):
        super().__init__(**data)


class SlugGenerator:
    def __init__(self, config: NamingConfig | None = None):
        self.config = config or NamingConfig()

    def generate_spec_id(self, slug: str | None = None, counter: int = 1) -> str:
        if slug:
            return slug.strip()
        if self.config.spec.require_slug:
            raise ValueError("Slug is required for spec")
        idx_str = str(counter).zfill(self.config.spec.pad)
        return f"{self.config.spec.prefix}{idx_str}"

    def generate_plan_id(
        self, parent_spec_id: str, slug: str | None = None, counter: int = 1
    ) -> str:
        sep = self.config.separator
        if slug:
            return f"{parent_spec_id}{sep}{slug.strip()}"
        if self.config.plan.require_slug:
            raise ValueError("Slug is required for plan")
        idx_str = str(counter).zfill(self.config.plan.pad)
        return f"{parent_spec_id}{sep}{self.config.plan.prefix}{idx_str}"

    def generate_task_id(
        self, parent_plan_id: str, slug: str | None = None, counter: int = 1
    ) -> str:
        sep = self.config.separator
        if slug:
            return f"{parent_plan_id}{sep}{slug.strip()}"
        if self.config.task.require_slug:
            raise ValueError("Slug is required for task")
        idx_str = str(counter).zfill(self.config.task.pad)
        return f"{parent_plan_id}{sep}{self.config.task.prefix}{idx_str}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_naming.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/core/naming.py tests/unit/test_naming.py
git commit -m "feat(core): hierarchical qualified slug and section path resolver"
```

---

### Task 5: Repositories Layer (Nodes, Sections, Relations, Runtime, Ledger)

**Files:**
- Create: `src/taskmanager/db/node_repo.py`
- Create: `src/taskmanager/db/runtime_repo.py`
- Create: `src/taskmanager/db/ledger_repo.py`
- Test: `tests/unit/test_repos.py`

**Interfaces:**
- Produces:
  - `NodeRepository`: CRUD methods for `Node`, `NodeSection`, `NodeRelation`, `NodeVerification`
  - `RuntimeRepository`: Acquisition, release, heartbeat, and listing of `Lease` and `FileLock`
  - `LedgerRepository`: Append events and query audit trail

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repos.py
from datetime import timedelta
from pathlib import Path
import time
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import FileLock, Lease, Node, NodeRelation, NodeSection
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository


def test_node_repo_crud(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    # Add Node
    node = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Test Task", status=NodeStatus.NOT_STARTED)
    repo.save_node(node)
    loaded = repo.get_node("AUTH-T01")
    assert loaded is not None
    assert loaded.title == "Test Task"

    # Add Section
    sec = NodeSection(
        node_id="AUTH-T01", section_key="steps", ordinal=1, header="## Steps", content="- [ ] Step 1"
    )
    repo.save_section(sec)
    loaded_sec = repo.get_section("AUTH-T01", "steps")
    assert loaded_sec is not None
    assert loaded_sec.content == "- [ ] Step 1"

    # Add Relation
    rel = NodeRelation(
        source_id="AUTH-P01", target_id="AUTH-T01", relation_type=RelationType.CONTAINS
    )
    repo.add_relation(rel)
    children = repo.get_children("AUTH-P01")
    assert "AUTH-T01" in children


def test_runtime_repo_leases_and_locks(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = RuntimeRepository(db)

    lease = Lease(
        task_id="AUTH-T01",
        agent_id="agent-1",
        session_id="sess-1",
        branch_name="tm/AUTH-T01",
        ttl_seconds=1,
    )
    lock = FileLock(file_path="src/auth/jwt.py", task_id="AUTH-T01")
    repo.acquire_lease(lease, [lock])

    assert repo.get_lease("AUTH-T01") is not None
    assert repo.is_file_locked("src/auth/jwt.py")

    # TTL expiration sweep
    time.sleep(1.1)
    expired = repo.sweep_expired_leases()
    assert "AUTH-T01" in expired
    assert not repo.is_file_locked("src/auth/jwt.py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repos.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.db.node_repo'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/db/node_repo.py
import json
import sqlite3
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import Node, NodeRelation, NodeSection, NodeVerification
from taskmanager.db.connection import DatabaseManager


class NodeRepository:
    def __init__(self, db_mgr: DatabaseManager):
        self.db = db_mgr

    def save_node(self, node: Node) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO nodes (id, kind, title, status, priority, target_repo, acceptable_models, frontmatter_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    priority=excluded.priority,
                    target_repo=excluded.target_repo,
                    acceptable_models=excluded.acceptable_models,
                    frontmatter_json=excluded.frontmatter_json,
                    updated_at=CURRENT_TIMESTAMP;
                """,
                (
                    node.id,
                    node.kind.value,
                    node.title,
                    node.status.value,
                    node.priority,
                    node.target_repo,
                    json.dumps(node.acceptable_models),
                    json.dumps(node.frontmatter),
                ),
            )
            # Sync FTS
            conn.execute(
                """
                INSERT INTO nodes_fts(rowid, node_id, title, frontmatter_text, content_text)
                VALUES ((SELECT rowid FROM nodes WHERE id = ?), ?, ?, ?, '')
                ON CONFLICT(rowid) DO UPDATE SET
                    title=excluded.title,
                    frontmatter_text=excluded.frontmatter_text;
                """,
                (node.id, node.id, node.title, json.dumps(node.frontmatter)),
            )
            conn.commit()

    def get_node(self, node_id: str) -> Node | None:
        with self.db.get_spec_connection() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if not row:
                return None
            return Node(
                id=row[0],
                kind=NodeKind(row[1]),
                title=row[2],
                status=NodeStatus(row[3]),
                priority=row[4],
                target_repo=row[5],
                acceptable_models=json.loads(row[6]),
                frontmatter=json.loads(row[7]),
            )

    def list_nodes(
        self, kind: NodeKind | None = None, status: NodeStatus | None = None
    ) -> list[Node]:
        query = "SELECT * FROM nodes WHERE 1=1"
        params: list[str] = []
        if kind:
            query += " AND kind = ?"
            params.append(kind.value)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                Node(
                    id=r[0],
                    kind=NodeKind(r[1]),
                    title=r[2],
                    status=NodeStatus(r[3]),
                    priority=r[4],
                    target_repo=r[5],
                    acceptable_models=json.loads(r[6]),
                    frontmatter=json.loads(r[7]),
                )
                for r in rows
            ]

    def save_section(self, section: NodeSection) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO node_sections (node_id, section_key, ordinal, header, content)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id, section_key) DO UPDATE SET
                    ordinal=excluded.ordinal,
                    header=excluded.header,
                    content=excluded.content;
                """,
                (
                    section.node_id,
                    section.section_key,
                    section.ordinal,
                    section.header,
                    section.content,
                ),
            )
            # Update content text in FTS
            conn.execute(
                """
                UPDATE nodes_fts SET content_text = (
                    SELECT GROUP_CONCAT(content, ' ') FROM node_sections WHERE node_id = ?
                ) WHERE node_id = ?;
                """,
                (section.node_id, section.node_id),
            )
            conn.commit()

    def get_section(self, node_id: str, section_key: str) -> NodeSection | None:
        with self.db.get_spec_connection() as conn:
            row = conn.execute(
                "SELECT * FROM node_sections WHERE node_id = ? AND section_key = ?",
                (node_id, section_key),
            ).fetchone()
            if not row:
                return None
            return NodeSection(
                node_id=row[0],
                section_key=row[1],
                ordinal=row[2],
                header=row[3],
                content=row[4],
            )

    def get_all_sections(self, node_id: str) -> list[NodeSection]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC",
                (node_id,),
            ).fetchall()
            return [
                NodeSection(
                    node_id=r[0], section_key=r[1], ordinal=r[2], header=r[3], content=r[4]
                )
                for r in rows
            ]

    def add_relation(self, relation: NodeRelation) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO node_relations (source_id, target_id, relation_type, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, relation_type) DO UPDATE SET
                    metadata_json=excluded.metadata_json;
                """,
                (
                    relation.source_id,
                    relation.target_id,
                    relation.relation_type.value,
                    json.dumps(relation.metadata),
                ),
            )
            conn.commit()

    def get_children(self, parent_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT target_id FROM node_relations WHERE source_id = ? AND relation_type = 'contains'",
                (parent_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def get_dependencies(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT target_id FROM node_relations WHERE source_id = ? AND relation_type = 'depends_on'",
                (node_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def get_blocked_by(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT source_id FROM node_relations WHERE target_id = ? AND relation_type = 'depends_on'",
                (node_id,),
            ).fetchall()
            return [r[0] for r in rows]

    def transfer_blocks(
        self, old_id: str, new_id: str, transfer_mode: str, custom_ids: list[str] | None = None
    ) -> None:
        with self.db.get_spec_connection() as conn:
            if transfer_mode == "all":
                conn.execute(
                    "UPDATE node_relations SET target_id = ? WHERE target_id = ? AND relation_type = 'depends_on'",
                    (new_id, old_id),
                )
            elif transfer_mode == "none":
                pass
            elif transfer_mode == "custom" and custom_ids:
                placeholders = ",".join("?" for _ in custom_ids)
                conn.execute(
                    f"UPDATE node_relations SET target_id = ? WHERE target_id = ? AND relation_type = 'depends_on' AND source_id IN ({placeholders})",
                    (new_id, old_id, *custom_ids),
                )
            conn.commit()

    def add_verification(self, ver: NodeVerification) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO node_verifications (node_id, verification_type, target_path, expected_pattern, codegraph_query_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ver.node_id,
                    ver.verification_type.value,
                    ver.target_path,
                    ver.expected_pattern,
                    ver.codegraph_query_json,
                ),
            )
            conn.commit()

    def get_verifications(self, node_id: str) -> list[NodeVerification]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT id, node_id, verification_type, target_path, expected_pattern, codegraph_query_json FROM node_verifications WHERE node_id = ?",
                (node_id,),
            ).fetchall()
            return [
                NodeVerification(
                    id=r[0],
                    node_id=r[1],
                    verification_type=VerificationType(r[2]),
                    target_path=r[3],
                    expected_pattern=r[4],
                    codegraph_query_json=r[5],
                )
                for r in rows
            ]
```

```python
# src/taskmanager/db/runtime_repo.py
from datetime import datetime, timedelta
import sqlite3
from taskmanager.core.models import FileLock, Lease
from taskmanager.db.connection import DatabaseManager


class RuntimeRepository:
    def __init__(self, db_mgr: DatabaseManager):
        self.db = db_mgr

    def acquire_lease(self, lease: Lease, locks: list[FileLock]) -> None:
        with self.db.get_runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO leases (task_id, agent_id, session_id, account_id, worktree_path, branch_name, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    account_id=excluded.account_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    last_heartbeat=CURRENT_TIMESTAMP,
                    ttl_seconds=excluded.ttl_seconds;
                """,
                (
                    lease.task_id,
                    lease.agent_id,
                    lease.session_id,
                    lease.account_id,
                    lease.worktree_path,
                    lease.branch_name,
                    lease.ttl_seconds,
                ),
            )
            for lock in locks:
                conn.execute(
                    """
                    INSERT INTO file_locks (file_path, task_id, lock_type)
                    VALUES (?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        task_id=excluded.task_id,
                        lock_type=excluded.lock_type;
                    """,
                    (lock.file_path, lock.task_id, lock.lock_type),
                )
            conn.commit()

    def get_lease(self, task_id: str) -> Lease | None:
        with self.db.get_runtime_connection() as conn:
            row = conn.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return Lease(
                task_id=row[0],
                agent_id=row[1],
                session_id=row[2],
                account_id=row[3],
                worktree_path=row[4],
                branch_name=row[5],
                ttl_seconds=row[8],
            )

    def heartbeat(self, task_id: str) -> bool:
        with self.db.get_runtime_connection() as conn:
            cursor = conn.execute(
                "UPDATE leases SET last_heartbeat = CURRENT_TIMESTAMP WHERE task_id = ?",
                (task_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_lease(self, task_id: str) -> None:
        with self.db.get_runtime_connection() as conn:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            conn.commit()

    def is_file_locked(self, file_path: str) -> bool:
        with self.db.get_runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT f.file_path, l.ttl_seconds, l.last_heartbeat
                FROM file_locks f
                JOIN leases l ON l.task_id = f.task_id
                WHERE f.file_path = ?
                """,
                (file_path,),
            ).fetchone()
            if not row:
                return False
            # Check if active
            ttl = row[1]
            last_hb_str = row[2]
            last_hb = datetime.fromisoformat(last_hb_str.replace("Z", "+00:00"))
            return (datetime.now() - last_hb).total_seconds() <= ttl

    def get_conflicting_tasks(self, file_paths: list[str]) -> dict[str, str]:
        conflicts: dict[str, str] = {}
        with self.db.get_runtime_connection() as conn:
            for p in file_paths:
                row = conn.execute(
                    """
                    SELECT f.task_id, l.agent_id, l.ttl_seconds, l.last_heartbeat
                    FROM file_locks f
                    JOIN leases l ON l.task_id = f.task_id
                    WHERE f.file_path = ?
                    """,
                    (p,),
                ).fetchone()
                if row:
                    ttl = row[2]
                    last_hb = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
                    if (datetime.now() - last_hb).total_seconds() <= ttl:
                        conflicts[p] = f"Task: {row[0]}, Agent: {row[1]}"
        return conflicts

    def sweep_expired_leases(self) -> list[str]:
        expired_tasks: list[str] = []
        with self.db.get_runtime_connection() as conn:
            rows = conn.execute("SELECT task_id, ttl_seconds, last_heartbeat FROM leases").fetchall()
            for r in rows:
                task_id = r[0]
                ttl = r[1]
                last_hb = datetime.fromisoformat(r[2].replace("Z", "+00:00"))
                if (datetime.now() - last_hb).total_seconds() > ttl:
                    expired_tasks.append(task_id)
            for t in expired_tasks:
                conn.execute("DELETE FROM leases WHERE task_id = ?", (t,))
                conn.execute("DELETE FROM file_locks WHERE task_id = ?", (t,))
            conn.commit()
        return expired_tasks
```

```python
# src/taskmanager/db/ledger_repo.py
import json
from taskmanager.core.models import LedgerEvent
from taskmanager.db.connection import DatabaseManager


class LedgerRepository:
    def __init__(self, db_mgr: DatabaseManager):
        self.db = db_mgr

    def append(self, event: LedgerEvent) -> None:
        with self.db.get_ledger_connection() as conn:
            conn.execute(
                """
                INSERT INTO ledger_events (actor_id, command, target_id, payload_json, diff_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.actor_id,
                    event.command,
                    event.target_id,
                    json.dumps(event.payload),
                    json.dumps(event.diff),
                ),
            )
            conn.commit()

    def list_events(self, target_id: str | None = None, limit: int = 50) -> list[LedgerEvent]:
        with self.db.get_ledger_connection() as conn:
            if target_id:
                rows = conn.execute(
                    "SELECT id, timestamp, actor_id, command, target_id, payload_json, diff_json FROM ledger_events WHERE target_id = ? ORDER BY id DESC LIMIT ?",
                    (target_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, timestamp, actor_id, command, target_id, payload_json, diff_json FROM ledger_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                LedgerEvent(
                    id=r[0],
                    actor_id=r[2],
                    command=r[3],
                    target_id=r[4],
                    payload=json.loads(r[5]),
                    diff=json.loads(r[6]),
                )
                for r in rows
            ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_repos.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/db/ tests/unit/test_repos.py
git commit -m "feat(db): node, runtime and ledger repositories"
```

---

### Task 6: Graph Engine & Virtual Status Resolver

**Files:**
- Create: `src/taskmanager/engine/graph.py`
- Test: `tests/unit/test_graph_engine.py`

**Interfaces:**
- Produces:
  - `GraphEngine`: Evaluates DAG cycle detection, computes virtual states (`BLOCKED`, `READY`, `IN_FLIGHT`), computes plan status rollups, and injects review gates.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_graph_engine.py
from pathlib import Path
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VirtualStatus
from taskmanager.core.models import Node, NodeRelation
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine


def test_virtual_status_blocked_and_ready(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    engine = GraphEngine(node_repo, runtime_repo)

    # Task 1 and Task 2
    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="Task 1", status=NodeStatus.NOT_STARTED)
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="Task 2", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    # T2 depends on T1
    node_repo.add_relation(
        NodeRelation(source_id="AUTH-T02", target_id="AUTH-T01", relation_type=RelationType.DEPENDS_ON)
    )

    # Initially: T1 is READY, T2 is BLOCKED
    assert engine.resolve_task_state("AUTH-T01") == VirtualStatus.READY
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.BLOCKED

    # Once T1 is COMPLETED, T2 becomes READY
    t1.status = NodeStatus.COMPLETED
    node_repo.save_node(t1)
    assert engine.resolve_task_state("AUTH-T02") == VirtualStatus.READY


def test_cycle_detection(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    engine = GraphEngine(node_repo, runtime_repo)

    node_repo.save_node(
        Node(id="A", kind=NodeKind.TASK, title="A", status=NodeStatus.NOT_STARTED)
    )
    node_repo.save_node(
        Node(id="B", kind=NodeKind.TASK, title="B", status=NodeStatus.NOT_STARTED)
    )
    node_repo.add_relation(
        NodeRelation(source_id="A", target_id="B", relation_type=RelationType.DEPENDS_ON)
    )

    # Adding B -> A creates cycle
    assert engine.would_cause_cycle("B", "A") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_graph_engine.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.engine'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/engine/graph.py
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VirtualStatus
from taskmanager.core.models import Node, NodeRelation
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository


class GraphEngine:
    def __init__(self, node_repo: NodeRepository, runtime_repo: RuntimeRepository):
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo

    def resolve_task_state(self, task_id: str) -> VirtualStatus | NodeStatus:
        # Check active lease
        lease = self.runtime_repo.get_lease(task_id)
        if lease:
            return VirtualStatus.IN_FLIGHT

        node = self.node_repo.get_node(task_id)
        if not node:
            raise ValueError(f"Node {task_id} does not exist")

        if node.status != NodeStatus.NOT_STARTED:
            return node.status

        # Check dependencies
        deps = self.node_repo.get_dependencies(task_id)
        for dep_id in deps:
            dep_node = self.node_repo.get_node(dep_id)
            if not dep_node or dep_node.status not in (
                NodeStatus.COMPLETED,
                NodeStatus.SUPERSEDED,
            ):
                return VirtualStatus.BLOCKED

        return VirtualStatus.READY

    def resolve_plan_status(self, plan_id: str) -> NodeStatus | VirtualStatus:
        children = self.node_repo.get_children(plan_id)
        if not children:
            return NodeStatus.NOT_STARTED

        child_nodes = [self.node_repo.get_node(cid) for cid in children]
        valid_children = [c for c in child_nodes if c is not None]

        completed = sum(
            1 for c in valid_children if c.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
        )
        if completed == len(valid_children):
            return NodeStatus.COMPLETED

        not_started = sum(1 for c in valid_children if c.status == NodeStatus.NOT_STARTED)
        if not_started == len(valid_children):
            return NodeStatus.NOT_STARTED

        # Check if all remaining are blocked
        uncompleted = [
            c
            for c in valid_children
            if c.status not in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
        ]
        all_blocked = all(
            self.resolve_task_state(c.id) == VirtualStatus.BLOCKED for c in uncompleted
        )
        if all_blocked:
            return VirtualStatus.BLOCKED

        return NodeStatus.IMPLEMENTING

    def would_cause_cycle(self, source_id: str, target_id: str) -> bool:
        # If source depends on target, target cannot reach source through dependencies
        visited: set[str] = set()
        queue: list[str] = [target_id]

        while queue:
            curr = queue.pop(0)
            if curr == source_id:
                return True
            if curr not in visited:
                visited.add(curr)
                queue.extend(self.node_repo.get_dependencies(curr))
        return False

    def inject_plan_review_gate(self, plan_id: str) -> str:
        gate_id = f"{plan_id}-REV"
        gate_node = Node(
            id=gate_id,
            kind=NodeKind.REVIEW_GATE,
            title=f"Plan-wide Review Gate for {plan_id}",
            status=NodeStatus.NOT_STARTED,
        )
        self.node_repo.save_node(gate_node)

        # Gate depends on all tasks in plan
        for child_id in self.node_repo.get_children(plan_id):
            if child_id != gate_id:
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=gate_id,
                        target_id=child_id,
                        relation_type=RelationType.DEPENDS_ON,
                    )
                )
        return gate_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_graph_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/engine/graph.py tests/unit/test_graph_engine.py
git commit -m "feat(engine): graph cycle detection, virtual status resolution and review gates"
```

---

### Task 7: Git Worktree & Concurrency Engine

**Files:**
- Create: `src/taskmanager/engine/git.py`
- Create: `src/taskmanager/engine/runtime.py`
- Test: `tests/unit/test_runtime_engine.py`

**Interfaces:**
- Produces:
  - `GitManager`: Interacts with git subprocesses to resolve common dir, create worktrees, remove worktrees, and commit.
  - `ExecutionCoordinator`: Atomic task claim with file collision checking, heartbeat, release, and sweep.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_runtime_engine.py
from pathlib import Path
import subprocess
from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.core.models import Node, NodeVerification, VerificationType
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator


def test_claim_task_collision_rejection(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph_engine = GraphEngine(node_repo, runtime_repo)
    coordinator = ExecutionCoordinator(node_repo, runtime_repo, graph_engine, None)

    # Task 1 touches "file_a.py"
    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="T1", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t1)
    node_repo.add_verification(
        NodeVerification(
            node_id="AUTH-T01",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="file_a.py",
        )
    )

    # Task 2 also touches "file_a.py"
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="T2", status=NodeStatus.NOT_STARTED)
    node_repo.save_node(t2)
    node_repo.add_verification(
        NodeVerification(
            node_id="AUTH-T02",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="file_a.py",
        )
    )

    # Claim Task 1
    lease1 = coordinator.start_task(
        task_id="AUTH-T01", agent_id="agent-1", session_id="sess-1", create_worktree=False
    )
    assert lease1 is not None

    # Claim Task 2 should fail due to collision
    try:
        coordinator.start_task(
            task_id="AUTH-T02", agent_id="agent-2", session_id="sess-1", create_worktree=False
        )
        assert False, "Should have raised collision error"
    except ValueError as e:
        assert "file_a.py" in str(e)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_runtime_engine.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.engine.runtime'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/engine/git.py
from pathlib import Path
import subprocess


class GitManager:
    def __init__(self, repo_root: Path):
        self.root = repo_root

    def get_common_dir(self) -> Path:
        res = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        p = Path(res.stdout.strip())
        if not p.is_absolute():
            p = (self.root / p).resolve()
        return p

    def create_worktree(self, branch_name: str, worktree_path: Path, base_ref: str = "HEAD") -> None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), base_ref],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )

    def remove_worktree(self, worktree_path: Path) -> None:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path)],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
```

```python
# src/taskmanager/engine/runtime.py
from pathlib import Path
from taskmanager.core.enums import NodeStatus, VirtualStatus
from taskmanager.core.models import FileLock, Lease
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine


class ExecutionCoordinator:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        git_mgr: GitManager | None = None,
    ):
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph_engine
        self.git = git_mgr

    def start_task(
        self,
        task_id: str,
        agent_id: str,
        session_id: str,
        account_id: str | None = None,
        create_worktree: bool = False,
        worktree_base: Path | None = None,
    ) -> Lease:
        state = self.graph.resolve_task_state(task_id)
        if state != VirtualStatus.READY:
            raise ValueError(f"Task {task_id} is not ready to start (current state: {state})")

        # Gather declared files
        vers = self.node_repo.get_verifications(task_id)
        declared_files = [v.target_path for v in vers]

        # Check file collision
        conflicts = self.runtime_repo.get_conflicting_tasks(declared_files)
        if conflicts:
            conflicting_str = ", ".join(f"{f} ({msg})" for f, msg in conflicts.items())
            raise ValueError(f"Cannot claim task {task_id} due to file collision: {conflicting_str}")

        branch_name = f"tm/{task_id}"
        worktree_path_str: str | None = None

        if create_worktree and self.git and worktree_base:
            worktree_dir = worktree_base / task_id
            self.git.create_worktree(branch_name, worktree_dir)
            worktree_path_str = str(worktree_dir)

        lease = Lease(
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            account_id=account_id,
            worktree_path=worktree_path_str,
            branch_name=branch_name,
        )
        locks = [FileLock(file_path=f, task_id=task_id) for f in declared_files]
        self.runtime_repo.acquire_lease(lease, locks)

        # Transition node status
        node = self.node_repo.get_node(task_id)
        if node:
            node.status = NodeStatus.IMPLEMENTING
            self.node_repo.save_node(node)

        return lease

    def heartbeat(self, task_id: str) -> bool:
        return self.runtime_repo.heartbeat(task_id)

    def stop_task(
        self,
        task_id: str,
        new_status: NodeStatus = NodeStatus.WAITING_REVIEW,
        remove_worktree: bool = False,
    ) -> None:
        lease = self.runtime_repo.get_lease(task_id)
        if lease and remove_worktree and lease.worktree_path and self.git:
            self.git.remove_worktree(Path(lease.worktree_path))

        self.runtime_repo.release_lease(task_id)
        node = self.node_repo.get_node(task_id)
        if node:
            node.status = new_status
            self.node_repo.save_node(node)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_runtime_engine.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/engine/git.py src/taskmanager/engine/runtime.py tests/unit/test_runtime_engine.py
git commit -m "feat(engine): git worktree management and execution coordinator"
```

---

### Task 8: Static Verification Engine (AST & Filesystem)

**Files:**
- Create: `src/taskmanager/engine/verification.py`
- Test: `tests/unit/test_verification.py`

**Interfaces:**
- Produces:
  - `VerificationResult`: Report dataclass with passed/failed status and defect details
  - `VerificationEngine`: Evaluates `file_exists`, `file_absent`, `symbol_signature` (Python AST), and optional `codegraph` CLI

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_verification.py
from pathlib import Path
from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine.verification import VerificationEngine


def test_ast_symbol_verification(tmp_path: Path):
    source_file = tmp_path / "auth.py"
    source_file.write_text(
        "def verify_jwt(token: str) -> bool:\n    return True\n"
    )

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="auth.py",
        expected_pattern="def verify_jwt(token: str) -> bool",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True


def test_missing_file_verification(tmp_path: Path):
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="nonexistent.py",
    )
    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is False
    assert "File nonexistent.py does not exist" in result.message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_verification.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.engine.verification'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/engine/verification.py
import ast
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification


@dataclass
class VerificationResult:
    verification_id: int | None
    target_path: str
    verification_type: VerificationType
    passed: bool
    message: str


class VerificationEngine:
    def __init__(self, target_root: Path):
        self.root = target_root

    def verify_assertion(self, ver: NodeVerification) -> VerificationResult:
        full_path = self.root / ver.target_path

        if ver.verification_type == VerificationType.FILE_EXISTS:
            if full_path.exists():
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, True, "File exists")
            return VerificationResult(ver.id, ver.target_path, ver.verification_type, False, f"File {ver.target_path} does not exist")

        if ver.verification_type == VerificationType.FILE_ABSENT:
            if not full_path.exists():
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, True, "File absent")
            return VerificationResult(ver.id, ver.target_path, ver.verification_type, False, f"File {ver.target_path} still exists")

        if ver.verification_type == VerificationType.SYMBOL_SIGNATURE:
            if not full_path.exists():
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, False, f"File {ver.target_path} missing")
            content = full_path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(content)
            except SyntaxError as e:
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, False, f"Syntax error in {ver.target_path}: {e}")

            expected_func = ver.expected_pattern.split("(")[0].replace("def ", "").strip() if ver.expected_pattern else ""
            found = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == expected_func:
                    found = True
                    break
            if found:
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, True, f"Symbol {expected_func} found")
            return VerificationResult(ver.id, ver.target_path, ver.verification_type, False, f"Symbol {expected_func} not found in {ver.target_path}")

        if ver.verification_type == VerificationType.CODEGRAPH_QUERY:
            if not shutil.which("codegraph"):
                return VerificationResult(ver.id, ver.target_path, ver.verification_type, True, "codegraph CLI not installed; skipped")
            # Run codegraph CLI
            res = subprocess.run(
                ["codegraph", "query", ver.codegraph_query_json or "{}"],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            passed = res.returncode == 0
            return VerificationResult(ver.id, ver.target_path, ver.verification_type, passed, res.stdout or res.stderr)

        return VerificationResult(ver.id, ver.target_path, ver.verification_type, True, "Verification passed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_verification.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/engine/verification.py tests/unit/test_verification.py
git commit -m "feat(engine): in-process AST verification and optional codegraph CLI runner"
```

---

### Task 9: Recommendation Heuristics Engine (`next-tasks`)

**Files:**
- Create: `src/taskmanager/engine/heuristics.py`
- Test: `tests/unit/test_heuristics.py`

**Interfaces:**
- Produces:
  - `ScoredTask`: Dataclass containing task, score, factor breakdown, and candidate status
  - `RecommendationEngine`: Multi-factor scoring formula implementation (`Priority`, `Unblocking`, `Plan Closeness`, `Closer Bonus`) with strategy overrides and model filtering

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_heuristics.py
from pathlib import Path
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VirtualStatus
from taskmanager.core.models import Node, NodeRelation
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine


def test_recommendation_scoring(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    graph = GraphEngine(node_repo, runtime_repo)
    engine = RecommendationEngine(node_repo, runtime_repo, graph)

    # Create plan and 2 tasks
    plan = Node(id="AUTH-P01", kind=NodeKind.PLAN, title="Plan", priority=100)
    node_repo.save_node(plan)

    t1 = Node(id="AUTH-T01", kind=NodeKind.TASK, title="T1", priority=90, acceptable_models=["sonnet"])
    t2 = Node(id="AUTH-T02", kind=NodeKind.TASK, title="T2", priority=50, acceptable_models=["flash"])
    node_repo.save_node(t1)
    node_repo.save_node(t2)

    node_repo.add_relation(
        NodeRelation(source_id="AUTH-P01", target_id="AUTH-T01", relation_type=RelationType.CONTAINS)
    )
    node_repo.add_relation(
        NodeRelation(source_id="AUTH-P01", target_id="AUTH-T02", relation_type=RelationType.CONTAINS)
    )

    # T1 unblocks T2
    node_repo.add_relation(
        NodeRelation(source_id="AUTH-T02", target_id="AUTH-T01", relation_type=RelationType.DEPENDS_ON)
    )

    ranked = engine.get_next_tasks(limit=5)
    assert len(ranked) == 1
    assert ranked[0].task_id == "AUTH-T01"  # T2 is BLOCKED
    assert ranked[0].score > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_heuristics.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.engine.heuristics'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/engine/heuristics.py
from dataclasses import dataclass
from taskmanager.core.enums import NodeKind, NodeStatus, VirtualStatus
from taskmanager.core.models import Node
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine


@dataclass
class ScoredTask:
    task_id: str
    title: str
    plan_id: str | None
    score: float
    priority: int
    acceptable_models: list[str]
    unblocking_count: int
    declared_files: list[str]


class RecommendationEngine:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
    ):
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph_engine

    def get_next_tasks(
        self,
        plan_id: str | None = None,
        model_filter: str | None = None,
        strategy: str = "balanced",
        limit: int = 5,
    ) -> list[ScoredTask]:
        # Strategy weights
        if strategy == "unblock-first":
            w_prio, w_unlock, w_close, w_adv = 0.20, 0.60, 0.10, 0.10
        elif strategy == "finish-plans":
            w_prio, w_unlock, w_close, w_adv = 0.10, 0.10, 0.50, 0.30
        elif strategy == "priority-strict":
            w_prio, w_unlock, w_close, w_adv = 1.0, 0.0, 0.0, 0.0
        else:
            w_prio, w_unlock, w_close, w_adv = 0.35, 0.30, 0.20, 0.15

        all_tasks = self.node_repo.list_nodes(kind=NodeKind.TASK)
        scored: list[ScoredTask] = []

        for task in all_tasks:
            # 1. Virtual readiness gate
            if self.graph.resolve_task_state(task.id) != VirtualStatus.READY:
                continue

            # Model filter
            if model_filter and task.acceptable_models and model_filter not in task.acceptable_models:
                continue

            # Declared files & collision check
            vers = self.node_repo.get_verifications(task.id)
            declared_files = [v.target_path for v in vers]
            if self.runtime_repo.get_conflicting_tasks(declared_files):
                continue

            # Find parent plan
            parent_plan_id: str | None = None
            for p in self.node_repo.list_nodes(kind=NodeKind.PLAN):
                if task.id in self.node_repo.get_children(p.id):
                    parent_plan_id = p.id
                    break

            if plan_id and parent_plan_id != plan_id:
                continue

            parent_plan = self.node_repo.get_node(parent_plan_id) if parent_plan_id else None
            plan_prio = parent_plan.priority if parent_plan else 50

            # Factors
            s_prio = (task.priority * 0.7) + (plan_prio * 0.3)
            blocked_downstream = self.node_repo.get_blocked_by(task.id)
            s_unlock = min(len(blocked_downstream) * 25.0, 100.0)

            s_close = 0.0
            s_adv = 0.0
            if parent_plan_id:
                siblings = self.node_repo.get_children(parent_plan_id)
                if siblings:
                    completed_count = sum(
                        1
                        for s in siblings
                        if (node := self.node_repo.get_node(s))
                        and node.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
                    )
                    s_close = (completed_count / len(siblings)) * 100.0
                    if completed_count == len(siblings) - 1:
                        s_adv = 100.0

            total_score = (
                w_prio * s_prio + w_unlock * s_unlock + w_close * s_close + w_adv * s_adv
            )

            scored.append(
                ScoredTask(
                    task_id=task.id,
                    title=task.title,
                    plan_id=parent_plan_id,
                    score=round(total_score, 2),
                    priority=task.priority,
                    acceptable_models=task.acceptable_models,
                    unblocking_count=len(blocked_downstream),
                    declared_files=declared_files,
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:limit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_heuristics.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/engine/heuristics.py tests/unit/test_heuristics.py
git commit -m "feat(engine): recommendation heuristics engine with multi-factor scoring"
```

---

### Task 10: Semantic Search & In-Engine Vector Indexing (`sqlite-vec`)

**Files:**
- Create: `src/taskmanager/engine/search.py`
- Test: `tests/unit/test_search.py`

**Interfaces:**
- Produces:
  - `EmbeddingProvider`: Protocol / base class for generating float32 vector embeddings (OpenAI vs local)
  - `SearchEngine`: Native `sqlite-vec` KNN matching joined with relational filters and FTS5 ranking

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_search.py
from pathlib import Path
import struct
from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.core.models import Node
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.search import MockEmbeddingProvider, SearchEngine


def test_sqlite_vec_search(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all(vector_dimensions=4)
    repo = NodeRepository(db)

    # Save 2 nodes
    repo.save_node(Node(id="AUTH-T01", kind=NodeKind.TASK, title="JWT Auth", status=NodeStatus.READY))
    repo.save_node(Node(id="DATA-T01", kind=NodeKind.TASK, title="Postgres Migration", status=NodeStatus.READY))

    provider = MockEmbeddingProvider(dimensions=4)
    engine = SearchEngine(db, provider)

    # Index vectors
    engine.index_node("AUTH-T01", [1.0, 0.0, 0.0, 0.0])
    engine.index_node("DATA-T01", [0.0, 1.0, 0.0, 0.0])

    # Search near AUTH-T01
    results = engine.search(query="JWT", query_vector=[0.9, 0.1, 0.0, 0.0], limit=2)
    assert len(results) > 0
    assert results[0]["node_id"] == "AUTH-T01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_search.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.engine.search'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/engine/search.py
from dataclasses import dataclass
import os
import struct
from typing import Any
import httpx
from taskmanager.db.connection import DatabaseManager


class MockEmbeddingProvider:
    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def get_embedding(self, text: str) -> list[float]:
        # Deterministic mock vector
        vec = [0.0] * self.dimensions
        for i, char in enumerate(text[: self.dimensions]):
            vec[i] = (ord(char) % 10) / 10.0
        return vec


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("TASKMANAGER_OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        self.api_key = api_key or os.getenv("TASKMANAGER_OPENAI_API_KEY") or ""
        self.model = model or os.getenv("TASKMANAGER_OPENAI_MODEL") or "text-embedding-3-small"

    def get_embedding(self, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"input": text, "model": self.model}
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]


class SearchEngine:
    def __init__(self, db_mgr: DatabaseManager, embedding_provider: Any):
        self.db = db_mgr
        self.provider = embedding_provider

    def _serialize_vec(self, vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    def index_node(
        self,
        node_id: str,
        vector: list[float],
        target_type: str = "title",
        section_key: str | None = None,
    ) -> None:
        raw_bytes = self._serialize_vec(vector)
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO vec_nodes (node_id, target_type, section_key, embedding)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    target_type=excluded.target_type,
                    section_key=excluded.section_key,
                    embedding=excluded.embedding;
                """,
                (node_id, target_type, section_key, raw_bytes),
            )
            conn.commit()

    def search(
        self,
        query: str,
        query_vector: list[float] | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        vec = query_vector or self.provider.get_embedding(query)
        raw_bytes = self._serialize_vec(vec)

        query_sql = """
        SELECT
            n.id,
            n.kind,
            n.title,
            n.status,
            v.target_type,
            v.section_key,
            v.distance
        FROM vec_nodes v
        JOIN nodes n ON n.id = v.node_id
        WHERE v.embedding MATCH ?
          AND k = ?
        """
        params: list[Any] = [raw_bytes, limit]

        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query_sql += f" AND n.kind IN ({placeholders})"
            params.extend(kinds)

        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query_sql += f" AND n.status IN ({placeholders})"
            params.extend(statuses)

        query_sql += " ORDER BY v.distance ASC"

        with self.db.get_spec_connection() as conn:
            rows = conn.execute(query_sql, tuple(params)).fetchall()
            return [
                {
                    "node_id": r[0],
                    "kind": r[1],
                    "title": r[2],
                    "status": r[3],
                    "target_type": r[4],
                    "section_key": r[5],
                    "distance": r[6],
                }
                for r in rows
            ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_search.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/engine/search.py tests/unit/test_search.py
git commit -m "feat(engine): sqlite-vec hybrid search engine"
```

---

### Task 11: Dynamic Markdown Projections & Bulk Importer

**Files:**
- Create: `src/taskmanager/renderers/markdown.py`
- Create: `src/taskmanager/renderers/importers.py`
- Test: `tests/unit/test_renderers.py`

**Interfaces:**
- Produces:
  - `MarkdownRenderer`: Renders `summary`, `subagent`, and `full` markdown projections on demand
  - `BulkImporter`: Parses structured markdown/yaml/json documents and creates entire graph nodes atomically

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_renderers.py
from pathlib import Path
from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.core.models import Node, NodeSection
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer


def test_markdown_renderer_projections(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    task = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="JWT Auth Task",
        status=NodeStatus.NOT_STARTED,
        priority=80,
        acceptable_models=["sonnet"],
    )
    repo.save_node(task)
    repo.save_section(
        NodeSection(
            node_id="AUTH-T01",
            section_key="steps",
            ordinal=1,
            header="### Steps",
            content="- [ ] Step 1: Write test",
        )
    )

    renderer = MarkdownRenderer(repo)
    summary = renderer.render("AUTH-T01", view="summary")
    assert "id: AUTH-T01" in summary
    assert "JWT Auth Task" in summary

    subagent_brief = renderer.render("AUTH-T01", view="subagent")
    assert "### Steps" in subagent_brief
    assert "- [ ] Step 1: Write test" in subagent_brief


def test_bulk_importer_json(tmp_path: Path):
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    importer = BulkImporter(repo)

    payload = {
        "spec": {"id": "AUTH", "title": "Auth Spec"},
        "plans": [
            {
                "id": "AUTH-P1",
                "title": "Token Plan",
                "tasks": [
                    {"id": "AUTH-T1", "title": "Create Token", "priority": 90}
                ],
            }
        ],
    }
    importer.import_dict(payload)

    assert repo.get_node("AUTH") is not None
    assert repo.get_node("AUTH-P1") is not None
    assert repo.get_node("AUTH-T1") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_renderers.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.renderers'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/renderers/markdown.py
import json
from taskmanager.core.models import Node
from taskmanager.db.node_repo import NodeRepository


class MarkdownRenderer:
    def __init__(self, node_repo: NodeRepository):
        self.node_repo = node_repo

    def render(self, node_id: str, view: str = "full") -> str:
        node = self.node_repo.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        sections = self.node_repo.get_all_sections(node_id)

        # Frontmatter
        frontmatter_dict = {
            "id": node.id,
            "kind": node.kind.value,
            "title": node.title,
            "status": node.status.value,
            "priority": node.priority,
            "acceptable_models": node.acceptable_models,
            **node.frontmatter,
        }

        yaml_lines = ["---"]
        for k, v in frontmatter_dict.items():
            yaml_lines.append(f"{k}: {json.dumps(v) if isinstance(v, (list, dict)) else v}")
        yaml_lines.append("---\n")
        frontmatter_text = "\n".join(yaml_lines)

        if view == "summary":
            overview_sec = self.node_repo.get_section(node_id, "overview")
            body = overview_sec.content if overview_sec else f"# {node.title}"
            return f"{frontmatter_text}\n{body}\n"

        if view == "subagent":
            # Render task brief with parent context if available
            out = [frontmatter_text, f"# Task Brief: {node.title}\n"]
            for sec in sections:
                out.append(f"{sec.header}\n\n{sec.content}\n")
            return "\n".join(out)

        # Full view
        out = [frontmatter_text, f"# {node.title}\n"]
        for sec in sections:
            out.append(f"{sec.header}\n\n{sec.content}\n")
        return "\n".join(out)
```

```python
# src/taskmanager/renderers/importers.py
import json
from typing import Any
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Node, NodeRelation, NodeSection
from taskmanager.db.node_repo import NodeRepository


class BulkImporter:
    def __init__(self, node_repo: NodeRepository):
        self.node_repo = node_repo

    def import_dict(self, data: dict[str, Any]) -> None:
        # Spec
        spec_data = data.get("spec")
        if spec_data:
            spec_node = Node(
                id=spec_data["id"],
                kind=NodeKind.SPEC,
                title=spec_data["title"],
                priority=spec_data.get("priority", 50),
            )
            self.node_repo.save_node(spec_node)

        # Plans
        for plan_data in data.get("plans", []):
            plan_node = Node(
                id=plan_data["id"],
                kind=NodeKind.PLAN,
                title=plan_data["title"],
                priority=plan_data.get("priority", 50),
            )
            self.node_repo.save_node(plan_node)
            if spec_data:
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=spec_data["id"],
                        target_id=plan_data["id"],
                        relation_type=RelationType.CONTAINS,
                    )
                )

            # Tasks
            for task_data in plan_data.get("tasks", []):
                task_node = Node(
                    id=task_data["id"],
                    kind=NodeKind.TASK,
                    title=task_data["title"],
                    priority=task_data.get("priority", 50),
                    acceptable_models=task_data.get("acceptable_models", []),
                )
                self.node_repo.save_node(task_node)
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=plan_data["id"],
                        target_id=task_data["id"],
                        relation_type=RelationType.CONTAINS,
                    )
                )
                # Dependencies
                for dep in task_data.get("depends_on", []):
                    self.node_repo.add_relation(
                        NodeRelation(
                            source_id=task_data["id"],
                            target_id=dep,
                            relation_type=RelationType.DEPENDS_ON,
                        )
                    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_renderers.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/renderers/ tests/unit/test_renderers.py
git commit -m "feat(renderers): markdown dynamic projections and bulk importer"
```

---

### Task 12: Dishka Dependency Injection Container & Typer CLI Commands

**Files:**
- Create: `src/taskmanager/di/container.py`
- Create: `src/taskmanager/cli/main.py`
- Test: `tests/integration/test_cli.py`

**Interfaces:**
- Produces:
  - `make_container(project_root: Path)`: DI container providing repos, engines, and managers
  - Full CLI command group `app` supporting `init`, `spec`, `plan`, `task`, `section`, `run`, `next`, `verify`, `render`, `import`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_cli.py
from pathlib import Path
from typer.testing import CliRunner
from taskmanager.cli.main import app

runner = CliRunner()


def test_cli_init_and_task_lifecycle(tmp_path: Path):
    # tm init
    res = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert (tmp_path / ".taskmanager/spec.db").exists()

    # tm spec add
    res = runner.invoke(
        app, ["spec", "add", "Auth Spec", "--slug", "AUTH", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0

    # tm plan add
    res = runner.invoke(
        app, ["plan", "add", "User Plan", "--spec", "AUTH", "--slug", "USER", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0

    # tm task add
    res = runner.invoke(
        app,
        ["task", "add", "Login Task", "--plan", "AUTH-USER", "--slug", "LOGIN", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0

    # tm render
    res = runner.invoke(app, ["render", "AUTH-USER-LOGIN", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "id: AUTH-USER-LOGIN" in res.stdout

    # tm next
    res = runner.invoke(app, ["next", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "AUTH-USER-LOGIN" in res.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_cli.py -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'taskmanager.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/taskmanager/di/container.py
from pathlib import Path
from dishka import Provider, Scope, make_container, provide
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.search import MockEmbeddingProvider, SearchEngine
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer


class TaskManagerProvider(Provider):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    @provide(scope=Scope.APP)
    def get_db_mgr(self) -> DatabaseManager:
        return DatabaseManager(self.root / ".taskmanager")

    @provide(scope=Scope.APP)
    def get_node_repo(self, db: DatabaseManager) -> NodeRepository:
        return NodeRepository(db)

    @provide(scope=Scope.APP)
    def get_runtime_repo(self, db: DatabaseManager) -> RuntimeRepository:
        return RuntimeRepository(db)

    @provide(scope=Scope.APP)
    def get_ledger_repo(self, db: DatabaseManager) -> LedgerRepository:
        return LedgerRepository(db)

    @provide(scope=Scope.APP)
    def get_graph_engine(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository
    ) -> GraphEngine:
        return GraphEngine(node_repo, runtime_repo)

    @provide(scope=Scope.APP)
    def get_git_mgr(self) -> GitManager:
        return GitManager(self.root)

    @provide(scope=Scope.APP)
    def get_coordinator(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        git_mgr: GitManager,
    ) -> ExecutionCoordinator:
        return ExecutionCoordinator(node_repo, runtime_repo, graph_engine, git_mgr)

    @provide(scope=Scope.APP)
    def get_heuristics(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
    ) -> RecommendationEngine:
        return RecommendationEngine(node_repo, runtime_repo, graph_engine)

    @provide(scope=Scope.APP)
    def get_renderer(self, node_repo: NodeRepository) -> MarkdownRenderer:
        return MarkdownRenderer(node_repo)

    @provide(scope=Scope.APP)
    def get_importer(self, node_repo: NodeRepository) -> BulkImporter:
        return BulkImporter(node_repo)
```

```python
# src/taskmanager/cli/main.py
import json
from pathlib import Path
from typing import Annotated
import typer
from rich import print
from rich.table import Table
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Node, NodeRelation, NodeSection
from taskmanager.core.naming import QualifiedPath
from taskmanager.db.connection import DatabaseManager
from taskmanager.di.container import TaskManagerProvider
from dishka import make_container

app = typer.Typer(name="taskmanager", help="Local agentic task tracker system")
spec_app = typer.Typer(name="spec", help="Manage specifications")
plan_app = typer.Typer(name="plan", help="Manage plans")
task_app = typer.Typer(name="task", help="Manage tasks")
run_app = typer.Typer(name="run", help="Manage execution leases")
app.add_typer(spec_app)
app.add_typer(plan_app)
app.add_typer(task_app)
app.add_typer(run_app)


def _get_root(path: Path | None) -> Path:
    return path.resolve() if path else Path.cwd()


@app.command()
def init(path: Annotated[Path | None, typer.Option("--path", "-C")] = None):
    root = _get_root(path)
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    print(f"[green]Initialized .taskmanager in {root}[/green]")


@spec_app.command("add")
def spec_add(
    title: str,
    slug: Annotated[str | None, typer.Option("--slug", "-s")] = None,
    priority: int = 50,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
):
    root = _get_root(path)
    container = make_container(TaskManagerProvider(root))
    node_repo = container.get(TaskManagerProvider).get_node_repo(DatabaseManager(root / ".taskmanager"))
    spec_id = slug or "S1"
    node_repo.save_node(Node(id=spec_id, kind=NodeKind.SPEC, title=title, priority=priority))
    print(f"[green]Added spec {spec_id}[/green]")


@plan_app.command("add")
def plan_add(
    title: str,
    spec: str = typer.Option(..., "--spec"),
    slug: Annotated[str | None, typer.Option("--slug", "-s")] = None,
    priority: int = 50,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
):
    root = _get_root(path)
    container = make_container(TaskManagerProvider(root))
    node_repo = container.get(TaskManagerProvider).get_node_repo(DatabaseManager(root / ".taskmanager"))
    plan_id = f"{spec}-{slug}" if slug else f"{spec}-P1"
    node_repo.save_node(Node(id=plan_id, kind=NodeKind.PLAN, title=title, priority=priority))
    node_repo.add_relation(NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS))
    print(f"[green]Added plan {plan_id}[/green]")


@task_app.command("add")
def task_add(
    title: str,
    plan: str = typer.Option(..., "--plan"),
    slug: Annotated[str | None, typer.Option("--slug", "-s")] = None,
    priority: int = 50,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
):
    root = _get_root(path)
    container = make_container(TaskManagerProvider(root))
    node_repo = container.get(TaskManagerProvider).get_node_repo(DatabaseManager(root / ".taskmanager"))
    task_id = f"{plan}-{slug}" if slug else f"{plan}-T1"
    node_repo.save_node(Node(id=task_id, kind=NodeKind.TASK, title=title, priority=priority))
    node_repo.add_relation(NodeRelation(source_id=plan, target_id=task_id, relation_type=RelationType.CONTAINS))
    print(f"[green]Added task {task_id}[/green]")


@app.command("render")
def render(
    qualified_id: str,
    view: str = "full",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
):
    root = _get_root(path)
    container = make_container(TaskManagerProvider(root))
    renderer = container.get(TaskManagerProvider).get_renderer(
        container.get(TaskManagerProvider).get_node_repo(DatabaseManager(root / ".taskmanager"))
    )
    qp = QualifiedPath.parse(qualified_id)
    output = renderer.render(qp.node_id, view=view)
    print(output)


@app.command("next")
def next_tasks(
    limit: int = 5,
    strategy: str = "balanced",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
):
    root = _get_root(path)
    container = make_container(TaskManagerProvider(root))
    provider = container.get(TaskManagerProvider)
    db = DatabaseManager(root / ".taskmanager")
    heuristics = provider.get_heuristics(
        provider.get_node_repo(db),
        provider.get_runtime_repo(db),
        provider.get_graph_engine(provider.get_node_repo(db), provider.get_runtime_repo(db)),
    )
    ranked = heuristics.get_next_tasks(strategy=strategy, limit=limit)
    table = Table(title="Recommended Next Tasks")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Plan", style="magenta")
    table.add_column("Score", justify="right", style="green")
    for t in ranked:
        table.add_row(t.task_id, t.title, t.plan_id or "-", str(t.score))
    print(table)


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_cli.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/di/ src/taskmanager/cli/ tests/integration/test_cli.py
git commit -m "feat(cli): dishka container setup and typer cli commands"
```

---

### Task 13: Bundled Agent Skills (Implementer & Dispatcher)

**Files:**
- Create: `src/taskmanager/skills/taskmanager/SKILL.md`
- Create: `src/taskmanager/skills/dispatcher/SKILL.md`
- Test: `tests/unit/test_skills.py`

**Interfaces:**
- Produces:
  - Valid `SKILL.md` documents containing instructions, workflows, and command reference for AI agents.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_skills.py
from pathlib import Path


def test_bundled_skills_exist():
    skill_dir = Path("src/taskmanager/skills")
    tm_skill = skill_dir / "taskmanager/SKILL.md"
    dispatcher_skill = skill_dir / "dispatcher/SKILL.md"

    assert tm_skill.exists()
    assert dispatcher_skill.exists()
    assert "name: taskmanager" in tm_skill.read_text(encoding="utf-8")
    assert "name: dispatcher" in dispatcher_skill.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_skills.py -v`  
Expected: FAIL with `AssertionError: assert False (tm_skill.exists())`

- [ ] **Step 3: Write minimal implementation**

```markdown
<!-- src/taskmanager/skills/taskmanager/SKILL.md -->
---
name: taskmanager
description: Use when executing, tracking, or updating tasks and plans via the taskmanager CLI
---

# TaskManager Agent Skill

Use `taskmanager` (`tm`) to claim, verify, and complete tasks.

## Workflow

1. **Read Brief**:
   ```bash
   tm render <task_id> --view subagent
   ```
2. **Claim & Prepare Worktree**:
   ```bash
   tm run start <task_id> --worktree
   ```
3. **Heartbeat**:
   Ping periodically while active:
   ```bash
   tm run heartbeat <task_id>
   ```
4. **Verify Implementation**:
   ```bash
   tm verify run <task_id>
   ```
5. **Complete / Hand Off**:
   ```bash
   tm run stop <task_id> --status WAITING_REVIEW
   ```
```

```markdown
<!-- src/taskmanager/skills/dispatcher/SKILL.md -->
---
name: dispatcher
description: Use when planning waves, routing tasks, balancing concurrency, and dispatching subagents
---

# Dispatcher Skill

Use `taskmanager` (`tm`) to schedule disjoint waves of work without file conflicts.

## Principles

1. **Wave Planning**: Disjoint file lists only. Tasks sharing files must never run in parallel.
2. **Next Tasks Discovery**:
   ```bash
   tm next -n 5 --strategy balanced
   ```
3. **Model Tiering**:
   Match the candidate's `acceptable_models` with available agent tiers.
4. **Review Gates**:
   Respect task-level or plan-level review gates before merging branches.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_skills.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/taskmanager/skills/ tests/unit/test_skills.py
git commit -m "docs(skills): bundled taskmanager and dispatcher agent skills"
```

---

### Task 14: Quality Gates & Full Verification

**Files:**
- Modify: `pyproject.toml`
- Test: Full test suite

- [ ] **Step 1: Run linter and formatter**

Run: `uv run ruff check . && uv run ruff format --check .`  
Expected: 0 errors

- [ ] **Step 2: Run strict type checking**

Run: `uv run mypy --strict src/taskmanager`  
Expected: 0 errors

- [ ] **Step 3: Run full test suite with coverage**

Run: `uv run pytest --cov=taskmanager --cov-report=term-missing`  
Expected: All tests pass with >85% coverage

- [ ] **Step 4: Commit any quality fixes**

```bash
git commit -m "chore: enforce ruff linting, mypy strict typing and test coverage"
```
