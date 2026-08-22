"""Authentication primitives for the invite-only beta."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..database import get_connection, now_iso


SESSION_COOKIE = "drama_session"
CSRF_COOKIE = "drama_csrf"
SESSION_HOURS = max(1, int(os.getenv("AUTH_SESSION_HOURS", "12")))
LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("AUTH_LOGIN_WINDOW_SECONDS", "600")))
LOGIN_MAX_ATTEMPTS = max(1, int(os.getenv("AUTH_LOGIN_MAX_ATTEMPTS", "5")))
PASSWORD_MIN_LENGTH = max(6, int(os.getenv("AUTH_PASSWORD_MIN_LENGTH", "6")))

_PASSWORD_HASHER = PasswordHasher()
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_attempts_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def validate_username(username: str) -> str:
    normalized = username.strip()
    if not 3 <= len(normalized) <= 120:
        raise AuthError("INVALID_USERNAME", "Username must contain 3 to 120 characters.")
    if any(ord(character) < 32 for character in normalized):
        raise AuthError("INVALID_USERNAME", "Username contains invalid characters.")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError(
            "WEAK_PASSWORD",
            f"Password must contain at least {PASSWORD_MIN_LENGTH} characters.",
        )
    if len(password) > 1024:
        raise AuthError("WEAK_PASSWORD", "Password is too long.")


def hash_password(password: str) -> str:
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _attempt_key(client_ip: str | None, username: str) -> str:
    return f"{client_ip or 'unknown'}:{username.casefold()}"


def check_login_rate_limit(client_ip: str | None, username: str) -> None:
    key = _attempt_key(client_ip, username)
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    with _login_attempts_lock:
        attempts = _login_attempts[key]
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            raise AuthError("LOGIN_RATE_LIMITED", "Too many login attempts. Try again later.")


def record_failed_login(client_ip: str | None, username: str) -> None:
    with _login_attempts_lock:
        _login_attempts[_attempt_key(client_ip, username)].append(time.monotonic())


def clear_login_attempts(client_ip: str | None, username: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(_attempt_key(client_ip, username), None)


def create_user(
    db: sqlite3.Connection,
    *,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, Any]:
    normalized = validate_username(username)
    if role not in {"user", "admin"}:
        raise AuthError("INVALID_ROLE", "Role must be user or admin.")
    user_id = f"usr-{uuid.uuid4().hex}"
    timestamp = now_iso()
    try:
        db.execute(
            "INSERT INTO users "
            "(id, username, password_hash, role, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (user_id, normalized, hash_password(password), role, timestamp, timestamp),
        )
    except sqlite3.IntegrityError as error:
        raise AuthError("USERNAME_UNAVAILABLE", "Username is already in use.") from error
    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return public_user(row)


def authenticate(username: str, password: str, client_ip: str | None) -> dict[str, Any]:
    normalized = username.strip()
    check_login_rate_limit(client_ip, normalized)
    with get_connection() as db:
        row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (normalized,)).fetchone()
        valid = bool(row and row["status"] == "active" and verify_password(row["password_hash"], password))
        if not valid:
            record_failed_login(client_ip, normalized)
            raise AuthError("INVALID_CREDENTIALS", "Invalid username or password.")
        if _PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            db.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (_PASSWORD_HASHER.hash(password), now_iso(), row["id"]),
            )
        db.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), row["id"]))
        fresh = db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    clear_login_attempts(client_ip, normalized)
    return public_user(fresh)


def create_session(user_id: str, *, client_ip: str | None, user_agent: str | None) -> tuple[str, str, dict[str, Any]]:
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    session_id = f"ses-{uuid.uuid4().hex}"
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(hours=SESSION_HOURS)
    with get_connection() as db:
        db.execute(
            "INSERT INTO auth_sessions "
            "(id, user_id, session_token_hash, expires_at, created_at, last_seen_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                user_id,
                token_hash(raw_token),
                expires_at.isoformat(timespec="seconds"),
                created_at.isoformat(timespec="seconds"),
                created_at.isoformat(timespec="seconds"),
                client_ip,
                (user_agent or "")[:512],
            ),
        )
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return raw_token, csrf_token, public_user(user)


def session_user(raw_token: str | None, *, touch: bool = True) -> dict[str, Any] | None:
    if not raw_token:
        return None
    digest = token_hash(raw_token)
    now = datetime.now(timezone.utc)
    with get_connection() as db:
        row = db.execute(
            "SELECT s.id AS session_id, s.expires_at, s.revoked_at, s.last_seen_at, "
            "u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.session_token_hash=?",
            (digest,),
        ).fetchone()
        if not row or row["revoked_at"] or row["status"] != "active" or _parse_datetime(row["expires_at"]) <= now:
            return None
        if touch and (now - _parse_datetime(row["last_seen_at"])).total_seconds() >= 60:
            db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (now_iso(), row["session_id"]))
        user = public_user(row)
        user["session_id"] = row["session_id"]
        user["session_expires_at"] = row["expires_at"]
        return user


def revoke_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    with get_connection() as db:
        db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE session_token_hash=? AND revoked_at IS NULL",
            (now_iso(), token_hash(raw_token)),
        )


def bootstrap_admin(username: str, password: str) -> tuple[dict[str, Any], int, bool]:
    normalized = validate_username(username)
    with get_connection() as db:
        existing = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (normalized,)).fetchone()
        created = False
        if existing:
            if existing["role"] != "admin":
                raise AuthError("USERNAME_UNAVAILABLE", "The username belongs to a non-admin account.")
            admin = public_user(existing)
        else:
            admin = create_user(db, username=normalized, password=password, role="admin")
            created = True
        result = db.execute(
            "UPDATE projects SET owner_user_id=? WHERE owner_user_id IS NULL",
            (admin["id"],),
        )
        assigned = result.rowcount
    return admin, assigned, created
