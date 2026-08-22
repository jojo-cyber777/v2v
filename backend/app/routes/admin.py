"""Minimal invite-only beta administration endpoints."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..access_control import require_admin
from ..database import get_connection, now_iso
from ..services import auth_service, credit_service


router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=1024)
    role: str = "user"
    confirm_admin: bool = False


class UserStatusUpdate(BaseModel):
    status: str


class InvitationCreate(BaseModel):
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)
    max_uses: int = Field(default=1, ge=1, le=1000)


def _auth_error(error: auth_service.AuthError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": error.code, "message": error.message})


@router.get("/users")
def list_users(request: Request) -> dict:
    require_admin(request)
    with get_connection() as db:
        rows = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        project_counts = {
            row["owner_user_id"]: row["count"]
            for row in db.execute(
                "SELECT owner_user_id, COUNT(*) AS count FROM projects "
                "WHERE owner_user_id IS NOT NULL GROUP BY owner_user_id"
            ).fetchall()
        }
    users = [auth_service.public_user(row) | {"project_count": project_counts.get(row["id"], 0)} for row in rows]
    return {"users": users}


@router.post("/users")
def create_user(payload: AdminUserCreate, request: Request) -> dict:
    require_admin(request)
    if payload.role == "admin" and not payload.confirm_admin:
        raise HTTPException(status_code=400, detail="Creating an administrator requires explicit confirmation.")
    try:
        with get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            user = auth_service.create_user(
                db,
                username=payload.username,
                password=payload.password,
                role=payload.role,
            )
            credit_service.grant_welcome_in_transaction(db, user["id"], user["role"])
    except auth_service.AuthError as error:
        raise _auth_error(error) from error
    return {"user": user}


@router.patch("/users/{user_id}/status")
def update_user_status(user_id: str, payload: UserStatusUpdate, request: Request) -> dict:
    actor = require_admin(request)
    if payload.status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="Status must be active or disabled.")
    with get_connection() as db:
        target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        if payload.status == "disabled" and target["role"] == "admin":
            active_admins = db.execute(
                "SELECT COUNT(*) AS count FROM users WHERE role='admin' AND status='active'"
            ).fetchone()["count"]
            if active_admins <= 1:
                raise HTTPException(status_code=409, detail="The last active administrator cannot be disabled.")
        db.execute(
            "UPDATE users SET status=?, updated_at=? WHERE id=?",
            (payload.status, now_iso(), user_id),
        )
        if payload.status == "disabled":
            db.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now_iso(), user_id),
            )
        updated = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return {"user": auth_service.public_user(updated), "updated_by": actor["id"]}


@router.post("/invitations")
def create_invitation(payload: InvitationCreate, request: Request) -> dict:
    actor = require_admin(request)
    raw_code = secrets.token_urlsafe(24)
    invitation_id = f"inv-{uuid.uuid4().hex}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)
    with get_connection() as db:
        db.execute(
            "INSERT INTO invitation_codes "
            "(id, code_hash, created_by, expires_at, max_uses, used_count, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 'active', ?)",
            (
                invitation_id,
                auth_service.token_hash(raw_code),
                actor["id"],
                expires_at.isoformat(timespec="seconds"),
                payload.max_uses,
                now_iso(),
            ),
        )
    return {
        "invitation": {
            "id": invitation_id,
            "code": raw_code,
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "max_uses": payload.max_uses,
            "status": "active",
        }
    }


@router.get("/invitations")
def list_invitations(request: Request) -> dict:
    require_admin(request)
    now = datetime.now(timezone.utc)
    with get_connection() as db:
        rows = db.execute(
            "SELECT i.id, i.created_by, i.expires_at, i.max_uses, i.used_count, i.used_by, "
            "i.status, i.created_at, i.used_at, creator.username AS created_by_username, "
            "used.username AS used_by_username "
            "FROM invitation_codes i JOIN users creator ON creator.id=i.created_by "
            "LEFT JOIN users used ON used.id=i.used_by ORDER BY i.created_at DESC"
        ).fetchall()
    invitations = []
    for row in rows:
        status = row["status"]
        if status == "active" and datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc) <= now:
            status = "expired"
        invitations.append({key: row[key] for key in row.keys() if key != "status"} | {"status": status})
    return {"invitations": invitations}


@router.post("/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, request: Request) -> dict:
    require_admin(request)
    with get_connection() as db:
        invitation = db.execute("SELECT * FROM invitation_codes WHERE id=?", (invitation_id,)).fetchone()
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found.")
        if invitation["status"] == "active":
            db.execute("UPDATE invitation_codes SET status='revoked' WHERE id=?", (invitation_id,))
        updated = db.execute(
            "SELECT id, expires_at, max_uses, used_count, used_by, status, created_at, used_at "
            "FROM invitation_codes WHERE id=?",
            (invitation_id,),
        ).fetchone()
    return {"invitation": dict(updated)}


@router.delete("/invitations/{invitation_id}")
def delete_unused_invitation(invitation_id: str, request: Request) -> dict:
    actor = require_admin(request)
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        invitation = db.execute(
            "SELECT id, used_count FROM invitation_codes WHERE id=?",
            (invitation_id,),
        ).fetchone()
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found.")
        if invitation["used_count"] != 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "INVITATION_ALREADY_USED",
                    "message": "An invitation that has been used cannot be deleted.",
                },
            )
        deleted = db.execute(
            "DELETE FROM invitation_codes WHERE id=? AND used_count=0",
            (invitation_id,),
        )
        if deleted.rowcount != 1:
            raise HTTPException(status_code=409, detail="Invitation is no longer deletable.")
    return {"deleted": True, "invitation_id": invitation_id, "deleted_by": actor["id"]}
