"""Owner-scoped storage for new uploads without relocating historical assets."""

from __future__ import annotations

import re
from pathlib import Path

from .database import ROOT, UPLOADS_DIR, get_connection


STORAGE_ROOT = ROOT / "storage" / "users"
BILLING_STORAGE_ROOT = ROOT / "storage" / "billing"
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def safe_component(value: str, label: str) -> str:
    if not value or not SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Invalid {label}.")
    return value


def project_storage_root(user_id: str, project_id: str) -> Path:
    return (
        STORAGE_ROOT
        / safe_component(user_id, "user_id")
        / "projects"
        / safe_component(project_id, "project_id")
    )


def project_uploads_dir(user_id: str, project_id: str) -> Path:
    path = project_storage_root(user_id, project_id) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recharge_screenshots_dir(user_id: str) -> Path:
    path = STORAGE_ROOT / safe_component(user_id, "user_id") / "billing" / "recharge-screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def billing_qr_codes_dir() -> Path:
    path = BILLING_STORAGE_ROOT / "wechat-qr-codes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_asset_url(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(STORAGE_ROOT.resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) < 5 or parts[1] != "projects" or parts[3] not in {"uploads", "outputs"}:
        return None
    project_id = parts[2]
    category = parts[3]
    tail = "/".join(parts[4:])
    return f"/{category}/{project_id}/{tail}"


def resolve_upload_asset_path(value: str) -> Path | None:
    """Resolve a public upload URL across owner-scoped and legacy storage."""
    if not value.startswith("/uploads/"):
        return None
    parts = tuple(part for part in value.removeprefix("/uploads/").split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        return None

    project_id = parts[0]
    if len(parts) >= 2:
        with get_connection() as db:
            project = db.execute(
                "SELECT owner_user_id FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
        if project and project["owner_user_id"]:
            scoped_root = project_storage_root(project["owner_user_id"], project_id) / "uploads"
            scoped = scoped_root.joinpath(*parts[1:]).resolve()
            try:
                scoped.relative_to(scoped_root.resolve())
            except ValueError:
                return None
            if scoped.is_file():
                return scoped

    legacy = UPLOADS_DIR.joinpath(*parts).resolve()
    try:
        legacy.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return None
    return legacy
