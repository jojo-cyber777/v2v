"""Persist final Canvas input and SDK container diagnostics for Xmax attempts."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260722_stage_3b1_xmax_attempt_audit_v3"


def apply_stage_3b1_xmax_attempt_audit_v3_migration(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(xmax_realtime_attempts)").fetchall()}
    additions = {
        "final_input_stream_active": "INTEGER",
        "final_input_video_track_count": "INTEGER",
        "final_input_track_id": "TEXT",
        "final_input_track_ready_state": "TEXT",
        "final_input_track_enabled": "INTEGER",
        "final_input_track_muted": "INTEGER",
        "final_input_track_settings": "TEXT",
        "local_element_tag_name": "TEXT",
        "remote_element_tag_name": "TEXT",
        "canvas_draw_count_before_connect": "INTEGER",
        "video_current_time_before_connect": "REAL",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE xmax_realtime_attempts ADD COLUMN {name} {definition}")
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
