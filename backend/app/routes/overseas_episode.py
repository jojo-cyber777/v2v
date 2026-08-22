"""One-click orchestration routes for a persisted overseas Episode result."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from ..services import overseas_episode_service

router = APIRouter(prefix="/api", tags=["overseas-episode"])


class VisualProgressBody(BaseModel):
    visual_task_id: str = Field(min_length=1, max_length=120)
    stage: str
    transform_completed: int = Field(default=0, ge=0)
    transform_total: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)
    xmax_sessions: int = Field(default=0, ge=0)


class VisualCompleteBody(BaseModel):
    visual_task_id: str = Field(min_length=1, max_length=120)


class TaskFailureBody(BaseModel):
    error_code: str = Field(min_length=1, max_length=120)
    error_message: str = Field(min_length=1, max_length=1000)
    failed_stage: str | None = Field(default=None, max_length=120)


def _raise(error: overseas_episode_service.OverseasEpisodeError) -> None:
    status = 404 if error.code in {"EPISODE_NOT_FOUND", "OVERSEAS_TASK_NOT_FOUND"} else 422
    raise HTTPException(
        status_code=status,
        detail={"error_code": error.code, "error_message": str(error)},
    ) from error


@router.get("/episodes/{episode_id}/overseas-version/latest")
def get_latest_overseas_episode_task(episode_id: str):
    try:
        return overseas_episode_service.get_latest_task(episode_id)
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)


@router.get("/overseas-version/{task_id}")
def get_overseas_episode_task(task_id: str):
    try:
        return overseas_episode_service.get_task_status(task_id)
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)


@router.post("/episodes/{episode_id}/overseas-version")
def create_overseas_episode_task(episode_id: str, background_tasks: BackgroundTasks):
    try:
        task, action = overseas_episode_service.create_or_resume_task(episode_id)
        if action == "start_audio" and task.get("audio_task_id"):
            background_tasks.add_task(
                overseas_episode_service.run_audio_stage,
                task["task_id"],
                task["audio_task_id"],
            )
        return task
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)


@router.post("/overseas-version/{task_id}/visual-progress")
def update_overseas_episode_visual_progress(task_id: str, payload: VisualProgressBody):
    try:
        return overseas_episode_service.update_visual_progress(
            task_id,
            visual_task_id=payload.visual_task_id,
            stage=payload.stage,
            transform_completed=payload.transform_completed,
            transform_total=payload.transform_total,
            fallback_count=payload.fallback_count,
            provider_calls=payload.provider_calls,
            xmax_sessions=payload.xmax_sessions,
        )
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)


@router.post("/overseas-version/{task_id}/visual-complete")
def complete_overseas_episode_visual(
    task_id: str,
    payload: VisualCompleteBody,
    background_tasks: BackgroundTasks,
):
    try:
        task, start_audio = overseas_episode_service.complete_visual_stage(
            task_id,
            payload.visual_task_id,
        )
        if start_audio and task.get("audio_task_id"):
            background_tasks.add_task(
                overseas_episode_service.run_audio_stage,
                task["task_id"],
                task["audio_task_id"],
            )
        return task
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)


@router.post("/overseas-version/{task_id}/failed")
def fail_overseas_episode_task(task_id: str, payload: TaskFailureBody):
    try:
        return overseas_episode_service.fail_task(
            task_id,
            payload.error_code,
            payload.error_message,
            stage=payload.failed_stage,
        )
    except overseas_episode_service.OverseasEpisodeError as error:
        _raise(error)
