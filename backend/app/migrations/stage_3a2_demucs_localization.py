"""Add persistent tasks for the Demucs-backed Audio Localization workflow."""

MIGRATION_ID = "20260724_stage_3a2_demucs_localization"


def apply(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audio_localization_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            source_task_id TEXT,
            mode TEXT NOT NULL DEFAULT 'demucs_dialogue_replacement',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending',
                    'preparing_assets',
                    'rendering_subtitles',
                    'separating_audio',
                    'building_tts_timeline',
                    'mixing_audio',
                    'muxing_final',
                    'completed',
                    'failed',
                    'interrupted'
                )),
            progress INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '{}',
            source_visual_path TEXT,
            source_audio_path TEXT,
            tts_timeline_path TEXT,
            separation_output_dir TEXT,
            vocals_path TEXT,
            no_vocals_path TEXT,
            mixed_audio_path TEXT,
            output_path TEXT,
            output_url TEXT,
            output_duration REAL,
            output_width INTEGER,
            output_height INTEGER,
            output_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            worker_return_code INTEGER,
            worker_stdout TEXT,
            worker_stderr TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audio_localization_tasks_episode_created
        ON audio_localization_tasks (episode_id, created_at DESC)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
