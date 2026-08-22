"""Persist the Xmax session confirmation and stop audit fields."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260722_stage_3b1_xmax_session_confirmation"


def apply_stage_3b1_xmax_session_confirmation_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(xmax_realtime_batches)").fetchall()}
    definitions = {
        "session_created_at": "TEXT",
        "started_at": "TEXT",
        "stop_requested_at": "TEXT",
        "stop_reason": "TEXT",
        "active_confirmation_source": "TEXT",
    }
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE xmax_realtime_batches ADD COLUMN {name} {definition}")
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
