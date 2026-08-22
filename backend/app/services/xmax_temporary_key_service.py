"""P3 temporary-key issuance, session binding, and credit settlement."""

from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..database import get_connection, now_iso
from ..environment import get_xmax_setting
from . import credit_service
from .secret_encryption_service import (
    SecretEncryptionError,
    decrypt_temporary_key,
    encrypt_temporary_key,
    temporary_key_fingerprint,
    temporary_key_last4,
)
from .xmax_auth_client import XmaxAuthClient, XmaxAuthClientError


FINAL_PROVIDER_STATUSES = {"inactive", "timeout", "expired"}
FINAL_SESSION_STATUSES = {"CLOSED", "TIMEOUT"}
ACTIVE_LOCAL_STATUSES = {"preparing", "active", "session_started", "pending_settlement"}


class XmaxTemporaryKeyError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class PrepareRequest:
    user_id: str
    project_id: str
    episode_id: str
    unit_id: str
    attempt_id: str
    requested_points_limit: int
    expire_seconds: int | None
    idempotency_key: str
    allow_completed_version_attempt: bool = False


client_factory: Callable[[], XmaxAuthClient] = XmaxAuthClient


def _config_int(name: str, default: int) -> int:
    try:
        value = int(get_xmax_setting(name, str(default)))
    except ValueError as error:
        raise XmaxTemporaryKeyError("XMAX_AUTH_NOT_CONFIGURED", f"Invalid server setting: {name}.", status_code=503) from error
    if value <= 0:
        raise XmaxTemporaryKeyError("XMAX_AUTH_NOT_CONFIGURED", f"Invalid server setting: {name}.", status_code=503)
    return value


def _safe_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"temporaryapikey", "x-api-key", "authorization", "permanentapikey"}:
                result[key] = "<redacted>"
            else:
                result[key] = _safe_snapshot(item)
        return result
    if isinstance(value, list):
        return [_safe_snapshot(item) for item in value]
    if isinstance(value, str) and (value.startswith("tk-") or value.startswith("uk-")):
        return "<redacted>"
    return value


def _snapshot_json(value: dict[str, Any]) -> str:
    return json.dumps(_safe_snapshot(value), ensure_ascii=True, sort_keys=True)


