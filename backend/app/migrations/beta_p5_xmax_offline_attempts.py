"""Allow the existing Xmax attempt authority to represent Offline tasks."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260813_beta_p5_xmax_offline_attempts"
TABLE_NAME = "xmax_browser_attempts"
TEMP_TABLE_NAME = "xmax_browser_attempts_offline_migration"


def _columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_NAME,),
    ).fetchone()
    if not table_row or not table_row[0]:
        raise RuntimeError("Xmax Offline attempts require the existing P4 attempt table.")

    columns = _columns(conn, TABLE_NAME)
    if "offline_task_uid" in columns:
        raise RuntimeError("The Offline task column exists without its registered migration.")
    if "temporary_key_record_id" not in columns:
        raise RuntimeError("The P4 temporary-key authority column is missing.")

    original_sql = str(table_row[0])
    nullable_sql = original_sql.replace(
        "temporary_key_record_id TEXT NOT NULL UNIQUE REFERENCES",
        "temporary_key_record_id TEXT UNIQUE REFERENCES",
        1,
    )
    if nullable_sql == original_sql:
        raise RuntimeError("The P4 temporary-key constraint did not match the expected schema.")
    rebuilt_sql = nullable_sql.replace(
        f"CREATE TABLE {TABLE_NAME}",
        f"CREATE TABLE {TEMP_TABLE_NAME}",
        1,
    ).replace(
        "temporary_key_record_id TEXT UNIQUE REFERENCES xmax_temporary_keys(id) ON DELETE RESTRICT,",
        "temporary_key_record_id TEXT UNIQUE REFERENCES xmax_temporary_keys(id) ON DELETE RESTRICT,\n"
        "                offline_task_uid TEXT,",
        1,
    )
    if "offline_task_uid TEXT" not in rebuilt_sql:
        raise RuntimeError("The Offline task UID column could not be added safely.")

    indexes = [
        str(row[0])
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE_NAME,),
        ).fetchall()
    ]
    copy_columns = ",".join(f'"{column}"' for column in columns)

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f'DROP TABLE IF EXISTS "{TEMP_TABLE_NAME}"')
        conn.execute(rebuilt_sql)
        conn.execute(
            f'INSERT INTO "{TEMP_TABLE_NAME}" ({copy_columns}) '
            f'SELECT {copy_columns} FROM "{TABLE_NAME}"'
        )
        conn.execute(f'DROP TABLE "{TABLE_NAME}"')
        conn.execute(f'ALTER TABLE "{TEMP_TABLE_NAME}" RENAME TO "{TABLE_NAME}"')
        for statement in indexes:
            conn.execute(statement)
        conn.execute(
            "CREATE UNIQUE INDEX uq_xmax_browser_attempt_offline_task_uid "
            "ON xmax_browser_attempts(offline_task_uid) "
            "WHERE offline_task_uid IS NOT NULL"
        )
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (MIGRATION_ID,),
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Foreign-key validation failed after the Xmax Offline attempt migration.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
