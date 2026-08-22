"""P4 browser-owned Xmax attempt orchestration around the P3 key lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import os
from typing import Any
import uuid
from datetime import datetime, timedelta, timezone

from ..database import ROOT, get_connection, now_iso
from ..user_storage import storage_asset_url
from . import credit_service
from . import media_service
from . import xmax_temporary_key_service as temporary_keys


MODEL_NAME = "x2.0"
PLAYBACK = {"playbackRate": 1}
PROMPT_TEMPLATE_VERSION_PREFIX = "xmax-character-replacement"
QUALITY_STATUSES = {
    "review_required",
    "approved",
    "rejected_no_effect",
    "rejected_identity_drift",
    "rejected_timeline",
    "invalid_output",
    "not_applicable",
}
DIRECT_PROCESSING_MODE = "level1_direct"
LEVEL2_PROCESSING_MODE = "level2_crop"
REALTIME_EXECUTION_MODE = "realtime"
OFFLINE_EXECUTION_MODE = "offline"
FAILURE_PHASES = {
    "temporary_key",
    "reference_upload",
    "session_connect",
    "remote_stream",
    "cos_source_video_upload",
    "cos_reference_image_upload",
}


def _sanitize_failure_text(value: Any, limit: int) -> str:
    text = str(value or "")
    text = re.sub(r"Bearer\s+[^\s,;}]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:tk|uk)-[A-Za-z0-9._~-]+", "[REDACTED_XMAX_KEY]", text)
    text = re.sub(
        r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|secret|password)\s*[=:]\s*)([^\s,;})]+)",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text[:limit]


def _failure_diagnostics(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    failure = value.get("failure") if isinstance(value.get("failure"), dict) else value
    phase = str(failure.get("phase") or "")
    message = _sanitize_failure_text(failure.get("message"), 500)
    if phase not in FAILURE_PHASES or not message:
        return None
    code = _sanitize_failure_text(failure.get("errorCode"), 120) or None
    stack = _sanitize_failure_text(failure.get("sanitizedStack"), 2000) or None
    if stack:
        stack = "\n".join(stack.splitlines()[:12])
    return {
        "errorName": _sanitize_failure_text(failure.get("errorName") or "Error", 120) or "Error",
        "errorCode": code,
        "message": message,
        "phase": phase,
        "sanitizedStack": stack,
    }


class XmaxBrowserAttemptError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PrepareBrowserAttemptRequest:
    user_id: str
    task_id: str
    unit_id: str
    requested_points_limit: int
    expire_seconds: int | None
    idempotency_key: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _local_asset_path(value: str | None) -> Path | None:
    if not value:
        return None
    raw = str(value).split("?", 1)[0]
    candidate = Path(raw)
    if candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()
    if raw.startswith("/uploads/") or raw.startswith("/outputs/"):
        candidate = ROOT / raw.lstrip("/")
        if candidate.is_file():
            return candidate.resolve()
    candidate = ROOT / raw.lstrip("/")
    return candidate.resolve() if candidate.is_file() else None


def _controlled_asset_url(path: Path, fallback: str | None = None) -> str:
    scoped = storage_asset_url(path)
    if scoped:
        return scoped
    for category in ("uploads", "outputs"):
        try:
            relative = path.resolve().relative_to((ROOT / category).resolve()).as_posix()
            return f"/{category}/{relative}"
        except ValueError:
            continue
    fallback_value = str(fallback or "").split("?", 1)[0]
    if fallback_value.startswith(("/uploads/", "/outputs/")):
        return fallback_value
    raise XmaxBrowserAttemptError(
        "XMAX_REFERENCE_URL_UNAVAILABLE",
        "The approved character reference is not available through protected media delivery.",
        status_code=409,
    )


def _versioned_source_url(url: str, sha256: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={sha256}"


def _source_shot_ids(task: sqlite3.Row, unit: sqlite3.Row) -> list[str]:
    try:
        plan = json.loads(task["scene_plan_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        plan = {}
    shots = plan.get("base_shots") or plan.get("shots") or []
    start = float(unit["start_time"])
    end = float(unit["end_time"])
    result: list[str] = []
    for index, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            continue
        shot_start = float(shot.get("source_start", shot.get("start_time", 0)) or 0)
        shot_end = float(shot.get("source_end", shot.get("end_time", 0)) or 0)
        if min(end, shot_end) - max(start, shot_start) > 0.0005:
            result.append(str(shot.get("parent_shot_id") or shot.get("id") or f"base-shot-{index:03d}"))
    return result or [str(unit["parent_shot_id"] or f"base-shot-{int(unit['sequence']):03d}")]


def _object_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _execution_mode(row: sqlite3.Row | dict[str, Any]) -> str:
    render = _object_json(row["render_json"])
    return OFFLINE_EXECUTION_MODE if render.get("execution_mode") == OFFLINE_EXECUTION_MODE else REALTIME_EXECUTION_MODE


def _provider_authority_bound(row: sqlite3.Row | dict[str, Any]) -> bool:
    if _execution_mode(row) == OFFLINE_EXECUTION_MODE:
        return bool(row["offline_task_uid"])
    return bool(row["session_uid"])


def _unit_input_snapshot(unit: sqlite3.Row) -> dict[str, Any]:
    original_path = _local_asset_path(unit["source_path"] or unit["source_url"])
    if not original_path:
        raise XmaxBrowserAttemptError(
            "XMAX_SOURCE_UNREADABLE",
            "The Unit source is not readable.",
            status_code=409,
        )
    original_sha256 = _sha256_file(original_path)
    original_url = _versioned_source_url(
        _controlled_asset_url(original_path, unit["source_url"]),
        original_sha256,
    )
    diagnostics = _object_json(unit["diagnostics_json"])
    route = diagnostics.get("input_route")
    route = route if isinstance(route, dict) else {}
    direct = {
        "processing_mode": DIRECT_PROCESSING_MODE,
        "preserve_current_unit_version": bool(
            route.get("processing_mode") == DIRECT_PROCESSING_MODE
            and route.get("preserve_current_unit_version")
        ),
        "preserved_source_by_user": bool(route.get("preserved_source_by_user")),
        "original_source": {
            "path": str(original_path),
            "url": original_url,
            "sha256": original_sha256,
        },
        "selected_source": {
            "path": str(original_path),
            "url": original_url,
            "sha256": original_sha256,
        },
        "crop": None,
        "scale": None,
        "source_duration": float(unit["proxy_duration"] or unit["duration"]),
        "content_duration": float(unit["content_duration"] or unit["duration"]),
        "pre_roll_duration": float(unit["pre_roll_duration"] or 0),
        "post_roll_duration": float(unit["post_roll_duration"] or 0),
    }
    if route.get("processing_mode") in {None, "", DIRECT_PROCESSING_MODE}:
        return direct
    if route.get("processing_mode") != LEVEL2_PROCESSING_MODE:
        raise XmaxBrowserAttemptError(
            "XMAX_INPUT_ROUTE_INVALID",
            "The Unit input route is not supported.",
            status_code=409,
        )
    if route.get("original_source_sha256") != original_sha256:
        raise XmaxBrowserAttemptError(
            "XMAX_ORIGINAL_SOURCE_CHANGED",
            "The original Unit source changed after Level 2 preprocessing.",
            status_code=409,
        )
    preprocessed_path = _local_asset_path(route.get("preprocessed_source_path") or route.get("preprocessed_source_url"))
    if not preprocessed_path:
        raise XmaxBrowserAttemptError(
            "XMAX_PREPROCESSED_SOURCE_UNREADABLE",
            "The Level 2 preprocessed source is not readable.",
            status_code=409,
        )
    preprocessed_sha256 = _sha256_file(preprocessed_path)
    preprocessed_url = _versioned_source_url(
        _controlled_asset_url(preprocessed_path, route.get("preprocessed_source_url")),
        preprocessed_sha256,
    )
    if route.get("preprocessed_source_sha256") != preprocessed_sha256:
        raise XmaxBrowserAttemptError(
            "XMAX_PREPROCESSED_SOURCE_CHANGED",
            "The Level 2 preprocessed source changed after approval.",
            status_code=409,
        )
    crop = route.get("crop")
    scale = route.get("scale")
    if not isinstance(crop, dict) or not all(type(crop.get(field)) is int and crop[field] >= 0 for field in ("x", "y", "width", "height")):
        raise XmaxBrowserAttemptError("XMAX_LEVEL2_CROP_INVALID", "The Level 2 crop snapshot is invalid.", status_code=409)
    if crop["width"] <= 0 or crop["height"] <= 0:
        raise XmaxBrowserAttemptError("XMAX_LEVEL2_CROP_INVALID", "The Level 2 crop dimensions are invalid.", status_code=409)
    if not isinstance(scale, dict) or not all(type(scale.get(field)) is int and scale[field] > 0 for field in ("width", "height")):
        raise XmaxBrowserAttemptError("XMAX_LEVEL2_SCALE_INVALID", "The Level 2 scale snapshot is invalid.", status_code=409)
    return {
        **direct,
        "processing_mode": LEVEL2_PROCESSING_MODE,
        "preserve_current_unit_version": bool(route.get("preserve_current_unit_version", True)),
        "selected_source": {
            "path": str(preprocessed_path),
            "url": preprocessed_url,
            "sha256": preprocessed_sha256,
        },
        "preprocessed_source": {
            "path": str(preprocessed_path),
            "url": preprocessed_url,
            "sha256": preprocessed_sha256,
        },
        "crop": crop,
        "scale": scale,
        "source_duration": float(route.get("source_duration") or unit["content_duration"] or unit["duration"]),
        "content_duration": float(unit["content_duration"] or unit["duration"]),
        "pre_roll_duration": 0.0,
        "post_roll_duration": 0.0,
    }


def configure_level2_input_route(
    *,
    user_id: str,
    task_id: str,
    unit_id: str,
    previous_attempt_id: str | None,
    preprocessed_source_path: Path,
    preprocessed_source_sha256: str,
    crop: dict[str, int],
    scale: dict[str, int],
    source_duration: float,
) -> dict[str, Any]:
    """Attach one audited Level 2 source without changing the current Unit output version."""
    timestamp = now_iso()
    with get_connection() as db:
        unit = db.execute(
            """SELECT s.*,t.project_id,t.episode_id,p.owner_user_id
               FROM xmax_thirty_second_segments s
               JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
               JOIN projects p ON p.id=t.project_id
               WHERE s.id=? AND t.id=?""",
            (unit_id, task_id),
        ).fetchone()
        previous = None
        if previous_attempt_id:
            previous = db.execute(
                "SELECT * FROM xmax_browser_attempts WHERE id=? AND user_id=? AND task_id=? AND unit_id=?",
                (previous_attempt_id, user_id, task_id, unit_id),
            ).fetchone()
        if not unit or unit["owner_user_id"] != user_id or (previous_attempt_id and not previous):
            raise XmaxBrowserAttemptError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
        if previous and previous["settlement_status"] != "settled":
            raise XmaxBrowserAttemptError("XMAX_PREVIOUS_ATTEMPT_NOT_SETTLED", "The previous attempt is not fully settled.", status_code=409)
        original_path = _local_asset_path(unit["source_path"] or unit["source_url"])
        processed = preprocessed_source_path.resolve()
        if not original_path or not processed.is_file():
            raise XmaxBrowserAttemptError("XMAX_PREPROCESSED_SOURCE_UNREADABLE", "The Level 2 source is not readable.", status_code=409)
        actual_preprocessed_sha256 = _sha256_file(processed)
        if actual_preprocessed_sha256 != preprocessed_source_sha256.lower():
            raise XmaxBrowserAttemptError("XMAX_PREPROCESSED_SOURCE_CHANGED", "The Level 2 source SHA-256 does not match.", status_code=409)
        route = {
            "processing_mode": LEVEL2_PROCESSING_MODE,
            "original_source_path": str(original_path),
            "original_source_url": _controlled_asset_url(original_path, unit["source_url"]),
            "original_source_sha256": _sha256_file(original_path),
            "preprocessed_source_path": str(processed),
            "preprocessed_source_url": _controlled_asset_url(processed),
            "preprocessed_source_sha256": actual_preprocessed_sha256,
            "crop": crop,
            "scale": scale,
            "source_duration": float(source_duration),
            "preserve_current_unit_version": True,
            "configured_at": timestamp,
        }
        diagnostics = _object_json(unit["diagnostics_json"])
        existing_route = diagnostics.get("input_route")
        if isinstance(existing_route, dict):
            comparable_existing = {key: value for key, value in existing_route.items() if key != "configured_at"}
            comparable_new = {key: value for key, value in route.items() if key != "configured_at"}
            if comparable_existing == comparable_new:
                return route
        diagnostics["input_route"] = route
        db.execute(
            "UPDATE xmax_thirty_second_segments SET diagnostics_json=?,updated_at=? WHERE id=? AND task_id=?",
            (_json(diagnostics), timestamp, unit_id, task_id),
        )
        if previous:
            render = _object_json(previous["render_json"])
            render["routing_evaluation"] = {
                "result": "incorrect_level1_input",
                "evaluated_at": timestamp,
                "replacement_processing_mode": LEVEL2_PROCESSING_MODE,
                "preprocessed_source_sha256": actual_preprocessed_sha256,
            }
            db.execute(
                "UPDATE xmax_browser_attempts SET render_json=?,updated_at=? WHERE id=? AND user_id=?",
                (_json(render), timestamp, previous_attempt_id, user_id),
            )
    return route


def configure_direct_input_route(*, user_id: str, task_id: str, unit_id: str) -> dict[str, Any]:
    """Restore the ordinary full-frame source for future attempts only."""
    timestamp = now_iso()
    with get_connection() as db:
        unit = db.execute(
            """SELECT s.*,p.owner_user_id FROM xmax_thirty_second_segments s
               JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
               JOIN projects p ON p.id=t.project_id
               WHERE s.id=? AND t.id=?""",
            (unit_id, task_id),
        ).fetchone()
        if not unit or unit["owner_user_id"] != user_id:
            raise XmaxBrowserAttemptError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
        diagnostics = _object_json(unit["diagnostics_json"])
        diagnostics.pop("input_route", None)
        db.execute(
            "UPDATE xmax_thirty_second_segments SET diagnostics_json=?,updated_at=? WHERE id=? AND task_id=?",
            (_json(diagnostics), timestamp, unit_id, task_id),
        )
    return {"processing_mode": DIRECT_PROCESSING_MODE, "provider_calls": 0, "xmax_sessions": 0, "credits_consumed": 0}


def prepare_level2_input(
    *, user_id: str, task_id: str, unit_id: str,
    crop: dict[str, int], scale: dict[str, int],
) -> dict[str, Any]:
    """Generate an immutable, owner-checked Level 2 preview without signing a key."""
    with get_connection() as db:
        unit = db.execute(
            """SELECT s.*,t.project_id,t.episode_id,p.owner_user_id
               FROM xmax_thirty_second_segments s
               JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
               JOIN projects p ON p.id=t.project_id
               WHERE s.id=? AND t.id=?""",
            (unit_id, task_id),
        ).fetchone()
    if not unit or unit["owner_user_id"] != user_id:
        raise XmaxBrowserAttemptError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    source = _local_asset_path(unit["source_path"] or unit["source_url"])
    if not source:
        raise XmaxBrowserAttemptError("XMAX_SOURCE_UNREADABLE", "The Unit source is not readable.", status_code=409)
    source_metadata = media_service.probe_media_metadata(source)
    normalized_crop = {key: int(crop[key]) for key in ("x", "y", "width", "height")}
    normalized_scale = {key: int(scale[key]) for key in ("width", "height")}
    fingerprint_payload = {
        "source_sha256": _sha256_file(source), "crop": normalized_crop, "scale": normalized_scale,
        "start": float(unit["pre_roll_duration"] or 0),
        "duration": float(unit["content_duration"] or unit["duration"]),
    }
    fingerprint = _sha256_text(_json(fingerprint_payload))[:20]
    safe_unit = "".join(character if character.isalnum() or character in "-_" else "_" for character in unit_id)
    output = ROOT / "outputs" / str(unit["project_id"]) / str(unit["episode_id"]) / "xmax-level2" / safe_unit / f"level2-{fingerprint}.mp4"
    if not output.is_file():
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp.mp4")
        try:
            media_service.crop_and_scale_video(
                source, temporary, crop=normalized_crop, scale=normalized_scale,
                start_time=float(unit["pre_roll_duration"] or 0),
                duration=float(unit["content_duration"] or unit["duration"]),
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output.exists():
                os.replace(temporary, output)
        finally:
            if temporary.exists():
                temporary.unlink()
    metadata = media_service.probe_media_metadata(output)
    media_service.decode_media_to_null(output)
    return {
        "task_id": task_id, "unit_id": unit_id,
        "original_source_url": _controlled_asset_url(source, unit["source_url"]),
        "original_source_sha256": fingerprint_payload["source_sha256"],
        "source_width": source_metadata["width"], "source_height": source_metadata["height"],
        "preprocessed_source_path": str(output), "preprocessed_source_url": _controlled_asset_url(output),
        "preprocessed_source_sha256": _sha256_file(output),
        "crop": normalized_crop, "scale": normalized_scale,
        "source_duration": float(metadata["duration"]), "content_duration": fingerprint_payload["duration"],
        "decode_valid": True,
    }


def confirm_level2_input(*, user_id: str, prepared: dict[str, Any]) -> dict[str, Any]:
    """Persist an already prepared Level 2 route; this never creates a Provider attempt."""
    authoritative = prepare_level2_input(
        user_id=user_id,
        task_id=prepared["task_id"],
        unit_id=prepared["unit_id"],
        crop=prepared["crop"],
        scale=prepared["scale"],
    )
    if prepared["preprocessed_source_sha256"].lower() != authoritative["preprocessed_source_sha256"]:
        raise XmaxBrowserAttemptError(
            "XMAX_LEVEL2_PREPARED_SOURCE_MISMATCH",
            "The confirmed Level 2 source does not match the owner-scoped prepared asset.",
            status_code=409,
        )
    with get_connection() as db:
        previous = db.execute(
            """SELECT id FROM xmax_browser_attempts
               WHERE user_id=? AND task_id=? AND unit_id=? AND settlement_status='settled'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (user_id, prepared["task_id"], prepared["unit_id"]),
        ).fetchone()
    route = configure_level2_input_route(
        user_id=user_id, task_id=prepared["task_id"], unit_id=prepared["unit_id"],
        previous_attempt_id=previous["id"] if previous else None,
        preprocessed_source_path=Path(authoritative["preprocessed_source_path"]),
        preprocessed_source_sha256=authoritative["preprocessed_source_sha256"],
        crop=authoritative["crop"], scale=authoritative["scale"], source_duration=authoritative["source_duration"],
    )
    return {"route": route, "provider_calls": 0, "xmax_sessions": 0, "credits_consumed": 0}


