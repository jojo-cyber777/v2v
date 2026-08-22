"""Local persistence and media endpoints for the browser Xmax shot pipeline.

These routes never call Xmax. The browser owns the realtime session; the API
only prepares source shots, stores captured WebM files, and merges them.
"""

from __future__ import annotations

import uuid
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from PIL import Image, ImageOps, UnidentifiedImageError

from ..database import ROOT, get_connection, json_dump, json_load, now_iso, row_to_dict
from ..services import media_service, task_state


router = APIRouter(prefix="/api")
OUTPUTS_DIR = ROOT / "outputs"
XMAX_PROMPT = "视频中角色替换成参考图中角色"
ACTIVE_STATUSES = {"prepared", "starting", "pending", "running", "preparing", "splitting", "processing", "recording", "merging"}
PREPARED_REUSE_MAX_SECONDS = 30 * 60
STARTING_NO_SESSION_REUSE_MAX_SECONDS = 30
STARTING_SESSION_ACTIVE_MAX_SECONDS = 10
RUNNING_HEARTBEAT_MAX_SECONDS = 30


class XmaxBatchPrepare(BaseModel):
    reference_character_id: str | None = None


class XmaxBatchFailure(BaseModel):
    code: str = "XMAX_SHOT_FAILED"
    message: str = "The Xmax shot pipeline failed."
    stage: str | None = None


class XmaxBatchStarting(BaseModel):
    shot_id: str


class XmaxBatchStarted(BaseModel):
    shot_id: str
    session_id: str
    active_confirmation_source: str | None = None


class XmaxBatchStop(BaseModel):
    reason: str = "user-stop"


class XmaxAttemptCreate(BaseModel):
    batch_id: str | None = None
    shot_id: str | None = None
    estimated_credits: float = 0


class XmaxAttemptUpdate(BaseModel):
    episode_id: str | None = None
    batch_id: str | None = None
    shot_id: str | None = None
    request_end_at: str | None = None
    session_id: str | None = None
    provider_request_method: str | None = None
    provider_request_url: str | None = None
    provider_http_status: int | None = None
    provider_response_body: str | dict[str, Any] | list[Any] | None = None
    provider_error_name: str | None = None
    provider_error_code: str | None = None
    provider_error_message: str | None = None
    provider_error_stack_head: str | None = None
    classified_error_code: str | None = None
    classified_error_message: str | None = None
    processed_seconds: float | None = None
    billed_credits: float | None = None
    estimated_credits: float | None = None
    status: str | None = None
    stop_reason: str | None = None
    page_hidden: bool | None = None
    tab_hidden: bool | None = None
    unload: bool | None = None
    output_stream_received: bool | None = None
    output_file_received: bool | None = None
    output_file_path: str | None = None
    final_input_stream_active: bool | None = None
    final_input_video_track_count: int | None = None
    final_input_track_id: str | None = None
    final_input_track_ready_state: str | None = None
    final_input_track_enabled: bool | None = None
    final_input_track_muted: bool | None = None
    final_input_track_settings: str | dict[str, Any] | None = None
    local_element_tag_name: str | None = None
    remote_element_tag_name: str | None = None
    canvas_draw_count_before_connect: int | None = None
    video_current_time_before_connect: float | None = None


class XmaxShotReference(BaseModel):
    character_ids: list[str]


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    item = row_to_dict(row)
    if not item:
        raise HTTPException(status_code=404, detail="Episode not found")
    return item


