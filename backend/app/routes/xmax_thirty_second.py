"""Persistence and fixed three-shot orchestration for the 30-second Xmax MVP.

The browser owns each Xmax realtime session. This router only prepares fixed
10-second source clips, persists each browser-recorded result, and joins the
three validated outputs. It never calls a Provider.
"""

from __future__ import annotations

import asyncio
import re
import json
import hashlib
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from starlette.datastructures import Headers

from ..access_control import require_request_user
from ..database import ROOT, UPLOADS_DIR, get_connection, now_iso, row_to_dict
from ..services import media_service, xmax_browser_attempt_service, xmax_offline_service
from ..services.xmax_visual_result_validity import evaluate_visual_result
from ..user_storage import STORAGE_ROOT, project_storage_root, resolve_upload_asset_path, storage_asset_url

router = APIRouter(prefix="/api")
OUTPUTS_DIR = ROOT / "outputs"
RAW_FIRST_MODULE_LOADED_AT = datetime.now(timezone.utc).isoformat()
RAW_FIRST_MODULE_SOURCE_PATH = str(Path(__file__).resolve())
RAW_FIRST_MODULE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RAW_FIRST_RUNTIME_FEATURE = "p4-raw-first-local-probe-v1"
_PROXY_PREPARATION_LOCK = threading.Lock()
_PROXY_PREPARATION_IN_FLIGHT: set[str] = set()
MIN_SOURCE_DURATION = 24.0
MAX_SOURCE_DURATION = 30.0
MIN_EPISODE_DURATION = 0.0
MAX_EPISODE_DURATION = 180.0
SHOT_SECONDS = 10.0
EXPECTED_SHOTS = 3
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MIN_OUTPUT_BYTES = 4 * 1024
TRANSFORM_TARGET_SECONDS = 2.5
TRANSFORM_MAX_SECONDS = 4.0
TRANSFORM_MIN_SECONDS = 1.0
MAX_AUTOMATIC_RETRIES = 1
ALLOWED_REFERENCE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ALLOWED_WEBM_TYPES = {"video/webm", "video/x-webm", "application/octet-stream"}
ALLOWED_OFFLINE_MP4_TYPES = {"video/mp4", "application/mp4", "application/octet-stream"}
REFERENCE_FIELDS = ("target_reference_path", "main_reference_image", "source_reference_image", "original_reference_image")
REFERENCE_TYPE_BY_FIELD = {
    "target_reference_path": "primary",
    "main_reference_image": "primary",
    "source_reference_image": "source",
    "original_reference_image": "original",
    "front_view_image": "frontal",
    "angle_view_image": "three_quarter",
    "side_view_image": "side",
    "full_body_image": "full_body",
}
REFERENCE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
QUALITY_STATUSES = {
    "review_required",
    "approved",
    "rejected_no_effect",
    "rejected_identity_drift",
    "rejected_timeline",
    "invalid_output",
    "not_applicable",
}
REJECTED_QUALITY_STATUSES = {"rejected_no_effect", "rejected_identity_drift", "rejected_timeline", "invalid_output"}


def _safe_component(value: str, label: str) -> str:
    if not value or not SAFE_COMPONENT.fullmatch(value):
        raise HTTPException(status_code=500, detail=f"Unsafe {label} stored in database")
    return value


def _default_quality_status(segment: dict[str, Any]) -> str:
    if bool(segment.get("preserve_original")) or segment.get("shot_type") in {"no_character", "transition", "multi_character"}:
        return "not_applicable"
    return "review_required"


