"""Add the persistent task table for the isolated Xmax single-shot MVP."""

MIGRATION_ID = "20260723_stage_3b1_xmax_single_shot"


def apply(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xmax_single_shot_tasks (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'preparing', 'running', 'completed', 'failed', 'cancelled')),
            prompt TEXT NOT NULL,
            reference_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_duration REAL NOT NULL,
            session_id TEXT,
            estimated_credits REAL NOT NULL DEFAULT 0,
            billed_credits REAL,
            processed_seconds REAL,
            connect_started_at TEXT,
            connect_resolved_at TEXT,
            reference_upload_started_at TEXT,
            reference_upload_resolved_at TEXT,
            restart_started_at TEXT,
            restart_resolved_at TEXT,
            output_path TEXT,
            output_url TEXT,
            output_duration REAL,
            file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            stop_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            cancelled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xmax_single_shot_tasks_episode_created
        ON xmax_single_shot_tasks (episode_id, created_at DESC)
        """
    )
