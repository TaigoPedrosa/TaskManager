import sqlite3
from pathlib import Path

import pytest

from taskmanager.db.connection import DatabaseManager


def test_wal_mode_and_pragmas(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path)
    db_mgr.init_all()

    with db_mgr.get_state_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert journal_mode == "wal"
        assert busy_timeout == 5000
        assert foreign_keys == 1

        vec_ver = conn.execute("SELECT vec_version();").fetchone()
        assert vec_ver is not None
        assert vec_ver[0].startswith("v")

    with db_mgr.get_state_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert journal_mode == "wal"
        assert busy_timeout == 5000
        assert foreign_keys == 1

    with db_mgr.get_ledger_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert journal_mode == "wal"
        assert busy_timeout == 5000
        assert foreign_keys == 1


def test_vec_nodes_virtual_table(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path)
    dimensions = 4
    db_mgr.init_all(vector_dimensions=dimensions)

    with db_mgr.get_state_connection() as conn:
        conn.execute(
            """
            INSERT INTO vec_nodes (node_id, target_type, section_key, embedding)
            VALUES (?, ?, ?, ?)
            """,
            ("SPEC-01", "spec", "overview", "[0.1, 0.2, 0.3, 0.4]"),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT node_id, target_type, section_key, distance
            FROM vec_nodes
            WHERE embedding MATCH '[0.1, 0.2, 0.3, 0.4]'
            ORDER BY distance
            LIMIT 1
            """
        ).fetchone()

        assert row is not None
        assert row[0] == "SPEC-01"
        assert row[1] == "spec"
        assert row[2] == "overview"
        assert pytest.approx(row[3], abs=1e-4) == 0.0


def test_fts5_virtual_table(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path)
    db_mgr.init_all()

    with db_mgr.get_state_connection() as conn:
        conn.execute(
            """
            INSERT INTO nodes_fts (node_id, title, frontmatter_text, content_text)
            VALUES (?, ?, ?, ?)
            """,
            (
                "TASK-01",
                "Implement authentication",
                '{"type": "backend"}',
                "JWT bearer token authorization",
            ),
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT node_id, title
            FROM nodes_fts
            WHERE nodes_fts MATCH 'authentication'
            """
        ).fetchone()
        assert row is not None
        assert row[0] == "TASK-01"
        assert row[1] == "Implement authentication"


def test_foreign_key_cascade_deletion(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path)
    db_mgr.init_all()

    with db_mgr.get_state_connection() as conn:
        conn.execute(
            """
            INSERT INTO nodes (id, kind, title, status)
            VALUES (?, ?, ?, ?)
            """,
            ("AUTH-01", "task", "Auth Task", "READY"),
        )
        conn.execute(
            """
            INSERT INTO node_sections (node_id, section_key, ordinal, header, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("AUTH-01", "details", 1, "Details", "Section content"),
        )
        conn.commit()

        sec_row = conn.execute(
            "SELECT * FROM node_sections WHERE node_id = ?", ("AUTH-01",)
        ).fetchone()
        assert sec_row is not None

        conn.execute("DELETE FROM nodes WHERE id = ?", ("AUTH-01",))
        conn.commit()

        sec_after = conn.execute(
            "SELECT * FROM node_sections WHERE node_id = ?", ("AUTH-01",)
        ).fetchone()
        assert sec_after is None

    with db_mgr.get_state_connection() as conn:
        conn.execute(
            """
            INSERT INTO leases (task_id, agent_id, session_id, branch_name, token)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("TASK-01", "agent-1", "sess-1", "feat/auth", "token-1"),
        )
        conn.execute(
            """
            INSERT INTO file_locks (file_path, task_id, lock_type)
            VALUES (?, ?, ?)
            """,
            ("src/auth.py", "TASK-01", "write"),
        )
        conn.commit()

        lock_row = conn.execute(
            "SELECT * FROM file_locks WHERE task_id = ?", ("TASK-01",)
        ).fetchone()
        assert lock_row is not None

        conn.execute("DELETE FROM leases WHERE task_id = ?", ("TASK-01",))
        conn.commit()

        lock_after = conn.execute(
            "SELECT * FROM file_locks WHERE task_id = ?", ("TASK-01",)
        ).fetchone()
        assert lock_after is None


def test_foreign_key_violation_raises(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path)
    db_mgr.init_all()

    with db_mgr.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
                INSERT INTO node_sections (node_id, section_key, ordinal, header, content)
                VALUES (?, ?, ?, ?, ?)
                """,
            ("NONEXISTENT", "details", 1, "Details", "Section content"),
        )
