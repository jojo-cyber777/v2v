"""Additive schema migration for Stage 3 A2-1 real dubbing and MVP mixing."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260716_stage_3a2_1_real_dubbing_mix"


def apply_stage_3a2_1_migration(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS voice_catalog_cache (
            voice_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            labels_json TEXT NOT NULL DEFAULT '{}',
            description TEXT,
            preview_url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS speaker_voice_bindings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT REFERENCES episodes(id) ON DELETE CASCADE,
            speaker_label TEXT NOT NULL,
            character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            voice_id TEXT NOT NULL,
            is_project_default INTEGER NOT NULL DEFAULT 0,
            is_episode_override INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_speaker_voice_bindings_episode
        ON speaker_voice_bindings(episode_id, speaker_label);
        CREATE INDEX IF NOT EXISTS idx_speaker_voice_bindings_project
        ON speaker_voice_bindings(project_id, speaker_label, is_project_default);

        CREATE TABLE IF NOT EXISTS voice_generation_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            current_segment_id TEXT,
            total_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            usage_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_generation_tasks_episode
        ON voice_generation_tasks(episode_id, created_at);

        CREATE TABLE IF NOT EXISTS voice_segment_outputs (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            transcript_segment_id TEXT NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
            output_kind TEXT NOT NULL DEFAULT 'segment',
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            voice_id TEXT NOT NULL,
            source_text_hash TEXT NOT NULL,
            raw_audio_path TEXT,
            aligned_audio_path TEXT,
            output_format TEXT,
            target_duration REAL NOT NULL,
            generated_duration REAL,
            aligned_duration REAL,
            speed_adjustment REAL NOT NULL DEFAULT 1,
            duration_warning INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            provider_request_id TEXT,
            error_code TEXT,
            error_message TEXT,
            usage_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_segment_outputs_episode
        ON voice_segment_outputs(episode_id, output_kind, status);
        CREATE INDEX IF NOT EXISTS idx_voice_segment_outputs_segment
        ON voice_segment_outputs(transcript_segment_id, voice_id, source_text_hash, model);

        CREATE TABLE IF NOT EXISTS audio_mix_settings (
            episode_id TEXT PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
            dialogue_original_volume REAL NOT NULL DEFAULT 0.15,
            non_dialogue_original_volume REAL NOT NULL DEFAULT 1,
            dub_voice_volume REAL NOT NULL DEFAULT 1,
            fade_ms INTEGER NOT NULL DEFAULT 80,
            include_target_subtitles INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dubbed_outputs (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            task_id TEXT REFERENCES voice_generation_tasks(id) ON DELETE SET NULL,
            audio_path TEXT,
            preview_video_path TEXT,
            mix_mode TEXT NOT NULL DEFAULT 'mvp_ducking_mix',
            include_target_subtitles INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            actual_duration REAL,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dubbed_outputs_episode
        ON dubbed_outputs(episode_id, created_at);
        """
    )
    db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
