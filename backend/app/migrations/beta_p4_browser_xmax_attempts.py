"""P4 browser Xmax attempt snapshots and durable output linkage."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260803_beta_p4_browser_xmax_attempts"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    required = {
        "users",
        "projects",
        "episodes",
        "characters",
        "xmax_thirty_second_tasks",
        "xmax_thirty_second_segments",
        "xmax_temporary_keys",
    }
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(f"P4 requires existing P1/P3/Xmax tables: {', '.join(missing)}")

    conn.execute("SAVEPOINT beta_p4_browser_xmax_attempts")
    try:
        conn.execute(
            """CREATE TABLE xmax_browser_attempts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES xmax_thirty_second_tasks(id) ON DELETE RESTRICT,
                unit_id TEXT NOT NULL REFERENCES xmax_thirty_second_segments(id) ON DELETE RESTRICT,
                source_shot_ids_json TEXT NOT NULL DEFAULT '[]',
                character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE RESTRICT,
                character_asset_version TEXT NOT NULL,
                reference_path TEXT NOT NULL,
                reference_url TEXT NOT NULL,
                reference_sha256 TEXT NOT NULL,
                prompt_template_version TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                playback_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                render_json TEXT NOT NULL,
                temporary_key_record_id TEXT NOT NULL UNIQUE REFERENCES xmax_temporary_keys(id) ON DELETE RESTRICT,
                session_uid TEXT UNIQUE,
                output_id TEXT UNIQUE,
                output_path TEXT,
                output_url TEXT,
                output_duration REAL,
                output_size INTEGER,
                output_mime_type TEXT,
                output_sha256 TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'prepared','session_bound','output_saved','persistence_failed',
                    'failed','aborted','settlement_pending','settled'
                )),
                settlement_status TEXT NOT NULL DEFAULT 'not_started' CHECK (settlement_status IN (
                    'not_started','pending','settled','conflict'
                )),
                consumed_points INTEGER,
                released_points INTEGER,
                last_error_code TEXT,
                last_error_message TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_xmax_browser_attempt_unit_created "
            "ON xmax_browser_attempts(unit_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX idx_xmax_browser_attempt_user_created "
            "ON xmax_browser_attempts(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX idx_xmax_browser_attempt_status "
            "ON xmax_browser_attempts(status, settlement_status, updated_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX uq_xmax_browser_attempt_unsettled_unit "
            "ON xmax_browser_attempts(user_id, unit_id) "
            "WHERE settlement_status IN ('not_started','pending','conflict')"
        )
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (MIGRATION_ID,),
        )
    except Exception:
        conn.execute("ROLLBACK TO beta_p4_browser_xmax_attempts")
        conn.execute("RELEASE beta_p4_browser_xmax_attempts")
        raise
    conn.execute("RELEASE beta_p4_browser_xmax_attempts")
