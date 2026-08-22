"""Add non-destructive Transform Unit and risk metadata to multi-shot segments."""

MIGRATION_ID = "20260725_stage_3b1_xmax_thirty_second_v11_quality"


def _add_column(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_segments'").fetchone()
    if not exists:
        return
    for name, definition in {
        "transform_unit_id": "TEXT",
        "parent_shot_id": "TEXT",
        "risk_score": "REAL NOT NULL DEFAULT 0",
        "risk_level": "TEXT NOT NULL DEFAULT 'low'",
        "reference_type": "TEXT",
    }.items():
        _add_column(conn, "xmax_thirty_second_segments", name, definition)
    conn.execute(
        """UPDATE xmax_thirty_second_segments
           SET transform_unit_id=COALESCE(transform_unit_id, id),
               parent_shot_id=COALESCE(parent_shot_id, printf('legacy-shot-%03d', sequence)),
               risk_score=COALESCE(risk_score, 0),
               risk_level=COALESCE(NULLIF(risk_level, ''), 'low')"""
    )
    conn.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
