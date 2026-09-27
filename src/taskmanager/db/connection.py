import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

import taskmanager
from taskmanager.db.schema import (
    CACHE_SCHEMA_SQL,
    LEDGER_SCHEMA_SQL,
    NODE_REV_TRIGGERS_SQL,
    SCHEMA_VERSION,
    STATE_MIGRATIONS,
    STATE_SCHEMA_SQL,
    STATE_SCHEMA_VERSION,
    vec_nodes_sql,
)

_SQLITE_HEADER = b"SQLite format 3\x00"
# The database files a pre-lifecycle tm kept; an archive moves each with its WAL and shared memory.
_LEGACY_FILES = ("spec.db", "runtime.db", "ledger.db")
# Written as plain text where a pre-lifecycle tm looks for its databases, so it fails with "file is
# not a database" instead of silently creating an empty estate beside this one. `ledger.db` is
# tombstoned too: a pre-lifecycle `tm audit list` opens it directly by that name, and this estate's
# own ledger lives at `audit.db` instead.
_TOMBSTONES = ("spec.db", "runtime.db", "ledger.db")

PRE_LIFECYCLE_MESSAGE = (
    "this directory holds a pre-lifecycle estate: run `tm init --archive` to move it to "
    "`.taskmanager/archive-<timestamp>/` and start fresh, then re-import the ongoing work"
)


class PreLifecycleEstate(Exception):
    def __init__(self) -> None:
        super().__init__(PRE_LIFECYCLE_MESSAGE)


class StateSchemaTooNew(Exception):
    def __init__(self, found: int) -> None:
        super().__init__(
            f"state.db is schema {found}, newer than this tm ({STATE_SCHEMA_VERSION}): upgrade tm"
        )


def _statements(script: str) -> list[str]:
    """A migration's own statements, in order, split by `sqlite3.complete_statement`
    (`sqlite3_complete()`), which reads a trigger body's own semicolons correctly rather than
    ending the statement at the first one."""
    statements = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if buf.strip() and sqlite3.complete_statement(buf):
            statements.append(buf)
            buf = ""
    assert not buf.strip(), f"incomplete statement left over: {buf!r}"
    return statements


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


