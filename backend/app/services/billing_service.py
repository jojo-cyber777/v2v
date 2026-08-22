"""Fixed-package manual recharge workflow for the invite-only beta."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ..database import get_connection, now_iso
from ..migrations.beta_p2_credits import PACKAGES
from . import credit_service


LEGACY_PACKAGE_VALUES = {
    "package_360": {(360, 4000)},
    "package_3600": {(3600, 40000)},
}


class BillingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def package_list() -> list[dict[str, Any]]:
    return [
        {"package_code": code, **values}
        for code, values in PACKAGES.items()
    ]


def _request_dict(row: sqlite3.Row, *, include_private: bool = False) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    screenshot_path = item.pop("screenshot_path", None)
    item["has_screenshot"] = bool(screenshot_path)
    item["screenshot_url"] = f"/api/recharge-requests/{item['id']}/screenshot" if screenshot_path else None
    if not include_private:
        item.pop("reviewed_by", None)
        item.pop("approval_ledger_id", None)
    return item


def get_settings() -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM billing_settings WHERE id='default'").fetchone()
    if not row:
        raise BillingError("BILLING_NOT_INITIALIZED", "Billing has not been initialized.")
    return {
        "recharge_instructions": row["recharge_instructions"],
        "packages": package_list(),
        "wechat_qr_available": bool(row["wechat_qr_path"]),
        "wechat_qr_url": "/api/billing/wechat-qr" if row["wechat_qr_path"] else None,
        "updated_at": row["updated_at"],
    }


def create_recharge_request(
    user_id: str,
    *,
    package_code: str,
    payment_time: str,
    payment_note: str | None,
    screenshot_path: str | None,
) -> dict[str, Any]:
    package = PACKAGES.get(package_code)
    if not package:
        raise BillingError("INVALID_RECHARGE_PACKAGE", "Select a supported recharge package.")
    contact = (payment_note or "").strip()
    if not contact:
        raise BillingError(
            "PAYMENT_CONTACT_REQUIRED",
            "A payer WeChat ID or contact email is required.",
        )
    request_id = f"rch-{uuid.uuid4().hex}"
    timestamp = now_iso()
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT id, status FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["status"] != "active":
            raise BillingError("USER_NOT_AVAILABLE", "User is not available.")
        db.execute(
            "INSERT INTO recharge_requests "
            "(id, user_id, package_code, amount_cny_fen, credit_amount, payment_time, payment_note, "
            "screenshot_path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)",
            (
                request_id,
                user_id,
                package_code,
                package["amount_cny_fen"],
                package["credit_amount"],
                payment_time,
                contact,
                screenshot_path,
                timestamp,
                timestamp,
            ),
        )
        row = db.execute("SELECT * FROM recharge_requests WHERE id=?", (request_id,)).fetchone()
    return _request_dict(row)


def list_user_requests(user_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    with get_connection() as db:
        total = db.execute(
            "SELECT COUNT(*) AS count FROM recharge_requests WHERE user_id=?",
            (user_id,),
        ).fetchone()["count"]
        rows = db.execute(
            "SELECT * FROM recharge_requests WHERE user_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [_request_dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def list_admin_requests(
    *,
    status: str | None = None,
    user_id: str | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    if status and status not in {"submitted", "credited", "rejected"}:
        raise BillingError("INVALID_RECHARGE_STATUS", "Invalid recharge status.")
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("r.status=?")
        params.append(status)
    if user_id:
        clauses.append("r.user_id=?")
        params.append(user_id)
    if username and username.strip():
        clauses.append("u.username LIKE ? COLLATE NOCASE")
        params.append(f"%{username.strip()}%")
    if created_from:
        clauses.append("r.payment_time>=?")
        params.append(created_from)
    if created_to:
        clauses.append("r.payment_time<=?")
        params.append(created_to)
    where = " AND ".join(clauses)
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    with get_connection() as db:
        total = db.execute(
            "SELECT COUNT(*) AS count FROM recharge_requests r "
            f"JOIN users u ON u.id=r.user_id WHERE {where}", params
        ).fetchone()["count"]
        rows = db.execute(
            "SELECT r.*, u.username FROM recharge_requests r JOIN users u ON u.id=r.user_id "
            f"WHERE {where} ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [_request_dict(row, include_private=True) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def approve_request(request_id: str, admin_user_id: str, review_note: str | None) -> dict[str, Any]:
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM recharge_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise BillingError("RECHARGE_REQUEST_NOT_FOUND", "Recharge request not found.")
        if row["status"] == "credited":
            return {"request": _request_dict(row, include_private=True), "idempotent": True}
        if row["status"] != "submitted":
            raise BillingError("ALREADY_REVIEWED", "The recharge request has already been reviewed.")
        package = PACKAGES.get(row["package_code"])
        stored_package = (row["credit_amount"], row["amount_cny_fen"])
        current_package = (
            (package["credit_amount"], package["amount_cny_fen"])
            if package
            else None
        )
        if stored_package != current_package and stored_package not in LEGACY_PACKAGE_VALUES.get(row["package_code"], set()):
            raise BillingError("RECHARGE_PACKAGE_MISMATCH", "Stored recharge package does not match the fixed catalog.")
        credit = credit_service.recharge_in_transaction(
            db,
            row["user_id"],
            row["credit_amount"],
            f"recharge_request:{row['id']}",
            operator_user_id=admin_user_id,
            note=(review_note or "").strip() or None,
            metadata={"request_id": row["id"], "package_code": row["package_code"]},
        )
        timestamp = now_iso()
        db.execute(
            "UPDATE recharge_requests SET status='credited', reviewed_by=?, reviewed_at=?, review_note=?, "
            "approval_ledger_id=?, updated_at=? WHERE id=? AND status='submitted'",
            (
                admin_user_id,
                timestamp,
                (review_note or "").strip() or None,
                credit["ledger"]["id"],
                timestamp,
                request_id,
            ),
        )
        updated = db.execute("SELECT * FROM recharge_requests WHERE id=?", (request_id,)).fetchone()
    return {"request": _request_dict(updated, include_private=True), "wallet": credit["wallet"], "idempotent": False}


def reject_request(request_id: str, admin_user_id: str, review_note: str) -> dict[str, Any]:
    note = review_note.strip()
    if not note:
        raise BillingError("REVIEW_NOTE_REQUIRED", "A rejection reason is required.")
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM recharge_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise BillingError("RECHARGE_REQUEST_NOT_FOUND", "Recharge request not found.")
        if row["status"] == "rejected":
            return {"request": _request_dict(row, include_private=True), "idempotent": True}
        if row["status"] != "submitted":
            raise BillingError("ALREADY_REVIEWED", "The recharge request has already been reviewed.")
        timestamp = now_iso()
        db.execute(
            "UPDATE recharge_requests SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=?, "
            "updated_at=? WHERE id=? AND status='submitted'",
            (admin_user_id, timestamp, note, timestamp, request_id),
        )
        updated = db.execute("SELECT * FROM recharge_requests WHERE id=?", (request_id,)).fetchone()
    return {"request": _request_dict(updated, include_private=True), "idempotent": False}


def update_settings(admin_user_id: str, instructions: str, qr_path: str | None = None) -> dict[str, Any]:
    normalized = instructions.strip()
    if not normalized:
        raise BillingError("RECHARGE_INSTRUCTIONS_REQUIRED", "Recharge instructions are required.")
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM billing_settings WHERE id='default'").fetchone()
        if not current:
            raise BillingError("BILLING_NOT_INITIALIZED", "Billing has not been initialized.")
        selected_qr = qr_path if qr_path is not None else current["wechat_qr_path"]
        db.execute(
            "UPDATE billing_settings SET wechat_qr_path=?, recharge_instructions=?, packages_json=?, "
            "updated_by=?, updated_at=? WHERE id='default'",
            (
                selected_qr,
                normalized,
                json.dumps(PACKAGES, ensure_ascii=False, sort_keys=True),
                admin_user_id,
                now_iso(),
            ),
        )
    return get_settings()
