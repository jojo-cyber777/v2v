"""Add independent execution and human quality review state to multi-shot outputs."""

MIGRATION_ID = "20260724_stage_3b1_xmax_thirty_second_quality"


def _add_column(conn, table: str, name: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def apply(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='xmax_thirty_second_segments'").fetchone()
    if not exists:
        return
    for name, definition in {
        "execution_status": "TEXT",
        "quality_status": "TEXT NOT NULL DEFAULT 'review_required'",
        "quality_reason": "TEXT",
        "quality_reviewed_at": "TEXT",
        "quality_reviewed_by": "TEXT",
        "retry_version": "INTEGER NOT NULL DEFAULT 0",
        "reference_preview_url": "TEXT",
        "use_source_for_merge": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _add_column(conn, "xmax_thirty_second_segments", name, definition)
    conn.execute("UPDATE xmax_thirty_second_segments SET execution_status=COALESCE(execution_status, status)")
    conn.execute(
        """UPDATE xmax_thirty_second_segments
           SET quality_status=CASE
             WHEN preserve_original=1 OR shot_type IN ('no_character', 'transition', 'multi_character')
               THEN 'not_applicable'
             ELSE COALESCE(NULLIF(quality_status, ''), 'review_required')
           END"""
    )
    conn.execute("INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))", (MIGRATION_ID,))
