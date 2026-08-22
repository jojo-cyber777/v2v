"""Add persistent startup-gate fields for the browser Xmax shot pipeline."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260722_stage_3b1_xmax_start_gate"


def apply_stage_3b1_xmax_start_gate_migration(db: sqlite3.Connection) -> None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        db.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(xmax_realtime_batches)").fetchall()}
    definitions = {
        "session_id": "TEXT",
        "current_shot": "INTEGER",
        "failed_stage": "TEXT",
        "finished_at": "TEXT",
    }
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE xmax_realtime_batches ADD COLUMN {name} {definition}")
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
