import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

from taskmanager.db.schema import (
    INDEX_STATE_SQL,
    LEDGER_SCHEMA_SQL,
    RUNTIME_SCHEMA_SQL,
    SPEC_SCHEMA_SQL,
    vec_nodes_sql,
)


class DatabaseManager:
    def __init__(self, taskmanager_dir: Path) -> None:
        self.taskmanager_dir = taskmanager_dir
        self.dir = taskmanager_dir
        self.spec_db = taskmanager_dir / "spec.db"
        self.runtime_db = taskmanager_dir / "runtime.db"
        self.ledger_db = taskmanager_dir / "ledger.db"
        # One connection per database, reused for this DatabaseManager's whole lifetime rather
        # than opened and closed on every get_*_connection() call -- see the three getters
        # below. No read here holds a long transaction open (each execute() outside an explicit
        # BEGIN is its own implicit transaction), so a reused connection still sees every write
        # another process commits via WAL; this changes connection lifecycle only, not isolation.
        self._spec_conn: sqlite3.Connection | None = None
        self._runtime_conn: sqlite3.Connection | None = None
        self._ledger_conn: sqlite3.Connection | None = None

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        self.taskmanager_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: safe because every caller (the CLI's single process, the web
        # server's single asyncio event-loop thread) already serializes its own access; it only
        # guards against the connection object being handed to a thread sqlite3 doesn't know
        # created it, which reuse now makes possible in principle even though nothing does it.
        conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
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
                conn.executescript(INDEX_STATE_SQL)
                # A repeat import used to insert every verification again.
                conn.execute(
                    """
                    DELETE FROM node_verifications WHERE id NOT IN (
                        SELECT MIN(id) FROM node_verifications
                        GROUP BY node_id, verification_type, target_path,
                                 COALESCE(expected_pattern, '')
                    )
                    """
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_verifications ON node_verifications "
                    "(node_id, verification_type, target_path, COALESCE(expected_pattern, ''))"
                )
                conn.commit()
        except sqlite3.Error:
            pass

    @contextmanager
    def get_spec_connection(self) -> Generator[sqlite3.Connection]:
        # _ensure_spec_migrations is meant to self-heal a tampered-with or older-version DB on
        # every open (a real test exercises exactly that: drop a table, expect it back on the
        # next connection) -- skipping it after the first call broke that. It stays unconditional;
        # what got expensive was opening a fresh connection (with a fresh extension load) to run
        # it against, which the cached connection below already fixes on its own: profiled on a
        # 342-task DB, /api/tree went from 14.06s to 0.155s from connection reuse alone.
        if self._spec_conn is None:
            self._spec_conn = self._create_connection(self.spec_db, load_vec=True)
        self._ensure_spec_migrations(self._spec_conn)
        yield self._spec_conn

    @contextmanager
    def get_runtime_connection(self) -> Generator[sqlite3.Connection]:
        if self._runtime_conn is None:
            self._runtime_conn = self._create_connection(self.runtime_db, load_vec=False)
        yield self._runtime_conn

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection]:
        if self._ledger_conn is None:
            self._ledger_conn = self._create_connection(self.ledger_db, load_vec=False)
        yield self._ledger_conn

    def close(self) -> None:
        """Close every cached connection. Optional -- process exit does this too -- but a
        long-lived caller (the web server) that wants to drop file handles explicitly can."""
        for attr in ("_spec_conn", "_runtime_conn", "_ledger_conn"):
            conn = getattr(self, attr)
            if conn is not None:
                conn.close()
                setattr(self, attr, None)

    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_spec_connection() as conn:
            conn.executescript(SPEC_SCHEMA_SQL)
            conn.executescript(INDEX_STATE_SQL)
            # Safe migration for existing databases
            cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
            if "ordinal" not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0;")
            conn.execute(vec_nodes_sql(vector_dimensions))
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
