"""Additive schema changes for A1/A2 workflow consistency."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260717_stage_3a_workflow_fixes"


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_stage_3a_workflow_fixes(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    _add_columns(db, "transcript_documents", {
        "source_priority": "TEXT NOT NULL DEFAULT 'speech_transcription'",
        "comparison_mode": "TEXT NOT NULL DEFAULT 'source_review'",
    })
    _add_columns(db, "subtitle_styles", {
        "preview_stale": "INTEGER NOT NULL DEFAULT 0",
        "preview_stale_reason": "TEXT",
        "font_key": "TEXT",
    })
    _add_columns(db, "voice_generation_tasks", {"target_language": "TEXT NOT NULL DEFAULT 'en'"})
    _add_columns(db, "voice_segment_outputs", {"target_language": "TEXT NOT NULL DEFAULT 'en'"})
    _add_columns(db, "dubbed_outputs", {"target_language": "TEXT NOT NULL DEFAULT 'en'"})
    db.execute(
        "UPDATE transcript_documents SET source_priority=CASE "
        "WHEN source_text_origin IN ('external_subtitle', 'embedded_subtitle') THEN 'subtitle_source' "
        "WHEN source_text_origin='speech_transcription' THEN 'speech_transcription' "
        "ELSE COALESCE(source_priority, 'speech_transcription') END "
        "WHERE source_priority IS NULL OR source_priority=''"
    )
    db.execute(
        "UPDATE transcript_documents SET comparison_mode=CASE "
        "WHEN selected_source_mode='side_by_side' THEN 'side_by_side_review' "
        "ELSE 'source_review' END "
        "WHERE comparison_mode IS NULL OR comparison_mode=''"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS voice_output_orphans ("
        "id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE, "
        "transcript_segment_id TEXT NOT NULL, output_kind TEXT NOT NULL, status TEXT NOT NULL, "
        "reason TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_output_orphans_episode "
        "ON voice_output_orphans(episode_id, created_at)"
    )
    db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
