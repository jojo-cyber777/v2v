"""Administrator credit adjustments, recharge review, and billing configuration."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from ..access_control import require_admin
from ..database import get_connection
from ..services import billing_file_service, billing_service, credit_service
from ..user_storage import billing_qr_codes_dir


router = APIRouter(prefix="/api/admin", tags=["admin-billing"])


class ManualAdjustmentPayload(BaseModel):
    operation: str
    amount: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReviewPayload(BaseModel):
    review_note: str = Field(default="", max_length=500)


def _credit_error(error: credit_service.CreditError) -> HTTPException:
    status = 409 if error.code in {"INSUFFICIENT_AVAILABLE_CREDITS", "IDEMPOTENCY_KEY_CONFLICT"} else 400
    return HTTPException(status_code=status, detail={"code": error.code, "message": error.message})


def _billing_error(error: billing_service.BillingError) -> HTTPException:
    status = 404 if error.code.endswith("NOT_FOUND") else 409 if error.code == "ALREADY_REVIEWED" else 400
    return HTTPException(status_code=status, detail={"code": error.code, "message": error.message})


@router.get("/credits/users/{user_id}")
def admin_user_credits(
    user_id: str,
    request: Request,
    entry_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    require_admin(request)
    with get_connection() as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    try:
        wallet = credit_service.get_balance(user_id)
        ledger = credit_service.list_ledger(
            user_id,
            entry_type=entry_type,
            page=page,
            page_size=page_size,
            include_private=True,
        )
    except credit_service.CreditError as error:
        raise _credit_error(error) from error
    return {
        "user": {"id": user["id"], "username": user["username"], "role": user["role"], "status": user["status"]},
        "wallet": wallet,
        "ledger": ledger,
    }


@router.post("/credits/users/{user_id}/adjust")
def adjust_user_credits(user_id: str, payload: ManualAdjustmentPayload, request: Request) -> dict:
    admin = require_admin(request)
    try:
        return credit_service.manual_adjustment(
            user_id,
            payload.operation,
            payload.amount,
            payload.reason,
            payload.idempotency_key,
            operator_user_id=admin["id"],
        )
    except credit_service.CreditError as error:
        raise _credit_error(error) from error


@router.get("/recharge-requests")
def admin_recharge_requests(
    request: Request,
    status: str | None = None,
    user_id: str | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    require_admin(request)
    try:
        return billing_service.list_admin_requests(
            status=status,
            user_id=user_id,
            username=username,
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )
    except billing_service.BillingError as error:
        raise _billing_error(error) from error


@router.post("/recharge-requests/{request_id}/approve")
def approve_recharge_request(request_id: str, payload: ReviewPayload, request: Request) -> dict:
    admin = require_admin(request)
    try:
        return billing_service.approve_request(request_id, admin["id"], payload.review_note)
    except (billing_service.BillingError, credit_service.CreditError) as error:
        if isinstance(error, credit_service.CreditError):
            raise _credit_error(error) from error
        raise _billing_error(error) from error


@router.post("/recharge-requests/{request_id}/reject")
def reject_recharge_request(request_id: str, payload: ReviewPayload, request: Request) -> dict:
    admin = require_admin(request)
    try:
        return billing_service.reject_request(request_id, admin["id"], payload.review_note)
    except billing_service.BillingError as error:
        raise _billing_error(error) from error


@router.put("/billing/settings")
async def update_billing_settings(
    request: Request,
    instructions: str = Form(..., min_length=1, max_length=2000),
    wechat_qr: UploadFile | None = File(default=None),
) -> dict:
    admin = require_admin(request)
    qr_path: str | None = None
    if wechat_qr is not None:
        try:
            qr_path = str(
                billing_file_service.store_validated_image(
                    await wechat_qr.read(),
                    billing_qr_codes_dir(),
                    prefix="wechat-qr",
                )
            )
        except billing_file_service.BillingFileError as error:
            raise HTTPException(status_code=400, detail={"code": error.code, "message": error.message}) from error
    try:
        return billing_service.update_settings(admin["id"], instructions, qr_path)
    except billing_service.BillingError as error:
        raise _billing_error(error) from error
