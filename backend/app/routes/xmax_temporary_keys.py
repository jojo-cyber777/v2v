"""Authenticated P3 Xmax temporary-key endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..access_control import require_request_user
from ..services import xmax_temporary_key_service as service


router = APIRouter(prefix="/api/xmax/temporary-keys", tags=["xmax-temporary-keys"])


class PreparePayload(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    episode_id: str = Field(min_length=1, max_length=200)
    unit_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    requested_points_limit: int = Field(gt=0)
    expire_seconds: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)


class BindSessionPayload(BaseModel):
    session_uid: str = Field(min_length=1, max_length=200)


def _http_error(error: service.XmaxTemporaryKeyError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})


@router.post("/prepare")
def prepare(payload: PreparePayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return service.prepare_temporary_key(
            service.PrepareRequest(
                user_id=user["id"],
                project_id=payload.project_id,
                episode_id=payload.episode_id,
                unit_id=payload.unit_id,
                attempt_id=payload.attempt_id,
                requested_points_limit=payload.requested_points_limit,
                expire_seconds=payload.expire_seconds,
                idempotency_key=payload.idempotency_key,
            )
        )
    except (service.XmaxTemporaryKeyError, service.SecretEncryptionError) as error:
        if isinstance(error, service.SecretEncryptionError):
            error = service.XmaxTemporaryKeyError(error.code, error.message, status_code=503)
        raise _http_error(error) from error


@router.get("/{temporary_key_id}")
def get_temporary_key(temporary_key_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return service.get_record(temporary_key_id, user["id"])
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/bind-session")
def bind_session(temporary_key_id: str, payload: BindSessionPayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return service.bind_session_uid(temporary_key_id, user["id"], payload.session_uid)
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/settle")
def settle(temporary_key_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return service.settle_temporary_key(temporary_key_id, user["id"])
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/abort")
def abort(temporary_key_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return service.abort_temporary_key(temporary_key_id, user["id"])
    except (service.XmaxTemporaryKeyError, service.SecretEncryptionError) as error:
        if isinstance(error, service.SecretEncryptionError):
            error = service.XmaxTemporaryKeyError(error.code, error.message, status_code=503)
        raise _http_error(error) from error
