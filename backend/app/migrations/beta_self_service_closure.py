"""Formal Unit selection and versioned final-output persistence for the beta."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260804_beta_self_service_closure"


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
        "xmax_thirty_second_tasks",
        "xmax_thirty_second_segments",
        "xmax_browser_attempts",
    }
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(f"Self-service closure requires existing P1-P4 tables: {', '.join(missing)}")

    conn.execute("SAVEPOINT beta_self_service_closure")
    try:
        conn.execute(
            """CREATE TABLE episode_unit_selections (
                unit_id TEXT PRIMARY KEY REFERENCES xmax_thirty_second_segments(id) ON DELETE RESTRICT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES xmax_thirty_second_tasks(id) ON DELETE RESTRICT,
                selected_source_type TEXT NOT NULL CHECK (
                    selected_source_type IN ('generated_attempt','original')
                ),
                selected_attempt_id TEXT REFERENCES xmax_browser_attempts(id) ON DELETE RESTRICT,
                selected_at TEXT NOT NULL,
                selected_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                CHECK (
                    (selected_source_type='original' AND selected_attempt_id IS NULL)
                    OR
                    (selected_source_type='generated_attempt' AND selected_attempt_id IS NOT NULL)
                )
            )"""
        )
        conn.execute(
            "CREATE INDEX idx_episode_unit_selections_episode "
            "ON episode_unit_selections(user_id, episode_id, task_id)"
        )
        conn.execute(
            """CREATE TABLE episode_final_outputs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
                task_id TEXT NOT NULL REFERENCES xmax_thirty_second_tasks(id) ON DELETE RESTRICT,
                status TEXT NOT NULL CHECK (status IN ('rendering','ready','failed')),
                output_path TEXT,
                output_url TEXT,
                output_sha256 TEXT,
                file_size INTEGER,
                duration REAL,
                width INTEGER,
                height INTEGER,
                fps REAL,
                video_codec TEXT,
                audio_codec TEXT,
                unit_selection_snapshot_json TEXT NOT NULL,
                audio_output_id TEXT NOT NULL,
                audio_revision TEXT NOT NULL,
                subtitle_revision TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX idx_episode_final_outputs_episode_created "
            "ON episode_final_outputs(user_id, episode_id, created_at DESC)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (MIGRATION_ID,),
        )
    except Exception:
        conn.execute("ROLLBACK TO beta_self_service_closure")
        conn.execute("RELEASE beta_self_service_closure")
        raise
    conn.execute("RELEASE beta_self_service_closure")
