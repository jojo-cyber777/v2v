"""Add real transcription metadata for Stage 3 A1-1."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260716_stage_3a1_1_elevenlabs_transcription"


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, definition in definitions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_stage_3a1_1_migration(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    _add_columns(db, "ai_tasks", {
        "provider_request_id": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "usage_json": "TEXT NOT NULL DEFAULT '{}'",
    })
    _add_columns(db, "transcript_documents", {
        "detected_language": "TEXT",
        "transcript_text": "TEXT NOT NULL DEFAULT ''",
        "provider_request_id": "TEXT",
        "timestamp_source": "TEXT",
        "audio_duration": "REAL",
        "speaker_count": "INTEGER NOT NULL DEFAULT 0",
        "segment_count": "INTEGER NOT NULL DEFAULT 0",
        "provider_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    })
    _add_columns(db, "transcript_segments", {
        "speaker_label": "TEXT",
        "confidence": "REAL",
        "timestamp_source": "TEXT",
    })
    db.execute(
        "UPDATE transcript_segments SET speaker_label=speaker "
        "WHERE speaker_label IS NULL OR speaker_label=''"
    )
    db.execute(
        "UPDATE transcript_documents SET segment_count=("
        "SELECT COUNT(*) FROM transcript_segments WHERE document_id=transcript_documents.id)"
    )
    db.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
