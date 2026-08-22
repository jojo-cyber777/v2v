"""Small additive fields for resumable A2 tasks."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260718_stage_3a_workflow_recovery"


def apply_stage_3a_workflow_recovery(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(voice_generation_tasks)").fetchall()}
    for name, definition in {
        "control_requested": "TEXT NOT NULL DEFAULT ''",
        "last_heartbeat": "TEXT",
        "segment_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if name not in columns:
            db.execute(f"ALTER TABLE voice_generation_tasks ADD COLUMN {name} {definition}")
    db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
