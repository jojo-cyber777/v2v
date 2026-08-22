"""Add source/target character references and per-shot Xmax bindings."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260721_stage_3b1_character_reference_bindings"


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply_stage_3b1_character_reference_bindings(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    _add_columns(db, "characters", {
        "source_crop_path": "TEXT",
        "target_reference_path": "TEXT",
    })
    _add_columns(db, "xmax_realtime_shots", {
        "character_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "reference_layout_json": "TEXT NOT NULL DEFAULT '[]'",
        "reference_path": "TEXT",
    })
    character_columns = {row["name"] for row in db.execute("PRAGMA table_info(characters)").fetchall()}
    source_expression = "original_reference_image" if "original_reference_image" in character_columns else "NULL"
    target_expression = "main_reference_image" if "main_reference_image" in character_columns else source_expression
    db.execute(
        f"UPDATE characters SET source_crop_path=COALESCE(source_crop_path, {source_expression}), "
        f"target_reference_path=COALESCE(target_reference_path, {target_expression})"
    )
    db.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
