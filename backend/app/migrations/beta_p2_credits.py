"""Invite-only beta credit wallets, ledger, and manual recharge requests."""

from __future__ import annotations

import json
import sqlite3


MIGRATION_ID = "20260803_beta_p2_credits"
PACKAGES = {
    "package_360": {"credit_amount": 360, "amount_cny_fen": 2800},
    "package_3600": {"credit_amount": 3600, "amount_cny_fen": 28000},
}


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (MIGRATION_ID,)).fetchone():
        return
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone():
        raise RuntimeError("The P1 authentication migration must be applied first.")

    statements = (
        """CREATE TABLE credit_wallets (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
            available_balance INTEGER NOT NULL DEFAULT 0 CHECK (available_balance >= 0),
            frozen_balance INTEGER NOT NULL DEFAULT 0 CHECK (frozen_balance >= 0),
            lifetime_granted INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_granted >= 0),
            lifetime_recharged INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_recharged >= 0),
            lifetime_consumed INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_consumed >= 0),
            lifetime_refunded INTEGER NOT NULL DEFAULT 0 CHECK (lifetime_refunded >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE credit_ledger (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            type TEXT NOT NULL CHECK (
                type IN ('grant', 'recharge', 'freeze', 'consume', 'release', 'refund', 'manual_adjustment')
            ),
            amount INTEGER NOT NULL CHECK (amount > 0),
            available_before INTEGER NOT NULL CHECK (available_before >= 0),
            available_after INTEGER NOT NULL CHECK (available_after >= 0),
            frozen_before INTEGER NOT NULL CHECK (frozen_before >= 0),
            frozen_after INTEGER NOT NULL CHECK (frozen_after >= 0),
            task_id TEXT,
            unit_id TEXT,
            session_uid TEXT,
            operator_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            note TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            metadata_json TEXT
        )""",
        "CREATE INDEX idx_credit_ledger_user_created ON credit_ledger (user_id, created_at DESC)",
        "CREATE INDEX idx_credit_ledger_user_type ON credit_ledger (user_id, type, created_at DESC)",
        """CREATE TABLE recharge_requests (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            package_code TEXT NOT NULL CHECK (package_code IN ('package_360', 'package_3600')),
            amount_cny_fen INTEGER NOT NULL CHECK (amount_cny_fen > 0),
            credit_amount INTEGER NOT NULL CHECK (credit_amount > 0),
            payment_time TEXT NOT NULL,
            payment_note TEXT,
            screenshot_path TEXT,
            status TEXT NOT NULL DEFAULT 'submitted'
                CHECK (status IN ('submitted', 'credited', 'rejected')),
            reviewed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at TEXT,
            review_note TEXT,
            approval_ledger_id TEXT REFERENCES credit_ledger(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX idx_recharge_requests_user_created ON recharge_requests (user_id, created_at DESC)",
        "CREATE INDEX idx_recharge_requests_status_created ON recharge_requests (status, created_at ASC)",
        """CREATE TABLE billing_settings (
            id TEXT PRIMARY KEY CHECK (id = 'default'),
            wechat_qr_path TEXT,
            recharge_instructions TEXT NOT NULL,
            packages_json TEXT NOT NULL,
            updated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL
        )""",
    )
    conn.execute("SAVEPOINT beta_p2_credits")
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO billing_settings "
            "(id, wechat_qr_path, recharge_instructions, packages_json, updated_by, updated_at) "
            "VALUES ('default', NULL, ?, ?, NULL, datetime('now'))",
            (
                "选择固定档位后使用微信付款，并提交付款时间和备注。积分由管理员核对后人工到账。",
                json.dumps(PACKAGES, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (MIGRATION_ID,),
        )
    except Exception:
        conn.execute("ROLLBACK TO beta_p2_credits")
        conn.execute("RELEASE beta_p2_credits")
        raise
    conn.execute("RELEASE beta_p2_credits")
