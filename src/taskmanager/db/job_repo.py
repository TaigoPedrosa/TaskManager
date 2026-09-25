import json
import uuid
from typing import Any

from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_COLUMNS = "id, kind, node_id, repo, target, state, step, worktree, pid, heartbeat, result_json"


def _row_to_job(row: tuple[Any, ...]) -> Job:
    return Job(
        id=row[0],
        kind=JobKind(row[1]),
        node_id=row[2],
        repo=row[3],
        target=row[4],
        state=JobState(row[5]),
        step=row[6],
        worktree=row[7],
        pid=row[8],
        heartbeat=parse_db_datetime(row[9]),
        result=json.loads(row[10]),
    )


class JobRepository:
    """Landing and sync jobs, and the branch locks their compare-and-swap runs under. Both live in
    `state.db`, so a job row and the node status it moves commit together."""

    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def create(self, job: Job) -> Job:
        created = job.model_copy(update={"id": job.id or f"{job.kind}-{uuid.uuid4().hex[:12]}"})
        with self.db.get_state_connection() as conn:
            conn.execute(
                f"INSERT INTO jobs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    created.id,
                    created.kind.value,
                    created.node_id,
                    created.repo,
                    created.target,
                    created.state.value,
                    created.step,
                    created.worktree,
                    created.pid,
                    to_db_timestamp(created.heartbeat),
                    json.dumps(created.result),
                ),
            )
            self.db.spec_commit(conn)
        return created

    def get(self, job_id: str) -> Job | None:
        with self.db.get_state_connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def for_node(self, node_id: str) -> list[Job]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE node_id = ? ORDER BY rowid ASC", (node_id,)
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def update(self, job: Job) -> None:
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET state = ?, step = ?, worktree = ?, pid = ?, heartbeat = ?,
                    result_json = ?
                WHERE id = ?
                """,
                (
                    job.state.value,
                    job.step,
                    job.worktree,
                    job.pid,
                    to_db_timestamp(job.heartbeat),
                    json.dumps(job.result),
                    job.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no job '{job.id}'")
            self.db.spec_commit(conn)

    def waiting_for_agent(self) -> list[Job]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE state = ? ORDER BY rowid ASC",
                (JobState.NEEDS_AGENT.value,),
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def acquire_branch(self, repo: str, branch: str, holder: str) -> bool:
        """Take `branch`'s lock for the job `holder`. A lock held by a job that is no longer
        running is stale, since only a running job moves a branch, and is taken over."""
        with self.db.get_state_connection() as conn:
            conn.execute(
                "DELETE FROM branch_locks WHERE repo = ? AND branch = ? AND holder <> ? "
                "AND holder NOT IN (SELECT id FROM jobs WHERE state = ?)",
                (repo, branch, holder, JobState.RUNNING.value),
            )
            conn.execute(
                "INSERT INTO branch_locks (repo, branch, holder, heartbeat) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(repo, branch) DO UPDATE SET heartbeat = excluded.heartbeat"
                " WHERE branch_locks.holder = excluded.holder",
                (repo, branch, holder, to_db_timestamp(None)),
            )
            (held_by,) = conn.execute(
                "SELECT holder FROM branch_locks WHERE repo = ? AND branch = ?", (repo, branch)
            ).fetchone()
            self.db.spec_commit(conn)
            return bool(held_by == holder)

    def release_branch(self, repo: str, branch: str, holder: str) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute(
                "DELETE FROM branch_locks WHERE repo = ? AND branch = ? AND holder = ?",
                (repo, branch, holder),
            )
            self.db.spec_commit(conn)
