"""Invite-only beta accounts, sessions, invitations, and project ownership."""

from __future__ import annotations

import sqlite3


MIGRATION_ID = "20260803_beta_p1_auth_isolation"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
        ON auth_sessions (user_id, expires_at);

        CREATE TABLE IF NOT EXISTS invitation_codes (
            id TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            expires_at TEXT NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
            used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
            used_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'used', 'expired', 'revoked')),
            created_at TEXT NOT NULL,
            used_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_invitation_codes_status_expiry
        ON invitation_codes (status, expires_at);
        """
    )
    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "owner_user_id" not in project_columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN owner_user_id TEXT "
            "REFERENCES users(id) ON DELETE RESTRICT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_owner_updated "
        "ON projects (owner_user_id, updated_at DESC)"
    )
    conn.execute(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_ID,),
    )
