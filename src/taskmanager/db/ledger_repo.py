import json
import logging
import sqlite3

from taskmanager.core.models import LedgerEvent
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp


class LedgerRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def append(self, event: LedgerEvent) -> None:
        self.db.after_commit(lambda: self._write(event))

    def _write(self, event: LedgerEvent) -> None:
        # The ledger records a write that has already committed, so failing here would report
        # that write as failed and drop every entry queued after this one.
        try:
            self._insert(event)
        except (sqlite3.Error, OSError) as exc:
            logging.getLogger(__name__).warning("ledger entry %s lost: %s", event.command, exc)

    def _insert(self, event: LedgerEvent) -> None:
        now_str = to_db_timestamp(event.timestamp)
        with self.db.get_ledger_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO ledger_events (
                    timestamp, actor_id, command, target_id, payload_json, diff_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_str,
                    event.actor_id,
                    event.command,
                    event.target_id,
                    json.dumps(event.payload),
                    json.dumps(event.diff),
                ),
            )
            conn.commit()
            if event.id is None and cursor.lastrowid is not None:
                event.id = cursor.lastrowid

    def list_events(self, target_id: str | None = None, limit: int = 50) -> list[LedgerEvent]:
        with self.db.get_ledger_connection() as conn:
            if target_id is not None:
                rows = conn.execute(
                    """
                    SELECT id, timestamp, actor_id, command, target_id, payload_json, diff_json
                    FROM ledger_events
                    WHERE target_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (target_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, timestamp, actor_id, command, target_id, payload_json, diff_json
                    FROM ledger_events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [
                LedgerEvent(
                    id=r[0],
                    timestamp=parse_db_datetime(r[1]),
                    actor_id=r[2],
                    command=r[3],
                    target_id=r[4],
                    payload=json.loads(r[5]),
                    diff=json.loads(r[6]),
                )
                for r in rows
            ]
