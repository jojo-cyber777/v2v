"""Additive task structure for full-video B1 processing and recovery."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260718_stage_3b1_workflow"


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_stage_3b1_workflow_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    _add_columns(db, "visual_transformation_tasks", {
        "prompt_summary_zh": "TEXT NOT NULL DEFAULT ''",
        "total_segments": "INTEGER NOT NULL DEFAULT 1",
        "completed_segments": "INTEGER NOT NULL DEFAULT 0",
        "skipped_segments": "INTEGER NOT NULL DEFAULT 0",
    })
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS visual_transformation_segments (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES visual_transformation_tasks(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_task_id TEXT,
            input_path TEXT,
            output_path TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_visual_segments_task ON visual_transformation_segments(task_id, sequence);
        """
    )
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
