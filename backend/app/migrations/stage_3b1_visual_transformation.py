"""Additive schema for the Stage 3 B1 real visual transformation PoC."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260718_stage_3b1_visual_transformation_poc"


def apply_stage_3b1_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS visual_transformation_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            provider_task_id TEXT,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            target_style TEXT NOT NULL,
            visual_scope TEXT NOT NULL DEFAULT 'full-scene',
            prompt_summary TEXT NOT NULL DEFAULT '',
            prompt_text TEXT NOT NULL DEFAULT '',
            reference_character_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER,
            estimated_credits REAL,
            estimated_cost_usd REAL,
            actual_credits REAL,
            actual_cost_usd REAL,
            error_code TEXT,
            error_message TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_visual_tasks_episode ON visual_transformation_tasks(episode_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_visual_tasks_provider ON visual_transformation_tasks(provider_task_id);

        CREATE TABLE IF NOT EXISTS visual_transformation_inputs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES visual_transformation_tasks(id) ON DELETE CASCADE,
            source_video_path TEXT NOT NULL,
            clip_path TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            fps REAL,
            aspect_ratio REAL NOT NULL,
            orientation TEXT NOT NULL,
            file_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS visual_transformation_outputs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES visual_transformation_tasks(id) ON DELETE CASCADE,
            output_url TEXT,
            local_output_path TEXT,
            output_duration REAL,
            width INTEGER,
            height INTEGER,
            aspect_ratio REAL,
            file_size INTEGER,
            output_hash TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS visual_quality_reviews (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE REFERENCES visual_transformation_tasks(id) ON DELETE CASCADE,
            identity_score INTEGER,
            motion_score INTEGER,
            background_score INTEGER,
            clothing_score INTEGER,
            flicker_score INTEGER,
            style_score INTEGER,
            decision TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
