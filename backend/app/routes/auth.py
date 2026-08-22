"""Login, logout, current account, and invite-only registration."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..access_control import require_request_user
from ..database import get_connection, now_iso
from ..services import auth_service, credit_service


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class InviteRegistrationPayload(LoginPayload):
    invitation_code: str = Field(min_length=16, max_length=256)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _secure_cookie(request: Request) -> bool:
    host = request.url.hostname or ""
    return request.url.scheme == "https" and host not in {"127.0.0.1", "localhost"}


def _set_auth_cookies(response: Response, request: Request, session_token: str, csrf_token: str) -> None:
    secure = _secure_cookie(request)
    max_age = auth_service.SESSION_HOURS * 3600
    response.set_cookie(
        auth_service.SESSION_COOKIE,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        auth_service.CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _auth_error(error: auth_service.AuthError, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": error.code, "message": error.message})


@router.post("/login")
def login(payload: LoginPayload, request: Request, response: Response) -> dict:
    try:
        user = auth_service.authenticate(payload.username, payload.password, _client_ip(request))
    except auth_service.AuthError as error:
        status = 429 if error.code == "LOGIN_RATE_LIMITED" else 401
        raise _auth_error(error, status) from error
    session_token, csrf_token, user = auth_service.create_session(
        user["id"],
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, request, session_token, csrf_token)
    return {"user": user}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    require_request_user(request)
    auth_service.revoke_session(request.cookies.get(auth_service.SESSION_COOKIE))
    response.delete_cookie(auth_service.SESSION_COOKIE, path="/")
    response.delete_cookie(auth_service.CSRF_COOKIE, path="/")
    return {"logged_out": True}


@router.get("/me")
def me(request: Request) -> dict:
    return {"user": require_request_user(request)}


@router.post("/register-with-invite")
def register_with_invite(payload: InviteRegistrationPayload, request: Request, response: Response) -> dict:
    code_hash = auth_service.token_hash(payload.invitation_code.strip())
    now = datetime.now(timezone.utc)
    try:
        with get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            invitation = db.execute(
                "SELECT * FROM invitation_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
            valid = bool(
                invitation
                and invitation["status"] == "active"
                and invitation["used_count"] < invitation["max_uses"]
                and datetime.fromisoformat(invitation["expires_at"]).astimezone(timezone.utc) > now
            )
            if not valid:
                raise auth_service.AuthError("INVITATION_INVALID", "Invitation is invalid or no longer available.")
            user = auth_service.create_user(db, username=payload.username, password=payload.password, role="user")
            credit_service.grant_welcome_in_transaction(db, user["id"], user["role"])
            changed = db.execute(
                "UPDATE invitation_codes SET used_count=used_count+1, used_by=?, used_at=?, "
                "status=CASE WHEN used_count+1 >= max_uses THEN 'used' ELSE 'active' END "
                "WHERE id=? AND status='active' AND used_count < max_uses",
                (user["id"], now_iso(), invitation["id"]),
            )
            if changed.rowcount != 1:
                raise auth_service.AuthError("INVITATION_INVALID", "Invitation is invalid or no longer available.")
    except auth_service.AuthError as error:
        raise _auth_error(error) from error
    session_token, csrf_token, user = auth_service.create_session(
        user["id"],
        client_ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, request, session_token, csrf_token)
    return {"user": user}
