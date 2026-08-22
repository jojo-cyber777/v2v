"""Persistent, redacted audit records for browser-owned Xmax attempts."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260722_stage_3b1_xmax_attempt_audit"


def apply_stage_3b1_xmax_attempt_audit_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS xmax_realtime_attempts (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            batch_id TEXT REFERENCES xmax_realtime_batches(id) ON DELETE SET NULL,
            shot_id TEXT REFERENCES xmax_realtime_shots(id) ON DELETE SET NULL,
            request_start_at TEXT NOT NULL,
            request_end_at TEXT,
            session_id TEXT,
            provider_request_method TEXT,
            provider_request_url TEXT,
            provider_http_status INTEGER,
            provider_response_body TEXT,
            provider_error_name TEXT,
            provider_error_code TEXT,
            provider_error_message TEXT,
            provider_error_stack_head TEXT,
            processed_seconds REAL NOT NULL DEFAULT 0,
            billed_credits REAL,
            estimated_credits REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'prepared',
            stop_reason TEXT,
            page_hidden INTEGER NOT NULL DEFAULT 0,
            tab_hidden INTEGER NOT NULL DEFAULT 0,
            unload INTEGER NOT NULL DEFAULT 0,
            output_stream_received INTEGER NOT NULL DEFAULT 0,
            output_file_received INTEGER NOT NULL DEFAULT 0,
            output_file_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_xmax_attempts_episode
        ON xmax_realtime_attempts(episode_id, request_start_at);
        CREATE INDEX IF NOT EXISTS idx_xmax_attempts_batch
        ON xmax_realtime_attempts(batch_id, request_start_at);
        """
    )
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
