"""Add additive persistence for the isolated three-shot Xmax MVP."""

MIGRATION_ID = "20260723_stage_3b1_xmax_thirty_second"


def apply(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xmax_thirty_second_tasks (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            prompt TEXT NOT NULL,
            reference_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_duration REAL NOT NULL,
            estimated_credits REAL NOT NULL DEFAULT 0,
            final_output_path TEXT,
            final_output_url TEXT,
            final_duration REAL,
            file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            cancelled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xmax_thirty_second_segments (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES xmax_thirty_second_tasks(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            source_path TEXT NOT NULL,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            output_path TEXT,
            output_url TEXT,
            output_duration REAL,
            file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, sequence)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xmax_thirty_second_tasks_episode_created "
        "ON xmax_thirty_second_tasks (episode_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xmax_thirty_second_segments_task_sequence "
        "ON xmax_thirty_second_segments (task_id, sequence)"
    )
