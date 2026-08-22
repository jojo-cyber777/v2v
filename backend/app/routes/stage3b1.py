"""Stage 3 B1 real visual transformation PoC routes."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..database import ROOT, get_connection, json_dump, json_load, now_iso, row_to_dict
from ..services import media_service
from ..services.provider_service import ProviderError
from ..services.visual_provider_service import available_visual_models, get_visual_transformation_provider, runway_configuration
from ..services.visual_prompt_service import build_visual_prompt


router = APIRouter(prefix="/api")
OUTPUTS_DIR = ROOT / "outputs"
ACTIVE_TASKS: set[str] = set()
ACTIVE_LOCK = threading.Lock()
POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 45 * 60


class VisualTaskCreate(BaseModel):
    start_time: float | None = Field(default=None, ge=0)
    end_time: float | None = Field(default=None, gt=0)
    target_style: str = "cinematic-realism"
    visual_scope: str = "full-scene"
    extra_description: str = ""
    model: str | None = None
    confirm_fee: bool = False


class VisualReviewPayload(BaseModel):
    identity_score: int | None = Field(default=None, ge=1, le=5)
    motion_score: int | None = Field(default=None, ge=1, le=5)
    background_score: int | None = Field(default=None, ge=1, le=5)
    clothing_score: int | None = Field(default=None, ge=1, le=5)
    flicker_score: int | None = Field(default=None, ge=1, le=5)
    style_score: int | None = Field(default=None, ge=1, le=5)
    decision: str
    notes: str = ""


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


def _locked_characters(project_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM characters WHERE project_id=? AND priority_level='Locked' ORDER BY character_code, created_at", (project_id,)).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def _public_output(path: str | None) -> str | None:
    if not path:
        return None
    try:
        relative = Path(path).resolve().relative_to(OUTPUTS_DIR.resolve())
    except (ValueError, OSError):
        return None
    return "/outputs/" + relative.as_posix()


def _ratio(width: int, height: int) -> str:
    aspect = width / height
    choices = [(1280 / 720, "1280:720"), (720 / 1280, "720:1280"), (960 / 960, "960:960"), (864 / 1080, "864:1080"), (1080 / 864, "1080:864")]
    return min(choices, key=lambda item: abs(item[0] - aspect))[1]


def _reference_payload(characters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    payload: list[dict[str, Any]] = []
    sent_ids: list[str] = []
    for character in characters[:5]:
        path_value = character.get("main_reference_image") or character.get("source_reference_image") or character.get("original_reference_image")
        path = Path(str(path_value)) if path_value else None
        if not path or not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        payload.append({"uri": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"), "tag": character.get("character_code") or character.get("id")})
        sent_ids.append(str(character.get("id")))
    return payload, sent_ids


def _task_payload(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Visual transformation task not found")
        task = row_to_dict(row) or {}
        input_row = db.execute("SELECT * FROM visual_transformation_inputs WHERE task_id=?", (task_id,)).fetchone()
        output_row = db.execute("SELECT * FROM visual_transformation_outputs WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        review_row = db.execute("SELECT * FROM visual_quality_reviews WHERE task_id=?", (task_id,)).fetchone()
        segment_rows = db.execute("SELECT * FROM visual_transformation_segments WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()
    task["reference_character_ids"] = json_load(task.get("reference_character_ids"), [])
    task["input"] = row_to_dict(input_row)
    if task["input"]:
        task["input"]["clip_url"] = _public_output(task["input"].get("clip_path"))
    task["output"] = row_to_dict(output_row)
    if task["output"]:
        task["output"]["local_output_url"] = _public_output(task["output"].get("local_output_path"))
        task["output"]["provider_output_url"] = None
        input_path = task.get("input", {}).get("clip_path") if task.get("input") else None
        output_path = task["output"].get("local_output_path")
        if input_path and output_path and Path(input_path).is_file() and Path(output_path).is_file():
            task["output"]["different_from_input"] = hashlib.sha256(Path(input_path).read_bytes()).hexdigest() != task["output"].get("output_hash")
    task["quality_review"] = row_to_dict(review_row)
    task["segments"] = [row_to_dict(row) or {} for row in segment_rows]
    task["provider_status"] = runway_configuration().public_status()
    return task


def _set_task(task_id: str, **values: Any) -> None:
    values["updated_at"] = now_iso()
    with get_connection() as db:
        db.execute(f"UPDATE visual_transformation_tasks SET {', '.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), task_id))


def _friendly_visual_error(code: str, message: str, detail: str = "") -> tuple[str, str]:
    normalized = f"{code} {message} {detail}".upper()
    if "INPUT_PREPROCESSING.SAFETY" in normalized or "THIRD_PARTY" in normalized:
        suffix = f" Technical detail: {detail}" if detail else ""
        return "INPUT_PREPROCESSING_SAFETY", f"The model rejected this input during preprocessing or safety checks. No fallback video was used.{suffix}"
    suffix = f" Technical detail: {detail}" if detail else ""
    return code, f"{message}{suffix}"


def _segment_ranges(start_time: float, end_time: float, max_seconds: float) -> list[tuple[float, float]]:
    duration = end_time - start_time
    count = max(1, int((duration + max_seconds - 0.0001) // max_seconds))
    while count > 1 and duration / count < 4:
        count -= 1
    segment_duration = duration / count
    return [(start_time + index * segment_duration, start_time + (index + 1) * segment_duration) for index in range(count)]


def _run_task(task_id: str) -> None:
    with ACTIVE_LOCK:
        if task_id in ACTIVE_TASKS:
            return
        ACTIVE_TASKS.add(task_id)
    segment_id: str | None = None
    try:
        with get_connection() as db:
            row = db.execute("SELECT * FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone()
            segment_rows = db.execute("SELECT * FROM visual_transformation_segments WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()
        task = row_to_dict(row) or {}
        if not task or task.get("status") in {"completed", "failed", "cancelled", "timed_out"}:
            return
        provider = get_visual_transformation_provider(task.get("model"))
        project = _project(task["project_id"])
        refs = _locked_characters(task["project_id"]) if project.get("processing_mode") == "character-consistency" else []
        reference_payload, sent_ids = _reference_payload(refs)
        if sent_ids and json_load(task.get("reference_character_ids"), []) != sent_ids:
            _set_task(task_id, reference_character_ids=json_dump(sent_ids))
        outputs: list[tuple[str, Path]] = []
        total = len(segment_rows) or 1
        for index, segment_row in enumerate(segment_rows or [None], start=1):
            with get_connection() as db:
                input_row = db.execute("SELECT * FROM visual_transformation_inputs WHERE task_id=? ORDER BY start_time LIMIT 1 OFFSET ?", (task_id, index - 1)).fetchone()
            input_item = row_to_dict(input_row) or {}
            if not input_item:
                raise ProviderError("VIDEO_INPUT_MISSING", "A queued visual segment input is missing.")
            segment_id = segment_row["id"] if segment_row else None
            if segment_id:
                with get_connection() as db:
                    db.execute("UPDATE visual_transformation_segments SET status='submitting', updated_at=? WHERE id=?", (now_iso(), segment_id))
            _set_task(task_id, status="submitting", started_at=task.get("started_at") or now_iso(), progress=int(((index - 1) / total) * 100))
            response = provider.submit_video_to_video(Path(input_item["clip_path"]), task["prompt_text"], _ratio(int(input_item["width"]), int(input_item["height"])), reference_payload)
            provider_task_id = response.get("provider_task_id")
            if not provider_task_id:
                raise ProviderError("PROVIDER_RESPONSE_INVALID", "Runway did not return a provider task ID.")
            _set_task(task_id, provider_task_id=provider_task_id, status="processing")
            if segment_id:
                with get_connection() as db:
                    db.execute("UPDATE visual_transformation_segments SET status='processing', provider_task_id=?, updated_at=? WHERE id=?", (provider_task_id, now_iso(), segment_id))
            started = time.monotonic()
            latest = response
            while time.monotonic() - started < MAX_WAIT_SECONDS:
                latest = provider.get_task_status(provider_task_id)
                status = latest.get("status")
                if status == "completed":
                    output_url = latest.get("output_url")
                    if not output_url:
                        raise ProviderError("PROVIDER_OUTPUT_MISSING", "Runway completed the task without an output URL.")
                    output_path = OUTPUTS_DIR / task["project_id"] / task["episode_id"] / "visual_poc" / "output" / f"visual_output_{task_id}_{index:03d}.mp4"
                    _set_task(task_id, status="downloading")
                    provider.download_output(output_url, output_path)
                    outputs.append((output_url, output_path))
                    if segment_id:
                        with get_connection() as db:
                            db.execute("UPDATE visual_transformation_segments SET status='completed', output_path=?, updated_at=? WHERE id=?", (str(output_path), now_iso(), segment_id))
                    break
                if status in {"failed", "cancelled"}:
                    raise ProviderError(latest.get("failure_code") or "PROVIDER_TASK_FAILED", latest.get("failure_message") or "Runway visual transformation failed.")
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                try:
                    provider.cancel_task(provider_task_id)
                except ProviderError:
                    pass
                raise ProviderError("PROVIDER_TIMEOUT", "Runway task exceeded the B1 maximum wait time.")
            _set_task(task_id, completed_segments=index, progress=int((index / total) * 100))
        if not outputs:
            raise ProviderError("PROVIDER_OUTPUT_MISSING", "No transformed segment output was downloaded.")
        output_path = OUTPUTS_DIR / task["project_id"] / task["episode_id"] / "visual_poc" / "output" / f"visual_output_{task_id}.mp4"
        metadata = media_service.concat_videos([path for _url, path in outputs], output_path)
        output_url = outputs[-1][0] if len(outputs) == 1 else None
        output_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        with get_connection() as db:
            db.execute("INSERT INTO visual_transformation_outputs (id, task_id, output_url, local_output_path, output_duration, width, height, aspect_ratio, file_size, output_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (f"vout-{uuid.uuid4().hex[:10]}", task_id, output_url, str(output_path), metadata["duration"], metadata["width"], metadata["height"], metadata["aspect_ratio"], output_path.stat().st_size, output_hash, now_iso()))
        _set_task(task_id, status="completed", completed_segments=total, progress=100, completed_at=now_iso())
    except ProviderError as error:
        code, message = _friendly_visual_error(error.code, str(error), error.detail or "")
        if 'segment_id' in locals() and segment_id:
            with get_connection() as db:
                db.execute("UPDATE visual_transformation_segments SET status='failed', error_code=?, error_message=?, updated_at=? WHERE id=?", (code, message, now_iso(), segment_id))
        _set_task(task_id, status="failed", error_code=code, error_message=message, completed_at=now_iso())
    except Exception as error:  # Keep background failures visible in the task record.
        if 'segment_id' in locals() and segment_id:
            with get_connection() as db:
                db.execute("UPDATE visual_transformation_segments SET status='failed', error_code='VISUAL_TASK_ERROR', error_message=?, updated_at=? WHERE id=?", (str(error)[:2000], now_iso(), segment_id))
        _set_task(task_id, status="failed", error_code="VISUAL_TASK_ERROR", error_message=str(error)[:2000], completed_at=now_iso())
    finally:
        with ACTIVE_LOCK:
            ACTIVE_TASKS.discard(task_id)


def resume_pending_visual_tasks() -> None:
    with get_connection() as db:
        rows = db.execute("SELECT id FROM visual_transformation_tasks WHERE status IN ('queued','preparing','submitting','processing','downloading')").fetchall()
    for row in rows:
        threading.Thread(target=_run_task, args=(row["id"],), daemon=True).start()


@router.get("/episodes/{episode_id}/visual-transformation")
def visual_transformation_state(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    project = _project(episode["project_id"])
    with get_connection() as db:
        row = db.execute("SELECT id FROM visual_transformation_tasks WHERE episode_id=? ORDER BY created_at DESC LIMIT 1", (episode_id,)).fetchone()
    provider = runway_configuration()
    return {"episode_id": episode_id, "project_id": project["id"], "project_mode": project.get("processing_mode") or "quick-transformation", "locked_characters": [{"id": item.get("id"), "name": item.get("name"), "character_code": item.get("character_code"), "reference_image": item.get("main_reference_image") or item.get("source_reference_image")} for item in _locked_characters(project["id"])], "visual_scope": "full-scene", "disabled_scopes": ["characters-only", "background-only", "local-redraw", "partial-redraw"], "default_full_video": True, "video_duration": episode.get("duration"), "video_width": episode.get("width"), "video_height": episode.get("height"), "video_aspect_ratio": episode.get("aspect_ratio"), "video_orientation": episode.get("orientation"), "video_url": f"/uploads/{Path(episode['video_path']).name}" if episode.get("video_path") else None, "task": _task_payload(row["id"]) if row else None, "provider": {**provider.public_status(), "models": available_visual_models(provider, episode.get("duration"))}}


@router.post("/episodes/{episode_id}/visual-transformation/tasks")
def create_visual_task(episode_id: str, payload: VisualTaskCreate, background_tasks: BackgroundTasks) -> dict[str, Any]:
    episode = _episode(episode_id)
    project = _project(episode["project_id"])
    if payload.visual_scope != "full-scene":
        raise HTTPException(status_code=400, detail="B1 currently supports full-scene transformation only; the selected scope is reserved for a later stage.")
    if not episode.get("video_path"):
        raise HTTPException(status_code=400, detail="Upload the current Episode video before starting the visual PoC.")
    try:
        source_metadata = media_service.probe_video_metadata(Path(episode["video_path"]))
    except media_service.MediaToolError as error:
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error), "technical_detail": error.stderr}) from error
    model_options = available_visual_models(runway_configuration(), source_metadata["duration"])
    requested_model = payload.model or next((item["id"] for item in model_options if item.get("available_for_duration")), runway_configuration().model)
    selected_model = next((item for item in model_options if item["id"] == requested_model), None)
    if not selected_model:
        raise HTTPException(status_code=400, detail={"code": "MODEL_UNAVAILABLE", "message": "The selected visual model is not in the backend allowlist."})
    if not selected_model.get("available_for_duration"):
        raise HTTPException(status_code=422, detail={"code": "MODEL_DURATION_UNSUPPORTED", "message": f"{requested_model} does not support the selected video duration."})
    provider = get_visual_transformation_provider(requested_model)
    try:
        provider.validate_configuration()
    except ProviderError as error:
        raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error), "technical_detail": error.detail}) from error
    start_time = payload.start_time if payload.start_time is not None else 0
    end_time = payload.end_time or source_metadata["duration"]
    end_time = min(end_time, source_metadata["duration"])
    duration = end_time - start_time
    if duration < 2:
        raise HTTPException(status_code=422, detail={"code": "VIDEO_DURATION_TOO_SHORT", "message": "The selected video must be at least 2 seconds."})
    prompt = build_visual_prompt(payload.target_style, source_metadata["aspect_ratio"], _locked_characters(project["id"]) if project.get("processing_mode") == "character-consistency" else [], payload.extra_description)
    if not payload.confirm_fee:
        usage = provider.estimate_usage(duration, source_metadata["width"], source_metadata["height"])
        return {"requires_fee_confirmation": True, "estimate": {**usage, "provider": "runway", "model": provider.configuration.model, "prompt_summary_zh": "全场景转绘：统一改变人物、服装、背景、道具和光影风格，同时保留原视频动作、镜头、构图和人物关系。", "prompt_text": prompt["full_prompt"]}, "provider": {**provider.configuration.public_status(), "models": available_visual_models(provider.configuration, duration)}}
    ranges = _segment_ranges(start_time, end_time, provider.configuration.max_test_seconds)
    try:
        clips: list[dict[str, Any]] = []
        for sequence, (segment_start, segment_end) in enumerate(ranges, start=1):
            segment_duration = segment_end - segment_start
            if segment_duration > provider.configuration.max_test_seconds + 0.05:
                raise ProviderError("VIDEO_DURATION_LIMIT", f"The selected model supports {provider.configuration.max_test_seconds:g} seconds per segment.")
            clip_path = OUTPUTS_DIR / project["id"] / episode_id / "visual_poc" / "input" / f"visual_input_{sequence:03d}.mp4"
            clip = media_service.trim_video(Path(episode["video_path"]), clip_path, segment_start, segment_end)
            provider.validate_input(Path(clip["path"]), clip["duration"], source_metadata["aspect_ratio"])
            clips.append({"sequence": sequence, "start_time": segment_start, "end_time": segment_end, **clip})
    except (ProviderError, media_service.MediaToolError) as error:
        code = getattr(error, "code", "VIDEO_CLIP_FAILED")
        detail = getattr(error, "detail", getattr(error, "stderr", ""))
        raise HTTPException(status_code=422, detail={"code": code, "message": str(error), "technical_detail": detail}) from error
    task_id = f"vtask-{uuid.uuid4().hex[:10]}"
    usage = provider.estimate_usage(sum(item["duration"] for item in clips), clips[0]["width"], clips[0]["height"])
    timestamp = now_iso()
    with get_connection() as db:
        db.execute("INSERT INTO visual_transformation_tasks (id, project_id, episode_id, provider, model, start_time, end_time, duration, target_style, visual_scope, prompt_summary, prompt_summary_zh, prompt_text, reference_character_ids, total_segments, status, estimated_credits, estimated_cost_usd, created_at, updated_at) VALUES (?, ?, ?, 'runway', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'queued', ?, ?, ?, ?)", (task_id, project["id"], episode_id, provider.configuration.model, start_time, end_time, clip["duration"], payload.target_style, payload.visual_scope, prompt["summary"], "全场景转绘：保持原视频的动作、镜头、构图和人物身份，只改变目标视觉风格。", prompt["full_prompt"], json_dump([item.get("id") for item in _locked_characters(project["id"]) if project.get("processing_mode") == "character-consistency"]), usage.get("estimated_credits"), usage.get("estimated_cost_usd"), timestamp, timestamp))
        db.execute("INSERT INTO visual_transformation_inputs (id, task_id, source_video_path, clip_path, start_time, end_time, duration, width, height, fps, aspect_ratio, orientation, file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (f"vin-{uuid.uuid4().hex[:10]}", task_id, episode["video_path"], clip["path"], start_time, end_time, clip["duration"], clip["width"], clip["height"], clip["fps"], clip["aspect_ratio"], clip["orientation"], Path(clip["path"]).stat().st_size, timestamp))
        db.execute("INSERT INTO visual_transformation_segments (id, task_id, sequence, start_time, end_time, duration, status, input_path, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, 'pending', ?, ?, ?)", (f"vseg-{uuid.uuid4().hex[:10]}", task_id, start_time, end_time, clip["duration"], clip["path"], timestamp, timestamp))
    if len(clips) > 1:
        with get_connection() as db:
            db.execute("UPDATE visual_transformation_tasks SET duration=?, total_segments=? WHERE id=?", (sum(item["duration"] for item in clips), len(clips), task_id))
            db.execute("DELETE FROM visual_transformation_inputs WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM visual_transformation_segments WHERE task_id=?", (task_id,))
            for clip in clips:
                db.execute("INSERT INTO visual_transformation_inputs (id, task_id, source_video_path, clip_path, start_time, end_time, duration, width, height, fps, aspect_ratio, orientation, file_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (f"vin-{uuid.uuid4().hex[:10]}", task_id, episode["video_path"], clip["path"], clip["start_time"], clip["end_time"], clip["duration"], clip["width"], clip["height"], clip["fps"], clip["aspect_ratio"], clip["orientation"], Path(clip["path"]).stat().st_size, timestamp))
                db.execute("INSERT INTO visual_transformation_segments (id, task_id, sequence, start_time, end_time, duration, status, input_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)", (f"vseg-{uuid.uuid4().hex[:10]}", task_id, clip["sequence"], clip["start_time"], clip["end_time"], clip["duration"], clip["path"], timestamp, timestamp))
    background_tasks.add_task(_run_task, task_id)
    return _task_payload(task_id)


@router.get("/visual-transformation-tasks/{task_id}")
def get_visual_task(task_id: str) -> dict[str, Any]:
    return _task_payload(task_id)


@router.post("/visual-transformation-tasks/{task_id}/refresh")
def refresh_visual_task(task_id: str) -> dict[str, Any]:
    """Re-read Provider state only; this endpoint never creates a new paid task."""
    with get_connection() as db:
        row = db.execute("SELECT provider_task_id, model, status FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Visual transformation task not found")
    if not row["provider_task_id"]:
        return _task_payload(task_id)
    try:
        latest = get_visual_transformation_provider(row["model"]).get_task_status(row["provider_task_id"])
    except ProviderError as error:
        raise HTTPException(status_code=502, detail={"code": error.code, "message": str(error), "technical_detail": error.detail}) from error
    status = latest.get("status")
    if status in {"processing", "queued", "submitted"}:
        _set_task(task_id, status="processing")
    elif status == "cancelled":
        _set_task(task_id, status="cancelled", completed_at=now_iso())
    elif status == "failed":
        _set_task(task_id, status="failed", error_code=latest.get("failure_code") or "PROVIDER_TASK_FAILED", error_message=latest.get("failure_message") or "Provider task failed.", completed_at=now_iso())
    return _task_payload(task_id)


@router.delete("/visual-transformation-tasks/{task_id}")
def delete_visual_task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT id FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Visual transformation task not found")
        db.execute("DELETE FROM visual_transformation_tasks WHERE id=?", (task_id,))
    return {"deleted": True, "task_id": task_id}


@router.post("/visual-transformation-tasks/{task_id}/cancel")
def cancel_visual_task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT provider_task_id, model, status FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Visual transformation task not found")
    if row["provider_task_id"] and row["status"] in {"processing", "submitting"}:
        try:
            get_visual_transformation_provider(row["model"]).cancel_task(row["provider_task_id"])
        except ProviderError as error:
            raise HTTPException(status_code=502, detail={"code": error.code, "message": str(error), "technical_detail": error.detail}) from error
    _set_task(task_id, status="cancelled", completed_at=now_iso())
    return _task_payload(task_id)


@router.post("/visual-transformation-tasks/{task_id}/quality-review")
def save_visual_review(task_id: str, payload: VisualReviewPayload) -> dict[str, Any]:
    if payload.decision not in {"usable", "adjust_prompt", "unusable"}:
        raise HTTPException(status_code=400, detail="Decision must be usable, adjust_prompt, or unusable.")
    with get_connection() as db:
        if not db.execute("SELECT id FROM visual_transformation_tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Visual transformation task not found")
        timestamp = now_iso()
        db.execute("INSERT INTO visual_quality_reviews (id, task_id, identity_score, motion_score, background_score, clothing_score, flicker_score, style_score, decision, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET identity_score=excluded.identity_score, motion_score=excluded.motion_score, background_score=excluded.background_score, clothing_score=excluded.clothing_score, flicker_score=excluded.flicker_score, style_score=excluded.style_score, decision=excluded.decision, notes=excluded.notes, updated_at=excluded.updated_at", (f"vrev-{uuid.uuid4().hex[:10]}", task_id, payload.identity_score, payload.motion_score, payload.background_score, payload.clothing_score, payload.flicker_score, payload.style_score, payload.decision, payload.notes, timestamp, timestamp))
    return _task_payload(task_id)
