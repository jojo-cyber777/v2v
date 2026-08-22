"""Persistent local state for the browser Xmax shot pipeline."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260721_stage_3b1_xmax_realtime"


def apply_stage_3b1_xmax_realtime_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS xmax_realtime_batches (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'preparing',
            reference_character_id TEXT,
            prompt TEXT NOT NULL,
            total_shots INTEGER NOT NULL DEFAULT 0,
            completed_shots INTEGER NOT NULL DEFAULT 0,
            processed_seconds REAL NOT NULL DEFAULT 0,
            estimated_credits REAL NOT NULL DEFAULT 0,
            actual_credits REAL,
            output_path TEXT,
            output_duration REAL,
            output_file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS xmax_realtime_shots (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES xmax_realtime_batches(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            source_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            output_path TEXT,
            output_duration REAL,
            file_size INTEGER,
            processed_seconds REAL NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_xmax_realtime_batches_episode
        ON xmax_realtime_batches(episode_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_xmax_realtime_shots_batch
        ON xmax_realtime_shots(batch_id, sequence);
        """
    )
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
