"""Authenticated P4 browser Xmax attempt endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from ..access_control import require_request_user
from ..services import xmax_browser_attempt_service as browser_attempts
from ..services import xmax_temporary_key_service as temporary_keys


router = APIRouter(prefix="/api/xmax/browser-attempts", tags=["xmax-browser-attempts"])


class PreparePayload(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    unit_id: str = Field(min_length=1, max_length=200)
    requested_points_limit: int = Field(gt=0)
    expire_seconds: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    execution_mode: str = Field(default="realtime", pattern=r"^(realtime|offline)$")


class BindSessionPayload(BaseModel):
    session_uid: str = Field(min_length=1, max_length=200)


class FailurePayload(BaseModel):
    error_code: str = Field(min_length=1, max_length=120)
    error_message: str = Field(min_length=1, max_length=500)
    persistence_failed: bool = False
    diagnostics: dict[str, Any] | None = None


class RecoverQualityPayload(BaseModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    session_uid: str = Field(min_length=1, max_length=200)
    quality_status: str = Field(default="approved", min_length=1, max_length=80)
    quality_reason: str = Field(default="", max_length=1000)


class CropRect(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ScaleSize(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class PrepareLevel2Payload(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    unit_id: str = Field(min_length=1, max_length=200)
    crop: CropRect
    scale: ScaleSize


class SelectDirectPayload(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    unit_id: str = Field(min_length=1, max_length=200)


class ConfirmLevel2Payload(PrepareLevel2Payload):
    preprocessed_source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


def _error(error: Exception) -> HTTPException:
    if isinstance(error, browser_attempts.XmaxBrowserAttemptError):
        return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})
    if isinstance(error, temporary_keys.XmaxTemporaryKeyError):
        return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})
    if isinstance(error, temporary_keys.SecretEncryptionError):
        return HTTPException(status_code=503, detail={"code": error.code, "message": error.message})
    raise error


@router.post("/prepare")
def prepare(payload: PreparePayload, request: Request, background_tasks: BackgroundTasks) -> dict:
    user = require_request_user(request)
    try:
        if payload.execution_mode == "offline":
            from . import xmax_thirty_second

            prepared = browser_attempts.prepare_offline_attempt(
                browser_attempts.PrepareBrowserAttemptRequest(
                    user_id=user["id"],
                    task_id=payload.task_id,
                    unit_id=payload.unit_id,
                    requested_points_limit=payload.requested_points_limit,
                    expire_seconds=None,
                    idempotency_key=payload.idempotency_key,
                )
            )
            attempt = prepared["attempt"]
            if not prepared["idempotent"] and not bool(
                (attempt.get("input_snapshot") or {}).get("preserve_current_unit_version")
            ):
                xmax_thirty_second._mark_segment_browser_running(payload.task_id, payload.unit_id)
            if attempt["settlement_status"] != "settled":
                background_tasks.add_task(xmax_thirty_second._run_offline_attempt_background, attempt["id"])
            return prepared
        return browser_attempts.prepare_browser_attempt(
            browser_attempts.PrepareBrowserAttemptRequest(
                user_id=user["id"],
                task_id=payload.task_id,
                unit_id=payload.unit_id,
                requested_points_limit=payload.requested_points_limit,
                expire_seconds=payload.expire_seconds,
                idempotency_key=payload.idempotency_key,
            )
        )
    except (browser_attempts.XmaxBrowserAttemptError, temporary_keys.XmaxTemporaryKeyError, temporary_keys.SecretEncryptionError) as error:
        raise _error(error) from error


@router.post("/level2/prepare")
def prepare_level2(payload: PrepareLevel2Payload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        prepared = browser_attempts.prepare_level2_input(
            user_id=user["id"], task_id=payload.task_id, unit_id=payload.unit_id,
            crop=payload.crop.model_dump(), scale=payload.scale.model_dump(),
        )
        return {key: value for key, value in prepared.items() if key != "preprocessed_source_path"}
    except (browser_attempts.XmaxBrowserAttemptError, browser_attempts.media_service.MediaToolError) as error:
        if isinstance(error, browser_attempts.media_service.MediaToolError):
            raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
        raise _error(error) from error


@router.post("/level1/confirm")
def confirm_level1(payload: SelectDirectPayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.configure_direct_input_route(
            user_id=user["id"], task_id=payload.task_id, unit_id=payload.unit_id,
        )
    except browser_attempts.XmaxBrowserAttemptError as error:
        raise _error(error) from error


@router.post("/level2/confirm")
def confirm_level2(payload: ConfirmLevel2Payload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.confirm_level2_input(user_id=user["id"], prepared={
            **payload.model_dump(),
            "preprocessed_source_sha256": payload.preprocessed_source_sha256.lower(),
        })
    except browser_attempts.XmaxBrowserAttemptError as error:
        raise _error(error) from error


@router.get("/{browser_attempt_id}")
def get_attempt(browser_attempt_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.get_attempt(browser_attempt_id, user["id"])
    except browser_attempts.XmaxBrowserAttemptError as error:
        raise _error(error) from error


@router.post("/{browser_attempt_id}/bind-session")
def bind_session(browser_attempt_id: str, payload: BindSessionPayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.bind_session(browser_attempt_id, user["id"], payload.session_uid)
    except (browser_attempts.XmaxBrowserAttemptError, temporary_keys.XmaxTemporaryKeyError) as error:
        raise _error(error) from error


@router.post("/{browser_attempt_id}/failed")
def mark_failed(browser_attempt_id: str, payload: FailurePayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.mark_failed(
            browser_attempt_id,
            user["id"],
            payload.error_code,
            payload.error_message,
            persistence=payload.persistence_failed,
            diagnostics=payload.diagnostics,
        )
    except browser_attempts.XmaxBrowserAttemptError as error:
        raise _error(error) from error


@router.post("/{browser_attempt_id}/finalize")
def finalize(browser_attempt_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.finalize_attempt(browser_attempt_id, user["id"])
    except (browser_attempts.XmaxBrowserAttemptError, temporary_keys.XmaxTemporaryKeyError) as error:
        raise _error(error) from error


@router.post("/{browser_attempt_id}/recover-quality")
def recover_quality(browser_attempt_id: str, payload: RecoverQualityPayload, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return browser_attempts.recover_settled_quality_attempt(
            browser_attempt_id,
            user["id"],
            expected_sha256=payload.expected_sha256,
            session_uid=payload.session_uid,
            quality_status=payload.quality_status,
            quality_reason=payload.quality_reason,
        )
    except browser_attempts.XmaxBrowserAttemptError as error:
        raise _error(error) from error
