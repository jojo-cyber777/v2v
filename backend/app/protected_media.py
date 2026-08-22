"""Authenticated, owner-checked media delivery with traversal protection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from .access_control import project_owner
from .database import ROOT, UPLOADS_DIR, get_connection
from .services import auth_service
from .user_storage import project_storage_root


OUTPUTS_DIR = ROOT / "outputs"


def _not_found_response() -> PlainTextResponse:
    return PlainTextResponse(
        "Not found.",
        status_code=404,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _authorized(user: dict[str, Any], project_id: str | None) -> bool:
    if not project_id:
        return False
    owner_id = project_owner(project_id)
    return bool(owner_id and (user["role"] == "admin" or owner_id == user["id"]))


def _path_variants(path: Path, category: str) -> tuple[str, ...]:
    values = {str(path.resolve())}
    try:
        relative = path.resolve().relative_to((ROOT / category).resolve()).as_posix()
        values.add(f"/{category}/{relative}")
    except ValueError:
        pass
    return tuple(values)


def _legacy_upload_project(path: Path) -> str | None:
    variants = _path_variants(path, "uploads")
    placeholders = ",".join("?" for _ in variants)
    queries = [
        (f"SELECT id AS project_id FROM projects WHERE video_path IN ({placeholders})", variants),
        (
            f"SELECT project_id FROM episodes WHERE video_path IN ({placeholders}) "
            f"OR thumbnail_path IN ({placeholders})",
            variants * 2,
        ),
        (
            "SELECT project_id FROM characters WHERE source_crop_path IN ({0}) OR target_reference_path IN ({0}) "
            "OR original_reference_image IN ({0}) OR front_view_image IN ({0}) OR angle_view_image IN ({0}) "
            "OR side_view_image IN ({0}) OR full_body_image IN ({0}) OR source_reference_image IN ({0}) "
            "OR main_reference_image IN ({0})".format(placeholders),
            variants * 9,
        ),
        (
            "SELECT c.project_id FROM character_reference_images r JOIN characters c ON c.id=r.character_id "
            f"WHERE r.image_path IN ({placeholders})",
            variants,
        ),
        (
            "SELECT e.project_id FROM episode_character_candidates c JOIN episodes e ON e.id=c.episode_id "
            f"WHERE c.representative_frame IN ({placeholders})",
            variants,
        ),
        (f"SELECT project_id FROM voice_profiles WHERE uploaded_audio_path IN ({placeholders})", variants),
    ]
    with get_connection() as db:
        for sql, params in queries:
            row = db.execute(sql, params).fetchone()
            if row:
                return row["project_id"]
    return None


def _safe_join(root: Path, parts: tuple[str, ...]) -> Path | None:
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


class ProtectedMediaFiles:
    def __init__(self, category: str) -> None:
        if category not in {"uploads", "outputs"}:
            raise ValueError("Unsupported media category.")
        self.category = category

    async def __call__(self, scope: dict, receive, send) -> None:
        request = Request(scope, receive=receive)
        user = auth_service.session_user(request.cookies.get(auth_service.SESSION_COOKIE))
        if not user:
            await _not_found_response()(scope, receive, send)
            return
        raw_path = str(scope.get("path", "")).lstrip("/")
        parts = tuple(part for part in raw_path.split("/") if part)
        if parts and parts[0] == self.category:
            parts = parts[1:]
        target: Path | None = None
        project_id: str | None = None
        if self.category == "outputs" and parts:
            project_id = parts[0]
            legacy = _safe_join(OUTPUTS_DIR, parts)
            if legacy and legacy.is_file():
                target = legacy
            else:
                owner_id = project_owner(project_id)
                if owner_id:
                    target = _safe_join(project_storage_root(owner_id, project_id) / "outputs", parts[1:])
        elif self.category == "uploads" and len(parts) >= 2 and project_owner(parts[0]):
            project_id = parts[0]
            owner_id = project_owner(project_id)
            if owner_id:
                target = _safe_join(project_storage_root(owner_id, project_id) / "uploads", parts[1:])
        elif self.category == "uploads":
            target = _safe_join(UPLOADS_DIR, parts)
            if target and target.is_file():
                project_id = _legacy_upload_project(target)
        if not target or not target.is_file() or not _authorized(user, project_id):
            await _not_found_response()(scope, receive, send)
            return
        response: Response = FileResponse(target)
        await response(scope, receive, send)
