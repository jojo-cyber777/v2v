"""Persist complete TTS input revisions and dubbing mix manifests."""

from __future__ import annotations

import sqlite3

from ..services import tts_input_revision_service


MIGRATION_ID = "20260726_stage_3a2_tts_mix_revision"


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    _add_columns(conn, "voice_segment_outputs", {"tts_input_hash": "TEXT"})
    _add_columns(
        conn,
        "dubbed_outputs",
        {
            "mix_input_manifest_json": "TEXT",
            "mix_revision": "TEXT",
        },
    )

    rows = conn.execute(
        "SELECT vso.id, vso.provider, vso.model, vso.voice_id, ts.id AS segment_id, ts.final_translation, "
        "ts.speaker, ts.speaker_label, ts.segment_type, ts.start_ms, ts.end_ms "
        "FROM voice_segment_outputs vso "
        "JOIN transcript_segments ts ON ts.id=vso.transcript_segment_id "
        "WHERE vso.tts_input_hash IS NULL"
    ).fetchall()
    for row in rows:
        speaker = str(row["speaker_label"] or row["speaker"] or "").strip()
        if not speaker:
            speaker = "Narrator" if row["segment_type"] == "narration" else "Speaker 01"
        conn.execute(
            "UPDATE voice_segment_outputs SET tts_input_hash=? WHERE id=?",
            (
                tts_input_revision_service.build_tts_input_revision(
                    {
                        "id": row["segment_id"],
                        "final_translation": row["final_translation"],
                        "dubbing_speaker": speaker,
                        "start_ms": row["start_ms"],
                        "end_ms": row["end_ms"],
                    },
                    {
                        "provider": row["provider"],
                        "model": row["model"],
                        "voice_id": row["voice_id"],
                    },
                ).input_hash,
                row["id"],
            ),
        )

    conn.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
