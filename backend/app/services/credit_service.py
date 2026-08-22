"""Transactional, integer-only credit wallet and immutable ledger service."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Callable

from ..database import get_connection, now_iso


WELCOME_GRANT = 100
LEDGER_TYPES = {"grant", "recharge", "freeze", "consume", "release", "refund", "manual_adjustment"}


class CreditError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _positive_integer(amount: int) -> int:
    if type(amount) is not int or amount <= 0:
        raise CreditError("INVALID_CREDIT_AMOUNT", "Credit amount must be a positive integer.")
    return amount


def _wallet_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _ledger_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    if item.get("metadata_json"):
        try:
            item["metadata"] = json.loads(item["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            item["metadata"] = None
    item.pop("metadata_json", None)
    return item


def ensure_wallet_in_transaction(db: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    user = db.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise CreditError("USER_NOT_FOUND", "User not found.")
    timestamp = now_iso()
    db.execute(
        "INSERT OR IGNORE INTO credit_wallets "
        "(user_id, available_balance, frozen_balance, lifetime_granted, lifetime_recharged, "
        "lifetime_consumed, lifetime_refunded, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, 0, 0, 0, ?, ?)",
        (user_id, timestamp, timestamp),
    )
    row = db.execute("SELECT * FROM credit_wallets WHERE user_id=?", (user_id,)).fetchone()
    return _wallet_dict(row)


def ensure_wallet(user_id: str) -> dict[str, Any]:
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return ensure_wallet_in_transaction(db, user_id)


def _apply_entry(
    db: sqlite3.Connection,
    *,
    user_id: str,
    entry_type: str,
    amount: int,
    idempotency_key: str,
    available_delta: int,
    frozen_delta: int,
    lifetime_field: str | None = None,
    task_id: str | None = None,
    unit_id: str | None = None,
    session_uid: str | None = None,
    operator_user_id: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount = _positive_integer(amount)
    if entry_type not in LEDGER_TYPES:
        raise CreditError("INVALID_LEDGER_TYPE", "Unsupported credit ledger type.")
    key = idempotency_key.strip()
    if not key or len(key) > 200:
        raise CreditError("INVALID_IDEMPOTENCY_KEY", "A valid idempotency key is required.")
    existing = db.execute("SELECT * FROM credit_ledger WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        if existing["user_id"] != user_id or existing["type"] != entry_type or existing["amount"] != amount:
            raise CreditError("IDEMPOTENCY_KEY_CONFLICT", "The idempotency key belongs to another operation.")
        wallet = ensure_wallet_in_transaction(db, user_id)
        return {"wallet": wallet, "ledger": _ledger_dict(existing), "idempotent": True}

    wallet = ensure_wallet_in_transaction(db, user_id)
    available_after = wallet["available_balance"] + available_delta
    frozen_after = wallet["frozen_balance"] + frozen_delta
    if available_after < 0:
        raise CreditError("INSUFFICIENT_AVAILABLE_CREDITS", "Insufficient available credits.")
    if frozen_after < 0:
        raise CreditError("INSUFFICIENT_FROZEN_CREDITS", "Insufficient frozen credits.")
    lifetime_fields = {
        "lifetime_granted",
        "lifetime_recharged",
        "lifetime_consumed",
        "lifetime_refunded",
    }
    if lifetime_field and lifetime_field not in lifetime_fields:
        raise CreditError("INVALID_LIFETIME_FIELD", "Invalid wallet summary field.")

    timestamp = now_iso()
    assignments = ["available_balance=?", "frozen_balance=?", "updated_at=?"]
    values: list[Any] = [available_after, frozen_after, timestamp]
    if lifetime_field:
        assignments.append(f"{lifetime_field}={lifetime_field}+?")
        values.append(amount)
    values.append(user_id)
    db.execute(f"UPDATE credit_wallets SET {', '.join(assignments)} WHERE user_id=?", values)

    ledger_id = f"crl-{uuid.uuid4().hex}"
    db.execute(
        "INSERT INTO credit_ledger "
        "(id, user_id, type, amount, available_before, available_after, frozen_before, frozen_after, "
        "task_id, unit_id, session_uid, operator_user_id, note, idempotency_key, created_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ledger_id,
            user_id,
            entry_type,
            amount,
            wallet["available_balance"],
            available_after,
            wallet["frozen_balance"],
            frozen_after,
            task_id,
            unit_id,
            session_uid,
            operator_user_id,
            note,
            key,
            timestamp,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
        ),
    )
    updated_wallet = db.execute("SELECT * FROM credit_wallets WHERE user_id=?", (user_id,)).fetchone()
    ledger = db.execute("SELECT * FROM credit_ledger WHERE id=?", (ledger_id,)).fetchone()
    return {"wallet": _wallet_dict(updated_wallet), "ledger": _ledger_dict(ledger), "idempotent": False}


def _transaction(operation: Callable[[sqlite3.Connection], dict[str, Any]]) -> dict[str, Any]:
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return operation(db)


def grant_once_in_transaction(
    db: sqlite3.Connection,
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    note: str | None = None,
    operator_user_id: str | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        db,
        user_id=user_id,
        entry_type="grant",
        amount=amount,
        idempotency_key=idempotency_key,
        available_delta=amount,
        frozen_delta=0,
        lifetime_field="lifetime_granted",
        note=note,
        operator_user_id=operator_user_id,
    )


def grant_once(user_id: str, amount: int, idempotency_key: str, *, note: str | None = None) -> dict[str, Any]:
    return _transaction(
        lambda db: grant_once_in_transaction(db, user_id, amount, idempotency_key, note=note)
    )


def grant_welcome_in_transaction(db: sqlite3.Connection, user_id: str, role: str) -> dict[str, Any] | None:
    ensure_wallet_in_transaction(db, user_id)
    if role != "user":
        return None
    idempotency_key = f"welcome_grant:{user_id}"
    existing = db.execute(
        "SELECT * FROM credit_ledger WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        return {
            "wallet": ensure_wallet_in_transaction(db, user_id),
            "ledger": _ledger_dict(existing),
            "idempotent": True,
        }
    return grant_once_in_transaction(
        db,
        user_id,
        WELCOME_GRANT,
        idempotency_key,
        note="New user welcome credits",
    )


def freeze(
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    task_id: str | None = None,
    unit_id: str | None = None,
) -> dict[str, Any]:
    return _transaction(
        lambda db: _apply_entry(
            db,
            user_id=user_id,
            entry_type="freeze",
            amount=amount,
            idempotency_key=idempotency_key,
            available_delta=-_positive_integer(amount),
            frozen_delta=amount,
            task_id=task_id,
            unit_id=unit_id,
        )
    )


def freeze_in_transaction(
    db: sqlite3.Connection,
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    task_id: str | None = None,
    unit_id: str | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        db,
        user_id=user_id,
        entry_type="freeze",
        amount=amount,
        idempotency_key=idempotency_key,
        available_delta=-_positive_integer(amount),
        frozen_delta=amount,
        task_id=task_id,
        unit_id=unit_id,
    )


def consume_frozen(
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    task_id: str | None = None,
    unit_id: str | None = None,
    session_uid: str | None = None,
) -> dict[str, Any]:
    return _transaction(
        lambda db: _apply_entry(
            db,
            user_id=user_id,
            entry_type="consume",
            amount=amount,
            idempotency_key=idempotency_key,
            available_delta=0,
            frozen_delta=-_positive_integer(amount),
            lifetime_field="lifetime_consumed",
            task_id=task_id,
            unit_id=unit_id,
            session_uid=session_uid,
        )
    )


def consume_frozen_in_transaction(
    db: sqlite3.Connection,
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    task_id: str | None = None,
    unit_id: str | None = None,
    session_uid: str | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        db,
        user_id=user_id,
        entry_type="consume",
        amount=amount,
        idempotency_key=idempotency_key,
        available_delta=0,
        frozen_delta=-_positive_integer(amount),
        lifetime_field="lifetime_consumed",
        task_id=task_id,
        unit_id=unit_id,
        session_uid=session_uid,
    )


def release_frozen(user_id: str, amount: int, idempotency_key: str, *, task_id: str | None = None) -> dict[str, Any]:
    return _transaction(
        lambda db: _apply_entry(
            db,
            user_id=user_id,
            entry_type="release",
            amount=amount,
            idempotency_key=idempotency_key,
            available_delta=_positive_integer(amount),
            frozen_delta=-amount,
            task_id=task_id,
        )
    )


def release_frozen_in_transaction(
    db: sqlite3.Connection,
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    task_id: str | None = None,
    unit_id: str | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        db,
        user_id=user_id,
        entry_type="release",
        amount=amount,
        idempotency_key=idempotency_key,
        available_delta=_positive_integer(amount),
        frozen_delta=-amount,
        task_id=task_id,
        unit_id=unit_id,
    )


def refund(user_id: str, amount: int, idempotency_key: str, *, task_id: str | None = None) -> dict[str, Any]:
    return _transaction(
        lambda db: _apply_entry(
            db,
            user_id=user_id,
            entry_type="refund",
            amount=amount,
            idempotency_key=idempotency_key,
            available_delta=_positive_integer(amount),
            frozen_delta=0,
            lifetime_field="lifetime_refunded",
            task_id=task_id,
        )
    )


def recharge_in_transaction(
    db: sqlite3.Connection,
    user_id: str,
    amount: int,
    idempotency_key: str,
    *,
    operator_user_id: str,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _apply_entry(
        db,
        user_id=user_id,
        entry_type="recharge",
        amount=amount,
        idempotency_key=idempotency_key,
        available_delta=amount,
        frozen_delta=0,
        lifetime_field="lifetime_recharged",
        operator_user_id=operator_user_id,
        note=note,
        metadata=metadata,
    )


def manual_adjustment(
    user_id: str,
    operation: str,
    amount: int,
    reason: str,
    idempotency_key: str,
    *,
    operator_user_id: str,
) -> dict[str, Any]:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise CreditError("ADJUSTMENT_REASON_REQUIRED", "A reason is required.")
    if operation not in {"add", "subtract"}:
        raise CreditError("INVALID_ADJUSTMENT_OPERATION", "Operation must be add or subtract.")
    amount = _positive_integer(amount)
    delta = amount if operation == "add" else -amount
    return _transaction(
        lambda db: _apply_entry(
            db,
            user_id=user_id,
            entry_type="manual_adjustment",
            amount=amount,
            idempotency_key=f"manual_adjustment:{user_id}:{idempotency_key.strip()}",
            available_delta=delta,
            frozen_delta=0,
            operator_user_id=operator_user_id,
            note=normalized_reason,
            metadata={"operation": operation},
        )
    )


def get_balance(user_id: str) -> dict[str, Any]:
    return ensure_wallet(user_id)


def list_ledger(
    user_id: str,
    *,
    entry_type: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_private: bool = False,
) -> dict[str, Any]:
    if entry_type and entry_type not in LEDGER_TYPES:
        raise CreditError("INVALID_LEDGER_TYPE", "Unsupported credit ledger type.")
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    clauses = ["user_id=?"]
    params: list[Any] = [user_id]
    if entry_type:
        clauses.append("type=?")
        params.append(entry_type)
    if created_from:
        clauses.append("created_at>=?")
        params.append(created_from)
    if created_to:
        clauses.append("created_at<=?")
        params.append(created_to)
    where = " AND ".join(clauses)
    with get_connection() as db:
        total = db.execute(f"SELECT COUNT(*) AS count FROM credit_ledger WHERE {where}", params).fetchone()["count"]
        rows = db.execute(
            f"SELECT * FROM credit_ledger WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    items = [_ledger_dict(row) for row in rows]
    if not include_private:
        for item in items:
            item.pop("operator_user_id", None)
            item.pop("note", None)
            item.pop("metadata", None)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def backfill_existing_users(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute("SELECT id, role FROM users ORDER BY created_at, id").fetchall()
    wallets_created = 0
    grants_created = 0
    for row in rows:
        existed = db.execute("SELECT 1 FROM credit_wallets WHERE user_id=?", (row["id"],)).fetchone()
        ensure_wallet_in_transaction(db, row["id"])
        if not existed:
            wallets_created += 1
        if row["role"] == "user":
            result = grant_welcome_in_transaction(db, row["id"], row["role"])
            if result and not result["idempotent"]:
                grants_created += 1
    return {"wallets_created": wallets_created, "welcome_grants_created": grants_created}
