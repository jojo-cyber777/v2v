"""Authenticated formal Episode selection and final-output endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..access_control import require_episode_access, require_request_user
from ..services import self_service_closure_service as closure


router = APIRouter(prefix="/api", tags=["self-service-closure"])


class UnitSelectionPayload(BaseModel):
    selected_source_type: str
    selected_attempt_id: str | None = None


class FinalOutputPayload(BaseModel):
    task_id: str | None = None


def _error(error: closure.SelfServiceClosureError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get("/episodes/{episode_id}/formal-closure")
def workspace(episode_id: str, request: Request, task_id: str | None = None) -> dict:
    user = require_request_user(request)
    project_id = getattr(request.state, "authorized_project_id", None) or require_episode_access(user, episode_id)
    try:
        return closure.get_workspace(user["id"], episode_id, task_id, verified_project_id=project_id)
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error


@router.put("/episodes/{episode_id}/formal-closure/units/{unit_id}/selection")
def select_unit(episode_id: str, unit_id: str, payload: UnitSelectionPayload, request: Request) -> dict:
    user = require_request_user(request)
    project_id = getattr(request.state, "authorized_project_id", None) or require_episode_access(user, episode_id)
    try:
        return closure.select_unit(
            user["id"], episode_id, unit_id,
            selected_source_type=payload.selected_source_type,
            selected_attempt_id=payload.selected_attempt_id,
            verified_project_id=project_id,
        )
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error


@router.post("/episodes/{episode_id}/formal-closure/final-outputs")
def create_final(episode_id: str, payload: FinalOutputPayload, request: Request) -> dict:
    user = require_request_user(request)
    project_id = getattr(request.state, "authorized_project_id", None) or require_episode_access(user, episode_id)
    try:
        return closure.create_final_output(
            user["id"], episode_id, payload.task_id, verified_project_id=project_id
        )
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error


@router.post("/episodes/{episode_id}/formal-closure/visual-final-outputs")
def create_visual_final(episode_id: str, payload: FinalOutputPayload, request: Request) -> dict:
    user = require_request_user(request)
    project_id = getattr(request.state, "authorized_project_id", None) or require_episode_access(user, episode_id)
    try:
        return closure.create_visual_final_output(
            user["id"], episode_id, payload.task_id, verified_project_id=project_id
        )
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error


@router.get("/final-outputs/{output_id}")
def get_final(output_id: str, request: Request) -> dict:
    user = require_request_user(request)
    try:
        return closure.get_final_output(user["id"], output_id)
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error


@router.get("/final-outputs/{output_id}/download")
def download_final(output_id: str, request: Request) -> FileResponse:
    user = require_request_user(request)
    try:
        path = closure.final_output_path(user["id"], output_id)
    except closure.SelfServiceClosureError as error:
        raise _error(error) from error
    return FileResponse(path, media_type="video/mp4", filename=f"{output_id}.mp4")