def _quality_status(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in QUALITY_STATUSES:
        raise HTTPException(status_code=422, detail={"error_code": "QUALITY_STATUS_INVALID", "allowed": sorted(QUALITY_STATUSES)})
    return normalized


def _segment_retry_allowed(segment: dict[str, Any]) -> bool:
    if bool(segment.get("preserve_original")) or segment.get("shot_type") in {"no_character", "transition", "multi_character"}:
        return False
    quality = segment.get("quality_status") or _default_quality_status(segment)
    execution = segment.get("execution_status") or segment.get("status")
    return quality in REJECTED_QUALITY_STATUSES or execution == "failed"


def _segment_diagnostics(segment: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(segment.get("diagnostics_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _segment_input_route(segment: dict[str, Any]) -> dict[str, Any]:
    route = _segment_diagnostics(segment).get("input_route")
    return route if isinstance(route, dict) else {}


def _preserve_source_by_user(segment: dict[str, Any]) -> bool:
    return bool(segment.get("use_source_for_merge")) and _segment_diagnostics(segment).get("preserveSourceByUser") is True


def _segment_quality_ready(segment: dict[str, Any]) -> bool:
    if bool(segment.get("preserve_original")) or segment.get("mapping_status") == "preserve_original":
        return True
    if _preserve_source_by_user(segment):
        return True
    execution = segment.get("execution_status") or segment.get("status")
    quality = segment.get("quality_status") or _default_quality_status(segment)
    return execution == "completed" and quality == "approved"


def _segment_merge_path(segment: dict[str, Any]) -> Path:
    if bool(segment.get("preserve_original")) or segment.get("mapping_status") == "preserve_original" or _preserve_source_by_user(segment):
        return Path(str(segment["source_path"]))
    return Path(str(segment["output_path"]))


def _segment_merge_ready(segment: dict[str, Any]) -> bool:
    if not _segment_quality_ready(segment):
        return False
    uses_source = bool(segment.get("preserve_original")) or segment.get("mapping_status") == "preserve_original" or _preserve_source_by_user(segment)
    return bool(segment.get("source_path") if uses_source else segment.get("output_path"))


def _episode(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT e.*, p.id AS project_id FROM episodes e JOIN projects p ON p.id=e.project_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def _source_path(episode: dict[str, Any]) -> Path:
    raw = str(episode.get("video_path") or "")
    if not raw:
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_MISSING"})
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    allowed_roots = (ROOT.resolve(), STORAGE_ROOT.resolve())
    if not any(path.is_relative_to(allowed_root) for allowed_root in allowed_roots):
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_INVALID"})
    if not path.is_file():
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_NOT_FOUND"})
    return path


def _public_output(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(OUTPUTS_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Output path escaped outputs directory") from exc
    return "/outputs/" + "/".join(relative.parts)


def _public_source(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return _public_output(path)
    return "/uploads/" + "/".join(relative.parts)


def _public_asset(path: Path) -> str:
    scoped_url = storage_asset_url(path)
    if scoped_url:
        return scoped_url
    try:
        return _public_source(path)
    except HTTPException:
        return _public_output(path)


def _public_reference_value(value: str | Path | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if text.startswith(("http://", "https://")):
        return text
    return _public_asset(Path(text))


def _cache_busted_reference_url(value: str | Path | None) -> str | None:
    if not value:
        return None
    text = str(value)
    local_path = _local_reference_candidate(text)
    if local_path and local_path.is_file():
        digest = hashlib.sha256(local_path.read_bytes()).hexdigest()[:8]
        base = _public_asset(local_path)
    else:
        base = _public_reference_value(value)
        digest = "latest"
    if not base:
        return None
    return f"{base}&v={digest}" if "?" in base else f"{base}?v={digest}"


def _image_file_readable(path: Path) -> bool:
    if path.suffix.lower() not in REFERENCE_EXTENSIONS or not path.is_file() or path.stat().st_size <= 0:
        return False
    with path.open("rb") as handle:
        header = handle.read(16)
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8")
    if path.suffix.lower() == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    return header[:4] == b"RIFF" and header[8:12] == b"WEBP"


def _local_reference_candidate(
    raw_value: str,
    *,
    project_id: str | None = None,
    owner_user_id: str | None = None,
) -> Path | None:
    raw = raw_value.strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        if parsed.path.startswith(("/uploads/", "/outputs/")):
            raw = parsed.path
        else:
            return None
    if project_id and raw.startswith(("/uploads/", "/outputs/")):
        public_parts = tuple(part for part in raw.split("/") if part)
        if len(public_parts) < 2:
            return None
        candidate_project_id = public_parts[1]
        if candidate_project_id != project_id:
            with get_connection() as conn:
                other_project = conn.execute(
                    "SELECT 1 FROM projects WHERE id=?",
                    (candidate_project_id,),
                ).fetchone()
            if other_project:
                return None
    if raw.startswith("/uploads/"):
        candidate = resolve_upload_asset_path(raw)
        if candidate is None:
            return None
    elif raw.startswith("/outputs/"):
        candidate = ROOT / raw.lstrip("/")
    else:
        path = Path(raw)
        candidates = [path] if path.is_absolute() else [ROOT / path, UPLOADS_DIR / path, OUTPUTS_DIR / path]
        candidate = next((item for item in candidates if item.exists()), candidates[0])
    try:
        resolved = candidate.resolve()
        allowed_roots = [ROOT.resolve()]
        if project_id and owner_user_id:
            scoped_project_root = project_storage_root(owner_user_id, project_id)
            allowed_roots.extend(
                (
                    (scoped_project_root / "uploads").resolve(),
                    (scoped_project_root / "outputs").resolve(),
                )
            )
        elif project_id is None:
            allowed_roots.append(STORAGE_ROOT.resolve())
        if not any(resolved.is_relative_to(allowed_root) for allowed_root in allowed_roots):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def resolve_character_reference(character_id: str, episode_id: str, project_id: str) -> dict[str, Any]:
    """Resolve one confirmed project character without treating asset URLs as filesystem paths."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT c.*, p.owner_user_id AS reference_owner_user_id "
            "FROM characters c JOIN projects p ON p.id=c.project_id "
            "WHERE c.id=? AND c.project_id=?",
            (character_id, project_id),
        ).fetchone()
    character = row_to_dict(row) if row else None
    base = {"character_id": character_id, "episode_id": episode_id, "reference_source": None, "resolved_local_path": None, "provider_url": None, "reference_preview_url": None, "validation_status": "invalid", "mapping_valid": False}
    if not character:
        return {**base, "error_code": "CHARACTER_NOT_FOUND", "error_message": "The selected character does not belong to this project."}
    base["character_name"] = character.get("name") or character_id
    if not bool(character.get("confirmed")) or character.get("priority_level") != "Locked":
        return {**base, "error_code": "CHARACTER_NOT_LOCKED", "error_message": "The selected character must be confirmed and Locked."}
    ordered_fields = list(REFERENCE_FIELDS)
    ordered_fields.extend(field for field in REFERENCE_TYPE_BY_FIELD if field not in ordered_fields)
    for field_name in dict.fromkeys(ordered_fields):
        raw = character.get(field_name)
        if not raw:
            continue
        raw_text = str(raw).strip()
        parsed = urlparse(raw_text)
        local_path = _local_reference_candidate(
            raw_text,
            project_id=project_id,
            owner_user_id=character.get("reference_owner_user_id"),
        )
        if local_path:
            if not _image_file_readable(local_path):
                return {**base, "reference_source": field_name, "reference_type": str(character.get("primary_reference_type") or REFERENCE_TYPE_BY_FIELD.get(field_name, field_name)) if field_name in {"target_reference_path", "main_reference_image"} else REFERENCE_TYPE_BY_FIELD.get(field_name, field_name), "resolved_local_path": str(local_path), "reference_preview_url": _public_asset(local_path), "error_code": "CHARACTER_REFERENCE_UNREADABLE", "error_message": "The selected character reference is not a readable non-empty image."}
            return {**base, "reference_source": field_name, "reference_type": str(character.get("primary_reference_type") or REFERENCE_TYPE_BY_FIELD.get(field_name, field_name)) if field_name in {"target_reference_path", "main_reference_image"} else REFERENCE_TYPE_BY_FIELD.get(field_name, field_name), "resolved_local_path": str(local_path), "reference_preview_url": _public_asset(local_path), "validation_status": "readable", "mapping_valid": True, "error_code": None, "error_message": None}
        if parsed.scheme in {"http", "https"}:
            return {**base, "reference_source": field_name, "reference_type": REFERENCE_TYPE_BY_FIELD.get(field_name, field_name), "provider_url": raw_text, "reference_preview_url": raw_text, "validation_status": "provider_url", "mapping_valid": True, "error_code": None, "error_message": None}
        return {**base, "reference_source": field_name, "reference_type": REFERENCE_TYPE_BY_FIELD.get(field_name, field_name), "error_code": "CHARACTER_REFERENCE_INVALID", "error_message": "The character reference path is outside the project assets or cannot be resolved."}
    return {**base, "error_code": "CHARACTER_REFERENCE_MISSING", "error_message": "The selected character has no target reference image."}


def _parse_shot_mappings(raw_value: str | None) -> dict[str, dict[str, Any]] | None:
    if not raw_value:
        return None
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "SHOT_MAPPING_INVALID", "error_message": str(exc)}) from exc
    if isinstance(parsed, dict):
        return {str(sequence): value for sequence, value in parsed.items() if isinstance(value, dict)}
    if isinstance(parsed, list):
        result: dict[str, dict[str, Any]] = {}
        for item in parsed:
            if not isinstance(item, dict) or item.get("sequence") is None:
                raise HTTPException(status_code=422, detail={"error_code": "SHOT_MAPPING_INVALID", "error_message": "Each Shot mapping must include sequence."})
            result[str(item["sequence"])] = item
        return result
    raise HTTPException(status_code=422, detail={"error_code": "SHOT_MAPPING_INVALID", "error_message": "shot_mappings_json must be a mapping object or sequence list."})


def _mapping_values(mapping: dict[str, Any]) -> tuple[str | None, bool, bool]:
    character_id = str(mapping.get("selected_character_id") or mapping.get("character_id") or "") or None
    reference_confirmed = bool(mapping.get("reference_face_confirmed") if "reference_face_confirmed" in mapping else mapping.get("reference_confirmed"))
    preserve_source = bool(mapping.get("preserve_source") if "preserve_source" in mapping else mapping.get("preserve_original"))
    return character_id, reference_confirmed, preserve_source


def _mapping_for_transform_unit(
    mappings: dict[str, dict[str, Any]] | None,
    unit: dict[str, Any],
    sequence: int,
) -> tuple[int, dict[str, Any]]:
    available_mappings = mappings or {}
    direct_mapping = available_mappings.get(str(sequence))
    if direct_mapping and direct_mapping.get("mapping_scope") == "transform_unit":
        return sequence, direct_mapping
    mapping_sequence = int(unit.get("parent_sequence") or sequence)
    return mapping_sequence, available_mappings.get(str(mapping_sequence), {})


def _task_dir_path(project_id: str, episode_id: str, attempt_id: str) -> Path:
    return OUTPUTS_DIR.resolve() / _safe_component(project_id, "project_id") / _safe_component(episode_id, "episode_id") / "xmax-thirty-second" / _safe_component(attempt_id, "attempt_id")


def _task_dir(project_id: str, episode_id: str, attempt_id: str) -> Path:
    directory = _task_dir_path(project_id, episode_id, attempt_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _task(task_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM xmax_thirty_second_tasks WHERE id=?", (task_id,)).fetchone()
    return row_to_dict(row) if row else None


def _segments(task_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()
    return [row_to_dict(row) for row in rows]


def _segment_diagnostics(segment: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(segment.get("diagnostics_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _segment_proxy_ready(segment: dict[str, Any]) -> bool:
    preparation = _segment_diagnostics(segment).get("proxy_preparation")
    if not isinstance(preparation, dict) or preparation.get("status") != "ready":
        return False
    source_path = Path(str(segment.get("source_path") or ""))
    try:
        proxy_duration = float(segment.get("proxy_duration") or 0)
    except (TypeError, ValueError):
        proxy_duration = 0
    return source_path.is_file() and source_path.stat().st_size > 0 and proxy_duration > 0


def _proxy_preparation_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    prepared_units = sum(1 for segment in segments if _segment_proxy_ready(segment))
    failed_units = sum(
        1
        for segment in segments
        if segment.get("error_code") == "PROXY_PREPARATION_FAILED"
        or _segment_diagnostics(segment).get("proxy_preparation", {}).get("status") == "failed"
    )
    total_units = len(segments)
    complete = total_units > 0 and prepared_units == total_units and failed_units == 0
    status = "failed" if failed_units else "ready" if complete else "preparing"
    return {
        "preparation_status": status,
        "preparation_complete": complete,
        "prepared_units": prepared_units,
        "preparation_total_units": total_units,
        "preparation_failed_units": failed_units,
        "preparation_progress": round((prepared_units / total_units) * 100, 2) if total_units else 0.0,
    }


def _payload(task: dict[str, Any]) -> dict[str, Any]:
    segments = _segments(task["id"])
    preparation = _proxy_preparation_summary(segments)
    result_evaluation = evaluate_visual_result(task, segments)
    try:
        scene_plan = json.loads(task.get("scene_plan_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        scene_plan = {}
    fallback_count = sum(1 for segment in segments if _preserve_source_by_user(segment))
    high_risk_count = sum(1 for segment in segments if segment.get("risk_level") == "high" and not bool(segment.get("preserve_original")))
    retry_count = sum(int(segment.get("retry_version") or 0) for segment in segments)
    ai_transform_count = sum(1 for segment in segments if segment.get("shot_type") == "single_character" and not _preserve_source_by_user(segment))
    if task.get("status") == "completed" and result_evaluation["result_valid"]:
        action_state = "completed"
    elif any((segment.get("execution_status") or segment.get("status")) in {"running", "local_ready", "uploading"} for segment in segments):
        action_state = "running"
    elif segments and all(_segment_merge_ready(segment) for segment in segments):
        action_state = "ready_to_merge"
    else:
        action_state = "awaiting_user_action"
    return {
        "task_id": task["id"],
        "attempt_id": task["attempt_id"],
        "project_id": task["project_id"],
        "episode_id": task["episode_id"],
        "status": task["status"],
        "workflow": _task_workflow(task),
        "action_state": action_state,
        "prompt": task["prompt"],
        "source_duration": task["source_duration"],
        "estimated_credits": task["estimated_credits"],
        "final_output_url": task.get("final_output_url"),
        "result_valid": bool(result_evaluation["result_valid"]),
        "result_classification": result_evaluation["classification"],
        "final_duration": task.get("final_duration"),
        "final_width": task.get("final_width"),
        "final_height": task.get("final_height"),
        "final_fps": task.get("final_fps"),
        "final_has_audio": bool(task.get("final_has_audio")) if task.get("final_has_audio") is not None else None,
        "source_audio_restored": bool(task.get("source_audio_restored")),
        "merged_at": task.get("merged_at"),
        "file_size": task.get("file_size"),
        "error_code": task.get("error_code"),
        "error_message": task.get("error_message"),
        "created_at": task["created_at"],
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
        "heartbeat_at": task.get("heartbeat_at"),
        **preparation,
        "reference_url": _public_reference_value(task.get("reference_path")),
        "scene_plan": scene_plan if isinstance(scene_plan, dict) and scene_plan else None,
        "preflight_summary": {
            "transform_unit_count": len(segments),
            "ai_transform_count": ai_transform_count,
            "source_fallback_count": fallback_count,
            "high_risk_count": high_risk_count,
            "retry_count": retry_count,
            "estimated_source_fallback_min": 0,
            "estimated_source_fallback_max": high_risk_count,
        },
        "segments": [
            {
                "segment_id": segment["id"],
                "transform_unit_id": segment.get("transform_unit_id") or segment["id"],
                "parent_shot_id": segment.get("parent_shot_id"),
                "sequence": segment["sequence"],
                "start_time": segment["start_time"],
                "end_time": segment["end_time"],
                "duration": segment["duration"],
                "source_url": segment["source_url"],
                "processing_mode": _segment_input_route(segment).get("processing_mode", "level1_direct"),
                "preprocessed_source_url": _segment_input_route(segment).get("preprocessed_source_url"),
                "original_source_sha256": _segment_input_route(segment).get("original_source_sha256"),
                "preprocessed_source_sha256": _segment_input_route(segment).get("preprocessed_source_sha256"),
                "crop": _segment_input_route(segment).get("crop"),
                "scale": _segment_input_route(segment).get("scale"),
                "preserve_current_unit_version": bool(_segment_input_route(segment).get("preserve_current_unit_version")),
                "source_start": segment["start_time"],
                "source_end": segment["end_time"],
                "content_duration": segment.get("content_duration") or segment["duration"],
                "proxy_duration": segment.get("proxy_duration") or segment["duration"],
                "proxy_ready": _segment_proxy_ready(segment),
                "pre_roll_duration": segment.get("pre_roll_duration") or 0,
                "post_roll_duration": segment.get("post_roll_duration") or 0,
                "shot_type": segment.get("shot_type") or "unknown",
                "selected_character_id": segment.get("selected_character_id"),
                "mapping_status": segment.get("mapping_status") or "manual_required",
                "reference_url": segment.get("reference_url"),
                "reference_preview_url": segment.get("reference_preview_url") or _cache_busted_reference_url(segment.get("reference_path") or segment.get("reference_url")),
                "reference_filename": Path(str(segment.get("reference_path") or segment.get("reference_url") or "")).name or None,
                "reference_type": segment.get("reference_type"),
                "preserve_original": bool(segment.get("preserve_original")),
                "use_source_for_merge": bool(segment.get("use_source_for_merge")),
                "preserve_source_by_user": _preserve_source_by_user(segment),
                "scene_score": segment.get("scene_score"),
                "cut_method": segment.get("cut_method"),
                "risk_score": float(segment.get("risk_score") or 0),
                "risk_level": segment.get("risk_level") or "low",
                "first_frame_url": segment.get("first_frame_url"),
                "middle_frame_url": segment.get("middle_frame_url"),
                "last_frame_url": segment.get("last_frame_url"),
                "timeline_status": segment.get("timeline_status"),
                "timeline_error_ms": segment.get("timeline_error_ms"),
                "status": segment["status"],
                "execution_status": segment.get("execution_status") or segment["status"],
                "quality_status": segment.get("quality_status") or _default_quality_status(segment),
                "quality_reason": segment.get("quality_reason"),
                "quality_reviewed_at": segment.get("quality_reviewed_at"),
                "quality_reviewed_by": segment.get("quality_reviewed_by"),
                "retry_version": int(segment.get("retry_version") or 0),
                "output_url": segment.get("output_url"),
                "output_duration": segment.get("output_duration"),
                "file_size": segment.get("file_size"),
                "error_code": segment.get("error_code"),
                "error_message": segment.get("error_message"),
                "diagnostics_json": segment.get("diagnostics_json"),
                "proxy_preparation_status": (
                    "ready"
                    if _segment_proxy_ready(segment)
                    else _segment_diagnostics(segment).get("proxy_preparation", {}).get("status", "pending")
                ),
                "heartbeat_at": segment.get("heartbeat_at"),
            }
            for segment in segments
        ],
    }


ACTIVE_TASK_STATUSES = {"pending", "running", "local_ready", "uploading", "interrupted", "failed", "merge_pending", "merging", "merge_failed"}
ACTIVE_CANDIDATE_STATUSES = {"pending", "running", "local_ready", "uploading", "interrupted", "failed", "merge_pending", "merging"}
ACTIVE_SUBMISSION_STATUSES = {"pending", "running", "local_ready", "uploading", "interrupted", "merge_pending", "merging"}


def _final_file_valid(task: dict[str, Any]) -> bool:
    evaluation = evaluate_visual_result(task, _segments(task["id"]))
    return bool(evaluation["result_valid"])


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    segments = _segments(task["id"])
    preparation = _proxy_preparation_summary(segments)
    evaluation = evaluate_visual_result(task, segments)
    final_file_valid = bool(evaluation["result_valid"])
    completed_shots = sum(1 for segment in segments if segment["status"] == "completed")
    merge_ready_count = sum(1 for segment in segments if _segment_merge_ready(segment))
    fallback_count = sum(1 for segment in segments if _preserve_source_by_user(segment))
    can_merge = merge_ready_count == len(segments) and bool(segments) and not final_file_valid and task["status"] in {"merge_pending", "merge_failed", "completed"}
    return {
        "task_id": task["id"],
        "attempt_id": task["attempt_id"],
        "project_id": task["project_id"],
        "episode_id": task["episode_id"],
        "status": task["status"],
        "workflow": _task_workflow(task),
        "created_at": task["created_at"],
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
        "merged_at": task.get("merged_at"),
        "completed_shots": completed_shots,
        "merge_ready_shots": merge_ready_count,
        "fallback_count": fallback_count,
        "total_shots": len(segments),
        **preparation,
        "final_output_url": task.get("final_output_url"),
        "final_file_valid": final_file_valid,
        "result_classification": evaluation["classification"],
        "real_output_count": evaluation["real_output_count"],
        "explicit_source_count": evaluation["explicit_source_count"],
        "automatic_fallback_count": evaluation["automatic_fallback_count"],
        "final_duration": task.get("final_duration"),
        "file_size": task.get("file_size"),
        "error_code": task.get("error_code"),
        "error_message": task.get("error_message"),
        "can_resume": task["status"] in ACTIVE_TASK_STATUSES and not final_file_valid,
        "can_merge": can_merge,
        "is_completed_result": final_file_valid,
    }


def _set_task_failed(task_id: str, code: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_tasks SET status='failed', error_code=?, error_message=?, updated_at=? WHERE id=? AND status!='completed'",
            (code, message[:1000], now_iso(), task_id),
        )


def _set_segment_state(segment_id: str, task_id: str, status: str, code: str | None = None, message: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_segments SET status=?, execution_status=?, error_code=?, error_message=?, updated_at=? WHERE id=? AND task_id=?",
            (status, status, code, (message or "")[:1000] or None, now_iso(), segment_id, task_id),
        )


def _merge_diagnostics(existing: str | None, updates: dict[str, Any]) -> str:
    try:
        parsed = json.loads(existing or "{}")
        diagnostics = parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostics = {}
    diagnostics.update(updates)
    return json.dumps(diagnostics, ensure_ascii=True)[:20000]


def _record_segment_failure(
    task_id: str,
    segment_id: str,
    code: str,
    message: str,
    *,
    quality_status: str = "invalid_output",
    diagnostics: dict[str, Any] | None = None,
    browser_attempt_id: str | None = None,
) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?", (segment_id, task_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
    segment = row_to_dict(row)
    timestamp = now_iso()
    if browser_attempt_id:
        with get_connection() as conn:
            versioned_redraw = xmax_browser_attempt_service.preserves_current_unit_version_in_transaction(
                conn,
                record_id=browser_attempt_id,
                task_id=task_id,
                unit_id=segment_id,
            )
            preserved_source = (
                xmax_browser_attempt_service.preserves_source_selection_in_transaction(
                    conn,
                    record_id=browser_attempt_id,
                    task_id=task_id,
                    unit_id=segment_id,
                )
                if versioned_redraw
                else False
            )
            if preserved_source:
                _restore_source_preserved_segment_projection(conn, task_id, segment_id, timestamp)
                _clear_stale_shot_failure(conn, task_id, timestamp)
                return {
                    "recovery_action": "preserved_source_version",
                    "retry_allowed": True,
                    "fallback_applied": False,
                    "retry_version": int(segment.get("retry_version") or 0),
                }
            previous = (
                xmax_browser_attempt_service.latest_playable_attempt_in_transaction(
                    conn,
                    task_id=task_id,
                    unit_id=segment_id,
                )
                if versioned_redraw
                else None
            )
            if previous:
                _restore_successful_segment_projection(conn, task_id, segment_id, previous, timestamp)
                _clear_stale_shot_failure(conn, task_id, timestamp)
                return {
                    "recovery_action": "preserved_current_version",
                    "retry_allowed": True,
                    "fallback_applied": False,
                    "retry_version": int(segment.get("retry_version") or 0),
                    "preserved_attempt_id": previous["id"],
                }
    diagnostic_payload = _merge_diagnostics(
        segment.get("diagnostics_json"),
        {
            **(diagnostics or {}),
            "outputPreflightPassed": False,
            "outputPreflightErrorCode": code,
            "automaticRetryUsed": False,
            "fallbackSourceSelected": False,
            "preserveSourceByUser": False,
            "awaitingUserAction": True,
        },
    )
    with get_connection() as conn:
        conn.execute(
            """UPDATE xmax_thirty_second_segments
               SET status='failed', execution_status='failed', quality_status=?,
                   quality_reason=?, use_source_for_merge=?, error_code=?, error_message=?,
                   diagnostics_json=?, updated_at=?
               WHERE id=? AND task_id=?""",
            (
                quality_status,
                message[:1000],
                0,
                code[:120],
                message[:1000],
                diagnostic_payload,
                timestamp,
                segment_id,
                task_id,
            ),
        )
    with get_connection() as conn:
        conn.execute(
            """UPDATE xmax_thirty_second_tasks
               SET status=?, error_code=?, error_message=?, updated_at=?
               WHERE id=? AND status NOT IN ('cancelled','completed')""",
            (
                "pending",
                "SHOT_FAILED_AWAITING_USER_ACTION",
                f"Shot {segment['sequence']} failed. Retry it or explicitly keep the source frame.",
                timestamp,
                task_id,
            ),
        )
    return {
        "recovery_action": "awaiting_user_action",
        "retry_allowed": True,
        "fallback_applied": False,
        "retry_version": int(segment.get("retry_version") or 0),
    }


def _restore_successful_segment_projection(
    conn: sqlite3.Connection,
    task_id: str,
    segment_id: str,
    attempt: dict[str, Any],
    timestamp: str,
) -> None:
    conn.execute(
        """UPDATE xmax_thirty_second_segments
           SET status='completed',execution_status='completed',quality_status='approved',
               quality_reason=NULL,quality_reviewed_at=NULL,quality_reviewed_by=NULL,
               output_path=?,output_url=?,output_duration=?,file_size=?,use_source_for_merge=0,
               error_code=NULL,error_message=NULL,updated_at=?
           WHERE id=? AND task_id=?""",
        (
            attempt["output_path"],
            attempt["output_url"],
            attempt["output_duration"],
            attempt["output_size"],
            timestamp,
            segment_id,
            task_id,
        ),
    )


def _promote_offline_versioned_attempt_projection(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    segment_id: str,
    attempt_id: str,
    timestamp: str,
) -> bool:
    if not xmax_browser_attempt_service.preserves_current_unit_version_in_transaction(
        conn,
        record_id=attempt_id,
        task_id=task_id,
        unit_id=segment_id,
    ):
        return False
    if xmax_browser_attempt_service.preserves_source_selection_in_transaction(
        conn,
        record_id=attempt_id,
        task_id=task_id,
        unit_id=segment_id,
    ):
        return False
    row = conn.execute(
        """SELECT * FROM xmax_browser_attempts
           WHERE id=? AND task_id=? AND unit_id=?""",
        (attempt_id, task_id, segment_id),
    ).fetchone()
    if not row:
        raise xmax_browser_attempt_service.XmaxBrowserAttemptError(
            "XMAX_BROWSER_ATTEMPT_NOT_FOUND",
            "Browser attempt not found.",
            status_code=404,
        )
    attempt = row_to_dict(row)
    if not attempt.get("output_path") or not attempt.get("output_url"):
        raise xmax_browser_attempt_service.XmaxBrowserAttemptError(
            "XMAX_OUTPUT_NOT_SAVED",
            "The Offline output must be saved before current-result promotion.",
            status_code=409,
        )
    _restore_successful_segment_projection(conn, task_id, segment_id, attempt, timestamp)
    _clear_stale_shot_failure(conn, task_id, timestamp)
    return True


def _restore_source_preserved_segment_projection(
    conn: sqlite3.Connection,
    task_id: str,
    segment_id: str,
    timestamp: str,
) -> None:
    row = conn.execute(
        "SELECT diagnostics_json FROM xmax_thirty_second_segments WHERE id=? AND task_id=?",
        (segment_id, task_id),
    ).fetchone()
    diagnostics_json = _merge_diagnostics(
        row["diagnostics_json"] if row else None,
        {
            "preserveSourceByUser": True,
            "fallbackSourceSelected": True,
            "awaitingUserAction": False,
        },
    )
    conn.execute(
        """UPDATE xmax_thirty_second_segments
           SET status='completed',execution_status='completed',quality_status='not_applicable',
               quality_reason=NULL,quality_reviewed_at=NULL,quality_reviewed_by=NULL,
               use_source_for_merge=1,error_code=NULL,error_message=NULL,
               diagnostics_json=?,updated_at=?
           WHERE id=? AND task_id=?""",
        (diagnostics_json, timestamp, segment_id, task_id),
    )


def _clear_stale_shot_failure(conn: sqlite3.Connection, task_id: str, timestamp: str) -> None:
    failed = conn.execute(
        """SELECT 1 FROM xmax_thirty_second_segments
           WHERE task_id=? AND COALESCE(execution_status,status)='failed' LIMIT 1""",
        (task_id,),
    ).fetchone()
    if not failed:
        conn.execute(
            """UPDATE xmax_thirty_second_tasks
               SET error_code=NULL,error_message=NULL,updated_at=?
               WHERE id=? AND error_code='SHOT_FAILED_AWAITING_USER_ACTION'""",
            (timestamp, task_id),
        )


class SegmentOutputValidationError(ValueError):
    def __init__(self, code: str, message: str, *, quality_status: str = "invalid_output", details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.quality_status = quality_status
        self.details = details or {}
        super().__init__(message)


def _summarize_packet_sequence(packets: list[dict[str, Any]]) -> dict[str, Any]:
    pts_values: list[float] = []
    dts_values: list[float] = []
    packet_ends: list[float] = []
    negative_timestamp_count = 0
    for packet in packets:
        pts = _float_or_none(packet.get("pts_time"))
        dts = _float_or_none(packet.get("dts_time"))
        start = pts if pts is not None else dts
        if start is None:
            continue
        duration = max(0.0, _float_or_none(packet.get("duration_time")) or 0.0)
        if pts is not None:
            pts_values.append(pts)
        if dts is not None:
            dts_values.append(dts)
        packet_ends.append(start + duration)
        if start < 0 or (dts is not None and dts < 0):
            negative_timestamp_count += 1

    starts = pts_values or dts_values
    ordered_unique = sorted(set(starts))
    gaps = [right - left for left, right in zip(ordered_unique, ordered_unique[1:])]
    first_timestamp = min(starts) if starts else None
    last_packet_end = max(packet_ends) if packet_ends else None
    return {
        "packet_count": len(packets),
        "timestamped_packet_count": len(starts),
        "unique_pts_count": len(set(pts_values)),
        "duplicate_pts_count": max(0, len(pts_values) - len(set(pts_values))),
        "non_monotonic_pts_count": sum(1 for left, right in zip(pts_values, pts_values[1:]) if right < left),
        "non_monotonic_dts_count": sum(1 for left, right in zip(dts_values, dts_values[1:]) if right < left),
        "negative_timestamp_count": negative_timestamp_count,
        "first_timestamp": first_timestamp,
        "last_packet_end": last_packet_end,
        "packet_timeline_span": (
            max(0.0, last_packet_end - first_timestamp)
            if first_timestamp is not None and last_packet_end is not None
            else None
        ),
        "max_timestamp_gap": max(gaps, default=0.0),
    }


def _summarize_packet_timeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    packets = payload.get("packets") if isinstance(payload.get("packets"), list) else []
    by_index: dict[int, list[dict[str, Any]]] = {}
    for packet in packets:
        try:
            stream_index = int(packet.get("stream_index"))
        except (TypeError, ValueError):
            continue
        by_index.setdefault(stream_index, []).append(packet)
    summaries: dict[str, dict[str, Any]] = {}
    for stream in streams:
        codec_type = str(stream.get("codec_type") or "")
        if codec_type not in {"video", "audio"} or codec_type in summaries:
            continue
        try:
            stream_index = int(stream.get("index"))
        except (TypeError, ValueError):
            continue
        summaries[codec_type] = {
            "stream_index": stream_index,
            "codec_name": stream.get("codec_name"),
            "pixel_format": stream.get("pix_fmt"),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "avg_frame_rate": stream.get("avg_frame_rate"),
            "r_frame_rate": stream.get("r_frame_rate"),
            "time_base": stream.get("time_base"),
            "stream_start_time": _float_or_none(stream.get("start_time")),
            "stream_duration": _float_or_none(stream.get("duration")),
            "nb_read_frames": int(stream.get("nb_read_frames") or 0),
            **_summarize_packet_sequence(by_index.get(stream_index, [])),
        }
    video = summaries.get("video") or {}
    audio = summaries.get("audio") or {}
    video_span = _float_or_none(video.get("packet_timeline_span"))
    audio_span = _float_or_none(audio.get("packet_timeline_span"))
    media_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    return {
        "format_start_time": _float_or_none(media_format.get("start_time")),
        "format_duration": _float_or_none(media_format.get("duration")),
        "video": video or None,
        "audio": audio or None,
        "audio_longer_than_video": bool(
            video_span is not None and audio_span is not None and audio_span > video_span + 0.01
        ),
    }


def _probe_video_packet_timeline(output_path: Path) -> dict[str, Any]:
    ffprobe = media_service.resolve_ffprobe_binary()
    if not ffprobe:
        return {
            "probe_succeeded": False,
            "first_packet_pts": None,
            "last_packet_end": None,
            "packet_timeline_span": None,
            "nb_read_frames": 0,
            "error": "ffprobe is unavailable.",
        }
    result = _run_audit_command([
        ffprobe,
        "-v", "error",
        "-count_frames",
        "-show_streams",
        "-show_format",
        "-show_packets",
        "-show_entries", "stream=index,codec_name,codec_type,pix_fmt,width,height,avg_frame_rate,r_frame_rate,time_base,start_time,duration,nb_read_frames:format=start_time,duration:packet=stream_index,pts_time,dts_time,duration_time",
        "-of", "json",
        str(output_path),
    ])
    try:
        payload = json.loads(result["stdout"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    timeline = _summarize_packet_timeline_payload(payload)
    video = timeline.get("video") or {}
    return {
        "probe_succeeded": result["returncode"] == 0,
        "first_packet_pts": video.get("first_timestamp"),
        "last_packet_end": video.get("last_packet_end"),
        "packet_timeline_span": video.get("packet_timeline_span"),
        "nb_read_frames": int(video.get("nb_read_frames") or video.get("timestamped_packet_count") or 0),
        "format_start_time": timeline.get("format_start_time"),
        "format_duration": timeline.get("format_duration"),
        "video_timeline": video or None,
        "audio_timeline": timeline.get("audio"),
        "audio_longer_than_video": timeline.get("audio_longer_than_video", False),
        "error": result["stderr"][-2000:] if result["returncode"] else None,
    }


def _duration_needs_packet_fallback(container_duration: float, target_duration: float) -> bool:
    near_zero_limit = max(0.05, target_duration * 0.10) if target_duration > 0 else 0.05
    return not math.isfinite(container_duration) or container_duration <= near_zero_limit


def _resolve_vp9_ffmpeg() -> str | None:
    candidates = [shutil.which("ffmpeg"), media_service.resolve_ffmpeg_binary()]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path = path.resolve(strict=True)
        except OSError:
            pass
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result = _run_audit_command([str(path), "-hide_banner", "-encoders"], timeout=30)
        if result["returncode"] == 0 and "libvpx-vp9" in result["stdout"]:
            return str(path)
    return None


def normalize_segment_packet_timeline(
    input_path: Path,
    output_path: Path,
    *,
    packet_duration: float,
    target_duration: float,
    fps: float,
) -> dict[str, Any]:
    if packet_duration <= 0 or target_duration <= 0:
        raise SegmentOutputValidationError(
            "OUTPUT_DURATION_INVALID",
            "The packet or target duration is invalid for timeline normalization.",
            quality_status="rejected_timeline",
        )
    ffmpeg = _resolve_vp9_ffmpeg()
    if not ffmpeg:
        raise SegmentOutputValidationError(
            "OUTPUT_TIMELINE_NORMALIZATION_UNAVAILABLE",
            "FFmpeg with libvpx-vp9 is required to normalize this WebM timeline.",
            quality_status="rejected_timeline",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_fps = min(60.0, max(1.0, float(fps or 30.0)))
    ratio = target_duration / packet_duration
    command = [
        ffmpeg,
        "-y",
        "-fflags", "+genpts",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-vf", (
            f"setpts={ratio:.12f}*(PTS-STARTPTS),"
            f"fps={safe_fps:.6f},trim=duration={target_duration:.6f}"
        ),
        "-an",
        "-c:v", "libvpx-vp9",
        "-b:v", "0",
        "-crf", "18",
        "-deadline", "good",
        "-cpu-used", "4",
        "-row-mt", "1",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    result = _run_audit_command(command, timeout=300)
    if result["returncode"] != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise SegmentOutputValidationError(
            "OUTPUT_TIMELINE_NORMALIZATION_FAILED",
            "FFmpeg could not normalize the WebM packet timeline.",
            quality_status="rejected_timeline",
            details={"stderr": result["stderr"][-4000:]},
        )
    return {
        "source_packet_duration": packet_duration,
        "target_duration": target_duration,
        "setpts_ratio": ratio,
        "fps": safe_fps,
        "command": command,
        "stderr": result["stderr"][-4000:],
        "output_path": str(output_path),
        "file_size": output_path.stat().st_size,
    }


def validate_segment_output_file(output_path: Path, segment: dict[str, Any], audit_dir: Path) -> dict[str, Any]:
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise SegmentOutputValidationError("OUTPUT_EMPTY", "The Xmax output file is empty.")
    if output_path.stat().st_size < MIN_OUTPUT_BYTES:
        raise SegmentOutputValidationError(
            "OUTPUT_FILE_TOO_SMALL",
            f"The Xmax output is only {output_path.stat().st_size} bytes.",
            details={"file_size": output_path.stat().st_size, "minimum_file_size": MIN_OUTPUT_BYTES},
        )
    timeline_probe: dict[str, Any] | None = None
    try:
        metadata = media_service.probe_media_metadata(output_path)
    except Exception as exc:
        timeline_probe = _probe_video_packet_timeline(output_path)
        video_timeline = timeline_probe.get("video_timeline") or {}
        packet_duration = float(timeline_probe.get("packet_timeline_span") or 0)
        width = int(video_timeline.get("width") or 0)
        height = int(video_timeline.get("height") or 0)
        codec = str(video_timeline.get("codec_name") or "")
        rate = str(video_timeline.get("avg_frame_rate") or video_timeline.get("r_frame_rate") or "0/1")
        try:
            numerator, denominator = (int(part) for part in rate.split("/", 1))
            fps = numerator / denominator if denominator else 0.0
        except (TypeError, ValueError):
            fps = 0.0
        if not timeline_probe.get("probe_succeeded") or packet_duration <= 0 or width <= 0 or height <= 0 or not codec:
            raise SegmentOutputValidationError("OUTPUT_NOT_READABLE", str(exc), details={"probe_error": str(exc)[:500]}) from exc
        metadata = {
            "duration": 0.0,
            "width": width,
            "height": height,
            "fps": fps,
            "video_codec": codec,
            "pixel_format": video_timeline.get("pixel_format"),
            "audio_codec": (timeline_probe.get("audio_timeline") or {}).get("codec_name"),
            "has_audio": bool(timeline_probe.get("audio_timeline")),
            "audio_channels": 0,
            "metadata_fallback": "packet_timeline",
        }
    container_duration = float(metadata.get("duration") or 0)
    target_duration = float(segment.get("content_duration") or segment.get("duration") or 0)
    output_duration = container_duration
    duration_source = "container"
    if _duration_needs_packet_fallback(container_duration, target_duration):
        timeline_probe = _probe_video_packet_timeline(output_path)
        packet_duration = float(timeline_probe.get("packet_timeline_span") or 0)
        if timeline_probe.get("probe_succeeded") and packet_duration > 0:
            output_duration = packet_duration
            duration_source = "packet_pts"
    if output_duration <= 0:
        raise SegmentOutputValidationError("OUTPUT_DURATION_INVALID", "Segment duration is invalid.")
    if target_duration > 0 and output_duration < target_duration * 0.5:
        raise SegmentOutputValidationError(
            "OUTPUT_DURATION_TOO_SHORT",
            f"Output duration {output_duration:.3f}s is below 50% of expected {target_duration:.3f}s.",
            quality_status="rejected_timeline",
            details={
                "output_duration": output_duration,
                "container_duration": container_duration,
                "duration_source": duration_source,
                "packet_timeline": timeline_probe,
                "target_duration": target_duration,
                "minimum_duration": target_duration * 0.5,
            },
        )
    if not metadata.get("video_codec") or int(metadata.get("width") or 0) <= 0 or int(metadata.get("height") or 0) <= 0:
        raise SegmentOutputValidationError(
            "OUTPUT_VIDEO_STREAM_INVALID",
            "The Xmax output video stream is missing codec or resolution metadata.",
            details={"metadata": metadata},
        )
    try:
        decode_health = media_service.inspect_video_decode_health(output_path, output_duration)
    except Exception as exc:
        raise SegmentOutputValidationError("OUTPUT_FRAME_DECODE_FAILED", str(exc), details={"decode_error": str(exc)[:500]}) from exc
    if float(decode_health.get("black_ratio") or 0) >= 0.90:
        raise SegmentOutputValidationError(
            "OUTPUT_BLACK_FRAMES",
            "The Xmax output is almost entirely black.",
            details=decode_health,
        )
    audit_dir.mkdir(parents=True, exist_ok=True)
    first_frame = audit_dir / f"{output_path.stem}-first.jpg"
    last_frame = audit_dir / f"{output_path.stem}-last.jpg"
    try:
        media_service.extract_video_frame(output_path, first_frame, 0.0)
        media_service.extract_video_frame(output_path, last_frame, max(0.0, output_duration - min(0.05, output_duration / 2)))
    except Exception as exc:
        first_frame.unlink(missing_ok=True)
        last_frame.unlink(missing_ok=True)
        raise SegmentOutputValidationError("OUTPUT_FRAME_DECODE_FAILED", str(exc), details={"decode_error": str(exc)[:500]}) from exc
    return {
        **metadata,
        "duration": output_duration,
        "container_duration": container_duration,
        "duration_source": duration_source,
        "packet_timeline": timeline_probe,
        **decode_health,
        "file_size": output_path.stat().st_size,
        "first_frame_path": str(first_frame),
        "last_frame_path": str(last_frame),
        "output_preflight_passed": True,
    }


def _run_audit_command(command: list[str], *, timeout: int = 120) -> dict[str, Any]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit_immutable_media(staging_path: Path, destination_path: Path, *, conflict_code: str) -> dict[str, Any]:
    """Move one complete staging file into place without ever replacing different bytes."""
    if not staging_path.is_file() or staging_path.stat().st_size <= 0:
        raise SegmentOutputValidationError("OUTPUT_EMPTY", "The Xmax output file is empty.")
    staging_sha256 = _sha256_path(staging_path)
    staging_size = staging_path.stat().st_size
    if destination_path.exists():
        if (
            not destination_path.is_file()
            or destination_path.stat().st_size != staging_size
            or _sha256_path(destination_path) != staging_sha256
        ):
            raise SegmentOutputValidationError(
                conflict_code,
                "This attempt already contains different immutable output bytes.",
                details={"destination_path": str(destination_path)},
            )
        staging_path.unlink(missing_ok=True)
        return {"path": str(destination_path), "sha256": staging_sha256, "size": staging_size, "idempotent": True}
    staging_path.replace(destination_path)
    return {"path": str(destination_path), "sha256": staging_sha256, "size": staging_size, "idempotent": False}


def _write_normalization_diagnostics(output_dir: Path, payload: dict[str, Any]) -> Path:
    diagnostics_path = output_dir / f"normalization-diagnostics-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    diagnostics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return diagnostics_path


def _local_probe_dir(probe_id: str) -> Path:
    return (OUTPUTS_DIR / "audit" / "p4-raw-first-persistence" / _safe_component(probe_id, "local_persistence_probe_id")).resolve()


def _require_local_probe_request(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail={"error_code": "LOCAL_PERSISTENCE_PROBE_FORBIDDEN"})


def _write_local_probe_manifest(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "local-persistence-probe.json"
    staging_path = output_dir / f".manifest-{uuid.uuid4().hex}.json"
    staging_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    staging_path.replace(manifest_path)
    return manifest_path


def _local_probe_public_payload(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "manifest_url": _public_output(manifest_path),
    }


async def _run_local_persistence_probe(
    *,
    request: Request,
    task: dict[str, Any],
    segment: dict[str, Any],
    output_file: UploadFile,
    probe_id: str,
    force_normalization_failure: bool,
) -> dict[str, Any]:
    _require_local_probe_request(request)
    output_dir = _local_probe_dir(probe_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_path = output_dir / f".uploading-{uuid.uuid4().hex}.webm"
    raw_path = output_dir / "remote.raw.webm"
    normalized_candidate = output_dir / f".normalizing-{uuid.uuid4().hex}.webm"
    normalized_path = output_dir / "remote.normalized.webm"
    started_at = now_iso()
    total = 0
    try:
        with staging_path.open("wb") as target:
            while True:
                chunk = await output_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    raise SegmentOutputValidationError("OUTPUT_TOO_LARGE", "The probe output exceeds the configured file-size limit.")
                target.write(chunk)
        try:
            raw_commit = _commit_immutable_media(
                staging_path,
                raw_path,
                conflict_code="LOCAL_PROBE_RAW_IMMUTABLE_CONFLICT",
            )
        except SegmentOutputValidationError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error_code": exc.code, "error_message": str(exc), **exc.details},
            ) from exc
        raw_mtime_ns = raw_path.stat().st_mtime_ns
        raw_metadata = validate_segment_output_file(raw_path, segment, output_dir / "quality-preflight" / "raw")
        packet_timeline = _probe_video_packet_timeline(raw_path)
        raw_packet_duration = float(packet_timeline.get("packet_timeline_span") or raw_metadata.get("duration") or 0)
        target_duration = float(segment.get("content_duration") or segment.get("duration") or 0)
        manifest: dict[str, Any] = {
            "attempt_type": "local_persistence_probe",
            "probe_id": probe_id,
            "status": "raw_output_persisted",
            "task_id": task["id"],
            "unit_id": segment["id"],
            "provider_call_started": False,
            "temporary_key_created": False,
            "session_created": False,
            "credit_change": 0,
            "started_at": started_at,
            "updated_at": now_iso(),
            "runtime": {
                "feature": RAW_FIRST_RUNTIME_FEATURE,
                "module_loaded_at": RAW_FIRST_MODULE_LOADED_AT,
                "module_source_path": RAW_FIRST_MODULE_SOURCE_PATH,
                "module_sha256": RAW_FIRST_MODULE_SHA256,
            },
            "raw_output": {
                "status": "persisted",
                "path": str(raw_path),
                "url": _public_output(raw_path),
                "sha256": raw_commit["sha256"],
                "size": raw_commit["size"],
                "mtime_ns": raw_mtime_ns,
                "mime_type": output_file.content_type or "video/webm",
                "metadata": {
                    "duration": raw_metadata.get("duration"),
                    "container_duration": raw_metadata.get("container_duration"),
                    "codec": raw_metadata.get("video_codec"),
                    "width": raw_metadata.get("width"),
                    "height": raw_metadata.get("height"),
                    "fps": raw_metadata.get("fps"),
                    "packet_timeline": packet_timeline,
                },
            },
            "normalization": {"status": "pending"},
        }
        manifest_path = _write_local_probe_manifest(output_dir, manifest)
        if force_normalization_failure:
            error = SegmentOutputValidationError(
                "LOCAL_PROBE_FORCED_NORMALIZATION_FAILURE",
                "The localhost-only persistence probe forced normalization to fail after raw persistence.",
                quality_status="rejected_timeline",
                details={"probe_only": True, "target_duration": target_duration, "source_packet_duration": raw_packet_duration},
            )
            diagnostics_payload = {
                "attempt_type": "local_persistence_probe",
                "probe_id": probe_id,
                "failure_stage": "normalization",
                "error_code": error.code,
                "error_message": str(error),
                "error_details": error.details,
                "raw_output": manifest["raw_output"],
                "created_at": now_iso(),
            }
            diagnostics_path = _write_normalization_diagnostics(output_dir, diagnostics_payload)
            manifest["status"] = "normalization_failed"
            manifest["updated_at"] = now_iso()
            manifest["normalization"] = {
                "status": "failed",
                "error_code": error.code,
                "error_message": str(error),
                "diagnostics_path": str(diagnostics_path),
                "diagnostics_url": _public_output(diagnostics_path),
            }
            manifest["raw_output"]["unchanged_after_normalization"] = (
                raw_path.stat().st_mtime_ns == raw_mtime_ns
                and raw_path.stat().st_size == raw_commit["size"]
                and _sha256_path(raw_path) == raw_commit["sha256"]
            )
            manifest_path = _write_local_probe_manifest(output_dir, manifest)
            return _local_probe_public_payload(manifest, manifest_path)
        source_fps = float(raw_metadata.get("fps") or 30.0)
        normalization = normalize_segment_packet_timeline(
            raw_path,
            normalized_candidate,
            packet_duration=raw_packet_duration,
            target_duration=target_duration,
            fps=source_fps,
        )
        normalized_metadata = validate_segment_output_file(
            normalized_candidate,
            segment,
            output_dir / "quality-preflight" / "normalized",
        )
        normalized_commit = _commit_immutable_media(
            normalized_candidate,
            normalized_path,
            conflict_code="LOCAL_PROBE_NORMALIZED_IMMUTABLE_CONFLICT",
        )
        raw_unchanged = (
            raw_path.stat().st_mtime_ns == raw_mtime_ns
            and raw_path.stat().st_size == raw_commit["size"]
            and _sha256_path(raw_path) == raw_commit["sha256"]
        )
        manifest["status"] = "normalized_output_ready"
        manifest["updated_at"] = now_iso()
        manifest["raw_output"]["unchanged_after_normalization"] = raw_unchanged
        manifest["normalization"] = {
            "status": "ready",
            "path": str(normalized_path),
            "url": _public_output(normalized_path),
            "sha256": normalized_commit["sha256"],
            "size": normalized_commit["size"],
            "metadata": {
                "duration": normalized_metadata.get("duration"),
                "container_duration": normalized_metadata.get("container_duration"),
                "codec": normalized_metadata.get("video_codec"),
                "width": normalized_metadata.get("width"),
                "height": normalized_metadata.get("height"),
                "fps": normalized_metadata.get("fps"),
                "packet_timeline": normalized_metadata.get("packet_timeline"),
            },
            "diagnostics": normalization,
        }
        manifest_path = _write_local_probe_manifest(output_dir, manifest)
        return _local_probe_public_payload(manifest, manifest_path)
    finally:
        staging_path.unlink(missing_ok=True)
        normalized_candidate.unlink(missing_ok=True)


def preserve_rejected_output_audit(
    output_path: Path,
    *,
    task: dict[str, Any],
    segment: dict[str, Any],
    validation_error: SegmentOutputValidationError,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    audit_dir = (
        OUTPUTS_DIR.resolve()
        / "audit"
        / _safe_component(str(task["episode_id"]), "episode_id")
        / _safe_component(str(task["id"]), "task_id")
        / "xmax-output-duration"
        / _safe_component(str(segment["id"]), "segment_id")
        / timestamp
    )
    audit_dir.mkdir(parents=True, exist_ok=False)
    raw_path = audit_dir / "rejected-raw.webm"
    shutil.copy2(output_path, raw_path)

    ffprobe = media_service.resolve_ffprobe_binary()
    ffmpeg = media_service.resolve_ffmpeg_binary()
    if not ffprobe or not ffmpeg:
        report = {
            "classification": "insufficient_evidence",
            "validation_error": {
                "code": validation_error.code,
                "message": str(validation_error),
                "details": validation_error.details,
            },
            "raw_path": str(raw_path),
            "file_size": raw_path.stat().st_size,
            "ffprobe_available": bool(ffprobe),
            "ffmpeg_available": bool(ffmpeg),
        }
        (audit_dir / "audit-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    metadata_probe = _run_audit_command([
        ffprobe,
        "-v", "error",
        "-count_frames",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(raw_path),
    ])
    packet_probe = _run_audit_command([
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_packets",
        "-show_entries", "packet=pts_time,dts_time,duration_time,flags",
        "-of", "json",
        str(raw_path),
    ])
    decode_probe = _run_audit_command([
        ffmpeg,
        "-v", "error",
        "-i", str(raw_path),
        "-map", "0:v:0",
        "-f", "null",
        os.devnull,
    ])

    try:
        metadata_payload = json.loads(metadata_probe["stdout"] or "{}")
    except json.JSONDecodeError:
        metadata_payload = {}
    try:
        packet_payload = json.loads(packet_probe["stdout"] or "{}")
    except json.JSONDecodeError:
        packet_payload = {}

    video_stream = next(
        (item for item in metadata_payload.get("streams", []) if item.get("codec_type") == "video"),
        {},
    )
    format_duration = _float_or_none((metadata_payload.get("format") or {}).get("duration"))
    stream_duration = _float_or_none(video_stream.get("duration"))
    nb_read_frames = int(video_stream.get("nb_read_frames") or 0)
    packet_starts: list[float] = []
    packet_ends: list[float] = []
    for packet in packet_payload.get("packets", []):
        start = _float_or_none(packet.get("pts_time"))
        if start is None:
            start = _float_or_none(packet.get("dts_time"))
        if start is None:
            continue
        duration = _float_or_none(packet.get("duration_time")) or 0.0
        packet_starts.append(start)
        packet_ends.append(start + max(0.0, duration))
    first_packet_pts = min(packet_starts) if packet_starts else None
    last_packet_end = max(packet_ends) if packet_ends else None
    packet_timeline_span = (
        max(0.0, last_packet_end - first_packet_pts)
        if first_packet_pts is not None and last_packet_end is not None
        else None
    )
    target_duration = float(segment.get("content_duration") or segment.get("duration") or 0)
    timeline_near_target = bool(
        target_duration > 0
        and packet_timeline_span is not None
        and target_duration * 0.90 <= packet_timeline_span <= target_duration * 1.25
    )
    classification = (
        "A_metadata_duration_error"
        if timeline_near_target and decode_probe["returncode"] == 0 and nb_read_frames > 0
        else "B_actual_timeline_too_short"
    )
    report = {
        "classification": classification,
        "validation_error": {
            "code": validation_error.code,
            "message": str(validation_error),
            "details": validation_error.details,
        },
        "raw_path": str(raw_path),
        "file_size": raw_path.stat().st_size,
        "target_duration": target_duration,
        "format_duration": format_duration,
        "stream_duration": stream_duration,
        "first_packet_pts": first_packet_pts,
        "last_packet_end": last_packet_end,
        "packet_timeline_span": packet_timeline_span,
        "nb_read_frames": nb_read_frames,
        "decode_succeeded": decode_probe["returncode"] == 0,
        "metadata_probe": metadata_probe,
        "packet_probe": packet_probe,
        "decode_probe": decode_probe,
    }
    (audit_dir / "audit-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def remux_rejected_output(raw_path: Path, remux_path: Path) -> dict[str, Any]:
    ffmpeg = media_service.resolve_ffmpeg_binary()
    if not ffmpeg:
        return {"attempted": False, "succeeded": False, "error": "FFmpeg is unavailable."}
    result = _run_audit_command([
        ffmpeg,
        "-y",
        "-fflags", "+genpts",
        "-i", str(raw_path),
        "-map", "0:v:0",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(remux_path),
    ])
    return {
        "attempted": True,
        "succeeded": result["returncode"] == 0 and remux_path.is_file() and remux_path.stat().st_size > 0,
        **result,
    }


def _allowed_webm(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in ALLOWED_WEBM_TYPES


def build_fixed_ranges(duration: float) -> list[tuple[float, float]]:
    """Return fixed ten-second ranges without scene detection."""
    if duration < MIN_SOURCE_DURATION or duration > MAX_SOURCE_DURATION:
        raise ValueError("The multi-shot MVP accepts source videos from 24 to 30 seconds.")
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < duration - 0.001:
        end = min(start + SHOT_SECONDS, duration)
        ranges.append((start, end))
        start = end
    return [(round(start, 3), round(end, 3)) for start, end in ranges]


def validate_episode_duration(raw_duration: Any) -> float:
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_SOURCE_DURATION", "source_duration": raw_duration}) from exc
    if not math.isfinite(duration) or duration <= MIN_EPISODE_DURATION:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_SOURCE_DURATION", "source_duration": raw_duration})
    if duration > MAX_EPISODE_DURATION:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "SOURCE_DURATION_OUT_OF_RANGE",
                "source_duration": duration,
                "max_seconds": MAX_EPISODE_DURATION,
                "user_message": "视频超过3分钟，请分集上传",
            },
        )
    return duration


def duration_within_ten_percent(source_duration: float, output_duration: float) -> bool:
    return source_duration > 0 and output_duration > 0 and abs(output_duration - source_duration) / source_duration <= 0.10


class ShotDurationRatioError(ValueError):
    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        ratio = details.get("calculated_ratio")
        sequence = details.get("sequence")
        prefix = f"Shot {sequence} " if sequence is not None else "Shot "
        super().__init__(f"{prefix}duration alignment ratio {float(ratio):.4f} is outside the allowed 0.5-2.0 range.")


def duration_alignment_ratio(target_duration: float, actual_duration: float, *, details: dict[str, Any] | None = None) -> float:
    if target_duration <= 0 or actual_duration <= 0:
        raise ValueError("Shot durations must be greater than zero.")
    ratio = target_duration / actual_duration
    if ratio < 0.5 or ratio > 2.0:
        raise ShotDurationRatioError({**(details or {}), "calculated_ratio": ratio, "allowed_range": {"min": 0.5, "max": 2.0}})
    return ratio


def _segment_output_stage(segment: dict[str, Any], actual_duration: float) -> str:
    """Infer the stage for legacy rows without adding a migration or changing files."""
    if segment.get("mapping_status") == "preserve_original" or bool(segment.get("preserve_original")):
        return "preserve_source"
    if segment.get("trimmed_output_duration") is not None:
        return "content_trimmed"

    target_duration = float(segment["end_time"]) - float(segment["start_time"])
    pre_roll = max(0.0, float(segment.get("pre_roll_duration") or 0))
    post_roll = max(0.0, float(segment.get("post_roll_duration") or 0))
    proxy_total = float(segment.get("proxy_duration") or 0)
    diagnostics: dict[str, Any] = {}
    try:
        parsed = json.loads(segment.get("diagnostics_json") or "{}")
        if isinstance(parsed, dict):
            diagnostics = parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostics = {}

    # Old browser outputs did not persist output_stage. A file that cannot cover
    # the proxy pre-roll plus the content interval is already content-only. The
    # remote media clock is an additional guard for the known legacy Shot 1.
    remote_last_media_time = diagnostics.get("remoteLastMediaTime")
    if remote_last_media_time is not None and proxy_total > 0 and float(remote_last_media_time) + 0.10 < proxy_total:
        return "content_trimmed"
    if actual_duration + 0.05 < pre_roll + target_duration:
        return "content_trimmed"
    if proxy_total > 0 and actual_duration > target_duration + max(0.25, post_roll * 0.25):
        return "raw_proxy"
    return "content_trimmed"


def _prepare_merge_inputs(task: dict[str, Any], segments: list[dict[str, Any]], paths: list[Path]) -> dict[str, Any]:
    """Probe and normalize each persisted Shot to a content-only merge input."""
    task_dir = _task_dir(task["project_id"], task["episode_id"], task["attempt_id"])
    temp_dir = task_dir / "merge-temp"
    prepared_paths: list[Path] = []
    target_durations: list[float] = []
    content_actual_durations: list[float] = []
    stages: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    try:
        for segment, path in zip(segments, paths):
            raw_metadata = media_service.probe_media_metadata(path)
            raw_duration = float(raw_metadata["duration"])
            target_duration = float(segment["end_time"]) - float(segment["start_time"])
            stage = _segment_output_stage(segment, raw_duration)
            content_path = path
            content_metadata = raw_metadata
            pre_roll = max(0.0, float(segment.get("pre_roll_duration") or 0))
            post_roll = max(0.0, float(segment.get("post_roll_duration") or 0))
            if stage == "raw_proxy":
                temp_dir.mkdir(parents=True, exist_ok=True)
                content_path = temp_dir / f"shot-{int(segment['sequence']):03d}-content.mp4"
                media_service.trim_video(path, content_path, pre_roll, pre_roll + float(segment.get("content_duration") or target_duration))
                content_metadata = media_service.probe_media_metadata(content_path)

            content_duration = float(content_metadata["duration"])
            ratio_details = {
                "sequence": int(segment["sequence"]),
                "target_duration": target_duration,
                "raw_file_duration": raw_duration,
                "output_stage": stage,
                "pre_roll": pre_roll,
                "post_roll": post_roll,
                "content_probe_duration": content_duration,
            }
            duration_alignment_ratio(target_duration, content_duration, details=ratio_details)
            prepared_paths.append(content_path)
            target_durations.append(target_duration)
            content_actual_durations.append(content_duration)
            stages.append(stage)
            diagnostics.append({**ratio_details, "content_path": str(content_path), "raw_file_size": path.stat().st_size})
        return {
            "paths": prepared_paths,
            "target_durations": target_durations,
            "actual_durations": content_actual_durations,
            "stages": stages,
            "diagnostics": diagnostics,
            "temp_dir": temp_dir,
        }
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _set_merge_failed(task_id: str, code: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_tasks SET status='merge_failed', error_code=?, error_message=?, updated_at=? WHERE id=? AND status!='cancelled'",
            (code, message[:1000], now_iso(), task_id),
        )


def _mark_client_lost(task_id: str) -> None:
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_segments SET status='interrupted', execution_status='interrupted', error_code='CLIENT_LOST', error_message='The browser run ended before this Shot was persisted.', updated_at=? WHERE task_id=? AND status IN ('running','local_ready','uploading')",
            (timestamp, task_id),
        )
        conn.execute(
            "UPDATE xmax_thirty_second_tasks SET status='interrupted', error_code='CLIENT_LOST', error_message='The browser run ended before the current Shot was persisted.', updated_at=? WHERE id=? AND status IN ('running','local_ready','uploading')",
            (timestamp, task_id),
        )


def _mark_segment_browser_running(task_id: str, segment_id: str) -> None:
    timestamp = now_iso()
    with get_connection() as conn:
        segment = conn.execute(
            "SELECT status, preserve_original, use_source_for_merge FROM xmax_thirty_second_segments WHERE id=? AND task_id=?",
            (segment_id, task_id),
        ).fetchone()
        if not segment:
            raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
        if segment["status"] == "completed":
            return
        if bool(segment["preserve_original"]) or bool(segment["use_source_for_merge"]):
            raise HTTPException(status_code=409, detail={"error_code": "SEGMENT_DOES_NOT_REQUIRE_PROVIDER"})
        if segment["status"] not in {"pending", "interrupted", "running"}:
            raise HTTPException(
                status_code=409,
                detail={"error_code": "SEGMENT_NOT_RESUMABLE", "status": segment["status"]},
            )
        conn.execute(
            "UPDATE xmax_thirty_second_segments "
            "SET status='running', execution_status='running', error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? "
            "WHERE id=? AND task_id=? AND status IN ('pending','interrupted','running')",
            (timestamp, timestamp, segment_id, task_id),
        )
        conn.execute(
            "UPDATE xmax_thirty_second_tasks "
            "SET status='running', error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? "
            "WHERE id=? AND status NOT IN ('completed','cancelled')",
            (timestamp, timestamp, task_id),
        )


def _scene_analysis_dir(episode: dict[str, Any]) -> Path:
    directory = OUTPUTS_DIR.resolve() / _safe_component(episode["project_id"], "project_id") / _safe_component(episode["id"], "episode_id") / "xmax-thirty-second" / "scene-analysis"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _merge_scene_candidates(candidates: list[dict[str, float]], duration: float, *, minimum_gap: float = 2.5) -> list[dict[str, float]]:
    normalized = sorted((item for item in candidates if 0.75 < item["timestamp"] < duration - 0.75), key=lambda item: item["timestamp"])
    merged: list[dict[str, float]] = []
    for candidate in normalized:
        if merged and candidate["timestamp"] - merged[-1]["timestamp"] < minimum_gap:
            if candidate["score"] > merged[-1]["score"]:
                merged[-1] = candidate
        else:
            merged.append(candidate)
    return merged


def build_scene_ranges(duration: float, candidates: list[dict[str, float]], *, max_seconds: float = 15.0) -> list[dict[str, float | str]]:
    """Use detected cuts first; fixed ranges are only a marked fallback."""
    boundaries = _merge_scene_candidates(candidates, duration)
    ranges: list[dict[str, float | str]] = []
    start = 0.0
    for candidate in boundaries:
        cut = float(candidate["timestamp"])
        if cut - start < 2.0:
            continue
        if cut - start > max_seconds:
            continue
        ranges.append({"source_start": round(start, 3), "source_end": round(cut, 3), "scene_score": float(candidate["score"]), "cut_method": "scene_detected"})
        start = cut
    if duration - start > max_seconds:
        # This is still scene-aware: use the strongest available internal cut.
        internal = [item for item in boundaries if start + 5.0 <= float(item["timestamp"]) <= duration - 5.0]
        if internal:
            split = max(internal, key=lambda item: item["score"])
            ranges.append({"source_start": round(start, 3), "source_end": round(float(split["timestamp"]), 3), "scene_score": float(split["score"]), "cut_method": "scene_detected_long_shot"})
            start = float(split["timestamp"])
        else:
            while duration - start > max_seconds:
                end = min(duration, start + 10.0)
                ranges.append({"source_start": round(start, 3), "source_end": round(end, 3), "scene_score": 0.0, "cut_method": "fixed_fallback"})
                start = end
    if duration - start > 0.001:
        ranges.append({"source_start": round(start, 3), "source_end": round(duration, 3), "scene_score": 0.0, "cut_method": "scene_detected_end"})
    if not ranges:
        ranges = []
        start = 0.0
        while duration - start > max_seconds:
            end = min(duration, start + 10.0)
            ranges.append({"source_start": round(start, 3), "source_end": round(end, 3), "scene_score": 0.0, "cut_method": "fixed_fallback"})
            start = end
        ranges.append({"source_start": round(start, 3), "source_end": round(duration, 3), "scene_score": 0.0, "cut_method": "fixed_fallback"})
    return ranges


def build_transform_units(
    base_ranges: list[dict[str, Any]],
    candidates: list[dict[str, float]],
    *,
    target_seconds: float = TRANSFORM_TARGET_SECONDS,
    max_seconds: float = TRANSFORM_MAX_SECONDS,
    min_seconds: float = TRANSFORM_MIN_SECONDS,
) -> list[dict[str, Any]]:
    """Split Base Shots into bounded units, snapping balanced cuts to visual signals."""
    if target_seconds <= 0 or max_seconds <= 0 or min_seconds <= 0 or target_seconds > max_seconds:
        raise ValueError("Transform Unit limits are invalid.")
    units: list[dict[str, Any]] = []
    for parent_index, base in enumerate(base_ranges, start=1):
        base_start = float(base["source_start"])
        base_end = float(base["source_end"])
        duration = base_end - base_start
        if duration <= 0:
            continue
        unit_count = max(1, math.ceil(duration / max_seconds))
        balanced_step = duration / unit_count
        selected_boundaries: list[tuple[float, float, str]] = []
        previous = base_start
        for boundary_index in range(1, unit_count):
            ideal = base_start + balanced_step * boundary_index
            remaining_units = unit_count - boundary_index
            lower = max(previous + min_seconds, base_end - max_seconds * remaining_units)
            upper = min(previous + max_seconds, base_end - min_seconds * remaining_units)
            if upper < lower:
                lower = upper = max(previous, min(ideal, base_end))
            nearby = [
                item for item in candidates
                if lower <= float(item["timestamp"]) <= upper and abs(float(item["timestamp"]) - ideal) <= 1.25
            ]
            if nearby:
                selected = min(nearby, key=lambda item: (abs(float(item["timestamp"]) - ideal) - float(item.get("score") or 0) * 0.35, -float(item.get("score") or 0)))
                boundary = float(selected["timestamp"])
                score = float(selected.get("score") or 0)
                method = "visual_signal"
            else:
                boundary = max(lower, min(ideal, upper))
                score = 0.0
                method = "balanced_fallback"
            selected_boundaries.append((boundary, score, method))
            previous = boundary
        boundaries = [(base_start, float(base.get("scene_score") or 0), str(base.get("cut_method") or "scene"))] + selected_boundaries + [(base_end, 0.0, "base_shot_end")]
        for child_index in range(len(boundaries) - 1):
            start = round(boundaries[child_index][0], 3)
            end = round(boundaries[child_index + 1][0], 3)
            if end - start <= 0.001:
                continue
            units.append(
                {
                    "transform_unit_id": f"transform-unit-{len(units) + 1:03d}",
                    "parent_shot_id": f"base-shot-{parent_index:03d}",
                    "parent_sequence": parent_index,
                    "source_start": start,
                    "source_end": end,
                    "target_duration": round(end - start, 3),
                    "scene_score": float(boundaries[child_index + 1][1] or base.get("scene_score") or 0),
                    "cut_method": boundaries[child_index + 1][2],
                    "base_cut_method": base.get("cut_method"),
                }
            )
    return units


def calculate_transform_risk(unit: dict[str, Any], shot_type: str = "unknown") -> dict[str, Any]:
    """Score only evidence available locally; no person detector is implied."""
    duration = float(unit.get("target_duration") or (float(unit["source_end"]) - float(unit["source_start"])))
    scene_score = max(0.0, min(1.0, float(unit.get("scene_score") or 0)))
    score = min(35.0, duration / TRANSFORM_MAX_SECONDS * 35.0)
    factors = [f"duration:{duration:.3f}s"]
    if unit.get("cut_method") == "balanced_fallback":
        score += 12.0
        factors.append("no_nearby_visual_cut")
    if scene_score >= 0.35:
        score += min(18.0, scene_score * 18.0)
        factors.append("strong_visual_change")
    if shot_type == "multi_character":
        score += 45.0
        factors.append("multi_character")
    elif shot_type == "single_character" and duration > 3.0:
        score += 10.0
        factors.append("long_single_character_unit")
    score = round(min(100.0, score), 2)
    level = "high" if score >= 60 else "medium" if score >= 35 else "low"
    return {"risk_score": score, "risk_level": level, "risk_factors": factors}


def summarize_transform_preflight(
    transform_units: list[dict[str, Any]],
    mappings: dict[str, dict[str, Any]],
) -> dict[str, int]:
    ai_units: list[dict[str, Any]] = []
    for fallback_sequence, unit in enumerate(transform_units, start=1):
        sequence = int(unit.get("sequence") or fallback_sequence)
        _, mapping = _mapping_for_transform_unit(mappings, unit, sequence)
        shot_type = str(mapping.get("shot_type") or "unknown")
        if shot_type == "single_character":
            ai_units.append({**unit, **calculate_transform_risk(unit, shot_type)})
    high_risk_count = sum(1 for item in ai_units if item["risk_level"] == "high")
    return {
        "transform_unit_count": len(transform_units),
        "ai_transform_count": len(ai_units),
        "high_risk_count": high_risk_count,
        "estimated_source_fallback_min": 0,
        "estimated_source_fallback_max": high_risk_count,
    }


def _scene_plan(episode: dict[str, Any], source_path: Path, threshold: float) -> dict[str, Any]:
    metadata = media_service.probe_video_metadata(source_path)
    duration = float(metadata["duration"])
    thumbnail_video_duration = min(duration, float(metadata.get("video_duration") or duration))
    thumbnail_fps = float(metadata.get("fps") or 0)

    def extract_scene_thumbnail(path: Path, timestamp: float) -> None:
        media_service.extract_video_frame(
            source_path,
            path,
            timestamp,
            video_duration=thumbnail_video_duration,
            fps=thumbnail_fps,
        )

    candidates = [candidate for candidate in media_service.detect_scene_candidates(source_path, threshold) if 0.75 < float(candidate["timestamp"]) < duration - 0.75]
    analysis_dir = _scene_analysis_dir(episode) / f"plan-{uuid.uuid4().hex[:10]}"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    candidate_payload: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        timestamp = float(candidate["timestamp"])
        thumbs = {}
        for label, offset in (("previous", -0.25), ("current", 0.0), ("next", 0.25)):
            path = analysis_dir / f"cut-{index:03d}-{label}.jpg"
            extract_scene_thumbnail(path, timestamp + offset)
            thumbs[label] = _public_output(path)
        candidate_payload.append({"timestamp": timestamp, "scene_score": candidate["score"], "previous_frame_url": thumbs["previous"], "current_frame_url": thumbs["current"], "next_frame_url": thumbs["next"]})
    ranges = build_scene_ranges(duration, candidates)
    fine_threshold = max(0.10, min(threshold, threshold * 0.5))
    fine_candidates = [candidate for candidate in media_service.detect_scene_candidates(source_path, fine_threshold) if 0.5 < float(candidate["timestamp"]) < duration - 0.5]
    transform_units = build_transform_units(ranges, fine_candidates)
    unit_shots: list[dict[str, Any]] = []
    for index, item in enumerate(transform_units, start=1):
        start = float(item["source_start"])
        end = float(item["source_end"])
        thumb_dir = analysis_dir / f"transform-unit-{index:03d}"
        frames = {}
        for label, timestamp in (("first", start), ("middle", (start + end) / 2), ("last", max(start, end - 0.04))):
            path = thumb_dir / f"{label}.jpg"
            extract_scene_thumbnail(path, timestamp)
            frames[label] = _public_output(path)
        risk = calculate_transform_risk(item)
        unit_shots.append({
            "sequence": index, "source_start": start, "source_end": end, "target_duration": round(end - start, 3),
            "scene_score": item["scene_score"], "cut_method": item["cut_method"], "shot_type": "unknown",
            "transform_unit_id": item["transform_unit_id"], "parent_shot_id": item["parent_shot_id"], "parent_sequence": item["parent_sequence"],
            "risk_score": risk["risk_score"], "risk_level": risk["risk_level"], "risk_factors": risk["risk_factors"],
            "detected_people": [], "mapping_status": "manual_required", "preserve_original": False,
            "first_frame_url": frames["first"], "middle_frame_url": frames["middle"], "last_frame_url": frames["last"],
        })
    shots: list[dict[str, Any]] = []
    for index, item in enumerate(ranges, start=1):
        start = float(item["source_start"])
        end = float(item["source_end"])
        thumb_dir = analysis_dir / f"base-shot-{index:03d}"
        frames = {}
        for label, timestamp in (("first", start), ("middle", (start + end) / 2), ("last", max(start, end - 0.04))):
            path = thumb_dir / f"{label}.jpg"
            extract_scene_thumbnail(path, timestamp)
            frames[label] = _public_output(path)
        children = [unit for unit in unit_shots if unit["parent_sequence"] == index]
        risk_score = max((float(unit["risk_score"]) for unit in children), default=0.0)
        shots.append(
            {
                "sequence": index,
                "parent_shot_id": f"base-shot-{index:03d}",
                "source_start": start,
                "source_end": end,
                "target_duration": round(end - start, 3),
                "scene_score": float(item.get("scene_score") or 0),
                "cut_method": item.get("cut_method") or "scene_detected",
                "risk_score": risk_score,
                "risk_level": "high" if risk_score >= 60 else "medium" if risk_score >= 35 else "low",
                "transform_unit_count": len(children),
                "transform_unit_ids": [unit["transform_unit_id"] for unit in children],
                "shot_type": "unknown",
                "detected_people": [],
                "mapping_status": "manual_required",
                "preserve_original": False,
                "first_frame_url": frames["first"],
                "middle_frame_url": frames["middle"],
                "last_frame_url": frames["last"],
            }
        )
    return {
        "episode_id": episode["id"],
        "project_id": episode["project_id"],
        "source_duration": duration,
        "threshold": threshold,
        "candidate_cuts": candidate_payload,
        "base_shot_count": len(ranges),
        "transform_unit_count": len(unit_shots),
        "base_shots": ranges,
        "shots": shots,
        "transform_units": unit_shots,
    }


def _scene_plan_cache_path(episode: dict[str, Any], threshold: float) -> Path:
    threshold_key = f"{threshold:.3f}".replace(".", "-")
    return (
        OUTPUTS_DIR.resolve()
        / _safe_component(episode["project_id"], "project_id")
        / _safe_component(episode["id"], "episode_id")
        / "xmax-thirty-second"
        / "scene-analysis"
        / f"scene-plan-{threshold_key}.json"
    )


def _scene_plan_source_signature(source_path: Path, threshold: float) -> dict[str, Any]:
    stat = source_path.stat()
    return {
        "path": str(source_path.resolve()),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "threshold": round(float(threshold), 6),
    }


def _load_persistent_scene_plan(
    episode: dict[str, Any],
    source_path: Path,
    threshold: float,
) -> dict[str, Any] | None:
    cache_path = _scene_plan_cache_path(episode, threshold)
    signature = _scene_plan_source_signature(source_path, threshold)
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                raise ValueError("Invalid scene plan cache payload.")
            plan = cached.get("plan")
            if (
                cached.get("version") == 1
                and cached.get("source") == signature
                and isinstance(plan, dict)
                and int(plan.get("base_shot_count") or 0) > 0
                and int(plan.get("transform_unit_count") or 0) > 0
            ):
                return {**plan, "persisted": True, "cache_status": "reused"}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


def _persistent_scene_plan(
    episode: dict[str, Any],
    source_path: Path,
    threshold: float,
) -> dict[str, Any]:
    """Reuse the exact Base Shot and Transform Unit plan for a stable Episode source."""
    cached = _load_persistent_scene_plan(episode, source_path, threshold)
    if cached:
        return cached
    cache_path = _scene_plan_cache_path(episode, threshold)
    signature = _scene_plan_source_signature(source_path, threshold)

    plan = _scene_plan(episode, source_path, threshold)
    payload = {
        "version": 1,
        "created_at": now_iso(),
        "source": signature,
        "plan": plan,
    }
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {**plan, "persisted": True, "cache_status": "created"}


def _write_persistent_scene_plan(
    episode: dict[str, Any],
    source_path: Path,
    threshold: float,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Atomically update an existing Scene Plan without rerunning analysis."""
    cache_path = _scene_plan_cache_path(episode, threshold)
    if not cache_path.is_file():
        raise HTTPException(status_code=409, detail={"error_code": "SCENE_PLAN_REQUIRED"})
    try:
        existing = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "SCENE_PLAN_INVALID", "error_message": str(exc)[:500]}) from exc
    signature = _scene_plan_source_signature(source_path, threshold)
    if existing.get("version") != 1 or existing.get("source") != signature:
        raise HTTPException(status_code=409, detail={"error_code": "SCENE_PLAN_SOURCE_CHANGED"})
    stored_plan = {
        key: value
        for key, value in plan.items()
        if key not in {"persisted", "cache_status"}
    }
    payload = {
        "version": 1,
        "created_at": existing.get("created_at") or now_iso(),
        "updated_at": now_iso(),
        "source": signature,
        "plan": stored_plan,
    }
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {**stored_plan, "persisted": True, "cache_status": "reused"}


def _apply_scene_mappings(
    episode: dict[str, Any],
    plan: dict[str, Any],
    mappings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    units = list(plan.get("transform_units") or [])
    available = {
        int(item.get("sequence") or sequence)
        for sequence, item in enumerate(units, start=1)
    }
    supplied = {int(sequence) for sequence in mappings}
    missing = sorted(available - supplied)
    invalid = sorted(supplied - available)
    if missing or invalid:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "SCENE_MAPPING_INCOMPLETE",
                "missing_sequences": missing,
                "invalid_sequences": invalid,
            },
        )
    updated_units: list[dict[str, Any]] = []
    for fallback_sequence, item in enumerate(units, start=1):
        sequence = int(item.get("sequence") or fallback_sequence)
        mapping = mappings[str(sequence)]
        shot_type = str(mapping.get("shot_type") or "unknown")
        character_id, reference_confirmed, preserve_source = _mapping_values(mapping)
        if shot_type == "single_character":
            if not character_id:
                raise HTTPException(status_code=422, detail={"error_code": "CHARACTER_MAPPING_REQUIRED", "sequence": sequence})
            if not reference_confirmed:
                raise HTTPException(status_code=422, detail={"error_code": "SINGLE_PERSON_REFERENCE_CONFIRMATION_REQUIRED", "sequence": sequence, "character_id": character_id})
            resolved = resolve_character_reference(character_id, episode["id"], episode["project_id"])
            if not resolved.get("mapping_valid"):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": resolved.get("error_code") or "CHARACTER_REFERENCE_INVALID",
                        "error_message": resolved.get("error_message"),
                        "sequence": sequence,
                        "character_id": character_id,
                    },
                )
            updated_units.append({
                **item,
                "shot_type": shot_type,
                "selected_character_id": character_id,
                "reference_face_confirmed": True,
                "mapping_status": "manual_confirmed",
                "preserve_original": False,
                "reference_preview_url": resolved.get("reference_preview_url"),
            })
            continue
        if shot_type == "multi_character" and not preserve_source:
            raise HTTPException(status_code=422, detail={"error_code": "MULTI_CHARACTER_REQUIRES_PRESERVE", "sequence": sequence})
        if shot_type not in {"multi_character", "no_character", "transition"}:
            raise HTTPException(status_code=422, detail={"error_code": "CHARACTER_MAPPING_REQUIRED", "sequence": sequence})
        updated_units.append({
            **item,
            "shot_type": shot_type,
            "selected_character_id": None,
            "reference_face_confirmed": False,
            "mapping_status": "preserve_original",
            "preserve_original": True,
            "reference_preview_url": None,
        })
    return {
        **plan,
        "transform_units": updated_units,
        "transform_unit_count": len(updated_units),
        "mappings_updated_at": now_iso(),
    }


def _ensure_episode_character_matches(
    conn: sqlite3.Connection,
    episode: dict[str, Any],
    plan: dict[str, Any],
) -> list[str]:
    """Persist only characters explicitly selected in the saved Scene mappings."""
    character_ids = sorted({
        str(unit.get("selected_character_id"))
        for unit in plan.get("transform_units") or []
        if unit.get("shot_type") == "single_character" and unit.get("selected_character_id")
    })
    if not character_ids:
        return []

    timestamp = now_iso()
    for character_id in character_ids:
        character = conn.execute(
            "SELECT id, name, character_code FROM characters WHERE id=? AND project_id=?",
            (character_id, episode["project_id"]),
        ).fetchone()
        if not character:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "CHARACTER_NOT_FOUND", "character_id": character_id},
            )
        detected_name = character["character_code"] or character["name"] or character_id
        existing = conn.execute(
            "SELECT id FROM episode_character_matches WHERE episode_id=? AND character_id=? ORDER BY created_at LIMIT 1",
            (episode["id"], character_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE episode_character_matches SET detected_name=?, match_status='user-match', confidence=1.0, use_project_asset=1, updated_at=? WHERE id=?",
                (detected_name, timestamp, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO episode_character_matches (id, episode_id, character_id, detected_name, match_status, confidence, use_project_asset, created_at, updated_at) VALUES (?, ?, ?, ?, 'user-match', 1.0, 1, ?, ?)",
                (f"match-{uuid.uuid4().hex[:8]}", episode["id"], character_id, detected_name, timestamp, timestamp),
            )
    return character_ids


def _persist_scene_mappings_and_episode_matches(
    episode: dict[str, Any],
    source_path: Path,
    threshold: float,
    updated_plan: dict[str, Any],
) -> dict[str, Any]:
    """Commit Episode bindings and the Scene Plan as one user save action."""
    cache_path = _scene_plan_cache_path(episode, threshold)
    original_payload = cache_path.read_bytes()
    try:
        with get_connection() as conn:
            _ensure_episode_character_matches(conn, episode, updated_plan)
            persisted_plan = _write_persistent_scene_plan(
                episode,
                source_path,
                threshold,
                updated_plan,
            )
    except Exception:
        restore_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.restore")
        try:
            restore_path.write_bytes(original_payload)
            restore_path.replace(cache_path)
        finally:
            restore_path.unlink(missing_ok=True)
        raise
    return persisted_plan


def _active_candidate_task_for_episode(episode_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in ACTIVE_CANDIDATE_STATUSES)
    with get_connection() as conn:
        rows = conn.execute(
            (
                "SELECT * FROM xmax_thirty_second_tasks "
                f"WHERE episode_id=? AND status IN ({placeholders}) "
                "ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC"
            ),
            (episode_id, *sorted(ACTIVE_CANDIDATE_STATUSES)),
        ).fetchall()
    for row in rows:
        task = row_to_dict(row)
        if _task_workflow(task) == "full_span_direct":
            continue
        if not _final_file_valid(task):
            return task
    return None


def _active_submission_task(conn: sqlite3.Connection, episode_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in ACTIVE_SUBMISSION_STATUSES)
    rows = conn.execute(
        (
            "SELECT * FROM xmax_thirty_second_tasks "
            f"WHERE episode_id=? AND status IN ({placeholders}) "
            "ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC"
        ),
        (episode_id, *sorted(ACTIVE_SUBMISSION_STATUSES)),
    ).fetchall()
    for row in rows:
        task = row_to_dict(row)
        if _task_workflow(task) == "full_span_direct":
            unit_rows = conn.execute(
                "SELECT status,execution_status FROM xmax_thirty_second_segments WHERE task_id=?",
                (task["id"],),
            ).fetchall()
            direct_in_flight = any(
                str(unit["execution_status"] or unit["status"] or "")
                in {"pending", "running", "local_ready", "uploading", "interrupted"}
                for unit in unit_rows
            )
            if not direct_in_flight:
                continue
        return task
    return None


def _task_workflow(task: dict[str, Any]) -> str:
    try:
        metadata = json.loads(task.get("scene_plan_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "advanced_scene_plan"
    if isinstance(metadata, dict) and metadata.get("workflow") == "full_span_direct":
        return "full_span_direct"
    return "advanced_scene_plan"


def _preparation_diagnostics(segment: dict[str, Any], status: str, **details: Any) -> str:
    diagnostics = _segment_diagnostics(segment)
    diagnostics["proxy_preparation"] = {
        "status": status,
        "updated_at": now_iso(),
        **details,
    }
    return json.dumps(diagnostics, ensure_ascii=True)


def _valid_existing_failed_proxy(segment: dict[str, Any]) -> dict[str, Any] | None:
    if segment.get("error_code") != "PROXY_PREPARATION_FAILED":
        return None
    proxy_path = Path(str(segment.get("source_path") or ""))
    if not proxy_path.is_file() or proxy_path.stat().st_size <= 0:
        return None
    try:
        metadata = media_service.probe_video_metadata(proxy_path)
        duration = float(metadata.get("duration") or metadata.get("video_duration") or 0)
    except (OSError, TypeError, ValueError, media_service.MediaToolError):
        return None
    if duration <= 0 or metadata.get("has_audio"):
        return None
    return {"path": str(proxy_path), **metadata, "duration": duration}


def _prepare_task_proxies(task_id: str) -> None:
    """Idempotently prepare local Unit proxies after the submission response."""
    with _PROXY_PREPARATION_LOCK:
        if task_id in _PROXY_PREPARATION_IN_FLIGHT:
            return
        _PROXY_PREPARATION_IN_FLIGHT.add(task_id)
    try:
        task = _task(task_id)
        if not task or task.get("status") in {"completed", "cancelled"}:
            return
        recovering_failed_task = (
            task.get("status") == "failed"
            and task.get("error_code") == "PROXY_PREPARATION_FAILED"
        )
        source_path = Path(str(task.get("source_path") or ""))
        if not source_path.is_file():
            raise media_service.MediaToolError("SOURCE_VIDEO_NOT_FOUND", "The source video is no longer available.")
        for segment in _segments(task_id):
            current_task = _task(task_id)
            if not current_task or current_task.get("status") == "cancelled":
                return
            if _segment_proxy_ready(segment):
                continue
            proxy_path = Path(str(segment["source_path"]))
            proxy = _valid_existing_failed_proxy(segment)
            timestamp = now_iso()
            with get_connection() as conn:
                if proxy is None:
                    conn.execute(
                        "UPDATE xmax_thirty_second_segments SET diagnostics_json=?, error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=? AND task_id=?",
                        (_preparation_diagnostics(segment, "preparing"), timestamp, timestamp, segment["id"], task_id),
                    )
                conn.execute(
                    "UPDATE xmax_thirty_second_tasks SET heartbeat_at=?, updated_at=? WHERE id=?",
                    (timestamp, timestamp, task_id),
                )
            if proxy is None:
                proxy = media_service.create_xmax_input_proxy(
                    source_path,
                    proxy_path,
                    float(segment["start_time"]) - float(segment.get("pre_roll_duration") or 0),
                    float(segment["end_time"]) + float(segment.get("post_roll_duration") or 0),
                )
            proxy_duration = float(proxy["duration"])
            is_preserved = bool(segment.get("preserve_original")) or segment.get("mapping_status") == "preserve_original"
            segment_status = "completed" if is_preserved else "pending"
            quality_status = "not_applicable" if is_preserved else (segment.get("quality_status") or "review_required")
            output_path = str(proxy_path) if is_preserved else None
            output_url = _public_source(proxy_path) if is_preserved else None
            file_size = proxy_path.stat().st_size if is_preserved else None
            completed_at = now_iso()
            refreshed_segment = {**segment, "proxy_duration": proxy_duration}
            with get_connection() as conn:
                conn.execute(
                    "UPDATE xmax_thirty_second_segments SET proxy_duration=?, status=?, execution_status=?, quality_status=?, output_path=?, output_url=?, output_duration=?, file_size=?, diagnostics_json=?, error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=? AND task_id=?",
                    (
                        proxy_duration,
                        segment_status,
                        segment_status,
                        quality_status,
                        output_path,
                        output_url,
                        proxy_duration if is_preserved else None,
                        file_size,
                        _preparation_diagnostics(refreshed_segment, "ready", proxy_duration=proxy_duration),
                        completed_at,
                        completed_at,
                        segment["id"],
                        task_id,
                    ),
                )
                conn.execute(
                    "UPDATE xmax_thirty_second_tasks SET error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=?",
                    (completed_at, completed_at, task_id),
                )
        prepared_segments = _segments(task_id)
        if (
            task.get("status") in {"pending", "interrupted"} or recovering_failed_task
        ) and _proxy_preparation_summary(prepared_segments)["preparation_complete"]:
            final_status = "merge_pending" if all(segment.get("status") == "completed" for segment in prepared_segments) else "pending"
            finished_at = now_iso()
            with get_connection() as conn:
                conn.execute(
                    "UPDATE xmax_thirty_second_tasks SET status=?, error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=?",
                    (final_status, finished_at, finished_at, task_id),
                )
    except Exception as exc:
        failed_at = now_iso()
        failed_segment = locals().get("segment")
        with get_connection() as conn:
            if isinstance(failed_segment, dict):
                conn.execute(
                    "UPDATE xmax_thirty_second_segments SET status='failed', execution_status='failed', diagnostics_json=?, error_code='PROXY_PREPARATION_FAILED', error_message=?, heartbeat_at=?, updated_at=? WHERE id=? AND task_id=?",
                    (
                        _preparation_diagnostics(failed_segment, "failed", error=str(exc)[:500]),
                        str(exc)[:500],
                        failed_at,
                        failed_at,
                        failed_segment["id"],
                        task_id,
                    ),
                )
            conn.execute(
                "UPDATE xmax_thirty_second_tasks SET status='failed', error_code='PROXY_PREPARATION_FAILED', error_message=?, heartbeat_at=?, updated_at=? WHERE id=?",
                (str(exc)[:500], failed_at, failed_at, task_id),
            )
    finally:
        with _PROXY_PREPARATION_LOCK:
            _PROXY_PREPARATION_IN_FLIGHT.discard(task_id)


def _kickoff_proxy_preparation(background_tasks: BackgroundTasks, task_id: str) -> None:
    background_tasks.add_task(_prepare_task_proxies, task_id)


def _sync_scene_mappings_to_active_task(
    episode: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    task = _active_candidate_task_for_episode(episode["id"])
    if not task:
        return {"active_task_id": None, "synced_segment_ids": []}

    units = list(plan.get("transform_units") or [])
    units_by_id = {
        str(unit.get("transform_unit_id")): unit
        for unit in units
        if unit.get("transform_unit_id")
    }
    units_by_sequence = {
        int(unit.get("sequence") or sequence): unit
        for sequence, unit in enumerate(units, start=1)
    }
    timestamp = now_iso()
    synced_segment_ids: list[str] = []
    reference_cache: dict[str, dict[str, Any]] = {}

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM xmax_thirty_second_segments WHERE task_id=? ORDER BY sequence",
            (task["id"],),
        ).fetchall()
        for row in rows:
            segment = row_to_dict(row)
            execution_status = segment.get("execution_status") or segment.get("status")
            if execution_status in {"running", "local_ready", "uploading", "completed"} or segment.get("output_path"):
                continue
            unit = units_by_id.get(str(segment.get("transform_unit_id") or ""))
            if not unit:
                unit = units_by_sequence.get(int(segment.get("sequence") or 0))
            if not unit:
                continue

            shot_type = str(unit.get("shot_type") or "unknown")
            diagnostics = _segment_diagnostics(segment)
            diagnostics.update({
                "preserveSourceByUser": False,
                "fallbackSourceSelected": False,
                "preserveSourceSelectedAt": None,
                "mappingSyncedAt": timestamp,
                "mappingSource": "scene_plan",
            })

            if shot_type == "single_character":
                character_id = str(unit.get("selected_character_id") or "")
                if not character_id or unit.get("mapping_status") != "manual_confirmed":
                    continue
                resolved = reference_cache.get(character_id)
                if resolved is None:
                    resolved = resolve_character_reference(character_id, episode["id"], episode["project_id"])
                    reference_cache[character_id] = resolved
                if not resolved.get("mapping_valid"):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_code": resolved.get("error_code") or "CHARACTER_REFERENCE_INVALID",
                            "error_message": resolved.get("error_message"),
                            "character_id": character_id,
                        },
                    )
                reference_value = resolved.get("resolved_local_path") or resolved.get("provider_url")
                diagnostics["awaitingUserAction"] = True
                conn.execute(
                    """
                    UPDATE xmax_thirty_second_segments
                    SET shot_type='single_character', selected_character_id=?,
                        mapping_status='manual_confirmed', preserve_original=0,
                        use_source_for_merge=0, status='pending', execution_status='pending',
                        quality_status='review_required', quality_reason=NULL,
                        reference_path=?, reference_url=?, reference_preview_url=?,
                        reference_type=?, error_code=NULL, error_message=NULL,
                        diagnostics_json=?, updated_at=?
                    WHERE id=? AND task_id=?
                    """,
                    (
                        character_id,
                        reference_value,
                        _public_reference_value(reference_value),
                        _cache_busted_reference_url(reference_value),
                        resolved.get("reference_type"),
                        json.dumps(diagnostics, ensure_ascii=True)[:20000],
                        timestamp,
                        segment["id"],
                        task["id"],
                    ),
                )
            elif shot_type in {"multi_character", "no_character", "transition"} and bool(unit.get("preserve_original")):
                diagnostics["awaitingUserAction"] = False
                conn.execute(
                    """
                    UPDATE xmax_thirty_second_segments
                    SET shot_type=?, selected_character_id=NULL,
                        mapping_status='preserve_original', preserve_original=1,
                        use_source_for_merge=0, status='completed', execution_status='completed',
                        quality_status='not_applicable', quality_reason=NULL,
                        reference_path=NULL, reference_url=NULL, reference_preview_url=NULL,
                        reference_type=NULL, error_code=NULL, error_message=NULL,
                        diagnostics_json=?, updated_at=?
                    WHERE id=? AND task_id=?
                    """,
                    (
                        shot_type,
                        json.dumps(diagnostics, ensure_ascii=True)[:20000],
                        timestamp,
                        segment["id"],
                        task["id"],
                    ),
                )
            else:
                continue
            synced_segment_ids.append(segment["id"])

        conn.execute(
            """
            UPDATE xmax_thirty_second_tasks
            SET scene_plan_json=?, status=CASE WHEN ? > 0 THEN 'pending' ELSE status END,
                error_code=CASE WHEN ? > 0 THEN NULL ELSE error_code END,
                error_message=CASE WHEN ? > 0 THEN NULL ELSE error_message END,
                updated_at=?
            WHERE id=?
            """,
            (
                json.dumps(plan, ensure_ascii=True),
                len(synced_segment_ids),
                len(synced_segment_ids),
                len(synced_segment_ids),
                timestamp,
                task["id"],
            ),
        )
    return {"active_task_id": task["id"], "synced_segment_ids": synced_segment_ids}


@router.get("/episodes/{episode_id}/xmax-thirty-second/scene-plan")
def get_scene_plan(episode_id: str, threshold: float = 0.35, create_if_missing: bool = True):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    if threshold < 0.1 or threshold > 0.9:
        raise HTTPException(status_code=422, detail={"error_code": "SCENE_THRESHOLD_INVALID"})
    try:
        source_path = _source_path(episode)
        plan = _load_persistent_scene_plan(episode, source_path, threshold)
        if not plan and create_if_missing:
            plan = _persistent_scene_plan(episode, source_path, threshold)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error_code": getattr(exc, "code", "SCENE_ANALYSIS_FAILED"), "error_message": str(exc)[:500]}) from exc
    if not plan:
        return {
            "episode_id": episode["id"],
            "project_id": episode["project_id"],
            "source_duration": float(episode.get("duration") or 0),
            "threshold": threshold,
            "persisted": False,
            "cache_status": "missing",
            "base_shot_count": None,
            "transform_unit_count": None,
            "base_shots": [],
            "transform_units": [],
            "candidate_cuts": [],
            "shots": [],
        }
    return plan


@router.put("/episodes/{episode_id}/xmax-thirty-second/scene-mappings")
def save_scene_mappings(episode_id: str, shot_mappings_json: str = Form(...)):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    source_path = _source_path(episode)
    plan = _load_persistent_scene_plan(episode, source_path, 0.35)
    if not plan:
        raise HTTPException(status_code=409, detail={"error_code": "SCENE_PLAN_REQUIRED"})
    mappings = _parse_shot_mappings(shot_mappings_json) or {}
    updated_plan = _apply_scene_mappings(episode, plan, mappings)
    persisted_plan = _persist_scene_mappings_and_episode_matches(episode, source_path, 0.35, updated_plan)
    sync_result = _sync_scene_mappings_to_active_task(episode, persisted_plan)
    return {**persisted_plan, **sync_result}


@router.get("/episodes/{episode_id}/xmax-thirty-second/tasks")
def list_xmax_thirty_second_tasks(episode_id: str):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM xmax_thirty_second_tasks WHERE episode_id=?", (episode_id,)).fetchall()
    tasks = [_task_summary(row_to_dict(row)) for row in rows]
    completed_results = sorted((item for item in tasks if item["is_completed_result"]), key=lambda item: item.get("completed_at") or item.get("updated_at") or item.get("created_at") or "", reverse=True)
    active_candidates = sorted((item for item in tasks if item["can_resume"] and item["status"] in ACTIVE_CANDIDATE_STATUSES), key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    historical_tasks = sorted((item for item in tasks if item not in active_candidates and item not in completed_results), key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return {"episode_id": episode_id, "active_candidates": active_candidates, "completed_results": completed_results, "historical_tasks": historical_tasks}


@router.get("/xmax-thirty-second/{task_id}")
def get_xmax_thirty_second_task(task_id: str, episode_id: str | None = None):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if episode_id and task["episode_id"] != episode_id:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_EPISODE_MISMATCH"})
    return _payload(task)


@router.get("/episodes/{episode_id}/xmax-thirty-second/latest")
def get_latest_thirty_second_task(episode_id: str):
    """Legacy compatibility only; UI task selection uses /tasks plus exact reads."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM xmax_thirty_second_tasks WHERE episode_id=? ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    if not row:
        return None
    task = row_to_dict(row)
    if task["status"] in {"running", "local_ready", "uploading"}:
        _mark_client_lost(task["id"])
        task = _task(task["id"])
        assert task is not None
    return _payload(task)


@router.post("/xmax-thirty-second/{task_id}/heartbeat")
def heartbeat_thirty_second_task(task_id: str):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] in {"completed", "cancelled"}:
        return _payload(task)
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute("UPDATE xmax_thirty_second_tasks SET heartbeat_at=?, updated_at=? WHERE id=?", (timestamp, timestamp, task_id))
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


async def _run_offline_attempt_background(attempt_id: str) -> None:
    worker_token = f"offline-worker-{uuid.uuid4().hex}"
    lease_seconds = int(os.getenv("XMAX_OFFLINE_JOB_TIMEOUT_SECONDS", "1800")) + 120
    try:
        claimed = xmax_browser_attempt_service.claim_offline_worker(
            attempt_id,
            worker_token,
            lease_seconds=lease_seconds,
        )
    except xmax_browser_attempt_service.XmaxBrowserAttemptError:
        return
    if not claimed:
        return

    result: xmax_offline_service.OfflineCompletedResult | None = None
    runtime: dict[str, Any] | None = None
    try:
        runtime = xmax_browser_attempt_service.get_offline_attempt_runtime(attempt_id)
        result = await asyncio.to_thread(xmax_offline_service.execute_attempt, attempt_id)
        headers = Headers({"content-type": result.content_type or "video/mp4"})
        with result.staged_path.open("rb") as source:
            upload = UploadFile(file=source, filename="provider-result.mp4", headers=headers)
            await upload_thirty_second_segment(
                result.task_id,
                result.unit_id,
                None,  # type: ignore[arg-type]
                output_file=upload,
                diagnostics_json=json.dumps(
                    {
                        "executionMode": "offline",
                        "offlineTaskUid": result.provider_task.get("uid"),
                        "processorId": result.provider_task.get("processor_id"),
                        "resolvedFps": result.provider_task.get("resolved_fps"),
                        "retryCount": result.provider_task.get("retry_count"),
                        "chargePoints": result.charge_points,
                    },
                    ensure_ascii=True,
                ),
                browser_attempt_id=attempt_id,
                local_persistence_probe_id=None,
                local_persistence_probe_force_failure=False,
            )
        with get_connection() as conn:
            xmax_browser_attempt_service.record_quality_status_in_transaction(
                conn,
                record_id=attempt_id,
                user_id=result.user_id,
                task_id=result.task_id,
                unit_id=result.unit_id,
                quality_status="approved",
                quality_reason="Automated Offline output preflight passed.",
                reviewer="xmax_offline_executor",
            )
            _promote_offline_versioned_attempt_projection(
                conn,
                task_id=result.task_id,
                segment_id=result.unit_id,
                attempt_id=attempt_id,
                timestamp=now_iso(),
            )
        refreshed_segments = _segments(result.task_id)
        if refreshed_segments and all(_segment_merge_ready(item) for item in refreshed_segments):
            with get_connection() as conn:
                conn.execute(
                    "UPDATE xmax_thirty_second_tasks SET status='merge_pending',error_code=NULL,error_message=NULL,updated_at=? WHERE id=? AND status!='cancelled'",
                    (now_iso(), result.task_id),
                )
        xmax_browser_attempt_service.update_offline_metadata(attempt_id, status="completed")
        xmax_browser_attempt_service.settle_offline_attempt(attempt_id, result.charge_points)
    except xmax_offline_service.XmaxOfflineError as error:
        runtime = runtime or xmax_browser_attempt_service.get_offline_attempt_runtime(attempt_id)
        xmax_browser_attempt_service.update_offline_metadata(
            attempt_id,
            status="submit_unknown" if error.submission_unknown else "error",
            failure_diagnostics={
                "error_code": error.code,
                "error_message": error.message,
                **error.diagnostics,
            },
        )
        if not error.submission_unknown:
            xmax_browser_attempt_service.mark_failed(
                attempt_id,
                runtime["user_id"],
                error.code,
                error.message,
                diagnostics={
                    "failure": {
                        "phase": error.diagnostics.get("phase") or "offline_execution",
                        "errorName": error.diagnostics.get("exception_type") or "XmaxOfflineError",
                        "errorCode": error.code,
                        "message": error.message,
                    },
                },
            )
            _record_segment_failure(
                runtime["task_id"],
                runtime["unit_id"],
                error.code,
                error.message,
                browser_attempt_id=attempt_id,
            )
            if error.charge_points is not None:
                xmax_browser_attempt_service.settle_offline_attempt(attempt_id, error.charge_points)
            elif not runtime.get("offline_task_uid"):
                xmax_browser_attempt_service.settle_offline_attempt(attempt_id, 0)
    except HTTPException as error:
        runtime = runtime or xmax_browser_attempt_service.get_offline_attempt_runtime(attempt_id)
        detail = error.detail if isinstance(error.detail, dict) else {}
        code = str(detail.get("error_code") or detail.get("code") or "XMAX_OFFLINE_PERSISTENCE_FAILED")
        message = str(detail.get("error_message") or detail.get("message") or "Offline output persistence failed.")
        xmax_browser_attempt_service.mark_failed(
            attempt_id,
            runtime["user_id"],
            code,
            message,
            persistence=True,
        )
        if result is not None:
            xmax_browser_attempt_service.settle_offline_attempt(attempt_id, result.charge_points)
    except Exception as error:
        runtime = runtime or xmax_browser_attempt_service.get_offline_attempt_runtime(attempt_id)
        xmax_browser_attempt_service.mark_failed(
            attempt_id,
            runtime["user_id"],
            "XMAX_OFFLINE_EXECUTION_FAILED",
            str(error)[:500],
        )
        _record_segment_failure(
            runtime["task_id"],
            runtime["unit_id"],
            "XMAX_OFFLINE_EXECUTION_FAILED",
            str(error)[:500],
            browser_attempt_id=attempt_id,
        )
        if result is not None:
            xmax_browser_attempt_service.settle_offline_attempt(attempt_id, result.charge_points)
        elif not runtime.get("offline_task_uid"):
            xmax_browser_attempt_service.settle_offline_attempt(attempt_id, 0)
    finally:
        if result is not None:
            result.staged_path.unlink(missing_ok=True)
        xmax_browser_attempt_service.release_offline_worker(attempt_id, worker_token)


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/browser-start")
def start_thirty_second_segment_in_browser(task_id: str, segment_id: str):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail={"error_code": f"TASK_{task['status'].upper()}"})
    _mark_segment_browser_running(task_id, segment_id)
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/failed")
def fail_thirty_second_segment(
    task_id: str,
    segment_id: str,
    error_code: str = Form("SHOT_FAILED"),
    error_message: str = Form("The Shot did not produce a usable output."),
    diagnostics_json: str | None = Form(default=None),
):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    try:
        diagnostics = json.loads(diagnostics_json or "{}")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnostics = {}
    browser_attempt_id = str(diagnostics.get("browserAttemptId") or "").strip() or None
    recovery = _record_segment_failure(
        task_id,
        segment_id,
        error_code[:120],
        error_message,
        diagnostics=diagnostics,
        browser_attempt_id=browser_attempt_id,
    )
    refreshed = _task(task_id)
    assert refreshed is not None
    return {**_payload(refreshed), "segment_recovery": recovery}


@router.post("/xmax-thirty-second/{task_id}/interrupt")
def interrupt_thirty_second_task(task_id: str):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    _mark_client_lost(task_id)
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


@router.post("/episodes/{episode_id}/xmax-thirty-second/tasks")
async def create_thirty_second_task(
    episode_id: str,
    background_tasks: BackgroundTasks,
    prompt: str = Form(...),
    reference_image: UploadFile | None = File(default=None),
    shot_mappings_json: str | None = Form(default=None),
    full_span: bool = Form(default=False),
    selected_character_id: str = Form(default=""),
):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    prompt_value = (prompt or "").strip()
    if not prompt_value:
        raise HTTPException(status_code=422, detail={"error_code": "PROMPT_REQUIRED"})
    full_span_requested = full_span is True
    selected_character_value = selected_character_id.strip() if isinstance(selected_character_id, str) else ""
    source_path = _source_path(episode)
    with get_connection() as conn:
        existing = _active_submission_task(conn, episode_id)
    if existing:
        expected_workflow = "full_span_direct" if full_span_requested else "advanced_scene_plan"
        if _task_workflow(existing) != expected_workflow:
            raise HTTPException(status_code=409, detail={"error_code": "OTHER_V2V_WORKFLOW_ACTIVE", "existing_task_id": existing["id"]})
        if full_span_requested:
            existing_segments = _segments(existing["id"])
            existing_character_id = str(existing_segments[0].get("selected_character_id") or "") if existing_segments else ""
            if str(existing.get("prompt") or "").strip() != prompt_value or existing_character_id != selected_character_value:
                raise HTTPException(status_code=409, detail={"error_code": "DIRECT_TASK_INPUT_LOCKED", "existing_task_id": existing["id"]})
        _kickoff_proxy_preparation(background_tasks, existing["id"])
        return {**_payload(existing), "reused": True, "existing_task_id": existing["id"]}

    if full_span_requested:
        duration = validate_episode_duration(episode.get("duration"))
        if duration > 60.0:
            raise HTTPException(status_code=422, detail={"error_code": "FULL_SPAN_DURATION_EXCEEDED", "source_duration": duration, "max_seconds": 60})
        character_id = selected_character_value
        if not character_id:
            raise HTTPException(status_code=422, detail={"error_code": "REFERENCE_CHARACTER_REQUIRED"})
        resolved_reference = resolve_character_reference(character_id, episode_id, episode["project_id"])
        if not resolved_reference.get("mapping_valid"):
            raise HTTPException(status_code=422, detail={
                "error_code": resolved_reference.get("error_code") or "CHARACTER_REFERENCE_INVALID",
                "error_message": resolved_reference.get("error_message"),
                "character_id": character_id,
            })
        selected_reference = Path(str(resolved_reference["resolved_local_path"]))
        task_id = f"xmax-30s-{uuid.uuid4().hex[:16]}"
        attempt_id = f"xmax-30s-attempt-{uuid.uuid4().hex[:16]}"
        segment_id = f"{task_id}-full-span"
        created_at = now_iso()
        task_dir = _task_dir_path(episode["project_id"], episode_id, attempt_id)
        segment_path = task_dir / "segments" / "full-span-proxy.mp4"
        metadata = {
            "workflow": "full_span_direct",
            "source_duration": duration,
            "unit": {"id": segment_id, "source_start": 0.0, "source_end": duration},
        }
        diagnostics = {
            "workflow": "full_span_direct",
            "scene_plan_executed": False,
            "proxy_preparation": {"status": "pending", "updated_at": created_at},
        }
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            concurrent = _active_submission_task(conn, episode_id)
            if concurrent:
                raise HTTPException(status_code=409, detail={"error_code": "V2V_TASK_ALREADY_ACTIVE", "existing_task_id": concurrent["id"]})
            conn.execute(
                "INSERT INTO xmax_thirty_second_tasks (id, attempt_id, project_id, episode_id, status, prompt, reference_path, source_path, source_duration, estimated_credits, created_at, updated_at, scene_threshold, scene_analysis_status, scene_analyzed_at, scene_plan_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, attempt_id, episode["project_id"], episode_id, "pending", prompt_value, str(selected_reference), str(source_path), duration, duration, created_at, created_at, 0.35, "not_required", None, json.dumps(metadata, ensure_ascii=True)),
            )
            conn.execute(
                "INSERT INTO xmax_thirty_second_segments (id, task_id, sequence, start_time, end_time, duration, source_path, source_url, status, output_path, output_url, output_duration, file_size, diagnostics_json, created_at, updated_at, shot_type, selected_character_id, mapping_status, reference_path, reference_url, preserve_original, scene_score, cut_method, first_frame_url, middle_frame_url, last_frame_url, content_duration, proxy_duration, pre_roll_duration, post_roll_duration, timeline_status, timeline_error_ms, transform_unit_id, parent_shot_id, risk_score, risk_level, reference_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (segment_id, task_id, 1, 0.0, duration, duration, str(segment_path), _public_source(segment_path), "pending", None, None, None, None, json.dumps(diagnostics, ensure_ascii=True), created_at, created_at, "single_character", character_id, "full_span_direct", str(selected_reference), resolved_reference.get("reference_preview_url"), 0, None, "full_span_direct", None, None, None, duration, None, 0.0, 0.0, "valid", 0.0, segment_id, "full-span-source", 0.0, "low", resolved_reference.get("reference_type")),
            )
            conn.execute(
                "UPDATE xmax_thirty_second_segments SET execution_status='pending', quality_status='review_required' WHERE id=? AND task_id=?",
                (segment_id, task_id),
            )
        task = _task(task_id)
        assert task is not None
        _kickoff_proxy_preparation(background_tasks, task_id)
        return {**_payload(task), "reused": False, "existing_task_id": None}

    mappings = _parse_shot_mappings(shot_mappings_json)
    if mappings is None:
        raise HTTPException(status_code=422, detail={"error_code": "SHOT_MAPPING_REQUIRED"})
    plan = _load_persistent_scene_plan(episode, source_path, 0.35)
    if not plan:
        raise HTTPException(status_code=409, detail={"error_code": "SCENE_PLAN_REQUIRED"})
    ranges = list(plan.get("transform_units") or [])
    if not ranges:
        raise HTTPException(status_code=422, detail={"error_code": "SCENE_PLAN_EMPTY"})
    duration = validate_episode_duration(plan.get("source_duration") or episode.get("duration"))
    task_id = f"xmax-30s-{uuid.uuid4().hex[:16]}"
    attempt_id = f"xmax-30s-attempt-{uuid.uuid4().hex[:16]}"
    created_at = now_iso()
    task_dir = _task_dir_path(episode["project_id"], episode_id, attempt_id)
    segment_dir = task_dir / "segments"
    plan_metadata = {"base_shots": plan.get("shots") or [], "shots": ranges}

    prepared: list[dict[str, Any]] = []
    first_reference: Path | None = None
    first_reference_value: str | Path | None = None
    for fallback_sequence, item in enumerate(ranges, start=1):
        sequence = int(item.get("sequence") or fallback_sequence)
        start_time = float(item["source_start"])
        end_time = float(item["source_end"])
        segment_id = f"{task_id}-segment-{sequence:03d}"
        content_duration = round(end_time - start_time, 3)
        mapping_sequence, requested_mapping = _mapping_for_transform_unit(mappings, item, sequence)
        requested_type = str(requested_mapping.get("shot_type") or "unknown")
        requested_character, requested_confirmed, requested_preserve = _mapping_values(requested_mapping)
        shot_type = str(item.get("shot_type") or "unknown")
        character_id = str(item.get("selected_character_id") or "") or None
        mapping_status = str(item.get("mapping_status") or "manual_required")
        preserve_original = bool(item.get("preserve_original"))
        saved_confirmed = mapping_status == "manual_confirmed"
        if (
            requested_type != shot_type
            or requested_character != character_id
            or requested_confirmed != saved_confirmed
            or requested_preserve != preserve_original
        ):
            raise HTTPException(status_code=409, detail={"error_code": "SCENE_MAPPING_STALE", "sequence": sequence, "parent_sequence": mapping_sequence})
        reference_url = None
        selected_reference = None
        reference_type = None
        if shot_type == "single_character":
            if not character_id or not saved_confirmed:
                raise HTTPException(status_code=422, detail={"error_code": "CHARACTER_MAPPING_REQUIRED", "sequence": sequence, "parent_sequence": mapping_sequence})
            resolved_reference = resolve_character_reference(character_id, episode_id, episode["project_id"])
            if not resolved_reference.get("mapping_valid"):
                raise HTTPException(status_code=422, detail={"error_code": resolved_reference.get("error_code") or "CHARACTER_REFERENCE_INVALID", "error_message": resolved_reference.get("error_message"), "sequence": sequence, "parent_sequence": mapping_sequence, "character_id": character_id, "reference_source": resolved_reference.get("reference_source")})
            selected_reference = Path(resolved_reference["resolved_local_path"]) if resolved_reference.get("resolved_local_path") else None
            first_reference = first_reference or selected_reference
            reference_url = resolved_reference.get("reference_preview_url")
            reference_type = resolved_reference.get("reference_type")
            first_reference_value = first_reference_value or (selected_reference or reference_url)
            pre_roll = min(1.0, start_time)
            post_roll = min(1.0, max(0.0, duration - end_time))
            segment_path = segment_dir / f"shot-{sequence:03d}-proxy.mp4"
        elif shot_type in {"multi_character", "no_character", "transition"} and preserve_original:
            pre_roll = 0.0
            post_roll = 0.0
            segment_path = segment_dir / f"shot-{sequence:03d}-source.mp4"
            mapping_status = "preserve_original"
        else:
            raise HTTPException(status_code=422, detail={"error_code": "MULTI_CHARACTER_REQUIRES_PRESERVE", "sequence": sequence})
        risk = calculate_transform_risk(item, shot_type)
        prepared.append({"id": segment_id, "transform_unit_id": item.get("transform_unit_id") or segment_id, "parent_shot_id": item.get("parent_shot_id") or f"base-shot-{sequence:03d}", "sequence": sequence, "start": start_time, "end": end_time, "duration": content_duration, "path": segment_path, "url": _public_source(segment_path), "shot_type": shot_type, "character_id": character_id, "mapping_status": mapping_status, "reference_path": str(selected_reference) if selected_reference else (reference_url if shot_type == "single_character" else None), "reference_url": reference_url, "reference_type": reference_type, "preserve": int(preserve_original), "scene_score": item.get("scene_score"), "cut_method": item.get("cut_method"), "risk_score": risk["risk_score"], "risk_level": risk["risk_level"], "pre": pre_roll, "post": post_roll, "first_frame_url": item.get("first_frame_url"), "middle_frame_url": item.get("middle_frame_url"), "last_frame_url": item.get("last_frame_url")})

    reference_path = first_reference_value or first_reference or source_path
    estimated_credits = sum(item["duration"] for item in prepared if item["shot_type"] == "single_character")
    created_new = False
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _active_submission_task(conn, episode_id)
        if not existing:
            conn.execute(
                "INSERT INTO xmax_thirty_second_tasks (id, attempt_id, project_id, episode_id, status, prompt, reference_path, source_path, source_duration, estimated_credits, created_at, updated_at, scene_threshold, scene_analysis_status, scene_analyzed_at, scene_plan_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                (task_id, attempt_id, episode["project_id"], episode_id, "pending", prompt_value, str(reference_path), str(source_path), duration, estimated_credits, created_at, created_at, 0.35, created_at, json.dumps(plan_metadata, ensure_ascii=True)),
            )
            for item in prepared:
                conn.execute(
                    "INSERT INTO xmax_thirty_second_segments (id, task_id, sequence, start_time, end_time, duration, source_path, source_url, status, output_path, output_url, output_duration, file_size, diagnostics_json, created_at, updated_at, shot_type, selected_character_id, mapping_status, reference_path, reference_url, preserve_original, scene_score, cut_method, first_frame_url, middle_frame_url, last_frame_url, content_duration, proxy_duration, pre_roll_duration, post_roll_duration, timeline_status, timeline_error_ms, transform_unit_id, parent_shot_id, risk_score, risk_level, reference_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["id"], task_id, item["sequence"], item["start"], item["end"], item["duration"], str(item["path"]), item["url"], "pending", None, None, None, None, json.dumps({"proxy_preparation": {"status": "pending", "updated_at": created_at}}, ensure_ascii=True), created_at, created_at, item["shot_type"], item["character_id"], item["mapping_status"], item["reference_path"], item["reference_url"], item["preserve"], item["scene_score"], item["cut_method"], item["first_frame_url"], item["middle_frame_url"], item["last_frame_url"], item["duration"], None, item["pre"], item["post"], "valid", 0.0, item["transform_unit_id"], item["parent_shot_id"], item["risk_score"], item["risk_level"], item["reference_type"]),
                )
                conn.execute(
                    "UPDATE xmax_thirty_second_segments SET execution_status=?, quality_status=? WHERE id=? AND task_id=?",
                    ("pending", "not_applicable" if item["preserve"] else "review_required", item["id"], task_id),
                )
            created_new = True
        else:
            task_id = existing["id"]
    task = _task(task_id)
    assert task is not None
    _kickoff_proxy_preparation(background_tasks, task_id)
    return {
        **_payload(task),
        "reused": not created_new,
        "existing_task_id": None if created_new else task_id,
    }


@router.post("/episodes/{episode_id}/xmax-thirty-second/tasks/{task_id}/prepare")
def resume_thirty_second_task_preparation(
    episode_id: str,
    task_id: str,
    background_tasks: BackgroundTasks,
):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    task = _task(task_id)
    if not task or task.get("episode_id") != episode_id:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    recoverable_proxy_failure = (
        task.get("status") == "failed"
        and task.get("error_code") == "PROXY_PREPARATION_FAILED"
    )
    if task.get("status") not in ACTIVE_SUBMISSION_STATUSES and not recoverable_proxy_failure:
        return _payload(task)
    _kickoff_proxy_preparation(background_tasks, task_id)
    return _payload(task)


@router.post("/episodes/{episode_id}/xmax-thirty-second/mappings/batch")
def batch_bind_thirty_second_mappings(
    episode_id: str,
    sequences: str = Form(...),
    selected_character_id: str = Form(default=""),
    reference_face_confirmed: bool = Form(default=True),
    preserve_source: bool = Form(default=False),
):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    try:
        parsed = json.loads(sequences)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("sequences must be a non-empty JSON array")
        normalized_sequences = sorted({int(item) for item in parsed})
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "SHOT_SEQUENCE_INVALID", "error_message": str(exc)}) from exc
    plan = _persistent_scene_plan(episode, _source_path(episode), 0.35)
    available = {
        int(item.get("sequence") or sequence)
        for sequence, item in enumerate(plan.get("transform_units") or plan["shots"], start=1)
    }
    invalid = [sequence for sequence in normalized_sequences if sequence not in available]
    if invalid:
        raise HTTPException(status_code=422, detail={"error_code": "SHOT_SEQUENCE_INVALID", "invalid_sequences": invalid})
    if preserve_source:
        mappings = [{"sequence": sequence, "shot_type": "no_character", "selected_character_id": None, "reference_face_confirmed": False, "preserve_source": True} for sequence in normalized_sequences]
    else:
        if not selected_character_id:
            raise HTTPException(status_code=422, detail={"error_code": "CHARACTER_MAPPING_REQUIRED"})
        if not reference_face_confirmed:
            raise HTTPException(status_code=422, detail={"error_code": "SINGLE_PERSON_REFERENCE_CONFIRMATION_REQUIRED", "character_id": selected_character_id})
        resolved = resolve_character_reference(selected_character_id, episode_id, episode["project_id"])
        if not resolved.get("mapping_valid"):
            raise HTTPException(status_code=422, detail={"error_code": resolved.get("error_code") or "CHARACTER_REFERENCE_INVALID", "error_message": resolved.get("error_message"), "character_id": selected_character_id})
        mappings = [{"sequence": sequence, "shot_type": "single_character", "selected_character_id": selected_character_id, "reference_face_confirmed": True, "preserve_source": False} for sequence in normalized_sequences]
    return {"episode_id": episode_id, "selected_character_id": selected_character_id or None, "reference_face_confirmed": bool(reference_face_confirmed and not preserve_source), "preserve_source": bool(preserve_source), "sequences": normalized_sequences, "mappings": mappings, "provider_call_started": False}


@router.post("/episodes/{episode_id}/xmax-thirty-second/preflight")
def preflight_thirty_second_task(episode_id: str, shot_mappings_json: str = Form(...)):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    mappings = _parse_shot_mappings(shot_mappings_json) or {}
    try:
        plan = _persistent_scene_plan(episode, _source_path(episode), 0.35)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error_code": "SCENE_ANALYSIS_FAILED", "error_message": str(exc)[:500]}) from exc
    results: list[dict[str, Any]] = []
    review_units = plan.get("transform_units") or plan["shots"]
    for fallback_sequence, item in enumerate(review_units, start=1):
        sequence = int(item.get("sequence") or fallback_sequence)
        _, mapping = _mapping_for_transform_unit(mappings, item, sequence)
        shot_type = str(mapping.get("shot_type") or "unknown")
        character_id, reference_confirmed, preserve_source = _mapping_values(mapping)
        result: dict[str, Any] = {
            "sequence": sequence,
            "transform_unit_id": item.get("transform_unit_id"),
            "parent_shot_id": item.get("parent_shot_id"),
            "shot_type": shot_type,
            "selected_character_id": character_id,
            "character_name": None,
            "reference_found": False,
            "reference_readable": False,
            "reference_preview_url": None,
            "mapping_valid": False,
            "error_code": None,
            "error_message": None,
        }
        unit_risk = calculate_transform_risk(item, shot_type)
        risk_score = float(unit_risk["risk_score"])
        result.update(
            risk_score=risk_score,
            risk_level="high" if risk_score >= 60 else "medium" if risk_score >= 35 else "low",
            risk_factors=unit_risk["risk_factors"],
        )
        if shot_type == "single_character":
            if not character_id:
                result.update(error_code="CHARACTER_MAPPING_REQUIRED", error_message="Choose a confirmed character for this Shot.")
            elif not reference_confirmed:
                result.update(error_code="SINGLE_PERSON_REFERENCE_CONFIRMATION_REQUIRED", error_message="Confirm that the selected reference contains one person.")
            else:
                resolved = resolve_character_reference(character_id, episode_id, episode["project_id"])
                result.update(
                    character_name=resolved.get("character_name"),
                    reference_found=bool(resolved.get("resolved_local_path") or resolved.get("provider_url")),
                    reference_readable=bool(resolved.get("mapping_valid")),
                    reference_preview_url=resolved.get("reference_preview_url"),
                    mapping_valid=bool(resolved.get("mapping_valid")),
                    error_code=resolved.get("error_code"),
                    error_message=resolved.get("error_message"),
                )
        elif shot_type == "multi_character":
            if preserve_source:
                result["mapping_valid"] = True
            else:
                result.update(error_code="MULTI_CHARACTER_REQUIRES_PRESERVE", error_message="Multi-character Shots must preserve the original frame in this MVP.")
        elif shot_type in {"no_character", "transition"}:
            result["mapping_valid"] = True
        else:
            result.update(error_code="CHARACTER_MAPPING_REQUIRED", error_message="Choose how this Shot should be processed.")
        results.append(result)
    transform_summary = summarize_transform_preflight(plan.get("transform_units") or [], mappings)
    return {
        "episode_id": episode_id,
        "shot_count": len(results),
        **transform_summary,
        "all_valid": all(item["mapping_valid"] for item in results),
        "shots": results,
    }


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/output")
async def upload_thirty_second_segment(
    task_id: str,
    segment_id: str,
    request: Request,
    output_file: UploadFile = File(...),
    diagnostics_json: str | None = Form(default=None),
    browser_attempt_id: str | None = Form(default=None),
    local_persistence_probe_id: str | None = Form(default=None),
    local_persistence_probe_force_failure: bool = Form(default=False),
):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] == "cancelled":
        raise HTTPException(status_code=409, detail={"error_code": f"TASK_{task['status'].upper()}"})
    attempt_execution_mode = "realtime"
    with get_connection() as conn:
        segment_row = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?", (segment_id, task_id)).fetchone()
        versioned_evaluation = bool(
            browser_attempt_id
            and xmax_browser_attempt_service.preserves_current_unit_version_in_transaction(
                conn,
                record_id=browser_attempt_id,
                task_id=task_id,
                unit_id=segment_id,
            )
        )
        if browser_attempt_id:
            attempt_execution_mode = xmax_browser_attempt_service.execution_mode_in_transaction(
                conn,
                record_id=browser_attempt_id,
                task_id=task_id,
                unit_id=segment_id,
            )
    if not segment_row:
        raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
    segment = row_to_dict(segment_row)
    if local_persistence_probe_id:
        return await _run_local_persistence_probe(
            request=request,
            task=task,
            segment=segment,
            output_file=output_file,
            probe_id=local_persistence_probe_id,
            force_normalization_failure=local_persistence_probe_force_failure,
        )
    if not versioned_evaluation and segment["status"] == "completed" and segment.get("output_path") and Path(segment["output_path"]).is_file():
        return _payload(task)
    filename = output_file.filename or "output.webm"
    offline_mp4 = (
        attempt_execution_mode == "offline"
        and Path(filename).suffix.lower() == ".mp4"
        and (output_file.content_type or "").lower() in ALLOWED_OFFLINE_MP4_TYPES
    )
    if not offline_mp4 and (Path(filename).suffix.lower() != ".webm" or not _allowed_webm(output_file.content_type or "")):
        recovery = {} if versioned_evaluation else _record_segment_failure(task_id, segment_id, "OUTPUT_FORMAT_INVALID", "Only a WebM segment is accepted.")
        raise HTTPException(status_code=422, detail={"error_code": "OUTPUT_FORMAT_INVALID", **recovery})
    task_dir = _task_dir(task["project_id"], task["episode_id"], task["attempt_id"])
    output_dir = task_dir / "outputs"
    if browser_attempt_id:
        output_dir = output_dir / "attempts" / _safe_component(browser_attempt_id, "browser_attempt_id")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_suffix = ".mp4" if offline_mp4 else ".webm"
    temp_path = output_dir / f".uploading-{uuid.uuid4().hex}{raw_suffix}"
    retry_version = int(segment.get("retry_version") or 0)
    raw_path = output_dir / ("provider.raw.mp4" if offline_mp4 else "remote.raw.webm") if browser_attempt_id else None
    output_name = "remote.normalized.webm" if browser_attempt_id else f"shot-{int(segment['sequence']):03d}" + (f"-v{retry_version}" if retry_version > 0 else "") + ".webm"
    final_path = output_dir / output_name
    remux_path = output_dir / f".remuxing-{uuid.uuid4().hex}.webm"
    normalized_path = output_dir / f".normalizing-{uuid.uuid4().hex}.webm"
    timeline_normalization: dict[str, Any] | None = None
    processing_path = temp_path
    raw_commit: dict[str, Any] | None = None
    try:
        if not versioned_evaluation:
            _set_segment_state(segment_id, task_id, "uploading")
            with get_connection() as conn:
                conn.execute("UPDATE xmax_thirty_second_tasks SET status='uploading', heartbeat_at=?, updated_at=? WHERE id=? AND status!='completed'", (now_iso(), now_iso(), task_id))
        total = 0
        with temp_path.open("wb") as target:
            while True:
                chunk = await output_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    raise SegmentOutputValidationError("OUTPUT_TOO_LARGE", "The Xmax output exceeds the configured file-size limit.")
                target.write(chunk)
        if total <= 0:
            raise SegmentOutputValidationError("OUTPUT_EMPTY", "The Xmax output file is empty.")
        if browser_attempt_id and raw_path is not None:
            raw_commit = _commit_immutable_media(
                temp_path,
                raw_path,
                conflict_code="OUTPUT_RAW_IMMUTABLE_CONFLICT",
            )
            processing_path = raw_path
            with get_connection() as conn:
                xmax_browser_attempt_service.record_raw_output_persisted_in_transaction(
                    conn,
                    record_id=browser_attempt_id,
                    task_id=task_id,
                    unit_id=segment_id,
                    raw_path=raw_path,
                    raw_url=_public_output(raw_path),
                    output_mime_type=output_file.content_type or "video/webm",
                )
        try:
            output_metadata = validate_segment_output_file(processing_path, segment, output_dir / "quality-preflight")
        except SegmentOutputValidationError as exc:
            audit = preserve_rejected_output_audit(
                processing_path,
                task=task,
                segment=segment,
                validation_error=exc,
            )
            if exc.code == "OUTPUT_DURATION_TOO_SHORT" and audit.get("classification") == "A_metadata_duration_error":
                remux = remux_rejected_output(Path(str(audit["raw_path"])), remux_path)
                audit["remux"] = remux
                Path(str(audit["raw_path"])).with_name("audit-report.json").write_text(
                    json.dumps(audit, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if remux.get("succeeded"):
                    try:
                        output_metadata = validate_segment_output_file(
                            remux_path,
                            segment,
                            output_dir / "quality-preflight",
                        )
                    except SegmentOutputValidationError as remux_error:
                        audit["remux_validation_error"] = {
                            "code": remux_error.code,
                            "message": str(remux_error),
                            "details": remux_error.details,
                        }
                        Path(str(audit["raw_path"])).with_name("audit-report.json").write_text(
                            json.dumps(audit, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    else:
                        processing_path = remux_path
                        exc = None
            if exc is not None:
                audit_details = {
                    "outputPreflight": exc.details,
                    "uploadedBytes": total,
                    "rejectedAudit": {
                        "classification": audit.get("classification"),
                        "rawPath": audit.get("raw_path"),
                        "reportPath": str(Path(str(audit["raw_path"])).with_name("audit-report.json")),
                        "formatDuration": audit.get("format_duration"),
                        "streamDuration": audit.get("stream_duration"),
                        "packetTimelineSpan": audit.get("packet_timeline_span"),
                        "nbReadFrames": audit.get("nb_read_frames"),
                        "decodeSucceeded": audit.get("decode_succeeded"),
                    },
                }
                recovery = {} if versioned_evaluation else _record_segment_failure(
                    task_id,
                    segment_id,
                    exc.code,
                    str(exc),
                    quality_status=exc.quality_status,
                    diagnostics=audit_details,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": exc.code,
                        "error_message": str(exc)[:500],
                        "failure_stage": "raw_validation",
                        "raw_output_persisted": bool(browser_attempt_id and raw_path and raw_path.is_file()),
                        "raw_output_sha256": raw_commit.get("sha256") if raw_commit else None,
                        **exc.details,
                        **recovery,
                    },
                ) from exc
        target_duration = float(segment.get("content_duration") or segment.get("duration") or 0)
        packet_duration = float(output_metadata.get("duration") or 0)
        if (
            offline_mp4
            or
            output_metadata.get("duration_source") == "packet_pts"
            and target_duration > 0
            and abs(packet_duration - target_duration) > max(0.05, target_duration * 0.05)
        ):
            try:
                source_metadata = media_service.probe_media_metadata(Path(str(segment["source_path"])))
                source_fps = float(source_metadata.get("fps") or 30.0)
            except Exception:
                source_fps = 30.0
            timeline_normalization = normalize_segment_packet_timeline(
                processing_path,
                normalized_path,
                packet_duration=packet_duration,
                target_duration=target_duration,
                fps=source_fps,
            )
            normalized_metadata = validate_segment_output_file(
                normalized_path,
                segment,
                output_dir / "quality-preflight",
            )
            normalized_duration = float(normalized_metadata.get("duration") or 0)
            if abs(normalized_duration - target_duration) > max(0.05, target_duration * 0.05):
                raise SegmentOutputValidationError(
                    "OUTPUT_TIMELINE_NORMALIZATION_INVALID",
                    (
                        f"Normalized duration {normalized_duration:.3f}s does not match "
                        f"the expected {target_duration:.3f}s."
                    ),
                    quality_status="rejected_timeline",
                    details={
                        "source_packet_duration": packet_duration,
                        "normalized_duration": normalized_duration,
                        "target_duration": target_duration,
                        "normalization": timeline_normalization,
                        "normalized_metadata": {
                            "container_duration": normalized_metadata.get("container_duration"),
                            "duration_source": normalized_metadata.get("duration_source"),
                            "packet_timeline": normalized_metadata.get("packet_timeline"),
                            "fps": normalized_metadata.get("fps"),
                        },
                    },
                )
            timeline_normalization["normalized_duration"] = normalized_duration
            timeline_normalization["normalized_frame_count"] = (
                (normalized_metadata.get("packet_timeline") or {}).get("nb_read_frames")
            )
            processing_path = normalized_path
            output_metadata = normalized_metadata
        output_duration = float(output_metadata.get("duration") or 0)
        if browser_attempt_id:
            if processing_path != normalized_path:
                shutil.copy2(processing_path, normalized_path)
                copied_metadata = validate_segment_output_file(
                    normalized_path,
                    segment,
                    output_dir / "quality-preflight",
                )
                output_duration = float(copied_metadata.get("duration") or 0)
                output_metadata = copied_metadata
            _commit_immutable_media(
                normalized_path,
                final_path,
                conflict_code="OUTPUT_NORMALIZED_IMMUTABLE_CONFLICT",
            )
        else:
            processing_path.replace(final_path)
        temp_path.unlink(missing_ok=True)
        remux_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)
        diagnostics = _segment_diagnostics(segment)
        if diagnostics_json:
            try:
                submitted_diagnostics = json.loads(diagnostics_json)
                if isinstance(submitted_diagnostics, dict):
                    diagnostics.update(submitted_diagnostics)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        diagnostics["preserveSourceByUser"] = False
        diagnostics["fallbackSourceSelected"] = False
        diagnostics["awaitingUserAction"] = False
        diagnostics["preserveSourceSelectedAt"] = None
        if timeline_normalization:
            diagnostics["timelineNormalization"] = timeline_normalization
        diagnostics["outputPreflight"] = {
            "passed": True,
            "fileSize": output_metadata["file_size"],
            "duration": output_duration,
            "containerDuration": output_metadata.get("container_duration"),
            "durationSource": output_metadata.get("duration_source"),
            "packetTimeline": output_metadata.get("packet_timeline"),
            "codec": output_metadata["video_codec"],
            "width": output_metadata["width"],
            "height": output_metadata["height"],
            "fps": output_metadata["fps"],
            "blackRatio": output_metadata["black_ratio"],
            "firstFrameDecoded": True,
            "lastFrameDecoded": True,
        }
        with get_connection() as conn:
            timestamp = now_iso()
            if not versioned_evaluation:
                conn.execute(
                    "UPDATE xmax_thirty_second_segments SET status='completed', execution_status='completed', quality_status=?, use_source_for_merge=0, output_path=?, output_url=?, output_duration=?, raw_output_duration=?, trimmed_output_duration=?, file_size=?, error_code=NULL, error_message=NULL, diagnostics_json=?, heartbeat_at=?, updated_at=?, input_loop=?, input_playback_count=?, input_playback_started_at=?, input_ended_at=?, remote_first_frame_at=?, remote_first_media_time=?, remote_last_frame_at=?, remote_last_media_time=?, recorder_started_at=?, recorder_stopped_at=?, reference_face_count=? WHERE id=? AND task_id=?",
                    (_default_quality_status(segment), str(final_path), _public_output(final_path), output_duration, output_duration, None, final_path.stat().st_size, json.dumps(diagnostics, ensure_ascii=True)[:20000], timestamp, timestamp, int(bool(diagnostics.get("inputLoop"))) if "inputLoop" in diagnostics else None, diagnostics.get("inputPlaybackCount"), diagnostics.get("inputPlaybackStartedAt"), diagnostics.get("inputEndedAt"), diagnostics.get("firstRemoteFrameAt"), diagnostics.get("remoteFirstMediaTime"), diagnostics.get("lastRemoteFrameAt"), diagnostics.get("remoteLastMediaTime"), diagnostics.get("recorderStartedAt"), diagnostics.get("recorderStoppedAt"), diagnostics.get("referenceFaceCount"), segment_id, task_id),
                )
                conn.execute("UPDATE xmax_thirty_second_tasks SET status='running', error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=? AND status!='cancelled'", (timestamp, timestamp, task_id))
            if browser_attempt_id:
                xmax_browser_attempt_service.record_output_saved_in_transaction(
                    conn,
                    record_id=browser_attempt_id,
                    task_id=task_id,
                    unit_id=segment_id,
                    output_path=final_path,
                    output_url=_public_output(final_path),
                    output_duration=output_duration,
                    output_mime_type="video/webm" if offline_mp4 else (output_file.content_type or "video/webm"),
                )
        refreshed_segments = _segments(task_id)
        if not versioned_evaluation and refreshed_segments and all(_segment_merge_ready(item) for item in refreshed_segments):
            with get_connection() as conn:
                conn.execute("UPDATE xmax_thirty_second_tasks SET status='merge_pending', updated_at=? WHERE id=? AND status!='cancelled'", (now_iso(), task_id))
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        remux_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)
        raise
    except SegmentOutputValidationError as exc:
        diagnostics_url: str | None = None
        if (
            browser_attempt_id
            and raw_path is not None
            and raw_path.is_file()
            and exc.code.startswith("OUTPUT_TIMELINE_NORMALIZATION")
        ):
            diagnostics_payload = {
                "attempt_id": browser_attempt_id,
                "task_id": task_id,
                "unit_id": segment_id,
                "failure_stage": "normalization",
                "error_code": exc.code,
                "error_message": str(exc),
                "error_details": exc.details,
                "raw_output": {
                    "path": str(raw_path),
                    "sha256": _sha256_path(raw_path),
                    "size": raw_path.stat().st_size,
                },
                "expected_duration": float(segment.get("content_duration") or segment.get("duration") or 0),
                "created_at": now_iso(),
            }
            diagnostics_path = _write_normalization_diagnostics(output_dir, diagnostics_payload)
            diagnostics_url = _public_output(diagnostics_path)
            with get_connection() as conn:
                xmax_browser_attempt_service.record_normalization_failed_in_transaction(
                    conn,
                    record_id=browser_attempt_id,
                    task_id=task_id,
                    unit_id=segment_id,
                    error_code=exc.code,
                    error_message=str(exc),
                    diagnostics_url=diagnostics_url,
                    diagnostics=diagnostics_payload,
                )
        temp_path.unlink(missing_ok=True)
        remux_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)
        recovery = {} if versioned_evaluation else _record_segment_failure(task_id, segment_id, exc.code, str(exc), quality_status=exc.quality_status, diagnostics=exc.details)
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": exc.code,
                "error_message": str(exc)[:500],
                "failure_stage": "normalization" if exc.code.startswith("OUTPUT_TIMELINE_NORMALIZATION") else "raw_persistence",
                "raw_output_persisted": bool(browser_attempt_id and raw_path and raw_path.is_file()),
                "raw_output_sha256": _sha256_path(raw_path) if browser_attempt_id and raw_path and raw_path.is_file() else None,
                "normalization_diagnostics_url": diagnostics_url,
                **exc.details,
                **recovery,
            },
        ) from exc
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        remux_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)
        if not versioned_evaluation:
            _set_segment_state(segment_id, task_id, "failed", "SEGMENT_OUTPUT_SAVE_FAILED", str(exc))
            _set_task_failed(task_id, "SEGMENT_OUTPUT_SAVE_FAILED", str(exc))
        raise HTTPException(status_code=500, detail={"error_code": "SEGMENT_OUTPUT_SAVE_FAILED", "error_message": str(exc)[:500]}) from exc
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/quality")
def review_thirty_second_segment(
    task_id: str,
    segment_id: str,
    request: Request,
    quality_status: str = Form(...),
    quality_reason: str = Form(default=""),
    reviewer: str = Form(default="manual_review"),
    browser_attempt_id: str = Form(default=""),
):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?", (segment_id, task_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
    segment = row_to_dict(row)
    requested = _quality_status(quality_status)
    if segment.get("preserve_original") or segment.get("shot_type") in {"no_character", "transition", "multi_character"}:
        if requested != "not_applicable":
            raise HTTPException(status_code=409, detail={"error_code": "QUALITY_NOT_APPLICABLE", "message": "Preserved source Shots cannot be quality-approved as a generated replacement."})
        requested = "not_applicable"
    elif segment.get("status") != "completed":
        raise HTTPException(status_code=409, detail={"error_code": "QUALITY_REVIEW_REQUIRES_COMPLETED", "message": "A generated Shot must be saved before manual quality review."})
    reason = (quality_reason or "")[:1000] or None
    actor = (reviewer or "manual_review")[:120]
    with get_connection() as conn:
        if browser_attempt_id:
            user = require_request_user(request)
            try:
                xmax_browser_attempt_service.record_quality_status_in_transaction(
                    conn,
                    record_id=browser_attempt_id,
                    user_id=user["id"],
                    task_id=task_id,
                    unit_id=segment_id,
                    quality_status=requested,
                    quality_reason=quality_reason,
                    reviewer=reviewer,
                )
            except xmax_browser_attempt_service.XmaxBrowserAttemptError as error:
                raise HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message}) from error
        elif segment["quality_status"] != requested or (segment.get("quality_reason") or None) != reason or (segment.get("quality_reviewed_by") or actor) != actor:
            reviewed_at = now_iso()
            conn.execute(
                "UPDATE xmax_thirty_second_segments SET quality_status=?, quality_reason=?, quality_reviewed_at=?, quality_reviewed_by=?, updated_at=? WHERE id=? AND task_id=?",
                (requested, reason, reviewed_at, actor, reviewed_at, segment_id, task_id),
            )
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/retry")
def prepare_thirty_second_segment_retry(
    task_id: str,
    segment_id: str,
    automatic: bool = Form(default=False),
    explicit_redraw: bool = Form(default=False),
):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?", (segment_id, task_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
    segment = row_to_dict(row)
    if segment.get("preserve_original") or segment.get("shot_type") in {"no_character", "transition", "multi_character"}:
        raise HTTPException(status_code=409, detail={"error_code": "PRESERVE_SOURCE_NOT_RETRYABLE", "message": "Preserved source Shots do not call a Provider."})
    with get_connection() as conn:
        previous_success = (
            xmax_browser_attempt_service.latest_playable_attempt_in_transaction(
                conn,
                task_id=task_id,
                unit_id=segment_id,
            )
            if explicit_redraw
            else None
        )
    source_preserved_redraw = bool(explicit_redraw and _preserve_source_by_user(segment))
    versioned_redraw = bool(explicit_redraw and (previous_success or source_preserved_redraw))
    if not _segment_retry_allowed(segment) and not versioned_redraw:
        raise HTTPException(status_code=409, detail={"error_code": "SHOT_RETRY_NOT_ALLOWED", "message": "Only a rejected or failed generated Shot can be retried."})
    if automatic and int(segment.get("retry_version") or 0) >= MAX_AUTOMATIC_RETRIES:
        raise HTTPException(status_code=409, detail={"error_code": "AUTOMATIC_RETRY_LIMIT_REACHED", "max_retries": MAX_AUTOMATIC_RETRIES})
    retry_version = int(segment.get("retry_version") or 0) + 1
    selected_character_id = str(segment.get("selected_character_id") or "")
    if not selected_character_id:
        raise HTTPException(status_code=409, detail={"error_code": "CHARACTER_MAPPING_REQUIRED", "message": "This Shot has no selected character to resolve for retry."})
    resolved_reference = resolve_character_reference(selected_character_id, task["episode_id"], task["project_id"])
    if not resolved_reference.get("mapping_valid"):
        raise HTTPException(status_code=422, detail={"error_code": resolved_reference.get("error_code") or "CHARACTER_REFERENCE_INVALID", "error_message": resolved_reference.get("error_message"), "character_id": selected_character_id})
    latest_path = resolved_reference.get("resolved_local_path") or resolved_reference.get("provider_url")
    latest_url = _cache_busted_reference_url(latest_path)
    latest_preview_url = latest_url
    latest_filename = Path(str(latest_path or "")).name or None
    latest_reference_type = resolved_reference.get("reference_type")
    timestamp = now_iso()
    retry_diagnostics = _merge_diagnostics(
        segment.get("diagnostics_json"),
        {
            "preserveSourceByUser": source_preserved_redraw,
            "fallbackSourceSelected": source_preserved_redraw,
            "awaitingUserAction": False if source_preserved_redraw else True,
        },
    )
    retry_diagnostics_value = json.loads(retry_diagnostics)
    if versioned_redraw:
        retry_diagnostics_value["input_route"] = {
            "processing_mode": "level1_direct",
            "preserve_current_unit_version": True,
            "preserved_source_by_user": source_preserved_redraw,
            "previous_attempt_id": previous_success["id"] if previous_success else None,
            "configured_at": timestamp,
        }
        retry_diagnostics = json.dumps(retry_diagnostics_value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with get_connection() as conn:
        if versioned_redraw:
            if source_preserved_redraw:
                _restore_source_preserved_segment_projection(conn, task_id, segment_id, timestamp)
            else:
                _restore_successful_segment_projection(conn, task_id, segment_id, previous_success, timestamp)
            conn.execute(
                """UPDATE xmax_thirty_second_segments
                   SET retry_version=?,reference_path=?,reference_url=?,reference_preview_url=?,
                       reference_type=?,diagnostics_json=?,updated_at=?
                   WHERE id=? AND task_id=?""",
                (
                    retry_version,
                    str(latest_path),
                    latest_url,
                    latest_preview_url,
                    latest_reference_type,
                    retry_diagnostics,
                    timestamp,
                    segment_id,
                    task_id,
                ),
            )
            _clear_stale_shot_failure(conn, task_id, timestamp)
        else:
            conn.execute(
                "UPDATE xmax_thirty_second_segments SET status='pending', execution_status='pending', quality_status='review_required', quality_reason=NULL, quality_reviewed_at=NULL, quality_reviewed_by=NULL, retry_version=?, reference_path=?, reference_url=?, reference_preview_url=?, reference_type=?, use_source_for_merge=0, error_code=NULL, error_message=NULL, diagnostics_json=?, updated_at=? WHERE id=? AND task_id=?",
                (retry_version, str(latest_path), latest_url, latest_preview_url, latest_reference_type, retry_diagnostics, timestamp, segment_id, task_id),
            )
            conn.execute(
                "UPDATE xmax_thirty_second_tasks SET status='pending', error_code=NULL, error_message=NULL, updated_at=? WHERE id=? AND status NOT IN ('cancelled','completed')",
                (timestamp, task_id),
            )
    return {
        "task_id": task_id,
        "segment_id": segment_id,
        "sequence": segment["sequence"],
        "retry_version": retry_version,
        "execution_status": "pending",
        "quality_status": "review_required",
        "selected_character_id": selected_character_id,
        "reference_path": _public_reference_value(latest_path),
        "reference_url": latest_url,
        "reference_preview_url": latest_preview_url,
        "reference_filename": latest_filename,
        "reference_type": latest_reference_type,
        "automatic": automatic,
        "explicit_redraw": versioned_redraw,
        "provider_call_started": False,
        "message": "Single-Shot retry prepared; Provider execution is not started by this endpoint.",
    }


@router.post("/xmax-thirty-second/{task_id}/segments/{segment_id}/merge-source")
def set_thirty_second_merge_source(task_id: str, segment_id: str, use_source_for_merge: bool = Form(...)):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM xmax_thirty_second_segments WHERE id=? AND task_id=?", (segment_id, task_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error_code": "SEGMENT_NOT_FOUND"})
    segment = row_to_dict(row)
    if segment.get("preserve_original") or segment.get("mapping_status") == "preserve_original":
        raise HTTPException(status_code=409, detail={"error_code": "SOURCE_ALREADY_SELECTED", "message": "This Shot already uses its source frame."})
    quality = segment.get("quality_status") or _default_quality_status(segment)
    execution = segment.get("execution_status") or segment.get("status")
    if use_source_for_merge:
        if execution in {"running", "local_ready", "uploading"}:
            raise HTTPException(status_code=409, detail={"error_code": "MERGE_SOURCE_NOT_ALLOWED", "message": "Stop the active Shot before selecting its source frame."})
    elif quality != "approved":
        raise HTTPException(status_code=409, detail={"error_code": "MERGE_OUTPUT_REQUIRES_APPROVAL", "message": "Only an approved generated Shot can be restored as the merge output."})
    timestamp = now_iso()
    diagnostics_json = _merge_diagnostics(
        segment.get("diagnostics_json"),
        {
            "preserveSourceByUser": bool(use_source_for_merge),
            "preserveSourceSelectedAt": timestamp if use_source_for_merge else None,
            "fallbackSourceSelected": bool(use_source_for_merge),
            "awaitingUserAction": False if use_source_for_merge else True,
        },
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_segments SET use_source_for_merge=?, diagnostics_json=?, updated_at=? WHERE id=? AND task_id=?",
            (int(use_source_for_merge), diagnostics_json, timestamp, segment_id, task_id),
        )
    refreshed_segments = _segments(task_id)
    if all(_segment_merge_ready(item) for item in refreshed_segments) and task["status"] not in {"cancelled", "completed"}:
        with get_connection() as conn:
            conn.execute("UPDATE xmax_thirty_second_tasks SET status='merge_pending', error_code=NULL, error_message=NULL, updated_at=? WHERE id=?", (timestamp, task_id))
    elif task["status"] not in {"cancelled", "completed"}:
        with get_connection() as conn:
            conn.execute(
                "UPDATE xmax_thirty_second_tasks SET status='pending', error_code=NULL, error_message=NULL, updated_at=? WHERE id=?",
                (timestamp, task_id),
            )
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


def _planned_segment_count(scene_plan_json: str | None) -> int:
    try:
        scene_plan = json.loads(scene_plan_json or "{}")
        transform_units = scene_plan.get("transform_units") or []
        planned_count = len(transform_units) or int(scene_plan.get("transform_unit_count") or 0)
        if not planned_count:
            planned_count = len(scene_plan.get("shots") or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        planned_count = 0
    return planned_count


@router.post("/xmax-thirty-second/{task_id}/merge")
def merge_thirty_second_task(task_id: str):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] == "cancelled":
        raise HTTPException(status_code=409, detail={"error_code": "TASK_CANCELLED"})
    if task["status"] == "merging":
        raise HTTPException(status_code=409, detail={"error_code": "MERGE_IN_PROGRESS"})
    if task.get("final_output_path") and task.get("final_output_url") and Path(task["final_output_path"]).is_file():
        return _payload(task)
    segments = _segments(task_id)
    planned_count = _planned_segment_count(task.get("scene_plan_json"))
    expected_count = planned_count or EXPECTED_SHOTS
    if len(segments) != expected_count:
        _set_merge_failed(task_id, "SEGMENTS_INCOMPLETE", "All planned Shot outputs are required before merge.")
        raise HTTPException(status_code=409, detail={"error_code": "SEGMENTS_INCOMPLETE", "expected_count": expected_count, "actual_count": len(segments)})
    incomplete = [segment["id"] for segment in segments if not _segment_merge_ready(segment)]
    if incomplete:
        _set_merge_failed(task_id, "SEGMENTS_INCOMPLETE", "All three validated segment outputs are required before merge.")
        raise HTTPException(status_code=409, detail={"error_code": "SEGMENTS_INCOMPLETE", "missing_segment_ids": incomplete})
    needs_review = [segment["id"] for segment in segments if not _segment_quality_ready(segment)]
    if needs_review:
        raise HTTPException(status_code=409, detail={"error_code": "QUALITY_REVIEW_REQUIRED", "segment_ids": needs_review, "message": "Approve each generated Shot before final merge."})
    paths = [_segment_merge_path(segment) for segment in segments]
    missing_files = [segment["id"] for segment, path in zip(segments, paths) if not path.is_file() or path.stat().st_size <= 0]
    if missing_files:
        _set_merge_failed(task_id, "SEGMENT_FILE_MISSING", "A completed segment file is missing.")
        raise HTTPException(status_code=422, detail={"error_code": "SEGMENT_FILE_MISSING", "missing_segment_ids": missing_files})
    source_path = Path(task["source_path"])
    if not source_path.is_file():
        _set_merge_failed(task_id, "SOURCE_VIDEO_NOT_FOUND", "The source video is missing and cannot provide the original audio.")
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_NOT_FOUND"})
    task_dir = _task_dir(task["project_id"], task["episode_id"], task["attempt_id"])
    final_path = task_dir / "final" / f"xmax-30-second-{task['id']}.mp4"
    with get_connection() as conn:
        conn.execute("UPDATE xmax_thirty_second_tasks SET status='merging', error_code=NULL, error_message=NULL, heartbeat_at=?, updated_at=? WHERE id=? AND status NOT IN ('cancelled','completed')", (now_iso(), now_iso(), task_id))
    merge_inputs: dict[str, Any] | None = None
    try:
        source_metadata = media_service.probe_media_metadata(source_path)
        merge_inputs = _prepare_merge_inputs(task, segments, paths)
        target_durations = merge_inputs["target_durations"]
        actual_durations = merge_inputs["actual_durations"]
        ratios = [duration_alignment_ratio(target, actual, details=detail) for target, actual, detail in zip(target_durations, actual_durations, merge_inputs["diagnostics"])]
        timeline_error = max(
            abs(float(segments[0]["start_time"])),
            max(abs(float(current["start_time"]) - float(previous["end_time"])) for previous, current in zip(segments, segments[1:])),
            abs(float(segments[-1]["end_time"]) - float(source_metadata["duration"])),
        )
        if timeline_error > 0.10:
            raise media_service.MediaToolError("SHOT_TIMELINE_INVALID", "Shot ranges do not cover the source timeline within 100ms.")
    except ShotDurationRatioError as exc:
        _set_merge_failed(task_id, "SHOT_DURATION_RATIO_INVALID", str(exc))
        raise HTTPException(status_code=422, detail={"error_code": "SHOT_DURATION_RATIO_INVALID", "error_message": str(exc), **exc.details}) from exc
    except Exception as exc:
        code = getattr(exc, "code", "MERGE_INPUT_INVALID")
        _set_merge_failed(task_id, code, str(exc))
        raise HTTPException(status_code=422, detail={"error_code": code, "error_message": str(exc)[:500]}) from exc
    try:
        merged = media_service.merge_xmax_shots_with_source_audio(merge_inputs["paths"], target_durations, actual_durations, source_path, final_path, source_metadata, [0.0] * len(paths), [0.0] * len(paths))
        final_metadata = media_service.probe_media_metadata(final_path)
        final_duration = float(final_metadata["duration"])
    except Exception as exc:
        _set_merge_failed(task_id, getattr(exc, "code", "MERGE_FAILED"), str(exc))
        raise HTTPException(status_code=422, detail={"error_code": getattr(exc, "code", "MERGE_FAILED"), "error_message": str(exc)[:500]}) from exc
    finally:
        if merge_inputs:
            shutil.rmtree(merge_inputs["temp_dir"], ignore_errors=True)
    source_duration = float(source_metadata["duration"])
    if abs(final_duration - source_duration) > 0.10 or final_metadata["width"] != source_metadata["width"] or final_metadata["height"] != source_metadata["height"] or (bool(final_metadata["has_audio"]) != bool(source_metadata["has_audio"])):
        message = f"Final media validation failed: duration={final_duration:.3f}s source={source_duration:.3f}s."
        _set_merge_failed(task_id, "FINAL_MEDIA_INVALID", message)
        raise HTTPException(status_code=422, detail={"error_code": "FINAL_MEDIA_INVALID", "error_message": message})
    completed_at = now_iso()
    with get_connection() as conn:
        conn.execute(
            "UPDATE xmax_thirty_second_tasks SET status='completed', final_output_path=?, final_output_url=?, final_duration=?, final_width=?, final_height=?, final_fps=?, final_has_audio=?, source_audio_restored=?, file_size=?, error_code=NULL, error_message=NULL, updated_at=?, merged_at=?, completed_at=? WHERE id=? AND status!='cancelled'",
            (str(final_path), _public_output(final_path), final_duration, final_metadata["width"], final_metadata["height"], final_metadata["fps"], int(bool(final_metadata["has_audio"])), int(bool(source_metadata["has_audio"])), final_path.stat().st_size, completed_at, completed_at, completed_at, task_id),
        )
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)


@router.post("/xmax-thirty-second/{task_id}/cancel")
def cancel_thirty_second_task(task_id: str):
    task = _task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute("UPDATE xmax_thirty_second_tasks SET status='cancelled', error_code=NULL, error_message=NULL, updated_at=?, cancelled_at=? WHERE id=? AND status!='completed'", (timestamp, timestamp, task_id))
        conn.execute("UPDATE xmax_thirty_second_segments SET status='cancelled', updated_at=? WHERE task_id=? AND status!='completed'", (timestamp, task_id))
    refreshed = _task(task_id)
    assert refreshed is not None
    return _payload(refreshed)
