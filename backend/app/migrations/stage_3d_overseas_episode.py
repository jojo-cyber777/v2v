"""Add the persistent one-click overseas Episode orchestration task."""

MIGRATION_ID = "20260725_stage_3d_overseas_episode"


def apply(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS overseas_episode_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
            stage TEXT NOT NULL DEFAULT 'preparing'
                CHECK (stage IN (
                    'preparing',
                    'analyzing_shots',
                    'transforming_shots',
                    'merging_visual',
                    'separating_audio',
                    'adapting_dialogue',
                    'rendering_subtitles',
                    'muxing_final',
                    'completed',
                    'failed'
                )),
            progress INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            visual_task_id TEXT,
            audio_task_id TEXT,
            transform_completed INTEGER NOT NULL DEFAULT 0,
            transform_total INTEGER NOT NULL DEFAULT 0,
            fallback_count INTEGER NOT NULL DEFAULT 0,
            final_output_path TEXT,
            final_output_url TEXT,
            error_code TEXT,
            error_message TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_overseas_episode_tasks_episode_created
        ON overseas_episode_tasks (episode_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_overseas_episode_tasks_one_active
        ON overseas_episode_tasks (episode_id)
        WHERE status='running'
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
