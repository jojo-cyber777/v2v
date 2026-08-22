"""Central request authentication and Project ownership enforcement."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import HTTPException, Request

from .database import get_connection
from .services import auth_service


PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/register-with-invite",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ACCOUNT_SCOPED_ROUTE_PATHS = {
    "/api/recharge-requests/{request_id}/screenshot",
    "/api/xmax/temporary-keys/{temporary_key_id}",
    "/api/xmax/temporary-keys/{temporary_key_id}/bind-session",
    "/api/xmax/temporary-keys/{temporary_key_id}/settle",
    "/api/xmax/temporary-keys/{temporary_key_id}/abort",
    "/api/xmax/browser-attempts/{browser_attempt_id}",
    "/api/xmax/browser-attempts/{browser_attempt_id}/bind-session",
    "/api/xmax/browser-attempts/{browser_attempt_id}/failed",
    "/api/xmax/browser-attempts/{browser_attempt_id}/finalize",
    "/api/xmax/browser-attempts/{browser_attempt_id}/recover-quality",
}


def _not_found() -> None:
    raise HTTPException(status_code=404, detail="Resource not found.")


def _project_id_from_table(table: str, row_id: str) -> str | None:
    direct_tables = {
        "tasks",
        "episode_batch_jobs",
        "characters",
        "voice_profiles",
        "ai_tasks",
        "voice_generation_tasks",
        "audio_localization_tasks",
        "visual_transformation_tasks",
        "xmax_realtime_batches",
        "xmax_single_shot_tasks",
        "xmax_thirty_second_tasks",
        "overseas_episode_tasks",
        "episode_final_outputs",
    }
    with get_connection() as db:
        if table in direct_tables:
            row = db.execute(f"SELECT project_id FROM {table} WHERE id=?", (row_id,)).fetchone()
            return row["project_id"] if row else None
        if table == "episodes":
            row = db.execute("SELECT project_id FROM episodes WHERE id=?", (row_id,)).fetchone()
        elif table == "character_looks":
            row = db.execute(
                "SELECT c.project_id FROM character_looks l JOIN characters c ON c.id=l.character_id WHERE l.id=?",
                (row_id,),
            ).fetchone()
        elif table == "xmax_realtime_attempts":
            row = db.execute(
                "SELECT e.project_id FROM xmax_realtime_attempts a JOIN episodes e ON e.id=a.episode_id WHERE a.id=?",
                (row_id,),
            ).fetchone()
        else:
            row = None
    return row["project_id"] if row else None


def _resource_project_id(request: Request) -> str | None:
    params = request.path_params
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if params.get("project_id"):
        return params["project_id"]
    if params.get("episode_id"):
        return _project_id_from_table("episodes", params["episode_id"])
    if params.get("character_id"):
        return _project_id_from_table("characters", params["character_id"])
    if params.get("look_id"):
        return _project_id_from_table("character_looks", params["look_id"])
    if params.get("voice_profile_id"):
        return _project_id_from_table("voice_profiles", params["voice_profile_id"])
    if params.get("job_id"):
        return _project_id_from_table("episode_batch_jobs", params["job_id"])
    if params.get("batch_id"):
        return _project_id_from_table("xmax_realtime_batches", params["batch_id"])
    if params.get("attempt_id"):
        return _project_id_from_table("xmax_realtime_attempts", params["attempt_id"])
    if params.get("output_id"):
        return _project_id_from_table("episode_final_outputs", params["output_id"])
    task_id = params.get("task_id")
    if not task_id:
        return None
    table_by_route = {
        "/api/tasks/{task_id}": "tasks",
        "/api/tasks/{task_id}/cancel": "tasks",
        "/api/tasks/{task_id}/retry": "tasks",
        "/api/ai-tasks/{task_id}": "ai_tasks",
        "/api/voice-generation-tasks/{task_id}": "voice_generation_tasks",
        "/api/voice-generation-tasks/{task_id}/pause": "voice_generation_tasks",
        "/api/voice-generation-tasks/{task_id}/resume": "voice_generation_tasks",
        "/api/voice-generation-tasks/{task_id}/cancel": "voice_generation_tasks",
        "/api/voice-generation-tasks/{task_id}/reset-stuck": "voice_generation_tasks",
        "/api/audio-localization/{task_id}": "audio_localization_tasks",
        "/api/visual-transformation-tasks/{task_id}": "visual_transformation_tasks",
        "/api/visual-transformation-tasks/{task_id}/refresh": "visual_transformation_tasks",
        "/api/visual-transformation-tasks/{task_id}/cancel": "visual_transformation_tasks",
        "/api/visual-transformation-tasks/{task_id}/quality-review": "visual_transformation_tasks",
        "/api/xmax-single-shot/{task_id}/output": "xmax_single_shot_tasks",
        "/api/xmax-single-shot/{task_id}/cancel": "xmax_single_shot_tasks",
        "/api/xmax-thirty-second/{task_id}": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/heartbeat": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/interrupt": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/merge": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/cancel": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/browser-start": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/failed": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/output": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/quality": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/retry": "xmax_thirty_second_tasks",
        "/api/xmax-thirty-second/{task_id}/segments/{segment_id}/merge-source": "xmax_thirty_second_tasks",
        "/api/overseas-version/{task_id}": "overseas_episode_tasks",
        "/api/overseas-version/{task_id}/visual-progress": "overseas_episode_tasks",
        "/api/overseas-version/{task_id}/visual-complete": "overseas_episode_tasks",
        "/api/overseas-version/{task_id}/failed": "overseas_episode_tasks",
    }
    table = table_by_route.get(route)
    return _project_id_from_table(table, task_id) if table else None


def project_owner(project_id: str) -> str | None:
    with get_connection() as db:
        row = db.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()
    return row["owner_user_id"] if row else None


def require_project_access(user: dict[str, Any], project_id: str | None) -> None:
    if not project_id:
        _not_found()
    owner_id = project_owner(project_id)
    if owner_id is None:
        _not_found()
    if user["role"] != "admin" and owner_id != user["id"]:
        _not_found()


def require_episode_access(user: dict[str, Any], episode_id: str) -> str:
    """Resolve an Episode through the same Project ownership rule used by middleware."""
    project_id = _project_id_from_table("episodes", episode_id)
    require_project_access(user, project_id)
    assert project_id is not None
    return project_id


def require_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(auth_service.CSRF_COOKIE, "")
    header_token = request.headers.get("x-csrf-token", "")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def current_request_user(request: Request) -> dict[str, Any] | None:
    cached = getattr(request.state, "auth_user", None)
    if cached:
        return cached
    user = auth_service.session_user(request.cookies.get(auth_service.SESSION_COOKIE))
    if user:
        request.state.auth_user = user
    return user


def require_request_user(request: Request) -> dict[str, Any]:
    user = current_request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_request_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


async def enforce_api_access(request: Request) -> None:
    if request.method == "OPTIONS":
        return
    if request.url.path in PUBLIC_API_PATHS:
        return
    user = require_request_user(request)
    if request.method not in SAFE_METHODS:
        require_csrf(request)
    if request.url.path.startswith("/api/admin/"):
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator access required.")
        return
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if route in ACCOUNT_SCOPED_ROUTE_PATHS:
        return
    if request.path_params.get("episode_id"):
        request.state.authorized_project_id = require_episode_access(
            user, request.path_params["episode_id"]
        )
        return
    project_id = _resource_project_id(request)
    if project_id is not None or request.path_params:
        # Catalog routes have path parameters but no project-bound identifiers.
        catalog_params = {"provider_id", "voice_type"}
        if not set(request.path_params).issubset(catalog_params):
            require_project_access(user, project_id)