class DatabaseManager:
    def __init__(self, taskmanager_dir: Path) -> None:
        self.taskmanager_dir = taskmanager_dir
        self.dir = taskmanager_dir
        self.state_db = taskmanager_dir / "state.db"
        # Not "ledger.db": that name is tombstoned so a pre-lifecycle tm's `tm audit list`
        # fails loudly instead of reading this database as its own.
        self.ledger_db = taskmanager_dir / "audit.db"
        self.cache_db = taskmanager_dir / "cache.db"
        # One connection per database *per thread*, reused for that thread's lifetime rather
        # than reopened on every get_*_connection() call (reopening, with its extension load,
        # dominated web load time). Per thread because the web server runs its database routes
        # in a threadpool: one shared connection interleaves concurrent cursors, so a
        # fetchone() returns another request's row or None. Each connection sees every other
        # connection's commits through WAL, so this changes lifecycle only, not isolation.
        self._local = threading.local()
        # Every connection with the thread that owns it. The web server's worker threads retire
        # after a few idle seconds, and a retired thread's thread-local dict is only dropped when
        # the garbage collector gets to it, so its connection would hold the database's file
        # descriptors open indefinitely: one leaked set per retired thread, until the process
        # hit its fd limit and every new thread failed with "unable to open database file".
        # Closing the dead threads' connections before opening a new one keeps the open set
        # bounded by the threads alive at once.
        self._all_conns: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._all_conns_lock = threading.Lock()

    def is_pre_lifecycle(self) -> bool:
        return not self.state_db.exists() and _is_sqlite(self.taskmanager_dir / "spec.db")

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        if self.is_pre_lifecycle():
            raise PreLifecycleEstate()
        self.taskmanager_dir.mkdir(parents=True, exist_ok=True)
        self._close_dead_threads_connections()
        # check_same_thread=False only so a dead thread's connection, and close(), can be closed
        # from whichever thread gets there; no connection is ever used by a thread other than
        # its own.
        conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000;")
        if db_path == self.state_db:
            try:
                self._migrate_state(conn)
            except BaseException:
                conn.close()
                raise
        # Only after the migration: a table rebuild drops `nodes`, and under enforced foreign keys
        # that drop cascades into every table referencing it. SQLite ignores this pragma inside a
        # transaction, so it cannot be switched off around the migration instead.
        conn.execute("PRAGMA foreign_keys = ON;")
        # After any refusal above: turning WAL on rewrites the file's header even for a
        # database this call is about to refuse outright, and a refusal must leave it untouched.
        conn.execute("PRAGMA journal_mode = WAL;")
        if load_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        with self._all_conns_lock:
            self._all_conns.append((threading.current_thread(), conn))
        return conn

    def _migrate_state(self, conn: sqlite3.Connection) -> None:
        """The first `state.db` connection this process opens brings it to
        `STATE_SCHEMA_VERSION`, or refuses it. `BEGIN IMMEDIATE` takes SQLite's write lock before
        the real check, so a second process racing this one blocks here instead of migrating
        twice: it re-reads `user_version` once the lock is its own and finds nothing left to do.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == STATE_SCHEMA_VERSION:
            return
        if version > STATE_SCHEMA_VERSION:
            raise StateSchemaTooNew(version)
        has_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
        if not has_nodes:
            return  # uninitialised directory: `tm init` creates it at the current version
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > STATE_SCHEMA_VERSION:
                raise StateSchemaTooNew(version)
            for target in range(version + 1, STATE_SCHEMA_VERSION + 1):
                # Not `executescript`: it commits whatever transaction is already open before
                # it runs a single statement, which would drop the lock above and let a second
                # connection start migrating too.
                for statement in _statements(STATE_MIGRATIONS[target]):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _close_dead_threads_connections(self) -> None:
        dead: list[tuple[threading.Thread, sqlite3.Connection]] = []
        with self._all_conns_lock:
            alive, self._all_conns = self._all_conns, []
            for owner, conn in alive:
                (self._all_conns if owner.is_alive() else dead).append((owner, conn))
        for _, conn in dead:
            conn.close()

    def _thread_conn(self, name: str, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, name, None)
        if conn is None:
            conn = self._create_connection(db_path, load_vec=load_vec)
            setattr(self._local, name, conn)
        return conn

    @property
    def _spec_tx_depth(self) -> int:
        """>0 while this thread holds `spec_transaction()` open: its repository calls skip
        their own commit, so a run of writes lands as one commit or none. Per thread, like the
        connection, so another thread's writes keep committing."""
        return int(getattr(self._local, "spec_tx_depth", 0))

    @_spec_tx_depth.setter
    def _spec_tx_depth(self, value: int) -> None:
        self._local.spec_tx_depth = value

    @property
    def in_transaction(self) -> bool:
        return self._spec_tx_depth > 0

    @contextmanager
    def get_state_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("state", self.state_db, load_vec=True)

    def spec_commit(self, conn: sqlite3.Connection) -> None:
        """The commit every repository write ends with -- except while `spec_transaction()`
        is open, where the caller wants one commit (or one rollback) for the whole run rather
        than one per write, so a later write failing does not leave an earlier one visible."""
        if self._spec_tx_depth == 0:
            conn.commit()

    def after_commit(self, action: Callable[[], None]) -> None:
        """Runs `action` once this thread's open `spec_transaction()` commits, and never if it
        rolls back; at once outside one. The ledger is another database, so an entry written
        inside a write that is then refused would otherwise outlive it."""
        if self._spec_tx_depth == 0:
            action()
        else:
            self._local.after_commit.append(action)

    @contextmanager
    def spec_transaction(self) -> Generator[sqlite3.Connection]:
        with self.get_state_connection() as conn:
            if self._spec_tx_depth == 0:
                self._local.after_commit = []
                if not conn.in_transaction:
                    # Reads inside the transaction decide its writes, so they run under the
                    # write lock: a claim cannot commit between a writer's read and its write.
                    conn.execute("BEGIN IMMEDIATE")
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
            if self._spec_tx_depth == 0:
                pending, self._local.after_commit = self._local.after_commit, []
                for action in pending:
                    action()

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("ledger", self.ledger_db)

    @contextmanager
    def get_cache_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("cache", self.cache_db)

    def close(self) -> None:
        """Close every thread's cached connections. Optional -- process exit does this too --
        but a long-lived caller (the web server) that wants to drop file handles explicitly can."""
        with self._all_conns_lock:
            conns, self._all_conns = self._all_conns, []
        for _, conn in conns:
            conn.close()
        self._local = threading.local()

    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_state_connection() as conn:
            conn.executescript(STATE_SCHEMA_SQL)
            conn.executescript(NODE_REV_TRIGGERS_SQL)
            conn.execute(vec_nodes_sql(vector_dimensions))
            conn.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
            conn.commit()

        with self.get_ledger_connection() as conn:
            conn.executescript(LEDGER_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        with self.get_cache_connection() as conn:
            conn.executescript(CACHE_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        tombstone = (
            f"taskmanager {taskmanager.__version__} owns this directory; its estate is in "
            "state.db. This file is not a database, so an older tm fails here instead of "
            "opening an empty estate.\n"
        )
        for name in _TOMBSTONES:
            (self.taskmanager_dir / name).write_text(tombstone, encoding="utf-8")

    def is_initialized(self) -> bool:
        if not self.state_db.exists():
            return False
        try:
            with self.get_state_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
                ).fetchone()
                return bool(row)
        except sqlite3.Error, OSError:
            return False

    @staticmethod
    def archive_pre_lifecycle(root: Path) -> Path:
        """Move a pre-lifecycle estate's databases under `.taskmanager/archive-<timestamp>/`,
        untouched, so `init_all` can start a fresh one. Nothing is ever deleted."""
        tm_dir = root / ".taskmanager"
        if not DatabaseManager(tm_dir).is_pre_lifecycle():
            raise ValueError(f"{tm_dir} holds no pre-lifecycle estate to archive")
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archive = tm_dir / f"archive-{stamp}"
        archive.mkdir()
        for name in _LEGACY_FILES:
            for suffix in ("", "-wal", "-shm"):
                src = tm_dir / f"{name}{suffix}"
                if src.exists():
                    src.rename(archive / src.name)
        return archive
