import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

from taskmanager.core.enums import LockType
from taskmanager.core.models import FileLock, Lease, LeaseAction, Node
from taskmanager.core.status import Action, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_LEASE_COLUMNS = (
    "task_id, agent_id, session_id, account_id, worktree_path, branch_name, acquired_at, "
    "last_heartbeat, ttl_seconds, action, review_hash, model, token"
)
_INSERT_LEASE = (
    f"INSERT INTO leases ({_LEASE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def lease_alive(ttl: int | None, last_heartbeat: datetime, now: datetime) -> bool:
    # A null TTL is a landing job stopped for an agent: its lease holds until the agent resumes.
    return ttl is None or (now - last_heartbeat).total_seconds() <= ttl


def _lease_row(lease: Lease) -> tuple[object, ...]:
    return (
        lease.task_id,
        lease.agent_id,
        lease.session_id,
        lease.account_id,
        lease.worktree_path,
        lease.branch_name,
        to_db_timestamp(lease.acquired_at),
        to_db_timestamp(lease.last_heartbeat),
        lease.ttl_seconds,
        lease.action.value if lease.action else None,
        lease.review_hash,
        lease.model,
        lease.token,
    )


def _row_to_lease(row: tuple[Any, ...]) -> Lease:
    return Lease(
        task_id=row[0],
        agent_id=row[1],
        session_id=row[2],
        account_id=row[3],
        worktree_path=row[4],
        branch_name=row[5],
        acquired_at=parse_db_datetime(row[6]),
        last_heartbeat=parse_db_datetime(row[7]),
        ttl_seconds=row[8],
        # the CHECK constraint on `leases.action` already excludes 'blocked'
        action=cast(LeaseAction, Action(row[9])) if row[9] else None,
        review_hash=row[10],
        model=row[11],
        token=row[12],
    )


class RuntimeRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def acquire_lease(self, lease: Lease, locks: list[FileLock]) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute(
                f"""
                {_INSERT_LEASE}
                ON CONFLICT(task_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    account_id=excluded.account_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    last_heartbeat=excluded.last_heartbeat,
                    ttl_seconds=excluded.ttl_seconds,
                    action=excluded.action,
                    review_hash=excluded.review_hash,
                    model=excluded.model,
                    token=excluded.token;
                """,
                _lease_row(lease),
            )
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (lease.task_id,))
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
            self.db.spec_commit(conn)

    def claim(
        self, lease: Lease, locks: list[FileLock], node: Node, expected: Status | None = None
    ) -> bool:
        """Take `node` from the stored status `expected` (by default its `claimed_from`) to the
        status it carries, with its lease and file locks, in one `BEGIN IMMEDIATE` transaction.
        A hold that leaves the node where it is names its own status as `expected`.

        False, with nothing written, when the stored status moved, a lease row exists for the
        node, or a lock row exists for one of the files. An expired lease still holds its rows
        until a sweep removes them, so callers sweep first.
        """
        expected = expected or node.claimed_from
        if expected is None:
            raise ValueError(f"a claim of '{node.id}' names the status it is claimed from")
        node.checked()
        with self.db.get_state_connection() as conn:
            if self.db.in_transaction or conn.in_transaction:
                raise RuntimeError("a claim is its own transaction and cannot join an open one")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM nodes WHERE id = ?", (node.id,)).fetchone()
                if row is None or row[0] != expected.value:
                    conn.rollback()
                    return False
                conn.execute(_INSERT_LEASE, _lease_row(lease))
                conn.executemany(
                    "INSERT INTO file_locks (file_path, task_id, lock_type) VALUES (?, ?, ?)",
                    [(lock.file_path, lock.task_id, lock.lock_type.value) for lock in locks],
                )
                conn.execute(
                    """
                    UPDATE nodes SET status = ?, claimed_from = ?, outcome = ?, fix_for = ?,
                        review_cycles = ?, merge_attempts = ?, step_failures = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        node.status.value,
                        node.claimed_from.value if node.claimed_from else None,
                        node.outcome.value if node.outcome else None,
                        node.fix_for.value if node.fix_for else None,
                        node.review_cycles,
                        node.merge_attempts,
                        node.step_failures,
                        to_db_timestamp(None),
                        node.id,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return True

    def set_worktree(self, task_id: str, token: str, path: str) -> None:
        """Only the claim named by `token`: a lease released or taken over meanwhile is left as
        its new holder wrote it."""
        with self.db.get_state_connection() as conn:
            conn.execute(
                "UPDATE leases SET worktree_path = ? WHERE task_id = ? AND token = ?",
                (path, task_id, token),
            )
            self.db.spec_commit(conn)

    def get_lease(self, task_id: str) -> Lease | None:
        with self.db.get_state_connection() as conn:
            row = conn.execute(
                f"SELECT {_LEASE_COLUMNS} FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_lease(row) if row else None

    def list_leases(self) -> list[Lease]:
        """Every lease row, expired ones included: a caller that counts live leases sweeps or
        checks `lease_alive` first."""
        with self.db.get_state_connection() as conn:
            rows = conn.execute(f"SELECT {_LEASE_COLUMNS} FROM leases ORDER BY task_id").fetchall()
        return [_row_to_lease(r) for r in rows]

    def list_locks(self) -> list[FileLock]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT file_path, task_id, lock_type FROM file_locks ORDER BY file_path"
            ).fetchall()
        return [FileLock(file_path=r[0], task_id=r[1], lock_type=LockType(r[2])) for r in rows]

    def park(self, node_id: str) -> None:
        """A job stopped for an agent keeps its lease and locks with no TTL, so no sweep frees
        the worktree it still owns."""
        with self.db.get_state_connection() as conn:
            conn.execute("UPDATE leases SET ttl_seconds = NULL WHERE task_id = ?", (node_id,))
            self.db.spec_commit(conn)

    def take_over(
        self, node_id: str, agent: str, session: str, ttl: int, model: str, token: str
    ) -> bool:
        """Hand a parked lease to `agent` under a new claim `token`, in one conditional write. A
        release followed by a new claim would let two agents each believe they hold the stopped
        job; here only the first writer finds the TTL still NULL."""
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE leases SET agent_id = ?, session_id = ?, ttl_seconds = ?, model = ?,
                    last_heartbeat = ?, token = ?
                WHERE task_id = ? AND ttl_seconds IS NULL
                """,
                (agent, session, ttl, model, to_db_timestamp(None), token, node_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def heartbeat(self, task_id: str) -> bool:
        now_str = to_db_timestamp(None)
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                "UPDATE leases SET last_heartbeat = ? WHERE task_id = ?",
                (now_str, task_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def release_lease(self, task_id: str) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            self.db.spec_commit(conn)

    def is_file_locked(self, file_path: str) -> bool:
        return bool(self.get_conflicting_tasks([file_path]))

    def get_conflicting_tasks(self, file_paths: list[str]) -> dict[str, str]:
        if not file_paths:
            return {}
        now = datetime.now(tz=UTC)
        placeholders = ",".join("?" for _ in file_paths)
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT f.file_path, f.task_id, l.agent_id, l.ttl_seconds, l.last_heartbeat
                FROM file_locks f
                JOIN leases l ON l.task_id = f.task_id
                WHERE f.file_path IN ({placeholders})
                """,
                tuple(file_paths),
            ).fetchall()
        return {
            path: f"Task: {task_id}, Agent: {agent_id}"
            for path, task_id, agent_id, ttl, last_hb in rows
            if lease_alive(ttl, parse_db_datetime(last_hb), now)
        }

    def sweep_expired_leases(self) -> list[str]:
        now = datetime.now(tz=UTC)
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT task_id, ttl_seconds, last_heartbeat FROM leases "
                "WHERE ttl_seconds IS NOT NULL"
            ).fetchall()
            expired = [
                task_id
                for task_id, ttl, last_hb in rows
                if not lease_alive(ttl, parse_db_datetime(last_hb), now)
            ]
            for task_id in expired:
                conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            self.db.spec_commit(conn)
        return expired
