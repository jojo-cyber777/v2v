"""Add heartbeat columns used by local task recovery."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260721_stage_3c_task_state"


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_stage_3c_task_state_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    _add_columns(db, "ai_tasks", {"heartbeat_at": "TEXT", "idempotency_key": "TEXT"})
    _add_columns(db, "voice_generation_tasks", {"heartbeat_at": "TEXT"})
    _add_columns(db, "voice_segment_outputs", {"heartbeat_at": "TEXT", "idempotency_key": "TEXT"})
    _add_columns(db, "xmax_realtime_batches", {"heartbeat_at": "TEXT"})
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
