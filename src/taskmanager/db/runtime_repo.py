from datetime import UTC, datetime

from taskmanager.core.models import FileLock, Lease
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp


class RuntimeRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def acquire_lease(self, lease: Lease, locks: list[FileLock]) -> None:
        with self.db.get_runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO leases (
                    task_id, agent_id, session_id, account_id,
                    worktree_path, branch_name, acquired_at, last_heartbeat, ttl_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    account_id=excluded.account_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    last_heartbeat=excluded.last_heartbeat,
                    ttl_seconds=excluded.ttl_seconds;
                """,
                (
                    lease.task_id,
                    lease.agent_id,
                    lease.session_id,
                    lease.account_id,
                    lease.worktree_path,
                    lease.branch_name,
                    to_db_timestamp(lease.acquired_at),
                    to_db_timestamp(lease.last_heartbeat),
                    lease.ttl_seconds,
                ),
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
            conn.commit()

    def get_lease(self, task_id: str) -> Lease | None:
        with self.db.get_runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT task_id, agent_id, session_id, account_id,
                       worktree_path, branch_name, acquired_at, last_heartbeat, ttl_seconds
                FROM leases WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not row:
                return None
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
            )

    def heartbeat(self, task_id: str) -> bool:
        now_str = to_db_timestamp(None)
        with self.db.get_runtime_connection() as conn:
            cursor = conn.execute(
                "UPDATE leases SET last_heartbeat = ? WHERE task_id = ?",
                (now_str, task_id),
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
            ttl = int(row[1])
            last_hb = parse_db_datetime(row[2])
            return (datetime.now(tz=UTC) - last_hb).total_seconds() <= ttl

    def get_conflicting_tasks(self, file_paths: list[str]) -> dict[str, str]:
        if not file_paths:
            return {}
        conflicts: dict[str, str] = {}
        now = datetime.now(tz=UTC)
        placeholders = ",".join("?" for _ in file_paths)
        with self.db.get_runtime_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT f.file_path, f.task_id, l.agent_id, l.ttl_seconds, l.last_heartbeat
                FROM file_locks f
                JOIN leases l ON l.task_id = f.task_id
                WHERE f.file_path IN ({placeholders})
                """,
                tuple(file_paths),
            ).fetchall()
            for r in rows:
                path = r[0]
                task_id = r[1]
                agent_id = r[2]
                ttl = int(r[3])
                last_hb = parse_db_datetime(r[4])
                if (now - last_hb).total_seconds() <= ttl:
                    conflicts[path] = f"Task: {task_id}, Agent: {agent_id}"
        return conflicts

    def sweep_expired_leases(self) -> list[str]:
        expired_tasks: list[str] = []
        now = datetime.now(tz=UTC)
        with self.db.get_runtime_connection() as conn:
            rows = conn.execute(
                "SELECT task_id, ttl_seconds, last_heartbeat FROM leases"
            ).fetchall()
            for r in rows:
                task_id = r[0]
                ttl = int(r[1])
                last_hb = parse_db_datetime(r[2])
                if (now - last_hb).total_seconds() > ttl:
                    expired_tasks.append(task_id)
            for t in expired_tasks:
                conn.execute("DELETE FROM leases WHERE task_id = ?", (t,))
                conn.execute("DELETE FROM file_locks WHERE task_id = ?", (t,))
            conn.commit()
        return expired_tasks
