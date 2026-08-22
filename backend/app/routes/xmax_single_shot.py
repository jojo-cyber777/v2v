"""Persistent backend contract for the isolated Xmax single-shot MVP.

The browser runner owns the realtime SDK session. These endpoints only persist
the task lifecycle and accept the completed WebM output.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..database import ROOT, UPLOADS_DIR, get_connection, now_iso, row_to_dict
from ..services import media_service
from ..user_storage import STORAGE_ROOT

router = APIRouter(prefix="/api")
OUTPUTS_DIR = ROOT / "outputs"

MIN_SOURCE_DURATION = 5.0
MAX_SOURCE_DURATION = 15.0
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
ALLOWED_REFERENCE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
ALLOWED_WEBM_TYPES = {"video/webm", "video/x-webm", "application/octet-stream"}
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
ACTIVE_STATUSES = {"pending", "preparing", "running"}


def _safe_component(value: str, label: str) -> str:
    if not value or not SAFE_COMPONENT.fullmatch(value):
        raise HTTPException(status_code=500, detail=f"Unsafe {label} stored in database")
    return value


def _row(task_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        record = conn.execute(
            "SELECT * FROM xmax_single_shot_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return row_to_dict(record) if record else None


def _episode(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        record = conn.execute(
            """
            SELECT e.*, p.id AS project_id
            FROM episodes e
            JOIN projects p ON p.id = e.project_id
            WHERE e.id = ?
            """,
            (episode_id,),
        ).fetchone()
    return row_to_dict(record) if record else None


def _safe_source_path(episode: dict[str, Any]) -> Path:
    raw = str(episode.get("video_path") or episode.get("source_video_path") or "")
    if not raw:
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_MISSING"})
    source = Path(raw)
    if not source.is_absolute():
        source = ROOT / source
    source = source.resolve()
    allowed_roots = (ROOT.resolve(), STORAGE_ROOT.resolve())
    if not any(source.is_relative_to(allowed_root) for allowed_root in allowed_roots):
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_INVALID"})
    if not source.is_file():
        raise HTTPException(status_code=422, detail={"error_code": "SOURCE_VIDEO_NOT_FOUND"})
    return source


def _safe_task_dir(task: dict[str, Any]) -> Path:
    project_id = _safe_component(str(task["project_id"]), "project_id")
    episode_id = _safe_component(str(task["episode_id"]), "episode_id")
    attempt_id = _safe_component(str(task["attempt_id"]), "attempt_id")
    directory = OUTPUTS_DIR.resolve() / project_id / episode_id / "xmax-single" / attempt_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _public_output(path: Path) -> str:
    root = OUTPUTS_DIR.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Output path escaped outputs directory") from exc
    return "/outputs/" + "/".join(relative.parts)


def _public_source(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return None
    return "/uploads/" + "/".join(relative.parts)


def _task_payload(task: dict[str, Any], *, idempotent: bool = False) -> dict[str, Any]:
    payload = {
        "task_id": task["id"],
        "attempt_id": task["attempt_id"],
        "project_id": task["project_id"],
        "episode_id": task["episode_id"],
        "status": task["status"],
        "prompt": task["prompt"],
        "source_url": _public_source(Path(task["source_path"])),
        "source_duration": task["source_duration"],
        "reference_url": _public_output(Path(task["reference_path"])),
        "estimated_credits": task["estimated_credits"],
        "billed_credits": task.get("billed_credits"),
        "processed_seconds": task.get("processed_seconds"),
        "output_url": task.get("output_url"),
        "output_duration": task.get("output_duration"),
        "file_size": task.get("file_size"),
        "stop_reason": task.get("stop_reason"),
        "error_code": task.get("error_code"),
        "error_message": task.get("error_message"),
        "created_at": task["created_at"],
        "completed_at": task.get("completed_at"),
    }
    if idempotent:
        payload["idempotent"] = True
    return payload


def _read_diagnostics(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"error_code": "DIAGNOSTICS_INVALID"}) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail={"error_code": "DIAGNOSTICS_INVALID"})
    return parsed


def _safe_error_summary(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"[A-Za-z]:\\[^\r\n]*", "<local-path>", text)
    return text[:500]


def _is_allowed_webm_content_type(content_type: str) -> bool:
    """Accept only the allowlisted base MIME, ignoring codec parameters."""
    base_mime = content_type.split(";", 1)[0].strip().lower()
    return base_mime in ALLOWED_WEBM_TYPES


def _diagnostic_values(parsed: dict[str, Any]) -> dict[str, Any]:
    allowed_text = {
        "session_id",
        "connect_started_at",
        "connect_resolved_at",
        "reference_upload_started_at",
        "reference_upload_resolved_at",
        "restart_started_at",
        "restart_resolved_at",
        "stop_reason",
    }
    values: dict[str, Any] = {}
    for key in allowed_text:
        value = parsed.get(key)
        if isinstance(value, str) and len(value) <= 256:
            values[key] = value
    for key in ("processed_seconds", "billed_credits"):
        value = parsed.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            values[key] = float(value)
    return values


def _set_failed(task_id: str, error_code: str, message: str) -> None:
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE xmax_single_shot_tasks
            SET status = 'failed', error_code = ?, error_message = ?, updated_at = ?
            WHERE id = ? AND status != 'completed'
            """,
            (error_code, message[:1000], timestamp, task_id),
        )


