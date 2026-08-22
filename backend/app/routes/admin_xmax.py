"""Administrator-only Xmax temporary-key reconciliation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..access_control import require_admin
from ..services import xmax_temporary_key_service as service


router = APIRouter(prefix="/api/admin/xmax/temporary-keys", tags=["admin-xmax"])


def _http_error(error: service.XmaxTemporaryKeyError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message})


@router.get("")
def list_temporary_keys(
    request: Request,
    user_id: str | None = None,
    username: str | None = None,
    project_id: str | None = None,
    unit_id: str | None = None,
    status: str | None = None,
    settlement_pending: bool = False,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict:
    require_admin(request)
    return service.list_records(
        user_id=user_id,
        username=username,
        project_id=project_id,
        unit_id=unit_id,
        local_status=status,
        settlement_pending=settlement_pending,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )


@router.get("/{temporary_key_id}")
def get_temporary_key(temporary_key_id: str, request: Request) -> dict:
    require_admin(request)
    try:
        return service.get_record(temporary_key_id, admin=True)
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/refresh-status")
def refresh_status(temporary_key_id: str, request: Request) -> dict:
    require_admin(request)
    try:
        return service.refresh_status(temporary_key_id, admin=True)
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/inactivate")
def inactivate(temporary_key_id: str, request: Request) -> dict:
    require_admin(request)
    try:
        return service.abort_temporary_key(temporary_key_id, admin=True)
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error


@router.post("/{temporary_key_id}/reconcile")
def reconcile(temporary_key_id: str, request: Request) -> dict:
    require_admin(request)
    try:
        return service.settle_temporary_key(temporary_key_id, admin=True)
    except service.XmaxTemporaryKeyError as error:
        raise _http_error(error) from error