PROVIDER_TIME_FIELDS = ("expireTimestamp", "createTimestamp", "updateTimestamp")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if type(value) is int:
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _parse_timezone_aware_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _time_field_diagnostic(field: str, value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else None
    if text is None:
        sample = f"<{_json_type(value)}>"
    elif text.startswith(("tk-", "uk-")):
        sample = "<redacted>"
    else:
        masked = "".join("#" if char.isdigit() else "A" if char.isalpha() else char for char in text[:80])
        sample = f"{masked}..." if len(text) > 80 else masked
    return {
        "field_name": field,
        "json_type": _json_type(value),
        "string_length": len(text) if text is not None else None,
        "redacted_format_sample": sample,
        "iso8601_parseable": _parse_timezone_aware_iso(value) is not None,
    }


def _normalize_provider_datetime(value: Any, field: str) -> str:
    parsed = _parse_timezone_aware_iso(value)
    if parsed is None:
        raise XmaxTemporaryKeyError(
            "XMAX_RESPONSE_INVALID",
            f"Xmax returned an invalid {field}.",
            status_code=502,
            diagnostics={"time_field": _time_field_diagnostic(field, value)},
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_provider_time_fields(
    payload: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    normalized = dict(payload)
    for field in PROVIDER_TIME_FIELDS:
        if field in payload or field in required:
            normalized[field] = _normalize_provider_datetime(payload.get(field), field)
    return normalized


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _public_record(row: sqlite3.Row | dict[str, Any], *, include_ledgers: bool = True) -> dict[str, Any]:
    item = dict(row)
    for key in ("temporary_key_ciphertext", "key_fingerprint", "provider_snapshot_json"):
        item.pop(key, None)
    if not include_ledgers:
        for key in ("freeze_ledger_id", "consume_ledger_id", "release_ledger_id", "refund_ledger_id"):
            item.pop(key, None)
    return item


def _load_unit(db: sqlite3.Connection, request: PrepareRequest) -> sqlite3.Row:
    row = db.execute(
        """SELECT s.id, s.status, s.execution_status, s.retry_version,
                  t.id AS task_id, t.project_id, t.episode_id
           FROM xmax_thirty_second_segments s
           JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
           WHERE s.id=?""",
        (request.unit_id,),
    ).fetchone()
    if not row or row["project_id"] != request.project_id or row["episode_id"] != request.episode_id:
        raise XmaxTemporaryKeyError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    project = db.execute("SELECT owner_user_id FROM projects WHERE id=?", (request.project_id,)).fetchone()
    if not project or project["owner_user_id"] != request.user_id:
        raise XmaxTemporaryKeyError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    episode = db.execute(
        "SELECT 1 FROM episodes WHERE id=? AND project_id=?",
        (request.episode_id, request.project_id),
    ).fetchone()
    if not episode:
        raise XmaxTemporaryKeyError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    status = row["execution_status"] or row["status"]
    if status not in {"pending", "failed", "interrupted"} and not (
        status == "completed" and request.allow_completed_version_attempt
    ):
        raise XmaxTemporaryKeyError("XMAX_UNIT_NOT_GENERATABLE", "This Unit is not available for a new generation attempt.", status_code=409)
    return row


def _validate_prepare(db: sqlite3.Connection, request: PrepareRequest) -> tuple[int, sqlite3.Row]:
    user = db.execute("SELECT status FROM users WHERE id=?", (request.user_id,)).fetchone()
    if not user or user["status"] != "active":
        raise XmaxTemporaryKeyError("XMAX_USER_NOT_ACTIVE", "The current account is not active.", status_code=403)
    if type(request.requested_points_limit) is not int or request.requested_points_limit <= 0:
        raise XmaxTemporaryKeyError("INVALID_POINTS_LIMIT", "pointsLimit must be a positive integer.")
    maximum = _config_int("XMAX_TEMP_KEY_MAX_POINTS", 50)
    if request.requested_points_limit > maximum:
        raise XmaxTemporaryKeyError("POINTS_LIMIT_EXCEEDED", "The requested pointsLimit exceeds the server limit.")
    default_expire = _config_int("XMAX_TEMP_KEY_DEFAULT_EXPIRE_SECONDS", 300)
    maximum_expire = _config_int("XMAX_TEMP_KEY_MAX_EXPIRE_SECONDS", 3600)
    expire_seconds = request.expire_seconds if request.expire_seconds is not None else default_expire
    if type(expire_seconds) is not int or expire_seconds <= 0 or expire_seconds > maximum_expire:
        raise XmaxTemporaryKeyError("INVALID_EXPIRE_SECONDS", "expireSeconds is outside the allowed range.")
    key = request.idempotency_key.strip()
    if len(key) < 8 or len(key) > 160:
        raise XmaxTemporaryKeyError("INVALID_IDEMPOTENCY_KEY", "A valid idempotency key is required.")
    if not request.attempt_id.strip() or len(request.attempt_id) > 200:
        raise XmaxTemporaryKeyError("INVALID_ATTEMPT_ID", "A valid attempt ID is required.")
    return expire_seconds, _load_unit(db, request)


def _existing_prepare_result(row: sqlite3.Row, request: PrepareRequest) -> dict[str, Any]:
    identity = (row["user_id"], row["project_id"], row["episode_id"], row["unit_id"], row["attempt_id"], row["points_limit"])
    expected = (request.user_id, request.project_id, request.episode_id, request.unit_id, request.attempt_id, request.requested_points_limit)
    if identity != expected:
        raise XmaxTemporaryKeyError("IDEMPOTENCY_KEY_CONFLICT", "The idempotency key belongs to another operation.", status_code=409)
    if row["local_status"] in {"active", "session_started", "pending_settlement"} and row["temporary_key_ciphertext"]:
        return {
            "internal_temporary_key_id": row["id"],
            "temporaryApiKey": decrypt_temporary_key(row["temporary_key_ciphertext"]),
            "expireTimestamp": row["expire_timestamp"],
            "pointsLimit": row["points_limit"],
            "idempotent": True,
        }
    if row["local_status"] == "preparing":
        raise XmaxTemporaryKeyError("XMAX_USAGE_PENDING", "Temporary-key issuance is already in progress.", status_code=409)
    raise XmaxTemporaryKeyError(row["last_error_code"] or "XMAX_TEMP_KEY_CREATE_FAILED", row["last_error_message"] or "The earlier issuance attempt failed.", status_code=409)


def prepare_temporary_key(request: PrepareRequest) -> dict[str, Any]:
    # Fail before freezing credits if encryption is not usable.
    encrypt_temporary_key("tk-encryption-preflight")
    timestamp = now_iso()
    record_id = f"xtk-{uuid.uuid4().hex}"
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM xmax_temporary_keys WHERE idempotency_key=?", (request.idempotency_key.strip(),)).fetchone()
        if existing:
            return _existing_prepare_result(existing, request)
        same_attempt = db.execute("SELECT * FROM xmax_temporary_keys WHERE attempt_id=?", (request.attempt_id.strip(),)).fetchone()
        if same_attempt:
            raise XmaxTemporaryKeyError("XMAX_ATTEMPT_ALREADY_PREPARED", "This attempt already has a temporary key.", status_code=409)
        expire_seconds, unit = _validate_prepare(db, request)
        try:
            freeze = credit_service.freeze_in_transaction(
                db,
                request.user_id,
                request.requested_points_limit,
                f"xmax:freeze:{request.idempotency_key.strip()}",
                task_id=unit["task_id"],
                unit_id=request.unit_id,
            )
        except credit_service.CreditError as error:
            raise XmaxTemporaryKeyError(error.code, error.message, status_code=409) from error
        try:
            db.execute(
                """INSERT INTO xmax_temporary_keys (
                    id,user_id,project_id,episode_id,unit_id,attempt_id,points_limit,consumed_points,
                    remaining_points,local_status,freeze_ledger_id,idempotency_key,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,0,?,'preparing',?,?,?,?)""",
                (
                    record_id, request.user_id, request.project_id, request.episode_id, request.unit_id,
                    request.attempt_id.strip(), request.requested_points_limit, request.requested_points_limit,
                    freeze["ledger"]["id"], request.idempotency_key.strip(), timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise XmaxTemporaryKeyError(
                "XMAX_ATTEMPT_ALREADY_PREPARED",
                "This Unit already has an active temporary-key attempt.",
                status_code=409,
            ) from error

    try:
        provider = client_factory().create_temporary_key(
            expire_seconds=expire_seconds,
            points_limit=request.requested_points_limit,
        )
        temporary_key = str(provider["temporaryApiKey"])
        provider = _normalize_provider_time_fields(provider, required=("expireTimestamp",))
        provider_limit = _strict_nonnegative_int(provider.get("pointsLimit"), "pointsLimit")
        consumed = _strict_nonnegative_int(provider.get("consumedPoints", 0), "consumedPoints")
        remaining = _strict_nonnegative_int(provider.get("remainingPoints", provider_limit - consumed), "remainingPoints")
        expire_timestamp = provider["expireTimestamp"]
        if provider_limit != request.requested_points_limit or consumed > provider_limit or remaining > provider_limit:
            raise XmaxTemporaryKeyError("XMAX_RESPONSE_INVALID", "Xmax returned inconsistent temporary-key limits.")
        ciphertext = encrypt_temporary_key(temporary_key)
        with get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE xmax_temporary_keys SET provider_uid=?,key_fingerprint=?,key_last4=?,
                       temporary_key_ciphertext=?,consumed_points=?,remaining_points=?,expire_timestamp=?,
                       provider_status=?,local_status='active',issued_at=?,last_status_checked_at=?,
                       provider_snapshot_json=?,updated_at=? WHERE id=? AND local_status='preparing'""",
                (
                    str(provider["uid"]), temporary_key_fingerprint(temporary_key), temporary_key_last4(temporary_key),
                    ciphertext, consumed, remaining, expire_timestamp, str(provider.get("status") or "active"),
                    now_iso(), now_iso(), _snapshot_json(provider), now_iso(), record_id,
                ),
            )
            if db.total_changes != 1:
                raise XmaxTemporaryKeyError("XMAX_INTERNAL_ERROR", "Temporary-key persistence conflicted.", status_code=409)
        return {
            "internal_temporary_key_id": record_id,
            "temporaryApiKey": temporary_key,
            "expireTimestamp": expire_timestamp,
            "pointsLimit": provider_limit,
            "idempotent": False,
        }
    except (XmaxAuthClientError, XmaxTemporaryKeyError, SecretEncryptionError) as error:
        _compensate_failed_prepare(
            record_id,
            request,
            error,
            locals().get("temporary_key"),
            getattr(error, "diagnostics", None),
        )
        if isinstance(error, XmaxTemporaryKeyError):
            raise
        raise XmaxTemporaryKeyError(error.code, error.message, status_code=502) from error
    except Exception as error:
        _compensate_failed_prepare(
            record_id,
            request,
            XmaxTemporaryKeyError("XMAX_INTERNAL_ERROR", "Temporary-key issuance failed."),
            locals().get("temporary_key"),
            None,
        )
        raise XmaxTemporaryKeyError("XMAX_INTERNAL_ERROR", "Temporary-key issuance failed.", status_code=500) from error


def _compensate_failed_prepare(
    record_id: str,
    request: PrepareRequest,
    error: Exception,
    temporary_key: str | None,
    diagnostics: dict[str, Any] | None,
) -> None:
    inactive_confirmed = temporary_key is None
    if temporary_key:
        try:
            provider = client_factory().inactivate_temporary_key(temporary_key)
            inactive_confirmed = str(provider.get("status") or "").lower() in FINAL_PROVIDER_STATUSES
        except Exception:
            inactive_confirmed = False
    code = getattr(error, "code", "XMAX_INTERNAL_ERROR")
    message = getattr(error, "message", "Temporary-key issuance failed.")
    try:
        ciphertext = None
        if temporary_key and not inactive_confirmed:
            try:
                ciphertext = encrypt_temporary_key(temporary_key)
            except SecretEncryptionError:
                ciphertext = None
        with get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
            if not row:
                return
            release = credit_service.release_frozen_in_transaction(
                db,
                request.user_id,
                request.requested_points_limit,
                f"xmax:release:create-failed:{request.idempotency_key.strip()}",
                task_id=None,
                unit_id=request.unit_id,
            )
            db.execute(
                """UPDATE xmax_temporary_keys SET temporary_key_ciphertext=?,key_fingerprint=?,key_last4=?,
                       provider_snapshot_json=COALESCE(?,provider_snapshot_json),local_status=?,release_ledger_id=?,
                       inactivated_at=?,last_error_code=?,last_error_message=?,updated_at=?
                   WHERE id=?""",
                (
                    ciphertext or row["temporary_key_ciphertext"],
                    temporary_key_fingerprint(temporary_key) if temporary_key else row["key_fingerprint"],
                    temporary_key_last4(temporary_key) if temporary_key else row["key_last4"],
                    _snapshot_json({"response_diagnostics": diagnostics}) if diagnostics else None,
                    "failed" if inactive_confirmed else "orphaned",
                    release["ledger"]["id"], now_iso() if inactive_confirmed else None,
                    code, message, now_iso(), record_id,
                ),
            )
    except Exception:
        # The caller still receives a hard failure; no secret is logged here.
        return


def _strict_nonnegative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise XmaxTemporaryKeyError("XMAX_RESPONSE_INVALID", f"Xmax returned an invalid {field}.")
    return value


def get_record(record_id: str, user_id: str | None = None, *, admin: bool = False) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
    if not row or (not admin and row["user_id"] != user_id):
        raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_NOT_FOUND", "Temporary-key record not found.", status_code=404)
    return _public_record(row, include_ledgers=admin)


def bind_session_uid(record_id: str, user_id: str, session_uid: str) -> dict[str, Any]:
    uid = session_uid.strip()
    if not uid or len(uid) > 200:
        raise XmaxTemporaryKeyError("INVALID_SESSION_UID", "A valid Session UID is required.")
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
        if not row or row["user_id"] != user_id:
            raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_NOT_FOUND", "Temporary-key record not found.", status_code=404)
        if row["session_uid"]:
            if hmac.compare_digest(row["session_uid"], uid):
                return _public_record(row)
            raise XmaxTemporaryKeyError("XMAX_SESSION_BINDING_CONFLICT", "This temporary key is already bound to another Session.", status_code=409)
        if row["local_status"] != "active":
            raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_NOT_ACTIVE", "This temporary key is not active.", status_code=409)
        try:
            db.execute(
                "UPDATE xmax_temporary_keys SET session_uid=?,local_status='session_started',updated_at=? WHERE id=?",
                (uid, now_iso(), record_id),
            )
        except sqlite3.IntegrityError as error:
            raise XmaxTemporaryKeyError("XMAX_SESSION_BINDING_CONFLICT", "This Session UID is already bound.", status_code=409) from error
        updated = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
    return _public_record(updated)


def _load_private_record(record_id: str, user_id: str | None = None, *, admin: bool = False) -> sqlite3.Row:
    with get_connection() as db:
        row = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
    if not row or (not admin and row["user_id"] != user_id):
        raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_NOT_FOUND", "Temporary-key record not found.", status_code=404)
    return row


def _save_snapshot(record_id: str, source: str, payload: dict[str, Any], session_uid: str | None = None) -> None:
    consumed_value = payload.get("consumedCredit", payload.get("consumedPoints"))
    remaining_value = payload.get("remainingPoints")
    consumed = consumed_value if type(consumed_value) is int and consumed_value >= 0 else None
    remaining = remaining_value if type(remaining_value) is int and remaining_value >= 0 else None
    status = payload.get("status")
    with get_connection() as db:
        db.execute(
            """INSERT INTO xmax_usage_snapshots
               (id,temporary_key_record_id,source,session_uid,consumed_points,remaining_points,
                provider_status,queried_at,response_json) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                f"xus-{uuid.uuid4().hex}", record_id, source, session_uid, consumed, remaining,
                str(status) if status is not None else None, now_iso(), _snapshot_json(payload),
            ),
        )


def _collect_usage(row: sqlite3.Row) -> dict[str, Any]:
    temporary_key = decrypt_temporary_key(row["temporary_key_ciphertext"])
    client = client_factory()
    session_payload: dict[str, Any] | None = None
    if row["session_uid"]:
        session_payload = _normalize_provider_time_fields(client.get_session_status(row["session_uid"]))
        returned_uid = session_payload.get("sessionUid")
        if returned_uid and returned_uid != row["session_uid"]:
            raise XmaxTemporaryKeyError("XMAX_SETTLEMENT_CONFLICT", "Xmax returned a different Session UID.", status_code=409)
        _save_snapshot(row["id"], "session", session_payload, row["session_uid"])
    key_payload = _normalize_provider_time_fields(client.get_temporary_key_status(temporary_key))
    returned_key = key_payload.get("temporaryApiKey")
    if returned_key and not hmac.compare_digest(temporary_key_fingerprint(str(returned_key)), row["key_fingerprint"]):
        raise XmaxTemporaryKeyError("XMAX_SETTLEMENT_CONFLICT", "Xmax returned a different temporary key.", status_code=409)
    _save_snapshot(row["id"], "temporary_key", key_payload)

    values: list[int] = []
    if session_payload is not None:
        values.append(_strict_nonnegative_int(session_payload.get("consumedCredit"), "session consumedCredit"))
    values.append(_strict_nonnegative_int(key_payload.get("consumedCredit", key_payload.get("consumedPoints")), "temporary-key consumedCredit"))
    consumed = max(values)
    provider_status = str(key_payload.get("status") or "").lower()
    session_status = str(session_payload.get("status") or "").upper() if session_payload else None
    final = provider_status in FINAL_PROVIDER_STATUSES
    return {
        "consumed_points": consumed,
        "provider_status": provider_status or None,
        "session_status": session_status,
        "final": final,
        "snapshot": {"temporary_key": _safe_snapshot(key_payload), "session": _safe_snapshot(session_payload)},
    }


def refresh_status(record_id: str, user_id: str | None = None, *, admin: bool = False) -> dict[str, Any]:
    row = _load_private_record(record_id, user_id, admin=admin)
    if not row["temporary_key_ciphertext"]:
        raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_STATUS_FAILED", "No provider key is available for status refresh.", status_code=409)
    try:
        usage = _collect_usage(row)
    except XmaxTemporaryKeyError as error:
        _mark_pending(record_id, error.code, error.message)
        _save_response_diagnostics(record_id, error.diagnostics)
        raise
    except (XmaxAuthClientError, SecretEncryptionError) as error:
        _mark_pending(record_id, error.code, error.message)
        raise XmaxTemporaryKeyError(error.code, error.message, status_code=502) from error
    if usage["consumed_points"] < row["consumed_points"] or usage["consumed_points"] > row["points_limit"]:
        with get_connection() as db:
            db.execute(
                """UPDATE xmax_temporary_keys SET local_status='settlement_conflict',provider_status=?,
                       last_status_checked_at=?,last_error_code='XMAX_SETTLEMENT_CONFLICT',
                       last_error_message='Provider usage conflicts with the frozen points limit.',
                       provider_snapshot_json=?,updated_at=? WHERE id=?""",
                (usage["provider_status"], now_iso(), _snapshot_json(usage["snapshot"]), now_iso(), record_id),
            )
        return get_record(record_id, user_id, admin=admin)
    with get_connection() as db:
        db.execute(
            """UPDATE xmax_temporary_keys SET consumed_points=?,remaining_points=?,provider_status=?,
                   last_status_checked_at=?,provider_snapshot_json=?,updated_at=? WHERE id=?""",
            (
                usage["consumed_points"], max(0, row["points_limit"] - usage["consumed_points"]),
                usage["provider_status"], now_iso(), _snapshot_json(usage["snapshot"]), now_iso(), record_id,
            ),
        )
    return get_record(record_id, user_id, admin=admin)


def _mark_pending(record_id: str, code: str, message: str) -> None:
    with get_connection() as db:
        db.execute(
            """UPDATE xmax_temporary_keys SET local_status='pending_settlement',settlement_started_at=COALESCE(settlement_started_at,?),
                   last_error_code=?,last_error_message=?,updated_at=? WHERE id=? AND local_status!='settled'""",
            (now_iso(), code, message, now_iso(), record_id),
        )


def _save_response_diagnostics(record_id: str, diagnostics: dict[str, Any] | None) -> None:
    if not diagnostics:
        return
    with get_connection() as db:
        db.execute(
            "UPDATE xmax_temporary_keys SET provider_snapshot_json=?,updated_at=? WHERE id=?",
            (_snapshot_json({"response_diagnostics": diagnostics}), now_iso(), record_id),
        )


def settle_temporary_key(record_id: str, user_id: str | None = None, *, admin: bool = False) -> dict[str, Any]:
    row = _load_private_record(record_id, user_id, admin=admin)
    if row["local_status"] == "settled":
        return _public_record(row, include_ledgers=admin)
    if not row["temporary_key_ciphertext"]:
        raise XmaxTemporaryKeyError("XMAX_USAGE_PENDING", "Temporary-key usage is not available yet.", status_code=409)
    try:
        usage = _collect_usage(row)
    except XmaxTemporaryKeyError as error:
        _save_response_diagnostics(record_id, error.diagnostics)
        if error.code == "XMAX_SETTLEMENT_CONFLICT":
            with get_connection() as db:
                db.execute(
                    """UPDATE xmax_temporary_keys SET local_status='settlement_conflict',
                           last_error_code=?,last_error_message=?,updated_at=? WHERE id=? AND local_status!='settled'""",
                    (error.code, error.message, now_iso(), record_id),
                )
        else:
            _mark_pending(record_id, error.code, error.message)
        raise
    except (XmaxAuthClientError, SecretEncryptionError) as error:
        _mark_pending(record_id, error.code, error.message)
        raise XmaxTemporaryKeyError(error.code, error.message, status_code=502) from error
    consumed = usage["consumed_points"]
    if not usage["final"]:
        _mark_pending(record_id, "XMAX_USAGE_PENDING", "Xmax usage is not final yet.")
        return get_record(record_id, user_id, admin=admin)

    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
        if current["local_status"] == "settled":
            return _public_record(current, include_ledgers=admin)
        if consumed < current["consumed_points"] or consumed > current["points_limit"]:
            db.execute(
                """UPDATE xmax_temporary_keys SET local_status='settlement_conflict',provider_status=?,
                       last_status_checked_at=?,last_error_code='XMAX_SETTLEMENT_CONFLICT',
                       last_error_message='Provider usage conflicts with the frozen points limit.',
                       provider_snapshot_json=?,updated_at=? WHERE id=?""",
                (usage["provider_status"], now_iso(), _snapshot_json(usage["snapshot"]), now_iso(), record_id),
            )
            return _public_record(db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone(), include_ledgers=admin)
        consume_id = current["consume_ledger_id"]
        release_id = current["release_ledger_id"]
        task_row = db.execute(
            "SELECT task_id FROM xmax_thirty_second_segments WHERE id=?",
            (current["unit_id"],),
        ).fetchone()
        task_id = task_row["task_id"] if task_row else None
        if consumed > 0 and not consume_id:
            consume = credit_service.consume_frozen_in_transaction(
                db, current["user_id"], consumed, f"xmax:consume:{record_id}",
                task_id=task_id, unit_id=current["unit_id"], session_uid=current["session_uid"],
            )
            consume_id = consume["ledger"]["id"]
        release_amount = current["points_limit"] - consumed
        if release_amount > 0 and not release_id:
            release = credit_service.release_frozen_in_transaction(
                db, current["user_id"], release_amount, f"xmax:release:{record_id}",
                task_id=task_id, unit_id=current["unit_id"],
            )
            release_id = release["ledger"]["id"]
        db.execute(
            """UPDATE xmax_temporary_keys SET consumed_points=?,remaining_points=?,provider_status=?,
                   local_status='settled',consume_ledger_id=?,release_ledger_id=?,settlement_started_at=COALESCE(settlement_started_at,?),
                   settled_at=?,last_status_checked_at=?,last_error_code=NULL,last_error_message=NULL,
                   provider_snapshot_json=?,updated_at=? WHERE id=?""",
            (
                consumed, release_amount, usage["provider_status"], consume_id, release_id, now_iso(),
                now_iso(), now_iso(), _snapshot_json(usage["snapshot"]), now_iso(), record_id,
            ),
        )
        updated = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=?", (record_id,)).fetchone()
    return _public_record(updated, include_ledgers=admin)


def abort_temporary_key(record_id: str, user_id: str | None = None, *, admin: bool = False) -> dict[str, Any]:
    row = _load_private_record(record_id, user_id, admin=admin)
    if row["local_status"] == "settled":
        return _public_record(row, include_ledgers=admin)
    if row["inactivated_at"] or str(row["provider_status"] or "").lower() in FINAL_PROVIDER_STATUSES:
        return settle_temporary_key(record_id, user_id, admin=admin)
    if not row["temporary_key_ciphertext"]:
        raise XmaxTemporaryKeyError("XMAX_TEMP_KEY_INACTIVATE_FAILED", "No provider key is available to inactivate.", status_code=409)
    temporary_key = decrypt_temporary_key(row["temporary_key_ciphertext"])
    try:
        provider = _normalize_provider_time_fields(client_factory().inactivate_temporary_key(temporary_key))
    except XmaxTemporaryKeyError as error:
        _mark_pending(record_id, error.code, error.message)
        _save_response_diagnostics(record_id, error.diagnostics)
        raise
    except XmaxAuthClientError as error:
        _mark_pending(record_id, error.code, error.message)
        raise XmaxTemporaryKeyError(error.code, error.message, status_code=502) from error
    status = str(provider.get("status") or "").lower()
    if status not in FINAL_PROVIDER_STATUSES:
        _mark_pending(record_id, "XMAX_USAGE_PENDING", "Xmax has not confirmed that the temporary key is inactive.")
        return get_record(record_id, user_id, admin=admin)
    _save_snapshot(record_id, "temporary_key", provider)
    with get_connection() as db:
        db.execute(
            """UPDATE xmax_temporary_keys SET provider_status=?,inactivated_at=?,last_status_checked_at=?,
                   provider_snapshot_json=?,updated_at=? WHERE id=?""",
            (status, now_iso(), now_iso(), _snapshot_json(provider), now_iso(), record_id),
        )
    return settle_temporary_key(record_id, user_id, admin=admin)


def list_records(
    *,
    user_id: str | None = None,
    username: str | None = None,
    project_id: str | None = None,
    unit_id: str | None = None,
    local_status: str | None = None,
    settlement_pending: bool = False,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: list[Any] = []
    for column, value in (("user_id", user_id), ("project_id", project_id), ("unit_id", unit_id), ("local_status", local_status)):
        if value:
            clauses.append(f"x.{column}=?")
            params.append(value)
    if username and username.strip():
        clauses.append("u.username LIKE ? COLLATE NOCASE")
        params.append(f"%{username.strip()}%")
    if settlement_pending:
        clauses.append("x.local_status='pending_settlement'")
    if created_from:
        clauses.append("x.created_at>=?")
        params.append(created_from)
    if created_to:
        clauses.append("x.created_at<=?")
        params.append(created_to)
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where = " AND ".join(clauses)
    with get_connection() as db:
        total = db.execute(
            "SELECT COUNT(*) AS count FROM xmax_temporary_keys x "
            f"JOIN users u ON u.id=x.user_id WHERE {where}",
            params,
        ).fetchone()["count"]
        rows = db.execute(
            "SELECT x.*, u.username, COALESCE(release.amount, 0) AS released_points "
            "FROM xmax_temporary_keys x JOIN users u ON u.id=x.user_id "
            "LEFT JOIN credit_ledger release ON release.id=x.release_ledger_id "
            f"WHERE {where} ORDER BY x.created_at DESC,x.id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": [_public_record(row) for row in rows], "total": total, "page": page, "page_size": page_size}
