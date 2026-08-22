"""Add final merge states and metadata to the persisted multi-shot task."""

MIGRATION_ID = "20260723_stage_3b1_xmax_thirty_second_merge"


def apply(conn):
    task_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_tasks'").fetchone()
    segment_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_segments'").fetchone()
    if not task_sql or not segment_sql:
        return
    if "merge_pending" in (task_sql[0] or ""):
        conn.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE xmax_thirty_second_segments RENAME TO xmax_thirty_second_segments_merge_legacy")
    conn.execute("ALTER TABLE xmax_thirty_second_tasks RENAME TO xmax_thirty_second_tasks_merge_legacy")
    conn.execute(
        """
        CREATE TABLE xmax_thirty_second_tasks (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','local_ready','uploading','merge_pending','merging','completed','failed','merge_failed','interrupted','cancelled')),
            prompt TEXT NOT NULL,
            reference_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_duration REAL NOT NULL,
            estimated_credits REAL NOT NULL DEFAULT 0,
            final_output_path TEXT,
            final_output_url TEXT,
            final_duration REAL,
            final_width INTEGER,
            final_height INTEGER,
            final_fps REAL,
            final_has_audio INTEGER,
            source_audio_restored INTEGER NOT NULL DEFAULT 0,
            file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            heartbeat_at TEXT,
            merged_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE xmax_thirty_second_segments (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES xmax_thirty_second_tasks(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            duration REAL NOT NULL,
            source_path TEXT NOT NULL,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','local_ready','uploading','completed','failed','interrupted','cancelled')),
            output_path TEXT,
            output_url TEXT,
            output_duration REAL,
            file_size INTEGER,
            error_code TEXT,
            error_message TEXT,
            diagnostics_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            heartbeat_at TEXT,
            UNIQUE(task_id, sequence)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO xmax_thirty_second_tasks
        SELECT id, attempt_id, project_id, episode_id, status, prompt, reference_path, source_path,
               source_duration, estimated_credits, final_output_path, final_output_url, final_duration,
               NULL, NULL, NULL, NULL, 0, file_size, error_code, error_message, created_at,
               updated_at, heartbeat_at, NULL, completed_at, cancelled_at
        FROM xmax_thirty_second_tasks_merge_legacy
        """
    )
    conn.execute(
        """
        INSERT INTO xmax_thirty_second_segments
        SELECT id, task_id, sequence, start_time, end_time, duration, source_path, source_url, status,
               output_path, output_url, output_duration, file_size, error_code, error_message,
               diagnostics_json, created_at, updated_at, heartbeat_at
        FROM xmax_thirty_second_segments_merge_legacy
        """
    )
    conn.execute("DROP TABLE xmax_thirty_second_segments_merge_legacy")
    conn.execute("DROP TABLE xmax_thirty_second_tasks_merge_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_xmax_thirty_second_tasks_episode_created ON xmax_thirty_second_tasks (episode_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_xmax_thirty_second_segments_task_sequence ON xmax_thirty_second_segments (task_id, sequence)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
