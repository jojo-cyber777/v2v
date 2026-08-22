"""Add scene, character mapping, proxy and timeline metadata to multi-shot tasks."""

MIGRATION_ID = "20260723_stage_3b1_xmax_thirty_second_scene_mapping"


def _add_column(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply(conn):
    task = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_tasks'").fetchone()
    segment = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_segments'").fetchone()
    if not task or not segment:
        return
    for name, definition in {
        "scene_threshold": "REAL",
        "scene_analysis_status": "TEXT",
        "scene_analyzed_at": "TEXT",
        "scene_plan_json": "TEXT",
    }.items():
        _add_column(conn, "xmax_thirty_second_tasks", name, definition)
    for name, definition in {
        "shot_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "selected_character_id": "TEXT",
        "mapping_status": "TEXT NOT NULL DEFAULT 'manual_required'",
        "reference_path": "TEXT",
        "reference_url": "TEXT",
        "preserve_original": "INTEGER NOT NULL DEFAULT 0",
        "scene_score": "REAL",
        "cut_method": "TEXT",
        "first_frame_url": "TEXT",
        "middle_frame_url": "TEXT",
        "last_frame_url": "TEXT",
        "content_duration": "REAL",
        "proxy_duration": "REAL",
        "pre_roll_duration": "REAL NOT NULL DEFAULT 0",
        "post_roll_duration": "REAL NOT NULL DEFAULT 0",
        "timeline_status": "TEXT",
        "timeline_error_ms": "REAL",
        "reference_face_count": "INTEGER",
        "input_loop": "INTEGER",
        "input_playback_count": "INTEGER",
        "input_playback_started_at": "REAL",
        "input_ended_at": "REAL",
        "remote_track_received_at": "REAL",
        "remote_first_frame_at": "REAL",
        "remote_first_media_time": "REAL",
        "remote_last_frame_at": "REAL",
        "remote_last_media_time": "REAL",
        "recorder_started_at": "REAL",
        "recorder_stopped_at": "REAL",
        "raw_output_duration": "REAL",
        "trimmed_output_duration": "REAL",
    }.items():
        _add_column(conn, "xmax_thirty_second_segments", name, definition)
    conn.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
