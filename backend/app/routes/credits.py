"""Authenticated user credit balance, ledger, billing settings, and recharge requests."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from ..access_control import require_request_user
from ..database import get_connection
from ..services import billing_file_service, billing_service, credit_service
from ..user_storage import BILLING_STORAGE_ROOT, recharge_screenshots_dir


router = APIRouter(prefix="/api", tags=["credits"])


def _credit_error(error: credit_service.CreditError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": error.code, "message": error.message})


def _billing_error(error: billing_service.BillingError) -> HTTPException:
    status = 404 if error.code.endswith("NOT_FOUND") else 400
    return HTTPException(status_code=status, detail={"code": error.code, "message": error.message})


def _file_error(error: billing_file_service.BillingFileError) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": error.code, "message": error.message})


@router.get("/credits/me")
def my_credits(request: Request) -> dict:
    user = require_request_user(request)
    try:
        wallet = credit_service.get_balance(user["id"])
    except credit_service.CreditError as error:
        raise _credit_error(error) from error
    return {key: value for key, value in wallet.items() if key != "user_id"}


@router.get("/credits/ledger")
def my_credit_ledger(
    request: Request,
    entry_type: str | None = Query(default=None, alias="type"),
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    user = require_request_user(request)
    try:
        return credit_service.list_ledger(
            user["id"],
            entry_type=entry_type,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )
    except credit_service.CreditError as error:
        raise _credit_error(error) from error


@router.get("/billing/config")
def billing_config(request: Request) -> dict:
    require_request_user(request)
    try:
        return billing_service.get_settings()
    except billing_service.BillingError as error:
        raise _billing_error(error) from error


@router.get("/billing/wechat-qr")
def wechat_qr(request: Request) -> FileResponse:
    require_request_user(request)
    with get_connection() as db:
        settings = db.execute("SELECT wechat_qr_path FROM billing_settings WHERE id='default'").fetchone()
    target = billing_file_service.resolve_private_file(
        settings["wechat_qr_path"] if settings else None,
        BILLING_STORAGE_ROOT,
    )
    if not target:
        raise HTTPException(status_code=404, detail="WeChat payment QR code is not configured.")
    return FileResponse(target)


@router.post("/recharge-requests")
async def submit_recharge_request(
    request: Request,
    package_code: str = Form(...),
    payment_time: datetime = Form(...),
    payment_note: str = Form(..., min_length=1, max_length=500),
    screenshot: UploadFile | None = File(default=None),
) -> dict:
    user = require_request_user(request)
    screenshot_path: str | None = None
    if screenshot is not None:
        try:
            content = await screenshot.read()
            screenshot_path = str(
                billing_file_service.store_validated_image(
                    content,
                    recharge_screenshots_dir(user["id"]),
                    prefix="payment",
                )
            )
        except billing_file_service.BillingFileError as error:
            raise _file_error(error) from error
    try:
        item = billing_service.create_recharge_request(
            user["id"],
            package_code=package_code,
            payment_time=payment_time.isoformat(),
            payment_note=payment_note,
            screenshot_path=screenshot_path,
        )
    except billing_service.BillingError as error:
        raise _billing_error(error) from error
    return {"request": item}


@router.get("/recharge-requests")
def my_recharge_requests(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    user = require_request_user(request)
    return billing_service.list_user_requests(user["id"], page=page, page_size=page_size)


@router.get("/recharge-requests/{request_id}/screenshot")
def recharge_screenshot(request_id: str, request: Request) -> FileResponse:
    user = require_request_user(request)
    with get_connection() as db:
        row = db.execute(
            "SELECT user_id, screenshot_path FROM recharge_requests WHERE id=?",
            (request_id,),
        ).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(status_code=404, detail="Recharge screenshot not found.")
    target = billing_file_service.resolve_private_file(
        row["screenshot_path"],
        recharge_screenshots_dir(row["user_id"]),
    )
    if not target:
        raise HTTPException(status_code=404, detail="Recharge screenshot not found.")
    return FileResponse(target)