@router.post("/episodes/{episode_id}/xmax-single-shot/tasks")
async def create_single_shot_task(
    episode_id: str,
    prompt: str = Form(...),
    reference_image: UploadFile = File(...),
):
    episode = _episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail={"error_code": "EPISODE_NOT_FOUND"})
    prompt_value = (prompt or "").strip()
    if not prompt_value:
        raise HTTPException(status_code=422, detail={"error_code": "PROMPT_REQUIRED"})

    source_path = _safe_source_path(episode)
    try:
        metadata = media_service.probe_video_metadata(source_path)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "SOURCE_VIDEO_INVALID", "error_message": str(exc)[:500]},
        ) from exc
    duration = float(metadata.get("duration") or 0)
    if duration < MIN_SOURCE_DURATION or duration > MAX_SOURCE_DURATION:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "SOURCE_DURATION_OUT_OF_RANGE",
                "source_duration": duration,
                "min_seconds": MIN_SOURCE_DURATION,
                "max_seconds": MAX_SOURCE_DURATION,
            },
        )

    content_type = (reference_image.content_type or "").lower()
    suffix = Path(reference_image.filename or "").suffix.lower()
    if content_type not in ALLOWED_REFERENCE_TYPES or (suffix and suffix not in {".jpg", ".jpeg", ".png", ".webp"}):
        raise HTTPException(status_code=422, detail={"error_code": "REFERENCE_IMAGE_TYPE_INVALID"})
    suffix = ALLOWED_REFERENCE_TYPES[content_type]
    reference_bytes = await reference_image.read(MAX_REFERENCE_BYTES + 1)
    if not reference_bytes:
        raise HTTPException(status_code=422, detail={"error_code": "REFERENCE_IMAGE_EMPTY"})
    if len(reference_bytes) > MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=413, detail={"error_code": "REFERENCE_IMAGE_TOO_LARGE"})

    task_id = f"xmax-single-{uuid.uuid4().hex[:16]}"
    attempt_id = f"xmax-single-attempt-{uuid.uuid4().hex[:16]}"
    created_at = now_iso()
    task_stub = {
        "id": task_id,
        "attempt_id": attempt_id,
        "project_id": episode["project_id"],
        "episode_id": episode_id,
    }
    task_dir = _safe_task_dir(task_stub)
    reference_path = task_dir / "reference" / f"reference{suffix}"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        reference_path.write_bytes(reference_bytes)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO xmax_single_shot_tasks (
                    id, attempt_id, project_id, episode_id, status, prompt,
                    reference_path, source_path, source_duration, estimated_credits,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt_id,
                    episode["project_id"],
                    episode_id,
                    prompt_value,
                    str(reference_path),
                    str(source_path),
                    duration,
                    duration,
                    created_at,
                    created_at,
                ),
            )
    except Exception:
        reference_path.unlink(missing_ok=True)
        raise
    task = _row(task_id)
    assert task is not None
    return _task_payload(task)