def _project(project_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    item = row_to_dict(row)
    if not item:
        raise HTTPException(status_code=404, detail="Project not found")
    return item


def _safe_path(value: str | None, *, output_only: bool = False) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if value.startswith('/') and not candidate.is_absolute():
        candidate = ROOT / value.lstrip('/')
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve()
        base = OUTPUTS_DIR.resolve() if output_only else ROOT.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return resolved


def _public_output(path: str | None) -> str | None:
    resolved = _safe_path(path, output_only=True)
    if not resolved:
        return None
    return "/outputs/" + resolved.relative_to(OUTPUTS_DIR.resolve()).as_posix()


def _public_reference(path: str | None) -> str | None:
    output = _public_output(path)
    if output:
        return output
    resolved = _reference_path(path)
    if not resolved:
        return None
    try:
        return "/" + resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None


def _reference_path(value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if value.startswith('/') and not candidate.is_absolute():
        candidate = ROOT / value.lstrip('/')
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(ROOT.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _batch_payload(batch_id: str) -> dict[str, Any]:
    with get_connection() as db:
        batch_row = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch_row:
            raise HTTPException(status_code=404, detail="Xmax batch not found")
        shot_rows = db.execute("SELECT * FROM xmax_realtime_shots WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall()
    batch = row_to_dict(batch_row) or {}
    shots = [row_to_dict(row) or {} for row in shot_rows]
    for shot in shots:
        shot["source_url"] = _public_output(shot.get("source_path"))
        shot["output_url"] = _public_output(shot.get("output_path"))
        shot["character_ids"] = json_load(shot.get("character_ids_json"), [])
        shot["reference_layout"] = json_load(shot.get("reference_layout_json"), [])
        shot["reference_url"] = _public_reference(shot.get("reference_path"))
    batch["shots"] = shots
    batch["output_url"] = _public_output(batch.get("output_path"))
    batch["prompt"] = XMAX_PROMPT
    return batch


AUDIT_TEXT_LIMIT = 8000
_SECRET_VALUE_RE = re.compile(
    r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|secret|password)\s*[=:]\s*)([^\s,;}]+)",
    re.IGNORECASE,
)


def _redact_audit_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(value)
    text = str(value)
    text = re.sub(r"Bearer\s+[^\s,;}]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = _SECRET_VALUE_RE.sub(r"\1[REDACTED]", text)
    return text[:AUDIT_TEXT_LIMIT]


def _attempt_payload(attempt_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM xmax_realtime_attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Xmax attempt not found")
    return row_to_dict(row) or {}


def _update_attempt(attempt_id: str, **values: Any) -> None:
    if not values:
        return
    with get_connection() as db:
        current = db.execute("SELECT status FROM xmax_realtime_attempts WHERE id=?", (attempt_id,)).fetchone()
    if current and str(current["status"]) in {"completed", "failed", "cancelled"}:
        # A late page-hidden/beforeunload request must not rewrite a terminal audit record.
        if values.get("status") and values["status"] != current["status"]:
            return
        if values.get("stop_reason") == "page-hidden":
            return
    if "status" in values and values["status"] in {"completed", "failed", "cancelled"}:
        values.setdefault("request_end_at", now_iso())
    values["updated_at"] = now_iso()
    with get_connection() as db:
        db.execute(
            f"UPDATE xmax_realtime_attempts SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), attempt_id),
        )


def _update_latest_attempt_for_batch(batch_id: str, shot_id: str | None = None, **values: Any) -> None:
    with get_connection() as db:
        if shot_id:
            row = db.execute(
                "SELECT id FROM xmax_realtime_attempts WHERE batch_id=? AND shot_id=? ORDER BY request_start_at DESC LIMIT 1",
                (batch_id, shot_id),
            ).fetchone()
        else:
            row = db.execute("SELECT id FROM xmax_realtime_attempts WHERE batch_id=? ORDER BY request_start_at DESC LIMIT 1", (batch_id,)).fetchone()
    if row:
        _update_attempt(str(row["id"]), **values)


def _set_batch(batch_id: str, **values: Any) -> None:
    if "status" in values:
        values["status"] = task_state.canonical_status(values["status"])
    values = task_state.heartbeat_update(values)
    with get_connection() as db:
        db.execute(
            f"UPDATE xmax_realtime_batches SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), batch_id),
        )


def _set_shot(shot_id: str, **values: Any) -> None:
    values["updated_at"] = now_iso()
    with get_connection() as db:
        db.execute(
            f"UPDATE xmax_realtime_shots SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), shot_id),
        )


def _batch_age_exceeded(value: str | None, max_seconds: int) -> bool:
    return task_state.heartbeat_expired(value, max_seconds)


def _batch_shot_resources_complete(batch: dict[str, Any]) -> bool:
    expected = int(batch.get("total_shots") or 0)
    if expected <= 0:
        return False
    with get_connection() as db:
        shots = db.execute("SELECT sequence, start_time, end_time, source_path FROM xmax_realtime_shots WHERE batch_id=? ORDER BY sequence", (batch["id"],)).fetchall()
    if len(shots) != expected:
        return False
    for shot in shots:
        source = _safe_path(shot["source_path"], output_only=True)
        if not source or not source.is_file():
            return False
        if int(shot["sequence"] or 0) <= 0 or float(shot["end_time"] or 0) <= float(shot["start_time"] or 0):
            return False
    return True


def _mark_stale_batch(batch_id: str) -> None:
    _set_batch(
        batch_id,
        status="interrupted",
        stop_reason="stale_batch_replaced",
        error_code="XMAX_STALE_BATCH_REPLACED",
        error_message="The previous Xmax batch was stale or inconsistent and was replaced before any session started.",
        failed_stage="prepare_batch",
        finished_at=now_iso(),
    )


def _prepare_batch_reuse_or_conflict(episode_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return a reusable batch or raise only for a genuinely active session."""
    with get_connection() as db:
        rows = db.execute("SELECT * FROM xmax_realtime_batches WHERE episode_id=? ORDER BY created_at DESC", (episode_id,)).fetchall()
    replaced_stale_batch_id: str | None = None
    for row in rows:
        batch = row_to_dict(row) or {}
        status = task_state.canonical_status(batch.get("status"))
        session_exists = bool(str(batch.get("session_id") or "").strip())
        resources_complete = _batch_shot_resources_complete(batch)
        if status == "prepared":
            if not session_exists and resources_complete and not _batch_age_exceeded(batch.get("created_at"), PREPARED_REUSE_MAX_SECONDS):
                return batch, replaced_stale_batch_id
            if session_exists or not resources_complete or _batch_age_exceeded(batch.get("created_at"), PREPARED_REUSE_MAX_SECONDS):
                _mark_stale_batch(str(batch["id"]))
                replaced_stale_batch_id = str(batch["id"])
        elif status == "starting":
            if not session_exists and resources_complete and not _batch_age_exceeded(batch.get("updated_at"), STARTING_NO_SESSION_REUSE_MAX_SECONDS):
                return batch, replaced_stale_batch_id
            if session_exists and resources_complete and not _batch_age_exceeded(batch.get("heartbeat_at") or batch.get("session_created_at") or batch.get("updated_at"), STARTING_SESSION_ACTIVE_MAX_SECONDS):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "XMAX_BATCH_ACTIVE",
                        "message": "An Xmax shot batch already has a live Session startup.",
                        "batch_id": batch["id"],
                        "status": status,
                        "session_id_exists": True,
                        "started_at": batch.get("started_at"),
                        "session_created_at": batch.get("session_created_at"),
                    },
                )
            _mark_stale_batch(str(batch["id"]))
            replaced_stale_batch_id = str(batch["id"])
        elif status == "running":
            if session_exists and resources_complete and not _batch_age_exceeded(batch.get("heartbeat_at") or batch.get("started_at") or batch.get("updated_at"), RUNNING_HEARTBEAT_MAX_SECONDS):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "XMAX_BATCH_ACTIVE",
                        "message": "An Xmax shot batch is already running with a live Session.",
                        "batch_id": batch["id"],
                        "status": status,
                        "session_id_exists": True,
                        "started_at": batch.get("started_at"),
                        "session_created_at": batch.get("session_created_at"),
                    },
                )
            _mark_stale_batch(str(batch["id"]))
            replaced_stale_batch_id = str(batch["id"])
        elif status in {"pending", "preparing", "splitting", "processing", "recording", "merging"}:
            _mark_stale_batch(str(batch["id"]))
            replaced_stale_batch_id = str(batch["id"])
    return None, replaced_stale_batch_id


def _recover_batch_start_state(batch_id: str) -> None:
    """Recover only genuinely stale starts; do not race the browser SDK startup."""
    with get_connection() as db:
        row = db.execute(
            "SELECT status, session_id, heartbeat_at, updated_at FROM xmax_realtime_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
    if not row:
        return
    status = str(row["status"] or "")
    if status in {"starting", "running"} and not str(row["session_id"] or "").strip():
        # The browser creates the SDK session asynchronously. A latest-batch
        # read can happen during that window, so only recover after the last
        # startup heartbeat/update is actually stale.
        last_update = row["heartbeat_at"] or row["updated_at"]
        if not last_update or not task_state.heartbeat_expired(last_update, 30):
            return
        _set_batch(
            batch_id,
            status="interrupted",
            error_code="XMAX_SESSION_START_INTERRUPTED",
            error_message="Xmax startup was interrupted before the SDK returned a Session object.",
            failed_stage="create_xmax_session",
            finished_at=now_iso(),
        )
    elif status in {"starting", "running"} and task_state.heartbeat_expired(row["heartbeat_at"], 30):
        _set_batch(
            batch_id,
            status="interrupted",
            error_code="XMAX_SESSION_HEARTBEAT_EXPIRED",
            error_message="The Xmax session heartbeat expired before the session was confirmed active.",
            failed_stage="wait_session_active",
            finished_at=now_iso(),
        )


def _find_reference(project_id: str, character_id: str | None) -> tuple[str, Path]:
    with get_connection() as db:
        if character_id:
            row = db.execute("SELECT * FROM characters WHERE id=? AND project_id=?", (character_id, project_id)).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM characters WHERE project_id=? AND confirmed=1 AND priority_level='Locked' ORDER BY character_code, created_at LIMIT 1",
                (project_id,),
            ).fetchone()
    character = row_to_dict(row)
    if not character:
        raise HTTPException(status_code=400, detail="Please confirm a character card with a primary reference image first.")
    if not character.get("confirmed") or character.get("priority_level") != "Locked":
        raise HTTPException(status_code=400, detail="The selected character card is not locked and confirmed.")
    reference = _reference_path(character.get("target_reference_path") or character.get("main_reference_image") or character.get("source_reference_image") or character.get("original_reference_image"))
    if not reference:
        raise HTTPException(status_code=400, detail="The confirmed character card has no readable primary reference image.")
    return str(character["id"]), reference


def _rounded_32(value: int) -> int:
    return max(32, ((value + 31) // 32) * 32)


def _build_reference_image(project_id: str, episode_id: str, batch_id: str, sequence: int, character_ids: list[str]) -> tuple[Path, list[dict[str, Any]]]:
    if not 1 <= len(character_ids) <= 3:
        raise HTTPException(status_code=422, detail="A shot must contain one to three confirmed characters.")
    if len(set(character_ids)) != len(character_ids):
        raise HTTPException(status_code=422, detail="A character can only appear once in a shot binding.")
    with get_connection() as db:
        rows = db.execute(f"SELECT * FROM characters WHERE project_id=? AND id IN ({','.join('?' for _ in character_ids)})", (project_id, *character_ids)).fetchall()
    by_id = {str(row["id"]): row_to_dict(row) or {} for row in rows}
    if len(by_id) != len(character_ids):
        raise HTTPException(status_code=400, detail="One or more selected characters do not belong to this project.")
    if len(character_ids) == 1:
        character = by_id[character_ids[0]]
        if not character.get("confirmed") or character.get("priority_level") != "Locked":
            raise HTTPException(status_code=400, detail=f"Character {character_ids[0]} must be confirmed before use.")
        path = _reference_path(character.get("target_reference_path") or character.get("main_reference_image"))
        if not path:
            raise HTTPException(status_code=400, detail=f"Character {character_ids[0]} is missing a target reference image.")
        return path, [{"character_id": character_ids[0], "position": "center", "order": 0}]
    images: list[Image.Image] = []
    for character_id in character_ids:
        character = by_id[character_id]
        if not character.get("confirmed") or character.get("priority_level") != "Locked":
            raise HTTPException(status_code=400, detail=f"Character {character_id} must be confirmed before use.")
        path = _reference_path(character.get("target_reference_path") or character.get("main_reference_image"))
        if not path:
            raise HTTPException(status_code=400, detail=f"Character {character_id} is missing a target reference image.")
        try:
            with Image.open(path) as source:
                images.append(ImageOps.exif_transpose(source).convert("RGB"))
        except (OSError, UnidentifiedImageError) as error:
            raise HTTPException(status_code=422, detail=f"Character {character_id} reference image is unreadable.") from error
    target_height = 512
    gap = 32
    resized = []
    for image in images:
        width = max(32, round(image.width * target_height / max(1, image.height)))
        resized.append(image.resize((width, target_height), Image.Resampling.LANCZOS))
    canvas_width = _rounded_32(sum(image.width for image in resized) + gap * (len(resized) + 1))
    canvas_height = _rounded_32(target_height + gap * 2)
    canvas = Image.new("RGB", (canvas_width, canvas_height), (28, 30, 34))
    x = gap
    layout: list[dict[str, Any]] = []
    positions = ["left", "center", "right"]
    for index, image in enumerate(resized):
        y = gap + (target_height - image.height) // 2
        canvas.paste(image, (x, y))
        layout.append({"character_id": character_ids[index], "position": positions[index], "order": index})
        x += image.width + gap
    reference_dir = OUTPUTS_DIR / str(project_id) / episode_id / "xmax" / batch_id / "references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    output_path = reference_dir / f"shot-{sequence:03d}-reference.jpg"
    canvas.save(output_path, format="JPEG", quality=88, optimize=True, progressive=True)
    return output_path, layout


@router.post("/episodes/{episode_id}/xmax-batches/prepare")
def prepare_xmax_batch(episode_id: str, payload: XmaxBatchPrepare) -> dict[str, Any]:
    episode = _episode(episode_id)
    project = _project(str(episode["project_id"]))
    source = _safe_path(episode.get("video_path"))
    if not source or not source.is_file():
        raise HTTPException(status_code=400, detail="The episode has no readable source video.")
    reference_id, _reference = _find_reference(str(project["id"]), payload.reference_character_id)
    reusable, replaced_stale_batch_id = _prepare_batch_reuse_or_conflict(episode_id)
    if reusable:
        payload = _batch_payload(str(reusable["id"]))
        payload.update({"reused": True, "replaced_stale_batch_id": replaced_stale_batch_id, "session_id_exists": bool(str(reusable.get("session_id") or "").strip())})
        return payload
    try:
        metadata = media_service.probe_video_metadata(source)
        try:
            cuts = media_service.detect_scene_cuts(source)
            split_mode = "scene-detection"
        except media_service.MediaToolError:
            cuts = []
            split_mode = "fixed-10-second-fallback"
        ranges = media_service.build_shot_ranges(float(metadata["duration"]), cuts)
    except media_service.MediaToolError as error:
        detail = {"code": error.code, "message": str(error), **error.details}
        detail.setdefault("stderr", error.stderr)
        detail.setdefault("current_working_directory", str(Path.cwd()))
        if error.code == "FFPROBE_UNAVAILABLE":
            detail.setdefault("searched_paths", [])
            detail.setdefault("resolved_path", None)
        raise HTTPException(status_code=422, detail=detail) from error
    if not ranges:
        raise HTTPException(status_code=422, detail="No valid shots could be created from the episode.")
    batch_id = f"xmax-batch-{uuid.uuid4().hex[:12]}"
    batch_dir = OUTPUTS_DIR / str(project["id"]) / episode_id / "xmax" / batch_id
    shots_dir = batch_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    created_shots: list[tuple[str, int, float, float, Path]] = []
    created_at = now_iso()
    try:
        for sequence, (start_time, end_time) in enumerate(ranges, start=1):
            shot_id = f"{batch_id}-shot-{sequence:03d}"
            shot_path = shots_dir / f"shot-{sequence:03d}-source.mp4"
            media_service.trim_video(source, shot_path, start_time, end_time)
            created_shots.append((shot_id, sequence, start_time, end_time, shot_path))
        with get_connection() as db:
            db.execute(
                "INSERT INTO xmax_realtime_batches (id, project_id, episode_id, status, reference_character_id, prompt, total_shots, estimated_credits, created_at, updated_at, heartbeat_at, failed_stage, current_shot) VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (batch_id, project["id"], episode_id, reference_id, XMAX_PROMPT, len(created_shots), sum(end - start for _id, _sequence, start, end, _path in created_shots), created_at, now_iso(), now_iso()),
            )
            for shot_id, sequence, start_time, end_time, shot_path in created_shots:
                db.execute(
                    "INSERT INTO xmax_realtime_shots (id, batch_id, sequence, start_time, end_time, duration, source_path, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (shot_id, batch_id, sequence, start_time, end_time, end_time - start_time, str(shot_path), created_at, now_iso()),
                )
    except media_service.MediaToolError as error:
        _set_batch(batch_id, status="failed", error_code=error.code, error_message=str(error)) if created_shots else None
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
    prepared_payload = {**_batch_payload(batch_id), "split_mode": split_mode, "source_duration": metadata["duration"]}
    prepared_payload.update({"reused": False, "replaced_stale_batch_id": replaced_stale_batch_id, "session_id_exists": False})
    return prepared_payload


@router.get("/xmax-batches/{batch_id}")
def get_xmax_batch(batch_id: str) -> dict[str, Any]:
    _recover_batch_start_state(batch_id)
    return _batch_payload(batch_id)


@router.get("/episodes/{episode_id}/xmax-batches/latest")
def get_latest_xmax_batch(episode_id: str) -> dict[str, Any] | None:
    _episode(episode_id)
    with get_connection() as db:
        row = db.execute("SELECT id FROM xmax_realtime_batches WHERE episode_id=? ORDER BY created_at DESC LIMIT 1", (episode_id,)).fetchone()
    if not row:
        return None
    _recover_batch_start_state(row["id"])
    return _batch_payload(row["id"])


@router.post("/episodes/{episode_id}/xmax-attempts")
def create_xmax_attempt(episode_id: str, payload: XmaxAttemptCreate) -> dict[str, Any]:
    _episode(episode_id)
    batch_id = payload.batch_id.strip() if payload.batch_id else None
    shot_id = payload.shot_id.strip() if payload.shot_id else None
    if batch_id:
        with get_connection() as db:
            batch = db.execute("SELECT id, episode_id FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or str(batch["episode_id"]) != episode_id:
            raise HTTPException(status_code=404, detail="Xmax batch not found for this episode.")
    if shot_id:
        with get_connection() as db:
            shot = db.execute("SELECT id, batch_id FROM xmax_realtime_shots WHERE id=?", (shot_id,)).fetchone()
        if not shot or (batch_id and str(shot["batch_id"]) != batch_id):
            raise HTTPException(status_code=404, detail="Xmax shot not found for this attempt.")
    timestamp = now_iso()
    attempt_id = f"xmax-attempt-{uuid.uuid4().hex[:16]}"
    with get_connection() as db:
        db.execute(
            "INSERT INTO xmax_realtime_attempts (id, episode_id, batch_id, shot_id, request_start_at, estimated_credits, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
            (attempt_id, episode_id, batch_id, shot_id, timestamp, max(0.0, float(payload.estimated_credits or 0)), timestamp, timestamp),
        )
    return _attempt_payload(attempt_id)


@router.patch("/xmax-attempts/{attempt_id}")
def update_xmax_attempt(attempt_id: str, payload: XmaxAttemptUpdate) -> dict[str, Any]:
    _attempt_payload(attempt_id)
    values = payload.model_dump(exclude_unset=True)
    text_fields = {
        "session_id", "provider_request_method", "provider_request_url", "provider_error_name",
        "provider_error_code", "provider_error_message", "provider_error_stack_head", "classified_error_code", "classified_error_message", "stop_reason", "output_file_path", "final_input_track_id", "final_input_track_ready_state", "local_element_tag_name", "remote_element_tag_name",
    }
    for key in text_fields:
        if key in values and values[key] is not None:
            values[key] = _redact_audit_text(values[key])
    if "provider_response_body" in values:
        values["provider_response_body"] = _redact_audit_text(values["provider_response_body"])
    if "final_input_track_settings" in values:
        values["final_input_track_settings"] = _redact_audit_text(values["final_input_track_settings"])
    for key in {"page_hidden", "tab_hidden", "unload", "output_stream_received", "output_file_received", "final_input_stream_active", "final_input_track_enabled", "final_input_track_muted"}:
        if key in values and values[key] is not None:
            values[key] = int(bool(values[key]))
    if "status" in values and values["status"] not in {"prepared", "starting", "running", "completed", "failed", "cancelled"}:
        raise HTTPException(status_code=422, detail="Unsupported Xmax attempt status.")
    if "batch_id" in values and values["batch_id"]:
        with get_connection() as db:
            row = db.execute("SELECT episode_id FROM xmax_realtime_batches WHERE id=?", (values["batch_id"],)).fetchone()
        attempt = _attempt_payload(attempt_id)
        if not row or str(row["episode_id"]) != str(attempt["episode_id"]):
            raise HTTPException(status_code=404, detail="Xmax batch not found for this attempt.")
    if "shot_id" in values and values["shot_id"]:
        with get_connection() as db:
            row = db.execute("SELECT batch_id FROM xmax_realtime_shots WHERE id=?", (values["shot_id"],)).fetchone()
        if not row or (values.get("batch_id") and str(row["batch_id"]) != str(values["batch_id"])):
            raise HTTPException(status_code=404, detail="Xmax shot not found for this attempt.")
    _update_attempt(attempt_id, **values)
    return _attempt_payload(attempt_id)


@router.get("/episodes/{episode_id}/xmax-attempts/latest")
def get_latest_xmax_attempt(episode_id: str) -> dict[str, Any] | None:
    _episode(episode_id)
    with get_connection() as db:
        row = db.execute("SELECT id FROM xmax_realtime_attempts WHERE episode_id=? ORDER BY request_start_at DESC LIMIT 1", (episode_id,)).fetchone()
    return _attempt_payload(str(row["id"])) if row else None


@router.post("/xmax-batches/{batch_id}/starting")
def mark_xmax_batch_starting(batch_id: str, payload: XmaxBatchStarting) -> dict[str, Any]:
    with get_connection() as db:
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        shot = db.execute("SELECT * FROM xmax_realtime_shots WHERE id=? AND batch_id=?", (payload.shot_id, batch_id)).fetchone()
    if not batch or not shot:
        raise HTTPException(status_code=404, detail={"code": "XMAX_SHOT_NOT_FOUND", "message": "Shot 001 is not part of this batch."})
    if int(shot["sequence"]) != 1:
        raise HTTPException(status_code=422, detail={"code": "XMAX_START_SHOT_INVALID", "message": "Only Shot 001 can be started in this validation step."})
    if str(batch["status"]) not in {"prepared", "starting"}:
        raise HTTPException(status_code=409, detail={"code": "XMAX_BATCH_NOT_PREPARED", "message": "The Xmax batch is not ready to start."})
    _set_batch(batch_id, status="starting", current_shot=1, failed_stage="create_xmax_session", error_code=None, error_message=None, finished_at=None)
    _update_latest_attempt_for_batch(batch_id, payload.shot_id, status="starting")
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/start")
def confirm_xmax_batch_started(batch_id: str, payload: XmaxBatchStarted) -> dict[str, Any]:
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=422, detail={"code": "XMAX_SESSION_ID_MISSING", "message": "Xmax did not return a session id."})
    with get_connection() as db:
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        shot = db.execute("SELECT * FROM xmax_realtime_shots WHERE id=? AND batch_id=?", (payload.shot_id, batch_id)).fetchone()
    if not batch or not shot:
        raise HTTPException(status_code=404, detail={"code": "XMAX_SHOT_NOT_FOUND", "message": "Shot 001 is not part of this batch."})
    if int(shot["sequence"]) != 1:
        raise HTTPException(status_code=422, detail={"code": "XMAX_START_SHOT_INVALID", "message": "Only Shot 001 can be started in this validation step."})
    if str(batch["status"]) not in {"prepared", "starting"}:
        raise HTTPException(status_code=409, detail={"code": "XMAX_BATCH_NOT_STARTABLE", "message": "The Xmax batch is not in a startable state."})
    _set_batch(
        batch_id,
        status="starting",
        current_shot=0,
        session_id=session_id,
        session_created_at=now_iso(),
        active_confirmation_source=None,
        failed_stage=None,
        error_code=None,
        error_message=None,
        finished_at=None,
    )
    _update_latest_attempt_for_batch(batch_id, payload.shot_id, status="starting", session_id=session_id)
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/running")
def confirm_xmax_batch_running(batch_id: str, payload: XmaxBatchStarted) -> dict[str, Any]:
    session_id = payload.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=422, detail={"code": "XMAX_SESSION_ID_MISSING", "message": "Xmax did not return a session id."})
    with get_connection() as db:
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        shot = db.execute("SELECT * FROM xmax_realtime_shots WHERE id=? AND batch_id=?", (payload.shot_id, batch_id)).fetchone()
    if not batch or not shot:
        raise HTTPException(status_code=404, detail={"code": "XMAX_SHOT_NOT_FOUND", "message": "Shot 001 is not part of this batch."})
    if int(shot["sequence"]) != 1:
        raise HTTPException(status_code=422, detail={"code": "XMAX_START_SHOT_INVALID", "message": "Only Shot 001 can be started in this validation step."})
    if str(batch["status"]) != "starting" or str(batch["session_id"] or "") != session_id:
        raise HTTPException(status_code=409, detail={"code": "XMAX_SESSION_NOT_STARTING", "message": "The Xmax session is not in the expected starting state."})
    _set_batch(
        batch_id,
        status="running",
        current_shot=1,
        started_at=now_iso(),
        active_confirmation_source=(payload.active_confirmation_source or "sdk_session").strip(),
        failed_stage=None,
        error_code=None,
        error_message=None,
        finished_at=None,
    )
    _update_latest_attempt_for_batch(batch_id, payload.shot_id, status="running", session_id=session_id)
    return _batch_payload(batch_id)


@router.put("/xmax-batches/{batch_id}/shots/{shot_id}/reference")
def save_xmax_shot_reference(batch_id: str, shot_id: str, payload: XmaxShotReference) -> dict[str, Any]:
    with get_connection() as db:
        shot = db.execute("SELECT * FROM xmax_realtime_shots WHERE id=? AND batch_id=?", (shot_id, batch_id)).fetchone()
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
    if not shot or not batch:
        raise HTTPException(status_code=404, detail="Xmax shot or batch not found")
    if task_state.canonical_status(batch["status"]) in {"cancelled", "failed", "interrupted", "completed"}:
        raise HTTPException(status_code=409, detail="This Xmax batch is no longer editable.")
    path, layout = _build_reference_image(str(batch["project_id"]), str(batch["episode_id"]), batch_id, int(shot["sequence"]), payload.character_ids)
    with get_connection() as db:
        db.execute("UPDATE xmax_realtime_shots SET character_ids_json=?, reference_layout_json=?, reference_path=?, updated_at=? WHERE id=?", (json_dump(payload.character_ids), json_dump(layout), str(path), now_iso(), shot_id))
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/shots/{shot_id}/result")
async def save_xmax_shot_result(batch_id: str, shot_id: str, file: UploadFile = File(...), processed_seconds: float = Form(default=0)) -> dict[str, Any]:
    with get_connection() as db:
        shot = db.execute("SELECT * FROM xmax_realtime_shots WHERE id=? AND batch_id=?", (shot_id, batch_id)).fetchone()
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
    if not shot or not batch:
        raise HTTPException(status_code=404, detail="Xmax shot or batch not found")
    if task_state.canonical_status(batch["status"]) in {"cancelled", "failed", "interrupted", "completed"}:
        raise HTTPException(status_code=409, detail="This Xmax batch is no longer accepting shot results.")
    shot_item = row_to_dict(shot) or {}
    batch_item = row_to_dict(batch) or {}
    output_path = Path(str(shot_item["source_path"])).parent / f"shot-{int(shot_item['sequence']):03d}-xmax.webm"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                target.write(chunk)
    finally:
        await file.close()
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        _set_shot(shot_id, status="failed", error_code="XMAX_OUTPUT_EMPTY", error_message="The captured shot file is empty.")
        _set_batch(batch_id, status="failed", error_code="XMAX_OUTPUT_EMPTY", error_message="The captured shot file is empty.")
        raise HTTPException(status_code=422, detail={"code": "XMAX_OUTPUT_EMPTY", "message": "The captured shot file is empty."})
    try:
        metadata = media_service.probe_video_metadata(output_path)
    except media_service.MediaToolError as error:
        _set_shot(shot_id, status="failed", error_code=error.code, error_message=str(error))
        _set_batch(batch_id, status="failed", error_code=error.code, error_message=str(error))
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
    bounded_processed = max(0.0, min(float(shot_item["duration"]), float(processed_seconds or metadata["duration"])))
    _set_shot(shot_id, status="completed", output_path=str(output_path), output_duration=metadata["duration"], file_size=output_path.stat().st_size, processed_seconds=bounded_processed, error_code=None, error_message=None)
    with get_connection() as db:
        completed = db.execute("SELECT COUNT(*) AS count, COALESCE(SUM(processed_seconds), 0) AS seconds FROM xmax_realtime_shots WHERE batch_id=? AND status='completed'", (batch_id,)).fetchone()
        db.execute("UPDATE xmax_realtime_batches SET completed_shots=?, processed_seconds=?, estimated_credits=?, updated_at=? WHERE id=?", (completed["count"], completed["seconds"], completed["seconds"], now_iso(), batch_id))
    _update_latest_attempt_for_batch(batch_id, shot_id, output_stream_received=True, output_file_received=True, output_file_path=str(output_path), processed_seconds=bounded_processed)
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/merge")
def merge_xmax_batch(batch_id: str) -> dict[str, Any]:
    with get_connection() as db:
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        shots = db.execute("SELECT * FROM xmax_realtime_shots WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall()
    if not batch:
        raise HTTPException(status_code=404, detail="Xmax batch not found")
    if not shots or any(row["status"] != "completed" or not row["output_path"] for row in shots):
        raise HTTPException(status_code=409, detail="All Xmax shots must be recorded before merging.")
    source = _safe_path(_episode(str(batch["episode_id"])).get("video_path"))
    outputs = [_safe_path(row["output_path"], output_only=True) for row in shots]
    if not source or any(path is None for path in outputs):
        raise HTTPException(status_code=422, detail="The source or transformed shot file is unavailable.")
    output_path = Path(str(outputs[0])).parent.parent / f"xmax-localized-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    _set_batch(batch_id, status="merging", error_code=None, error_message=None)
    try:
        metadata = media_service.merge_video_with_original_audio(source, [path for path in outputs if path], output_path)
    except media_service.MediaToolError as error:
        _set_batch(batch_id, status="failed", error_code=error.code, error_message=str(error))
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
    _set_batch(batch_id, status="completed", output_path=str(output_path), output_duration=metadata["duration"], output_file_size=output_path.stat().st_size)
    _update_latest_attempt_for_batch(batch_id, status="completed", output_file_received=True, output_file_path=str(output_path), processed_seconds=float(batch["processed_seconds"] or 0), billed_credits=float(batch["actual_credits"]) if batch["actual_credits"] is not None else None)
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/stop")
def stop_xmax_batch(batch_id: str, payload: XmaxBatchStop | None = None) -> dict[str, Any]:
    _batch_payload(batch_id)
    reason = (payload.reason if payload else "user-stop").strip()[:120] or "user-stop"
    with get_connection() as db:
        row = db.execute("SELECT status FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Xmax batch not found")
    current_status = task_state.canonical_status(row["status"])
    if current_status in {"completed", "cancelled", "failed", "interrupted"}:
        return _batch_payload(batch_id)
    failure_reasons = {"XMAX_SESSION_ACTIVE_TIMEOUT", "wait_xmax_session_active", "session-active-timeout", "XMAX_OUTPUT_READY_TIMEOUT", "wait_xmax_output_ready", "output-ready-timeout", "sdk-error", "network-error"}
    final_status = "failed" if reason in failure_reasons else "cancelled"
    output_ready_timeout = reason in {"XMAX_OUTPUT_READY_TIMEOUT", "wait_xmax_output_ready", "output-ready-timeout"}
    _set_batch(
        batch_id,
        status=final_status,
        stop_requested_at=now_iso(),
        stop_reason=reason,
        failed_stage="wait_xmax_output_ready" if output_ready_timeout else "wait_xmax_session_active" if reason in {"XMAX_SESSION_ACTIVE_TIMEOUT", "wait_xmax_session_active", "session-active-timeout"} else None,
        error_code="XMAX_OUTPUT_READY_TIMEOUT" if output_ready_timeout else "XMAX_SESSION_ACTIVE_TIMEOUT" if reason in {"XMAX_SESSION_ACTIVE_TIMEOUT", "wait_xmax_session_active", "session-active-timeout"} else None,
        error_message="Xmax session was created, but no usable output stream arrived within 10 seconds." if output_ready_timeout else "Xmax session did not become active within 10 seconds." if reason in {"XMAX_SESSION_ACTIVE_TIMEOUT", "wait_xmax_session_active", "session-active-timeout"} else None,
        finished_at=now_iso(),
    )
    _update_latest_attempt_for_batch(batch_id, status=final_status, stop_reason=reason, request_end_at=now_iso(), processed_seconds=float(_batch_payload(batch_id).get("processed_seconds") or 0))
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/reset")
def reset_xmax_batch(batch_id: str) -> dict[str, Any]:
    with get_connection() as db:
        batch = db.execute("SELECT * FROM xmax_realtime_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="Xmax batch not found")
    status = task_state.canonical_status(batch["status"])
    if status not in {"failed", "interrupted", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only a failed or interrupted Xmax batch can be restarted.")
    with get_connection() as db:
        db.execute(
            "UPDATE xmax_realtime_shots SET status='pending', error_code=NULL, error_message=NULL, processed_seconds=0, output_path=NULL, output_duration=NULL, file_size=NULL, updated_at=? WHERE batch_id=? AND status!='completed'",
            (now_iso(), batch_id),
        )
    _set_batch(batch_id, status="pending", error_code=None, error_message=None, output_path=None, output_duration=None, output_file_size=None)
    return _batch_payload(batch_id)


@router.post("/xmax-batches/{batch_id}/fail")
def fail_xmax_batch(batch_id: str, payload: XmaxBatchFailure) -> dict[str, Any]:
    _batch_payload(batch_id)
    _set_batch(batch_id, status="failed", failed_stage=payload.stage, error_code=payload.code, error_message=payload.message[:1000], finished_at=now_iso())
    _update_latest_attempt_for_batch(batch_id, status="failed", stop_reason=payload.code, request_end_at=now_iso(), provider_error_code=payload.code, provider_error_message=payload.message[:1000])
    return _batch_payload(batch_id)
