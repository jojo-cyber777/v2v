"""Additive schema migration for Stage 3 A1 transcription and subtitles."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260716_stage_3a1_transcription_subtitles"


def apply_stage_3a1_migration(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            stage TEXT,
            error_code TEXT,
            error_message TEXT,
            stderr TEXT,
            input_path TEXT,
            output_path TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_tasks_episode ON ai_tasks(episode_id, created_at);

        CREATE TABLE IF NOT EXISTS subtitle_sources (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            language TEXT,
            file_path TEXT,
            file_format TEXT,
            stream_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            manual_override INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_sources_episode ON subtitle_sources(episode_id, priority DESC);

        CREATE TABLE IF NOT EXISTS subtitle_streams (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES subtitle_sources(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            stream_index INTEGER NOT NULL,
            codec_name TEXT,
            language TEXT,
            title TEXT,
            disposition_default INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transcript_documents (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
            source_id TEXT REFERENCES subtitle_sources(id) ON DELETE SET NULL,
            source_text_origin TEXT NOT NULL DEFAULT 'manual',
            source_language TEXT NOT NULL DEFAULT 'auto',
            selected_source_mode TEXT NOT NULL DEFAULT 'subtitle_primary',
            revision INTEGER NOT NULL DEFAULT 1,
            provider TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'empty',
            user_edited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transcript_segments (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES transcript_documents(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_text TEXT NOT NULL DEFAULT '',
            source_text_origin TEXT NOT NULL DEFAULT 'manual',
            speaker TEXT NOT NULL DEFAULT 'Speaker 1',
            segment_type TEXT NOT NULL DEFAULT 'dialogue',
            literal_translation TEXT NOT NULL DEFAULT '',
            localized_translation TEXT NOT NULL DEFAULT '',
            final_translation TEXT NOT NULL DEFAULT '',
            translation_status TEXT NOT NULL DEFAULT 'not_started',
            subtitle_enabled INTEGER NOT NULL DEFAULT 1,
            review_status TEXT NOT NULL DEFAULT 'pending',
            user_edited INTEGER NOT NULL DEFAULT 0,
            translation_edited INTEGER NOT NULL DEFAULT 0,
            stale_for_voice INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(document_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_segments_timeline ON transcript_segments(document_id, start_ms, seq);

        CREATE TABLE IF NOT EXISTS translation_documents (
            id TEXT PRIMARY KEY,
            transcript_document_id TEXT NOT NULL UNIQUE REFERENCES transcript_documents(id) ON DELETE CASCADE,
            source_language TEXT NOT NULL,
            target_language TEXT NOT NULL,
            translation_style TEXT NOT NULL DEFAULT 'natural',
            provider TEXT,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'not_started',
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subtitle_styles (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
            render_mode TEXT NOT NULL DEFAULT 'add_target_subtitle',
            font_family TEXT NOT NULL DEFAULT 'Arial',
            font_size INTEGER NOT NULL DEFAULT 42,
            text_color TEXT NOT NULL DEFAULT '#FFFFFF',
            outline_color TEXT NOT NULL DEFAULT '#000000',
            outline_width INTEGER NOT NULL DEFAULT 2,
            background_color TEXT NOT NULL DEFAULT '#00000099',
            position TEXT NOT NULL DEFAULT 'bottom',
            safe_margin_percent INTEGER NOT NULL DEFAULT 8,
            max_lines INTEGER NOT NULL DEFAULT 2,
            mask_height_percent INTEGER NOT NULL DEFAULT 22,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subtitle_outputs (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            task_id TEXT REFERENCES ai_tasks(id) ON DELETE SET NULL,
            output_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_format TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_outputs_episode ON subtitle_outputs(episode_id, created_at);
        """
    )
    db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