def _load_snapshot(
    db: sqlite3.Connection,
    request: PrepareBrowserAttemptRequest,
    *,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    unit = db.execute(
        """SELECT s.*, t.project_id, t.episode_id, t.prompt, t.scene_plan_json,
                  p.owner_user_id
           FROM xmax_thirty_second_segments s
           JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
           JOIN projects p ON p.id=t.project_id
           WHERE s.id=? AND t.id=?""",
        (request.unit_id, request.task_id),
    ).fetchone()
    if not unit or unit["owner_user_id"] != request.user_id:
        raise XmaxBrowserAttemptError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    preparation = _object_json(unit["diagnostics_json"]).get("proxy_preparation")
    proxy_path = _local_asset_path(unit["source_path"] or unit["source_url"])
    try:
        proxy_duration = float(unit["proxy_duration"] or 0)
    except (TypeError, ValueError):
        proxy_duration = 0
    if (
        not isinstance(preparation, dict)
        or preparation.get("status") != "ready"
        or not proxy_path
        or proxy_path.stat().st_size <= 0
        or proxy_duration <= 0
    ):
        raise XmaxBrowserAttemptError(
            "XMAX_UNIT_PROXY_NOT_READY",
            "The Unit proxy is still being prepared. Wait for preparation to complete before starting Xmax.",
            status_code=409,
        )
    input_snapshot = _unit_input_snapshot(unit)
    execution_status = unit["execution_status"] or unit["status"]
    versioned_evaluation = bool(input_snapshot["preserve_current_unit_version"])
    if execution_status not in {"pending", "failed", "interrupted"} and not (execution_status == "completed" and versioned_evaluation):
        raise XmaxBrowserAttemptError(
            "XMAX_UNIT_NOT_GENERATABLE",
            "This Unit is not available for a new generation attempt.",
            status_code=409,
        )
    character_id = str(unit["selected_character_id"] or "")
    if not character_id:
        raise XmaxBrowserAttemptError("CHARACTER_MAPPING_REQUIRED", "The Unit has no approved character mapping.", status_code=409)
    character = db.execute(
        """SELECT * FROM characters
           WHERE id=? AND project_id=? AND confirmed=1 AND priority_level='Locked'""",
        (character_id, unit["project_id"]),
    ).fetchone()
    if not character:
        raise XmaxBrowserAttemptError("CHARACTER_REFERENCE_INVALID", "The mapped character is not approved.", status_code=409)
    prompt = str(prompt_override if prompt_override is not None else unit["prompt"] or "").strip()
    if not prompt:
        raise XmaxBrowserAttemptError("PROMPT_REQUIRED", "The Unit prompt is empty.", status_code=409)
    prompt_hash = _sha256_text(prompt)
    locked_asset = db.execute(
        """SELECT character_asset_version,reference_path,reference_url,reference_sha256,
                  prompt_template_version,prompt_hash
           FROM xmax_browser_attempts
           WHERE user_id=? AND task_id=? AND character_id=?
           ORDER BY created_at,id LIMIT 1""",
        (request.user_id, request.task_id, character_id),
    ).fetchone()
    if locked_asset:
        reference_path = _local_asset_path(locked_asset["reference_path"])
        if not reference_path or _sha256_file(reference_path) != locked_asset["reference_sha256"]:
            raise XmaxBrowserAttemptError(
                "CHARACTER_ASSET_VERSION_MISMATCH",
                "The character asset locked by this visual task is missing or has changed.",
                status_code=409,
            )
        if prompt_override is None and prompt_hash != locked_asset["prompt_hash"]:
            raise XmaxBrowserAttemptError(
                "CHARACTER_PROMPT_VERSION_MISMATCH",
                "The base character prompt changed after this visual task was locked.",
                status_code=409,
            )
        reference_url = locked_asset["reference_url"]
        reference_sha256 = locked_asset["reference_sha256"]
        character_asset_version = locked_asset["character_asset_version"]
        prompt_template_version = (
            f"{PROMPT_TEMPLATE_VERSION_PREFIX}-offline-charx-{prompt_hash[:12]}"
            if prompt_override is not None
            else locked_asset["prompt_template_version"]
        )
    else:
        candidates = (
            character["target_reference_path"],
            character["main_reference_image"],
            unit["reference_path"],
            character["source_reference_image"],
        )
        reference_path = next((path for value in candidates if (path := _local_asset_path(value))), None)
        if not reference_path:
            raise XmaxBrowserAttemptError("CHARACTER_REFERENCE_UNREADABLE", "The approved character reference is not readable.", status_code=409)
        reference_sha256 = _sha256_file(reference_path)
        reference_url = _controlled_asset_url(reference_path, unit["reference_url"])
        character_asset_version = f"charasset-{reference_sha256[:20]}"
        prompt_template_version = (
            f"{PROMPT_TEMPLATE_VERSION_PREFIX}-offline-charx-{prompt_hash[:12]}"
            if prompt_override is not None
            else f"{PROMPT_TEMPLATE_VERSION_PREFIX}-{prompt_hash[:12]}"
        )
    task = {"scene_plan_json": unit["scene_plan_json"]}
    return {
        "project_id": unit["project_id"],
        "episode_id": unit["episode_id"],
        "unit_id": unit["id"],
        "task_id": request.task_id,
        "character_id": character_id,
        "character_asset_version": character_asset_version,
        "reference_path": str(reference_path),
        "reference_url": reference_url,
        "reference_sha256": reference_sha256,
        "prompt": prompt,
        "prompt_hash": prompt_hash,
        "prompt_template_version": prompt_template_version,
        "source_shot_ids": _source_shot_ids(task, unit),
        "input_snapshot": input_snapshot,
    }


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _public_attempt(row: sqlite3.Row | dict[str, Any], temporary_record: dict[str, Any] | None = None) -> dict[str, Any]:
    item = dict(row)
    item.pop("reference_path", None)
    item["source_shot_ids"] = json.loads(item.pop("source_shot_ids_json", "[]") or "[]")
    item["playback"] = json.loads(item.pop("playback_json", "{}") or "{}")
    item.pop("context_json", None)
    render = _object_json(item.pop("render_json", None))
    item["execution_mode"] = (
        OFFLINE_EXECUTION_MODE
        if render.get("execution_mode") == OFFLINE_EXECUTION_MODE
        else REALTIME_EXECUTION_MODE
    )
    offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
    item["offline"] = {
        "status": offline.get("status"),
        "provider_video_url": offline.get("provider_video_url"),
        "provider_reference_url": offline.get("provider_reference_url"),
        "charge_points": offline.get("charge_points"),
        "processor_id": offline.get("processor_id"),
        "retry_count": offline.get("retry_count"),
        "result_url": offline.get("result_url"),
        "failure_diagnostics": offline.get("failure_diagnostics"),
        "submitted_at": offline.get("submitted_at"),
        "updated_at": offline.get("updated_at"),
    }
    snapshot = render.get("input_snapshot") if isinstance(render.get("input_snapshot"), dict) else {}
    original = snapshot.get("original_source") if isinstance(snapshot.get("original_source"), dict) else {}
    selected = snapshot.get("selected_source") if isinstance(snapshot.get("selected_source"), dict) else {}
    preprocessed = snapshot.get("preprocessed_source") if isinstance(snapshot.get("preprocessed_source"), dict) else {}
    item["processing_mode"] = snapshot.get("processing_mode", DIRECT_PROCESSING_MODE)
    item["input_snapshot"] = {
        "original_source_url": original.get("url"),
        "original_source_sha256": original.get("sha256"),
        "selected_source_url": selected.get("url"),
        "selected_source_sha256": selected.get("sha256"),
        "preprocessed_source_url": preprocessed.get("url"),
        "preprocessed_source_sha256": preprocessed.get("sha256"),
        "crop": snapshot.get("crop"),
        "scale": snapshot.get("scale"),
        "preserve_current_unit_version": bool(snapshot.get("preserve_current_unit_version")),
        "preserved_source_by_user": bool(snapshot.get("preserved_source_by_user")),
    }
    item["routing_evaluation"] = render.get("routing_evaluation")
    item["failure_diagnostics"] = render.get("failure_diagnostics")
    raw_output = render.get("raw_output") if isinstance(render.get("raw_output"), dict) else {}
    normalization = render.get("normalization") if isinstance(render.get("normalization"), dict) else {}
    item["raw_output_persisted"] = raw_output.get("status") == "persisted"
    item["raw_output_url"] = raw_output.get("url")
    item["raw_output_sha256"] = raw_output.get("sha256")
    item["raw_output_size"] = raw_output.get("size")
    item["normalization_status"] = normalization.get("status")
    item["normalization_error_code"] = normalization.get("error_code")
    item["normalization_diagnostics_url"] = normalization.get("diagnostics_url")
    item["output_saved"] = bool(item.get("output_id") and item.get("output_url"))
    if temporary_record is not None:
        item["temporary_key"] = {
            "id": temporary_record.get("id"),
            "points_limit": temporary_record.get("points_limit"),
            "consumed_points": temporary_record.get("consumed_points"),
            "remaining_points": temporary_record.get("remaining_points"),
            "local_status": temporary_record.get("local_status"),
            "provider_status": temporary_record.get("provider_status"),
            "session_uid": temporary_record.get("session_uid"),
            "last_error_code": temporary_record.get("last_error_code"),
            "last_error_message": temporary_record.get("last_error_message"),
        }
    return item


def _record(record_id: str, user_id: str) -> sqlite3.Row:
    with get_connection() as db:
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=? AND user_id=?", (record_id, user_id)).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    return row


def prepare_browser_attempt(request: PrepareBrowserAttemptRequest) -> dict[str, Any]:
    key = request.idempotency_key.strip()
    if len(key) < 8 or len(key) > 160:
        raise XmaxBrowserAttemptError("INVALID_IDEMPOTENCY_KEY", "A valid idempotency key is required.")
    with get_connection() as db:
        existing = db.execute(
            "SELECT * FROM xmax_browser_attempts WHERE idempotency_key=? AND user_id=?",
            (key, request.user_id),
        ).fetchone()
        if existing and (existing["task_id"] != request.task_id or existing["unit_id"] != request.unit_id):
            raise XmaxBrowserAttemptError(
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key belongs to another Unit attempt.",
                status_code=409,
            )
        active = None if existing else db.execute(
            """SELECT id FROM xmax_browser_attempts
               WHERE user_id=? AND unit_id=?
                 AND settlement_status IN ('not_started','pending','conflict')
               ORDER BY created_at DESC LIMIT 1""",
            (request.user_id, request.unit_id),
        ).fetchone()
        if active:
            raise XmaxBrowserAttemptError(
                "XMAX_UNIT_ATTEMPT_ACTIVE",
                "This Unit already has an attempt awaiting completion or settlement.",
                status_code=409,
            )
        snapshot = None if existing else _load_snapshot(db, request)
    attempt_id = existing["id"] if existing else f"xba-{hashlib.sha256(f'{request.user_id}|{request.unit_id}|{key}'.encode()).hexdigest()[:24]}"
    project_id = existing["project_id"] if existing else snapshot["project_id"]
    episode_id = existing["episode_id"] if existing else snapshot["episode_id"]
    try:
        grant = temporary_keys.prepare_temporary_key(
            temporary_keys.PrepareRequest(
                user_id=request.user_id,
                project_id=project_id,
                episode_id=episode_id,
                unit_id=request.unit_id,
                attempt_id=attempt_id,
                requested_points_limit=request.requested_points_limit,
                expire_seconds=request.expire_seconds,
                idempotency_key=f"p4:{key}",
                allow_completed_version_attempt=bool(
                    snapshot
                    and snapshot["input_snapshot"].get("preserve_current_unit_version")
                ),
            )
        )
    except (temporary_keys.XmaxTemporaryKeyError, temporary_keys.SecretEncryptionError):
        raise
    if not existing:
        assert snapshot is not None
        timestamp = now_iso()
        try:
            with get_connection() as db:
                db.execute(
                    """INSERT INTO xmax_browser_attempts
                       (id,user_id,project_id,episode_id,task_id,unit_id,source_shot_ids_json,
                        character_id,character_asset_version,reference_path,reference_url,reference_sha256,
                        prompt_template_version,prompt_hash,model,playback_json,context_json,render_json,
                        temporary_key_record_id,status,settlement_status,idempotency_key,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        attempt_id, request.user_id, snapshot["project_id"], snapshot["episode_id"],
                        request.task_id, request.unit_id, _json(snapshot["source_shot_ids"]),
                        snapshot["character_id"], snapshot["character_asset_version"], snapshot["reference_path"],
                        snapshot["reference_url"], snapshot["reference_sha256"], snapshot["prompt_template_version"],
                        snapshot["prompt_hash"], MODEL_NAME, _json(PLAYBACK),
                        _json({"prompt": snapshot["prompt"], "refImageUrl": snapshot["reference_url"], "character_asset_version": snapshot["character_asset_version"]}),
                        _json({"localContainer": True, "remoteContainer": True, "input_snapshot": snapshot["input_snapshot"]}), grant["internal_temporary_key_id"],
                        "prepared", "not_started", key, timestamp, timestamp,
                    ),
                )
        except Exception as error:
            try:
                temporary_keys.abort_temporary_key(grant["internal_temporary_key_id"], request.user_id)
            except Exception:
                pass
            if isinstance(error, sqlite3.IntegrityError):
                raise XmaxBrowserAttemptError(
                    "XMAX_UNIT_ATTEMPT_ACTIVE",
                    "This Unit already has an attempt awaiting completion or settlement.",
                    status_code=409,
                ) from error
            raise
    row = _record(attempt_id, request.user_id)
    with get_connection() as db:
        context_row = db.execute("SELECT context_json,render_json FROM xmax_browser_attempts WHERE id=?", (attempt_id,)).fetchone()
    runtime_context = json.loads(context_row["context_json"])
    input_snapshot = _object_json(context_row["render_json"]).get("input_snapshot") or {}
    selected_source = input_snapshot.get("selected_source") or {}
    return {
        "attempt": _public_attempt(row),
        "temporaryApiKey": grant["temporaryApiKey"],
        "expireTimestamp": grant["expireTimestamp"],
        "pointsLimit": grant["pointsLimit"],
        "idempotent": bool(existing or grant.get("idempotent")),
        "runtime": {
            "model": row["model"],
            "playback": json.loads(row["playback_json"]),
            "prompt": runtime_context["prompt"],
            "refImageUrl": row["reference_url"],
            "characterAssetVersion": row["character_asset_version"],
            "processingMode": input_snapshot.get("processing_mode", DIRECT_PROCESSING_MODE),
            "sourceUrl": selected_source.get("url"),
            "sourceSha256": selected_source.get("sha256"),
            "sourceDuration": input_snapshot.get("source_duration"),
            "contentDuration": input_snapshot.get("content_duration"),
            "preRollDuration": input_snapshot.get("pre_roll_duration", 0),
            "postRollDuration": input_snapshot.get("post_roll_duration", 0),
            "crop": input_snapshot.get("crop"),
            "scale": input_snapshot.get("scale"),
            "preserveCurrentUnitVersion": bool(input_snapshot.get("preserve_current_unit_version")),
        },
    }


def prepare_offline_attempt(request: PrepareBrowserAttemptRequest) -> dict[str, Any]:
    key = request.idempotency_key.strip()
    if len(key) < 8 or len(key) > 160:
        raise XmaxBrowserAttemptError("INVALID_IDEMPOTENCY_KEY", "A valid idempotency key is required.")
    if type(request.requested_points_limit) is not int or request.requested_points_limit <= 0:
        raise XmaxBrowserAttemptError("INVALID_POINTS_LIMIT", "A positive whole-number credit limit is required.")

    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM xmax_browser_attempts WHERE idempotency_key=? AND user_id=?",
            (key, request.user_id),
        ).fetchone()
        if existing:
            if existing["task_id"] != request.task_id or existing["unit_id"] != request.unit_id:
                raise XmaxBrowserAttemptError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key belongs to another Unit attempt.",
                    status_code=409,
                )
            if _execution_mode(existing) != OFFLINE_EXECUTION_MODE:
                raise XmaxBrowserAttemptError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key belongs to a realtime attempt.",
                    status_code=409,
                )
            return {"attempt": _public_attempt(existing), "idempotent": True}

        active = db.execute(
            """SELECT * FROM xmax_browser_attempts
               WHERE user_id=? AND unit_id=?
                 AND settlement_status IN ('not_started','pending','conflict')
               ORDER BY created_at DESC LIMIT 1""",
            (request.user_id, request.unit_id),
        ).fetchone()
        if active:
            if _execution_mode(active) == OFFLINE_EXECUTION_MODE:
                return {"attempt": _public_attempt(active), "idempotent": True}
            raise XmaxBrowserAttemptError(
                "XMAX_UNIT_ATTEMPT_ACTIVE",
                "This Unit already has a realtime attempt awaiting completion or settlement.",
                status_code=409,
            )

        snapshot = _load_snapshot(db, request)
        attempt_id = f"xba-{hashlib.sha256(f'{request.user_id}|{request.unit_id}|{key}'.encode()).hexdigest()[:24]}"
        timestamp = now_iso()
        try:
            freeze = credit_service.freeze_in_transaction(
                db,
                request.user_id,
                request.requested_points_limit,
                f"xmax:offline:freeze:{attempt_id}",
                task_id=request.task_id,
                unit_id=request.unit_id,
            )
        except credit_service.CreditError as error:
            raise XmaxBrowserAttemptError(error.code, error.message, status_code=409) from error

        render = {
            "execution_mode": OFFLINE_EXECUTION_MODE,
            "localContainer": False,
            "remoteContainer": False,
            "input_snapshot": snapshot["input_snapshot"],
            "offline": {
                "status": "prepared",
                "quality": "sd",
                "points_limit": request.requested_points_limit,
                "freeze_ledger_id": freeze["ledger"]["id"],
                "submit_attempt_count": 0,
                "updated_at": timestamp,
            },
        }
        try:
            db.execute(
                """INSERT INTO xmax_browser_attempts
                   (id,user_id,project_id,episode_id,task_id,unit_id,source_shot_ids_json,
                    character_id,character_asset_version,reference_path,reference_url,reference_sha256,
                    prompt_template_version,prompt_hash,model,playback_json,context_json,render_json,
                    temporary_key_record_id,offline_task_uid,status,settlement_status,idempotency_key,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, request.user_id, snapshot["project_id"], snapshot["episode_id"],
                    request.task_id, request.unit_id, _json(snapshot["source_shot_ids"]),
                    snapshot["character_id"], snapshot["character_asset_version"], snapshot["reference_path"],
                    snapshot["reference_url"], snapshot["reference_sha256"], snapshot["prompt_template_version"],
                    snapshot["prompt_hash"], MODEL_NAME, _json(PLAYBACK),
                    _json({
                        "prompt": snapshot["prompt"],
                        "refImageUrl": snapshot["reference_url"],
                        "character_asset_version": snapshot["character_asset_version"],
                    }),
                    _json(render), None, None, "prepared", "not_started", key, timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise XmaxBrowserAttemptError(
                "XMAX_UNIT_ATTEMPT_ACTIVE",
                "This Unit already has an attempt awaiting completion or settlement.",
                status_code=409,
            ) from error
        created = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (attempt_id,)).fetchone()
    return {"attempt": _public_attempt(created), "idempotent": False}


def get_offline_attempt_runtime(record_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
    if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
        raise XmaxBrowserAttemptError("XMAX_OFFLINE_ATTEMPT_NOT_FOUND", "Offline attempt not found.", status_code=404)
    render = _object_json(row["render_json"])
    context = _object_json(row["context_json"])
    snapshot = render.get("input_snapshot") if isinstance(render.get("input_snapshot"), dict) else {}
    selected = snapshot.get("selected_source") if isinstance(snapshot.get("selected_source"), dict) else {}
    return {
        **_row_dict(row),
        "prompt": str(context.get("prompt") or ""),
        "selected_source_path": str(selected.get("path") or ""),
        "selected_source_sha256": str(selected.get("sha256") or ""),
        "offline": render.get("offline") if isinstance(render.get("offline"), dict) else {},
        "preserve_current_unit_version": bool(snapshot.get("preserve_current_unit_version")),
    }


def update_offline_metadata(record_id: str, **values: Any) -> dict[str, Any]:
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
        if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
            raise XmaxBrowserAttemptError("XMAX_OFFLINE_ATTEMPT_NOT_FOUND", "Offline attempt not found.", status_code=404)
        render = _object_json(row["render_json"])
        offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
        offline.update(values)
        offline["updated_at"] = now_iso()
        render["offline"] = offline
        db.execute(
            "UPDATE xmax_browser_attempts SET render_json=?,updated_at=? WHERE id=?",
            (_json(render), offline["updated_at"], record_id),
        )
        updated = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
    return _public_attempt(updated)


def claim_offline_worker(record_id: str, worker_token: str, *, lease_seconds: int) -> bool:
    now = datetime.now(timezone.utc)
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
        if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
            raise XmaxBrowserAttemptError("XMAX_OFFLINE_ATTEMPT_NOT_FOUND", "Offline attempt not found.", status_code=404)
        if row["settlement_status"] == "settled":
            return False
        render = _object_json(row["render_json"])
        offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
        lease_until_raw = str(offline.get("worker_lease_until") or "")
        try:
            lease_until = datetime.fromisoformat(lease_until_raw)
        except ValueError:
            lease_until = None
        if lease_until and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if offline.get("worker_token") and lease_until and lease_until > now:
            return False
        offline.update({
            "worker_token": worker_token,
            "worker_claimed_at": now.isoformat(),
            "worker_lease_until": (now + timedelta(seconds=max(60, lease_seconds))).isoformat(),
            "updated_at": now.isoformat(),
        })
        render["offline"] = offline
        db.execute(
            "UPDATE xmax_browser_attempts SET render_json=?,updated_at=? WHERE id=?",
            (_json(render), now_iso(), record_id),
        )
    return True


def release_offline_worker(record_id: str, worker_token: str) -> None:
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
        if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
            return
        render = _object_json(row["render_json"])
        offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
        if offline.get("worker_token") != worker_token:
            return
        offline.pop("worker_token", None)
        offline.pop("worker_lease_until", None)
        offline["updated_at"] = now_iso()
        render["offline"] = offline
        db.execute(
            "UPDATE xmax_browser_attempts SET render_json=?,updated_at=? WHERE id=?",
            (_json(render), offline["updated_at"], record_id),
        )


def bind_offline_task_uid(record_id: str, offline_task_uid: str, **metadata: Any) -> dict[str, Any]:
    uid = offline_task_uid.strip()
    if not uid:
        raise XmaxBrowserAttemptError("XMAX_OFFLINE_TASK_UID_REQUIRED", "An Offline task UID is required.")
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
        if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
            raise XmaxBrowserAttemptError("XMAX_OFFLINE_ATTEMPT_NOT_FOUND", "Offline attempt not found.", status_code=404)
        if row["offline_task_uid"] and row["offline_task_uid"] != uid:
            raise XmaxBrowserAttemptError(
                "XMAX_OFFLINE_TASK_BINDING_CONFLICT",
                "This attempt is already bound to another Offline task.",
                status_code=409,
            )
        render = _object_json(row["render_json"])
        offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
        offline.update(metadata)
        offline.update({"status": metadata.get("status") or "submitted", "updated_at": now_iso()})
        render["offline"] = offline
        try:
            db.execute(
                "UPDATE xmax_browser_attempts SET offline_task_uid=?,render_json=?,updated_at=? WHERE id=?",
                (uid, _json(render), offline["updated_at"], record_id),
            )
        except sqlite3.IntegrityError as error:
            raise XmaxBrowserAttemptError(
                "XMAX_OFFLINE_TASK_BINDING_CONFLICT",
                "This Offline task is already bound to another attempt.",
                status_code=409,
            ) from error
        updated = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
    return _public_attempt(updated)


def settle_offline_attempt(record_id: str, charge_points: int) -> dict[str, Any]:
    if type(charge_points) is not int or charge_points < 0:
        raise XmaxBrowserAttemptError("XMAX_OFFLINE_CHARGE_INVALID", "Offline chargePoints is invalid.", status_code=502)
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
        if not row or _execution_mode(row) != OFFLINE_EXECUTION_MODE:
            raise XmaxBrowserAttemptError("XMAX_OFFLINE_ATTEMPT_NOT_FOUND", "Offline attempt not found.", status_code=404)
        if row["settlement_status"] == "settled":
            return _public_attempt(row)
        render = _object_json(row["render_json"])
        offline = render.get("offline") if isinstance(render.get("offline"), dict) else {}
        points_limit = int(offline.get("points_limit") or 0)
        if charge_points > points_limit:
            db.execute(
                "UPDATE xmax_browser_attempts SET settlement_status='conflict',last_error_code=?,last_error_message=?,updated_at=? WHERE id=?",
                (
                    "XMAX_SETTLEMENT_CONFLICT",
                    "Provider chargePoints exceeds the frozen credit limit.",
                    now_iso(),
                    record_id,
                ),
            )
            raise XmaxBrowserAttemptError(
                "XMAX_SETTLEMENT_CONFLICT",
                "Provider chargePoints exceeds the frozen credit limit.",
                status_code=409,
            )
        consume_id = offline.get("consume_ledger_id")
        release_id = offline.get("release_ledger_id")
        if charge_points > 0 and not consume_id:
            consume = credit_service.consume_frozen_in_transaction(
                db,
                row["user_id"],
                charge_points,
                f"xmax:offline:consume:{record_id}",
                task_id=row["task_id"],
                unit_id=row["unit_id"],
                session_uid=row["offline_task_uid"],
            )
            consume_id = consume["ledger"]["id"]
        released_points = points_limit - charge_points
        if released_points > 0 and not release_id:
            release = credit_service.release_frozen_in_transaction(
                db,
                row["user_id"],
                released_points,
                f"xmax:offline:release:{record_id}",
                task_id=row["task_id"],
                unit_id=row["unit_id"],
            )
            release_id = release["ledger"]["id"]
        offline.update({
            "charge_points": charge_points,
            "consume_ledger_id": consume_id,
            "release_ledger_id": release_id,
            "settled_at": now_iso(),
            "updated_at": now_iso(),
        })
        render["offline"] = offline
        status = "settled" if row["status"] == "output_saved" else row["status"]
        db.execute(
            """UPDATE xmax_browser_attempts
               SET status=?,settlement_status='settled',consumed_points=?,released_points=?,
                   render_json=?,updated_at=?,completed_at=COALESCE(completed_at,?) WHERE id=?""",
            (
                status, charge_points, released_points, _json(render), now_iso(), now_iso(), record_id,
            ),
        )
        updated = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=?", (record_id,)).fetchone()
    return _public_attempt(updated)


def preserves_current_unit_version_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
) -> bool:
    row = db.execute(
        "SELECT render_json FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    snapshot = _object_json(row["render_json"]).get("input_snapshot")
    return bool(isinstance(snapshot, dict) and snapshot.get("preserve_current_unit_version"))


def execution_mode_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
) -> str:
    row = db.execute(
        "SELECT render_json FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    return _execution_mode(row)


def preserves_source_selection_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
) -> bool:
    row = db.execute(
        "SELECT render_json FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    snapshot = _object_json(row["render_json"]).get("input_snapshot")
    return bool(isinstance(snapshot, dict) and snapshot.get("preserved_source_by_user"))


def latest_playable_attempt_in_transaction(
    db: sqlite3.Connection,
    *,
    task_id: str,
    unit_id: str,
) -> dict[str, Any] | None:
    """Return the latest settled, readable output attempt for one Unit."""
    rows = db.execute(
        """SELECT * FROM xmax_browser_attempts
           WHERE task_id=? AND unit_id=? AND status='settled' AND settlement_status='settled'
             AND output_path IS NOT NULL AND output_url IS NOT NULL
           ORDER BY created_at DESC,rowid DESC""",
        (task_id, unit_id),
    ).fetchall()
    for row in rows:
        output_path = Path(str(row["output_path"] or ""))
        if output_path.is_file() and output_path.stat().st_size > 0:
            return _row_dict(row)
    return None


def get_attempt(record_id: str, user_id: str) -> dict[str, Any]:
    row = _record(record_id, user_id)
    temporary = (
        temporary_keys.get_record(row["temporary_key_record_id"], user_id)
        if row["temporary_key_record_id"]
        else None
    )
    return _public_attempt(row, temporary)


def bind_session(record_id: str, user_id: str, session_uid: str) -> dict[str, Any]:
    row = _record(record_id, user_id)
    uid = session_uid.strip()
    if not uid:
        raise XmaxBrowserAttemptError("XMAX_SESSION_UID_REQUIRED", "A Session UID is required.")
    if row["session_uid"] and row["session_uid"] != uid:
        raise XmaxBrowserAttemptError("XMAX_SESSION_BINDING_CONFLICT", "This attempt is already bound to another Session.", status_code=409)
    temporary_keys.bind_session_uid(row["temporary_key_record_id"], user_id, uid)
    with get_connection() as db:
        try:
            db.execute(
                "UPDATE xmax_browser_attempts SET session_uid=?,status='session_bound',updated_at=? WHERE id=? AND user_id=?",
                (uid, now_iso(), record_id, user_id),
            )
        except sqlite3.IntegrityError as error:
            raise XmaxBrowserAttemptError("XMAX_SESSION_BINDING_CONFLICT", "This Session is already bound.", status_code=409) from error
    return get_attempt(record_id, user_id)


def record_raw_output_persisted_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
    raw_path: Path,
    raw_url: str,
    output_mime_type: str,
) -> bool:
    """Record one immutable browser Remote before any validation or normalization."""
    row = db.execute(
        "SELECT * FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    if not _provider_authority_bound(row):
        raise XmaxBrowserAttemptError("XMAX_PROVIDER_TASK_NOT_BOUND", "The Xmax execution was not bound before raw output persistence.", status_code=409)
    if not raw_path.is_file() or raw_path.stat().st_size <= 0:
        raise XmaxBrowserAttemptError("XMAX_RAW_OUTPUT_MISSING", "The immutable Remote output is missing.", status_code=409)

    raw_sha256 = _sha256_file(raw_path)
    raw_size = raw_path.stat().st_size
    render = _object_json(row["render_json"])
    existing = render.get("raw_output") if isinstance(render.get("raw_output"), dict) else {}
    if existing:
        if (
            existing.get("sha256") != raw_sha256
            or int(existing.get("size") or 0) != raw_size
            or Path(str(existing.get("path") or "")).resolve() != raw_path.resolve()
        ):
            raise XmaxBrowserAttemptError(
                "XMAX_RAW_OUTPUT_IMMUTABLE_CONFLICT",
                "This attempt already has a different immutable Remote output.",
                status_code=409,
            )
        return False

    timestamp = now_iso()
    render["raw_output"] = {
        "status": "persisted",
        "path": str(raw_path),
        "url": raw_url,
        "sha256": raw_sha256,
        "size": raw_size,
        "mime_type": output_mime_type,
        "persisted_at": timestamp,
    }
    render["normalization"] = {"status": "pending", "updated_at": timestamp}
    db.execute(
        """UPDATE xmax_browser_attempts
           SET status=CASE WHEN status IN ('prepared','session_bound') THEN 'session_bound' ELSE status END,
               render_json=?,updated_at=?
           WHERE id=?""",
        (_json(render), timestamp, record_id),
    )
    return True


def record_normalization_failed_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
    error_code: str,
    error_message: str,
    diagnostics_url: str | None,
    diagnostics: dict[str, Any],
) -> None:
    """Preserve raw-output evidence while recording a recoverable local normalization failure."""
    row = db.execute(
        "SELECT * FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    render = _object_json(row["render_json"])
    raw_output = render.get("raw_output") if isinstance(render.get("raw_output"), dict) else {}
    if raw_output.get("status") != "persisted":
        raise XmaxBrowserAttemptError(
            "XMAX_RAW_OUTPUT_NOT_PERSISTED",
            "Normalization failure cannot be recorded before the immutable Remote is saved.",
            status_code=409,
        )
    timestamp = now_iso()
    render["normalization"] = {
        "status": "failed",
        "error_code": error_code[:120],
        "error_message": error_message[:500],
        "diagnostics_url": diagnostics_url,
        "diagnostics": diagnostics,
        "updated_at": timestamp,
    }
    db.execute(
        """UPDATE xmax_browser_attempts
           SET status='persistence_failed',render_json=?,last_error_code=?,last_error_message=?,updated_at=?
           WHERE id=?""",
        (_json(render), error_code[:120], error_message[:500], timestamp, record_id),
    )


def record_output_saved_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    task_id: str,
    unit_id: str,
    output_path: Path,
    output_url: str,
    output_duration: float,
    output_mime_type: str,
) -> str:
    row = db.execute(
        "SELECT * FROM xmax_browser_attempts WHERE id=? AND task_id=? AND unit_id=?",
        (record_id, task_id, unit_id),
    ).fetchone()
    if not row:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    if not _provider_authority_bound(row):
        raise XmaxBrowserAttemptError("XMAX_PROVIDER_TASK_NOT_BOUND", "The Xmax execution was not bound before output persistence.", status_code=409)
    output_id = row["output_id"] or f"xbo-{uuid.uuid4().hex}"
    timestamp = now_iso()
    output_sha256 = _sha256_file(output_path)
    render = _object_json(row["render_json"])
    render["normalization"] = {
        "status": "ready",
        "output_path": str(output_path),
        "output_url": output_url,
        "output_sha256": output_sha256,
        "output_size": output_path.stat().st_size,
        "output_duration": output_duration,
        "updated_at": timestamp,
    }
    db.execute(
        """UPDATE xmax_browser_attempts SET output_id=?,output_path=?,output_url=?,output_duration=?,
                  output_size=?,output_mime_type=?,output_sha256=?,status='output_saved',render_json=?,updated_at=?
           WHERE id=?""",
        (
            output_id, str(output_path), output_url, output_duration, output_path.stat().st_size,
            output_mime_type, output_sha256, _json(render), timestamp, record_id,
        ),
    )
    return output_id


def record_quality_status_in_transaction(
    db: sqlite3.Connection,
    *,
    record_id: str,
    user_id: str,
    task_id: str,
    unit_id: str,
    quality_status: str,
    quality_reason: str,
    reviewer: str,
) -> bool:
    """Persist a P4 quality decision once and bind it to the exact saved output attempt."""
    if quality_status not in QUALITY_STATUSES:
        raise XmaxBrowserAttemptError("QUALITY_STATUS_INVALID", "The requested quality status is invalid.", status_code=422)
    attempt = db.execute(
        "SELECT * FROM xmax_browser_attempts WHERE id=? AND user_id=? AND task_id=? AND unit_id=?",
        (record_id, user_id, task_id, unit_id),
    ).fetchone()
    if not attempt:
        raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt not found.", status_code=404)
    if not _provider_authority_bound(attempt):
        raise XmaxBrowserAttemptError("XMAX_PROVIDER_TASK_NOT_BOUND", "The Xmax execution is not bound to this attempt.", status_code=409)
    if not attempt["output_id"] or not attempt["output_path"] or not attempt["output_sha256"]:
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_NOT_SAVED", "The Remote output must be saved before quality review.", status_code=409)
    segment = db.execute(
        "SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?",
        (unit_id, task_id),
    ).fetchone()
    if not segment:
        raise XmaxBrowserAttemptError("XMAX_UNIT_NOT_FOUND", "The requested Unit was not found.", status_code=404)
    if segment["status"] != "completed" or segment["execution_status"] != "completed":
        raise XmaxBrowserAttemptError("QUALITY_REVIEW_REQUIRES_COMPLETED", "The Remote output must be completed before quality review.", status_code=409)
    reason = (quality_reason or "")[:1000] or None
    actor = (reviewer or "manual_review")[:120]
    render = _object_json(attempt["render_json"])
    snapshot = render.get("input_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("preserve_current_unit_version"):
        existing_decision = render.get("quality_evaluation") if isinstance(render.get("quality_evaluation"), dict) else {}
        decision_fields = {
            "quality_status": quality_status,
            "quality_reason": reason,
            "quality_reviewed_by": actor,
            "output_sha256": attempt["output_sha256"],
        }
        unchanged = all(existing_decision.get(key) == value for key, value in decision_fields.items())
        if not unchanged:
            decision = {**decision_fields, "quality_reviewed_at": now_iso()}
            render["quality_evaluation"] = decision
            db.execute(
                "UPDATE xmax_browser_attempts SET render_json=?,updated_at=? WHERE id=?",
                (_json(render), decision["quality_reviewed_at"], record_id),
            )
        return not unchanged
    unchanged = (
        segment["quality_status"] == quality_status
        and (segment["quality_reason"] or None) == reason
        and (segment["quality_reviewed_by"] or actor) == actor
    )
    if not unchanged:
        reviewed_at = now_iso()
        db.execute(
            """UPDATE xmax_thirty_second_segments
               SET quality_status=?,quality_reason=?,quality_reviewed_at=?,quality_reviewed_by=?,updated_at=?
               WHERE id=? AND task_id=?""",
            (quality_status, reason, reviewed_at, actor, reviewed_at, unit_id, task_id),
        )
    return not unchanged


def _require_ledger_row(
    db: sqlite3.Connection,
    ledger_id: str | None,
    *,
    expected_type: str,
    expected_amount: int,
    user_id: str,
    task_id: str,
    unit_id: str,
    session_uid: str | None = None,
) -> sqlite3.Row:
    if not ledger_id:
        raise XmaxBrowserAttemptError("XMAX_SETTLEMENT_LEDGER_INCOMPLETE", f"The {expected_type} ledger entry is missing.", status_code=409)
    row = db.execute("SELECT * FROM credit_ledger WHERE id=?", (ledger_id,)).fetchone()
    if (
        not row
        or row["type"] != expected_type
        or row["amount"] != expected_amount
        or row["user_id"] != user_id
        or row["task_id"] != task_id
        or row["unit_id"] != unit_id
        or (session_uid is not None and row["session_uid"] != session_uid)
    ):
        raise XmaxBrowserAttemptError("XMAX_SETTLEMENT_LEDGER_MISMATCH", f"The {expected_type} ledger entry does not match this attempt.", status_code=409)
    return row


def recover_settled_quality_attempt(
    record_id: str,
    user_id: str,
    *,
    expected_sha256: str,
    session_uid: str,
    quality_status: str,
    quality_reason: str,
) -> dict[str, Any]:
    """Recover a saved, settled attempt without issuing keys, touching credits, or calling Xmax."""
    if quality_status not in QUALITY_STATUSES:
        raise XmaxBrowserAttemptError("QUALITY_STATUS_INVALID", "The requested quality status is invalid.", status_code=422)
    attempt = _record(record_id, user_id)
    existing_render = json.loads(attempt["render_json"] or "{}")
    if attempt["status"] == "settled" and existing_render.get("quality_recovery_audit"):
        return {"attempt": get_attempt(record_id, user_id), "recovery": {"idempotent": True}}
    if attempt["status"] != "persistence_failed" or attempt["settlement_status"] != "settled":
        raise XmaxBrowserAttemptError("XMAX_ATTEMPT_NOT_RECOVERABLE", "Only a settled persistence failure can be recovered.", status_code=409)
    expected = expected_sha256.strip().lower()
    if expected != str(attempt["output_sha256"] or "").lower():
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_SHA256_MISMATCH", "The expected Remote SHA-256 does not match the attempt record.", status_code=409)
    if session_uid.strip() != str(attempt["session_uid"] or ""):
        raise XmaxBrowserAttemptError("XMAX_SESSION_UID_MISMATCH", "The supplied Session UID does not match the attempt.", status_code=409)
    output_path = Path(str(attempt["output_path"] or ""))
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_MISSING", "The saved Remote output is missing.", status_code=409)
    actual_sha256 = _sha256_file(output_path)
    if actual_sha256 != expected:
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_SHA256_MISMATCH", "The saved Remote output bytes do not match the recorded SHA-256.", status_code=409)
    try:
        metadata = media_service.probe_video_metadata(output_path)
        decode = media_service.inspect_video_decode_health(output_path, float(metadata["duration"]))
    except Exception as error:
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_DECODE_FAILED", "The saved Remote output cannot be completely decoded.", status_code=409) from error
    if not decode.get("decode_succeeded"):
        raise XmaxBrowserAttemptError("XMAX_OUTPUT_DECODE_FAILED", "The saved Remote output cannot be completely decoded.", status_code=409)
    timestamp = now_iso()
    with get_connection() as db:
        current = db.execute("SELECT * FROM xmax_browser_attempts WHERE id=? AND user_id=?", (record_id, user_id)).fetchone()
        key = db.execute("SELECT * FROM xmax_temporary_keys WHERE id=? AND user_id=?", (current["temporary_key_record_id"], user_id)).fetchone() if current else None
        if not current or not key:
            raise XmaxBrowserAttemptError("XMAX_BROWSER_ATTEMPT_NOT_FOUND", "Browser attempt settlement evidence is missing.", status_code=404)
        if key["attempt_id"] != record_id or key["unit_id"] != current["unit_id"] or key["project_id"] != current["project_id"] or key["episode_id"] != current["episode_id"]:
            raise XmaxBrowserAttemptError("XMAX_ATTEMPT_OWNERSHIP_MISMATCH", "The temporary-key record does not match this attempt.", status_code=409)
        if key["session_uid"] != current["session_uid"] or key["session_uid"] != session_uid:
            raise XmaxBrowserAttemptError("XMAX_SESSION_UID_MISMATCH", "The settled Session does not match this attempt.", status_code=409)
        if key["local_status"] != "settled" or key["provider_status"] != "inactive":
            raise XmaxBrowserAttemptError("XMAX_SETTLEMENT_NOT_FINAL", "The temporary key is not inactive and fully settled.", status_code=409)
        consumed = int(key["consumed_points"])
        remaining = int(key["remaining_points"])
        points_limit = int(key["points_limit"])
        if consumed + remaining != points_limit or current["consumed_points"] != consumed or current["released_points"] != remaining:
            raise XmaxBrowserAttemptError("XMAX_SETTLEMENT_TOTAL_MISMATCH", "The settled point totals do not match the attempt.", status_code=409)
        _require_ledger_row(db, key["freeze_ledger_id"], expected_type="freeze", expected_amount=points_limit, user_id=user_id, task_id=current["task_id"], unit_id=current["unit_id"])
        if consumed:
            _require_ledger_row(db, key["consume_ledger_id"], expected_type="consume", expected_amount=consumed, user_id=user_id, task_id=current["task_id"], unit_id=current["unit_id"], session_uid=session_uid)
        if remaining:
            _require_ledger_row(db, key["release_ledger_id"], expected_type="release", expected_amount=remaining, user_id=user_id, task_id=current["task_id"], unit_id=current["unit_id"])
        wallet = db.execute("SELECT * FROM credit_wallets WHERE user_id=?", (user_id,)).fetchone()
        if not wallet or wallet["frozen_balance"] != 0:
            raise XmaxBrowserAttemptError("XMAX_WALLET_STILL_FROZEN", "The wallet still contains frozen points for this settled attempt.", status_code=409)
        segment = db.execute(
            "SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?",
            (current["unit_id"], current["task_id"]),
        ).fetchone()
        if not segment or Path(str(segment["output_path"] or "")).resolve() != output_path.resolve() or segment["status"] != "completed" or segment["execution_status"] != "completed":
            raise XmaxBrowserAttemptError("XMAX_OUTPUT_LINK_MISMATCH", "The Unit is not linked to the saved Remote output.", status_code=409)
        reason = (quality_reason or "")[:1000] or None
        if segment["quality_status"] != quality_status or (segment["quality_reason"] or None) != reason:
            db.execute(
                """UPDATE xmax_thirty_second_segments
                   SET quality_status=?,quality_reason=?,quality_reviewed_at=?,quality_reviewed_by='p4_recovery',updated_at=?
                   WHERE id=? AND task_id=?""",
                (quality_status, reason, timestamp, timestamp, current["unit_id"], current["task_id"]),
            )
        render = json.loads(current["render_json"] or "{}")
        audit = {
            "recovered_at": timestamp,
            "previous_status": current["status"],
            "previous_error_code": current["last_error_code"],
            "session_uid": session_uid,
            "output_sha256": actual_sha256,
            "output_size": output_path.stat().st_size,
            "output_duration": round(float(metadata["duration"]), 6),
            "decode_succeeded": bool(decode.get("decode_succeeded")),
            "quality_status": quality_status,
            "ledger_entries_verified": 1 + int(bool(consumed)) + int(bool(remaining)),
            "wallet_available_balance": wallet["available_balance"],
            "wallet_frozen_balance": wallet["frozen_balance"],
        }
        render["quality_recovery_audit"] = audit
        db.execute(
            """UPDATE xmax_browser_attempts
               SET status='settled',render_json=?,updated_at=?
               WHERE id=? AND user_id=? AND status='persistence_failed' AND settlement_status='settled'""",
            (_json(render), timestamp, record_id, user_id),
        )
    return {"attempt": get_attempt(record_id, user_id), "recovery": {**audit, "idempotent": False}}


def mark_failed(
    record_id: str,
    user_id: str,
    code: str,
    message: str,
    *,
    persistence: bool = False,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _record(record_id, user_id)
    render = _object_json(row["render_json"])
    failure = _failure_diagnostics(diagnostics)
    if failure:
        render["failure_diagnostics"] = failure
    with get_connection() as db:
        db.execute(
            """UPDATE xmax_browser_attempts
               SET status=?,
                   last_error_code=CASE WHEN last_error_code LIKE 'OUTPUT_TIMELINE_NORMALIZATION%' THEN last_error_code ELSE ? END,
                   last_error_message=CASE WHEN last_error_code LIKE 'OUTPUT_TIMELINE_NORMALIZATION%' THEN last_error_message ELSE ? END,
                   render_json=?,
                   updated_at=?
               WHERE id=? AND user_id=? AND status!='settled'""",
            (
                "persistence_failed" if persistence else "failed",
                code[:120],
                message[:500],
                _json(render),
                now_iso(),
                record_id,
                user_id,
            ),
        )
    return get_attempt(record_id, user_id)


def finalize_attempt(record_id: str, user_id: str) -> dict[str, Any]:
    row = _record(record_id, user_id)
    try:
        temporary = temporary_keys.abort_temporary_key(row["temporary_key_record_id"], user_id)
    except temporary_keys.XmaxTemporaryKeyError as error:
        with get_connection() as db:
            db.execute(
                """UPDATE xmax_browser_attempts SET settlement_status='pending',
                          status=CASE WHEN status='output_saved' THEN 'settlement_pending' ELSE status END,
                          last_error_code=?,last_error_message=?,updated_at=? WHERE id=?""",
                (error.code, error.message[:500], now_iso(), record_id),
            )
        raise
    local_status = str(temporary.get("local_status") or "")
    if local_status == "settled":
        settlement_status = "settled"
        status = "settled" if row["status"] == "output_saved" else row["status"]
    elif local_status == "settlement_conflict":
        settlement_status = "conflict"
        status = row["status"]
    else:
        settlement_status = "pending"
        status = "settlement_pending" if row["status"] == "output_saved" else row["status"]
    consumed = temporary.get("consumed_points")
    points_limit = temporary.get("points_limit")
    released = points_limit - consumed if type(points_limit) is int and type(consumed) is int and local_status == "settled" else None
    with get_connection() as db:
        db.execute(
            """UPDATE xmax_browser_attempts SET status=?,settlement_status=?,consumed_points=?,
                      released_points=?,updated_at=?,completed_at=CASE WHEN ?='settled' THEN ? ELSE completed_at END
               WHERE id=?""",
            (status, settlement_status, consumed, released, now_iso(), settlement_status, now_iso(), record_id),
        )
    return get_attempt(record_id, user_id)


def audit_processing_unit_compatibility(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, list[str]]:
    """Deterministic grouping contract; original shot records remain unchanged."""
    reasons: list[str] = []
    if abs(float(left["end_time"]) - float(right["start_time"])) > 0.001:
        reasons.append("not_time_contiguous")
    for field in ("character_id", "character_asset_version", "processing_mode", "target_language", "audio_revision", "reference_sha256", "prompt_template_version"):
        if left.get(field) != right.get(field):
            reasons.append(f"different_{field}")
    if left.get("force_split") or right.get("force_split"):
        reasons.append("forced_split")
    return not reasons, reasons
