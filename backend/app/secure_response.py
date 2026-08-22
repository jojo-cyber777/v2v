"""Prevent local absolute paths from leaking through JSON API responses."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from .database import ROOT
from .user_storage import storage_asset_url


WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_PATH_FRAGMENT = re.compile(r"[A-Za-z]:[\\/]|\\\\")


def public_path(value: str) -> str:
    if not WINDOWS_PATH_FRAGMENT.search(value):
        return value
    if not WINDOWS_ABSOLUTE_PATH.match(value) and not value.startswith("\\\\"):
        return "Server operation failed. See server logs for details."
    path = Path(value)
    storage_url = storage_asset_url(path)
    if storage_url:
        return storage_url
    for category in ("uploads", "outputs", "mock-assets"):
        root = ROOT / category
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return f"/{category}/{relative.as_posix()}"
    return path.name


def sanitize_local_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_local_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_local_paths(item) for item in value)
    if isinstance(value, str):
        return public_path(value)
    return value


class SecureJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(sanitize_local_paths(content))
