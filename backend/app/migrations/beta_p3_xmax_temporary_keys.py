"""P3 Xmax temporary-key lifecycle and usage snapshots."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260803_beta_p3_xmax_temporary_keys"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_xmax_temp_key_active_unit
               ON xmax_temporary_keys(unit_id)
               WHERE local_status IN ('preparing','active','session_started','pending_settlement')"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_xmax_temp_key_unsettled_unit
               ON xmax_temporary_keys(unit_id)
               WHERE local_status IN (
                   'preparing','active','session_started','pending_settlement',
                   'settlement_conflict','orphaned'
               )"""
        )
        return
    required = {"users", "projects", "episodes", "credit_wallets", "credit_ledger"}
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(f"P3 requires existing P1/P2 tables: {', '.join(missing)}")

    statements = (
        """CREATE TABLE xmax_temporary_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE RESTRICT,
            unit_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE,
            provider_uid TEXT,
            key_fingerprint TEXT,
            key_last4 TEXT,
            temporary_key_ciphertext TEXT,
            points_limit INTEGER NOT NULL CHECK (points_limit > 0),
            consumed_points INTEGER NOT NULL DEFAULT 0 CHECK (consumed_points >= 0),
            remaining_points INTEGER NOT NULL CHECK (remaining_points >= 0),
            expire_timestamp INTEGER,
            provider_status TEXT,
            local_status TEXT NOT NULL CHECK (local_status IN (
                'preparing','active','session_started','pending_settlement','settled',
                'settlement_conflict','cancelled','expired','failed','orphaned'
            )),
            session_uid TEXT,
            freeze_ledger_id TEXT REFERENCES credit_ledger(id) ON DELETE RESTRICT,
            consume_ledger_id TEXT REFERENCES credit_ledger(id) ON DELETE RESTRICT,
            release_ledger_id TEXT REFERENCES credit_ledger(id) ON DELETE RESTRICT,
            refund_ledger_id TEXT REFERENCES credit_ledger(id) ON DELETE RESTRICT,
            idempotency_key TEXT NOT NULL UNIQUE,
            issued_at TEXT,
            inactivated_at TEXT,
            settlement_started_at TEXT,
            settled_at TEXT,
            last_status_checked_at TEXT,
            last_error_code TEXT,
            last_error_message TEXT,
            provider_snapshot_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE UNIQUE INDEX idx_xmax_temp_key_session_uid ON xmax_temporary_keys(session_uid) WHERE session_uid IS NOT NULL",
        "CREATE INDEX idx_xmax_temp_key_user_created ON xmax_temporary_keys(user_id, created_at DESC)",
        "CREATE INDEX idx_xmax_temp_key_unit_created ON xmax_temporary_keys(unit_id, created_at DESC)",
        """CREATE UNIQUE INDEX idx_xmax_temp_key_active_unit
           ON xmax_temporary_keys(unit_id)
           WHERE local_status IN ('preparing','active','session_started','pending_settlement')""",
        """CREATE UNIQUE INDEX idx_xmax_temp_key_unsettled_unit
           ON xmax_temporary_keys(unit_id)
           WHERE local_status IN (
               'preparing','active','session_started','pending_settlement',
               'settlement_conflict','orphaned'
           )""",
        "CREATE INDEX idx_xmax_temp_key_status_updated ON xmax_temporary_keys(local_status, updated_at)",
        """CREATE TABLE xmax_usage_snapshots (
            id TEXT PRIMARY KEY,
            temporary_key_record_id TEXT NOT NULL REFERENCES xmax_temporary_keys(id) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('temporary_key','session','range_stats')),
            session_uid TEXT,
            consumed_points INTEGER CHECK (consumed_points IS NULL OR consumed_points >= 0),
            remaining_points INTEGER CHECK (remaining_points IS NULL OR remaining_points >= 0),
            provider_status TEXT,
            queried_at TEXT NOT NULL,
            response_json TEXT NOT NULL
        )""",
        "CREATE INDEX idx_xmax_usage_key_queried ON xmax_usage_snapshots(temporary_key_record_id, queried_at DESC)",
    )
    conn.execute("SAVEPOINT beta_p3_xmax_temporary_keys")
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (MIGRATION_ID,),
        )
    except Exception:
        conn.execute("ROLLBACK TO beta_p3_xmax_temporary_keys")
        conn.execute("RELEASE beta_p3_xmax_temporary_keys")
        raise
    conn.execute("RELEASE beta_p3_xmax_temporary_keys")
