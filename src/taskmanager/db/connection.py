import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

from taskmanager.db.schema import LEDGER_SCHEMA_SQL, RUNTIME_SCHEMA_SQL, SPEC_SCHEMA_SQL


class DatabaseManager:
    def __init__(self, taskmanager_dir: Path) -> None:
        self.taskmanager_dir = taskmanager_dir
        self.dir = taskmanager_dir
        self.spec_db = taskmanager_dir / "spec.db"
        self.runtime_db = taskmanager_dir / "runtime.db"
        self.ledger_db = taskmanager_dir / "ledger.db"

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        self.taskmanager_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        if load_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        return conn

    def _ensure_spec_migrations(self, conn: sqlite3.Connection) -> None:
        try:
            has_nodes = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone()
            if has_nodes:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
                if "ordinal" not in cols:
                    conn.execute("ALTER TABLE nodes ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0;")
                    conn.commit()
        except sqlite3.Error:
            pass

    @contextmanager
    def get_spec_connection(self) -> Generator[sqlite3.Connection]:
        conn = self._create_connection(self.spec_db, load_vec=True)
        self._ensure_spec_migrations(conn)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_runtime_connection(self) -> Generator[sqlite3.Connection]:
        conn = self._create_connection(self.runtime_db, load_vec=False)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection]:
        conn = self._create_connection(self.ledger_db, load_vec=False)
        try:
            yield conn
        finally:
            conn.close()

    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_spec_connection() as conn:
            conn.executescript(SPEC_SCHEMA_SQL)
            # Safe migration for existing databases
            cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
            if "ordinal" not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0;")
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

    def is_initialized(self) -> bool:
        if not self.spec_db.exists():
            return False
        try:
            with self.get_spec_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
                ).fetchone()
                return bool(row)
        except sqlite3.Error, OSError:
            return False
