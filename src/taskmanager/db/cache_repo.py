import hashlib
import json
from datetime import UTC, datetime

from taskmanager.core.models import GateRun
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


class CacheRepository:
    """Results worth reusing across commands: gate baselines per target sha, and condition exit
    codes. Every write commits at once; nothing here joins a `state.db` transaction."""

    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def get_baseline(self, repo: str, sha: str, template_hash: str) -> GateRun | None:
        with self.db.get_cache_connection() as conn:
            row = conn.execute(
                "SELECT exit_code, failing_json, tail FROM gate_baselines "
                "WHERE repo = ? AND target_sha = ? AND template_hash = ?",
                (repo, sha, template_hash),
            ).fetchone()
        if row is None:
            return None
        failing = None if row[1] is None else frozenset(json.loads(row[1]))
        return GateRun(exit_code=row[0], failing=failing, tail=row[2])

    def put_baseline(self, repo: str, sha: str, template_hash: str, run: GateRun) -> None:
        failing = None if run.failing is None else json.dumps(sorted(run.failing))
        with self.db.get_cache_connection() as conn:
            conn.execute(
                """
                INSERT INTO gate_baselines
                    (repo, target_sha, template_hash, exit_code, failing_json, tail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, target_sha, template_hash) DO UPDATE SET
                    exit_code = excluded.exit_code,
                    failing_json = excluded.failing_json,
                    tail = excluded.tail,
                    created_at = excluded.created_at
                """,
                (repo, sha, template_hash, run.exit_code, failing, run.tail, to_db_timestamp(None)),
            )
            conn.commit()

    def get_condition(self, node_id: str, idx: int, command: str, max_age: int) -> int | None:
        """The exit code last recorded for this condition, while it is at most `max_age` seconds
        old and was recorded for the same command; None otherwise."""
        with self.db.get_cache_connection() as conn:
            row = conn.execute(
                "SELECT exit_code, checked_at FROM condition_results "
                "WHERE node_id = ? AND idx = ? AND command_hash = ?",
                (node_id, idx, _command_hash(command)),
            ).fetchone()
        if row is None:
            return None
        age = (datetime.now(tz=UTC) - parse_db_datetime(row[1])).total_seconds()
        return int(row[0]) if age <= max_age else None

    def put_condition(self, node_id: str, idx: int, command: str, exit_code: int) -> None:
        with self.db.get_cache_connection() as conn:
            conn.execute(
                """
                INSERT INTO condition_results (node_id, idx, command_hash, exit_code, checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id, idx) DO UPDATE SET
                    command_hash = excluded.command_hash,
                    exit_code = excluded.exit_code,
                    checked_at = excluded.checked_at
                """,
                (node_id, idx, _command_hash(command), exit_code, to_db_timestamp(None)),
            )
            conn.commit()