@router.post("/xmax-single-shot/{task_id}/output")
async def upload_single_shot_output(
    task_id: str,
    output_file: UploadFile = File(...),
    diagnostics_json: str | None = Form(default=None),
):
    task = _row(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] == "cancelled":
        raise HTTPException(status_code=409, detail={"error_code": "TASK_CANCELLED"})
    if task["status"] == "completed":
        return _task_payload(task, idempotent=True)
    if task["status"] not in {"pending", "preparing", "running", "failed"}:
        raise HTTPException(status_code=409, detail={"error_code": "TASK_NOT_ACCEPTING_OUTPUT"})

    filename = output_file.filename or "output.webm"
    content_type = output_file.content_type or ""
    if Path(filename).suffix.lower() != ".webm" or not _is_allowed_webm_content_type(content_type):
        _set_failed(task_id, "OUTPUT_FORMAT_INVALID", "Only a readable WebM output is accepted.")
        raise HTTPException(status_code=422, detail={"error_code": "OUTPUT_FORMAT_INVALID"})

    diagnostics = _diagnostic_values(_read_diagnostics(diagnostics_json))
    task_dir = _safe_task_dir(task)
    temp_path = task_dir / f".uploading-{uuid.uuid4().hex}.webm"
    final_name = f"xmax-single-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{task['attempt_id']}.webm"
    final_path = task_dir / final_name
    moved = False
    remux_path: Path | None = None
    candidate_path = temp_path
    remux_attempted = False
    remux_succeeded = False
    original_probe_error: str | None = None
    remux_probe_error: str | None = None
    temporary_paths = [temp_path]
    try:
        with temp_path.open("wb") as target:
            total = 0
            while True:
                chunk = await output_file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT_BYTES:
                    raise HTTPException(status_code=413, detail={"error_code": "OUTPUT_TOO_LARGE"})
                target.write(chunk)
        if total <= 0:
            raise HTTPException(status_code=422, detail={"error_code": "OUTPUT_EMPTY"})
        try:
            metadata = media_service.probe_video_metadata(temp_path)
        except Exception as exc:
            original_probe_error = _safe_error_summary(exc)
            remux_attempted = True
            remux_path = task_dir / f".remuxing-{uuid.uuid4().hex}.webm"
            temporary_paths.append(remux_path)
            ffmpeg = media_service.resolve_ffmpeg_binary()
            if not ffmpeg:
                remux_probe_error = "FFMPEG_UNAVAILABLE"
            else:
                remux_result = subprocess.run(
                    [ffmpeg, "-y", "-fflags", "+genpts", "-i", str(temp_path), "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(remux_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                    check=False,
                )
                if remux_result.returncode != 0 or not remux_path.is_file() or remux_path.stat().st_size <= 0:
                    remux_probe_error = _safe_error_summary((remux_result.stderr or "FFmpeg remux failed.").splitlines()[-1])
            if remux_probe_error:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": "OUTPUT_NOT_READABLE",
                        "error_message": "The video metadata is incomplete or invalid.",
                        "original_probe_error": original_probe_error,
                        "remux_probe_error": remux_probe_error,
                    },
                ) from exc
            try:
                metadata = media_service.probe_video_metadata(remux_path)
                candidate_path = remux_path
                remux_succeeded = True
            except Exception as remux_exc:
                remux_probe_error = _safe_error_summary(remux_exc)
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": "OUTPUT_NOT_READABLE",
                        "error_message": "The video metadata is incomplete or invalid.",
                        "original_probe_error": original_probe_error,
                        "remux_probe_error": remux_probe_error,
                    },
                ) from remux_exc
        output_duration = float(metadata.get("duration") or 0)
        if output_duration <= 0:
            raise HTTPException(status_code=422, detail={"error_code": "OUTPUT_DURATION_INVALID"})
        total = candidate_path.stat().st_size
        candidate_path.replace(final_path)
        for temporary_path in temporary_paths:
            if temporary_path != candidate_path:
                temporary_path.unlink(missing_ok=True)
        moved = True
        completed_at = now_iso()
        update_fields = {
            "status": "completed",
            "output_path": str(final_path),
            "output_url": _public_output(final_path),
            "output_duration": output_duration,
            "file_size": total,
            "processed_seconds": diagnostics.pop("processed_seconds", min(task["source_duration"], output_duration)),
            "completed_at": completed_at,
            "updated_at": completed_at,
            **diagnostics,
        }
        assignments = ", ".join(f"{key} = ?" for key in update_fields)
        with get_connection() as conn:
            conn.execute(
                f"UPDATE xmax_single_shot_tasks SET {assignments} WHERE id = ? AND status != 'completed'",
                (*update_fields.values(), task_id),
            )
    except HTTPException as exc:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        if moved:
            final_path.unlink(missing_ok=True)
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        _set_failed(
            task_id,
            str(detail.get("error_code") or "OUTPUT_UPLOAD_FAILED"),
            str(detail.get("error_message") or detail.get("error_code") or "Output upload failed."),
        )
        raise
    except Exception as exc:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        if moved:
            final_path.unlink(missing_ok=True)
        _set_failed(task_id, "OUTPUT_PERSIST_FAILED", str(exc))
        raise HTTPException(status_code=500, detail={"error_code": "OUTPUT_PERSIST_FAILED"}) from exc
    updated = _row(task_id)
    assert updated is not None
    payload = _task_payload(updated)
    payload["persistence_diagnostics"] = {
        "remux_attempted": remux_attempted,
        "remux_succeeded": remux_succeeded,
        "original_probe_error": original_probe_error,
        "remux_probe_error": remux_probe_error,
    }
    return payload


@router.get("/episodes/{episode_id}/xmax-single-shot/latest")
def get_latest_single_shot(episode_id: str):
    with get_connection() as conn:
        record = conn.execute(
            """
            SELECT * FROM xmax_single_shot_tasks
            WHERE episode_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (episode_id,),
        ).fetchone()
    if not record:
        return None
    return _task_payload(row_to_dict(record))


@router.post("/xmax-single-shot/{task_id}/cancel")
def cancel_single_shot(task_id: str):
    task = _row(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND"})
    if task["status"] == "completed":
        raise HTTPException(status_code=409, detail={"error_code": "TASK_COMPLETED"})
    if task["status"] == "cancelled":
        return _task_payload(task)
    if task["status"] not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail={"error_code": "TASK_NOT_CANCELLABLE"})
    timestamp = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE xmax_single_shot_tasks
            SET status = 'cancelled', cancelled_at = ?, stop_reason = 'user-cancelled', updated_at = ?
            WHERE id = ? AND status IN ('pending', 'preparing', 'running')
            """,
            (timestamp, timestamp, task_id),
        )
    updated = _row(task_id)
    assert updated is not None
    return _task_payload(updated)
