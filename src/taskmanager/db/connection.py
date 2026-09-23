import sqlite3
import threading
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
        # One connection per database *per thread*, reused for that thread's lifetime rather
        # than reopened on every get_*_connection() call (reopening, with its extension load,
        # dominated web load time). Per thread because the web server runs its database routes
        # in a threadpool: one shared connection interleaves concurrent cursors, so a
        # fetchone() returns another request's row or None. Each connection sees every other
        # connection's commits through WAL, so this changes lifecycle only, not isolation.
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        self.taskmanager_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False only so close() can close every thread's connection from
        # whichever thread calls it; no connection is ever used by a thread other than its own.
        conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        if load_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        with self._all_conns_lock:
            self._all_conns.append(conn)
        return conn

    def _thread_conn(self, name: str, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, name, None)
        if conn is None:
            conn = self._create_connection(db_path, load_vec=load_vec)
            setattr(self._local, name, conn)
        return conn

    @property
    def _spec_tx_depth(self) -> int:
        """>0 while this thread holds `spec_transaction()` open: its repository calls skip
        their own commit and the migration self-heal, so a run of writes lands as one commit
        or none. Per thread, like the connection, so another thread's writes keep committing."""
        return int(getattr(self._local, "spec_tx_depth", 0))

    @_spec_tx_depth.setter
    def _spec_tx_depth(self, value: int) -> None:
        self._local.spec_tx_depth = value

    def _ensure_spec_migrations(self, conn: sqlite3.Connection) -> None:
        try:
            # One query decides whether anything needs healing at all: `nodes`, `index_state`
            # and the verification uniqueness index all existing is the steady state every call
            # hits, so that state costs one `sqlite_master` lookup rather than the five
            # statements below (including a whole-table GROUP BY) -- with hundreds of
            # NodeRepository calls per web request, running those five unconditionally was
            # 48,638 calls for one `/api/tree` on a 541-task DB, 11.4s of its 12.9s. A dropped
            # table, or a database old enough to predate the uniqueness index, is still caught
            # and healed on the very next call, which is what the self-heal test exercises.
            row = conn.execute(
                "SELECT "
                "(SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'), "
                "(SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_state'), "
                "(SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_node_verifications')"
            ).fetchone()
            has_nodes, has_index_state, has_uq_index = row
            if not has_nodes:
                return
            if has_index_state and has_uq_index:
                return
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
        conn = self._thread_conn("spec", self.spec_db, load_vec=True)
        if self._spec_tx_depth == 0:
            self._ensure_spec_migrations(conn)
        yield conn

    def spec_commit(self, conn: sqlite3.Connection) -> None:
        """The commit every repository write ends with -- except while `spec_transaction()`
        is open, where the caller wants one commit (or one rollback) for the whole run rather
        than one per write, so a later write failing does not leave an earlier one visible."""
        if self._spec_tx_depth == 0:
            conn.commit()

    @contextmanager
    def spec_transaction(self) -> Generator[sqlite3.Connection]:
        with self.get_spec_connection() as conn:
            self._spec_tx_depth += 1
            try:
                yield conn
            except BaseException:
                if self._spec_tx_depth == 1:
                    conn.rollback()
                raise
            else:
                if self._spec_tx_depth == 1:
                    conn.commit()
            finally:
                self._spec_tx_depth -= 1

    @contextmanager
    def get_runtime_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("runtime", self.runtime_db)

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("ledger", self.ledger_db)

    def close(self) -> None:
        """Close every thread's cached connections. Optional -- process exit does this too --
        but a long-lived caller (the web server) that wants to drop file handles explicitly can."""
        with self._all_conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            conn.close()
        self._local = threading.local()

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
