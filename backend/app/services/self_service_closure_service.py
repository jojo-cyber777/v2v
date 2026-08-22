"""Provider-free Unit selection and formal Episode finalization."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any
import uuid
import wave

from ..database import ROOT, get_connection, now_iso
from ..user_storage import storage_asset_url
from . import audio_localization_service, media_service


OUTPUTS_DIR = ROOT / "outputs"
GENERATED = "generated_attempt"
ORIGINAL = "original"
VISUAL_ONLY_REVISION_PREFIX = "visual-only:"


class SelfServiceClosureError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _object_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _output_mode(item: dict[str, Any]) -> str:
    return "visual-only" if str(item.get("audio_revision") or "").startswith(VISUAL_ONLY_REVISION_PREFIX) else "localized"


def _write_visual_only_silence(path: Path, duration: float) -> Path:
    if duration <= 0:
        raise SelfServiceClosureError(
            "VISUAL_FINAL_AUDIO_DURATION_INVALID",
            "The visual-only timeline duration is invalid.",
        )
    sample_rate = 48_000
    frame_count = max(1, math.ceil(duration * sample_rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        silent_frame = b"\x00\x00\x00\x00"
        remaining = frame_count
        while remaining:
            count = min(remaining, sample_rate)
            output.writeframesraw(silent_frame * count)
            remaining -= count
    return path


def _owned_episode(
    db: sqlite3.Connection,
    user_id: str,
    episode_id: str,
    *,
    verified_project_id: str | None = None,
) -> sqlite3.Row:
    if verified_project_id:
        row = db.execute(
            """SELECT e.*,p.owner_user_id,p.name AS project_name
               FROM episodes e JOIN projects p ON p.id=e.project_id
               WHERE e.id=? AND e.project_id=?""",
            (episode_id, verified_project_id),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT e.*,p.owner_user_id,p.name AS project_name
               FROM episodes e JOIN projects p ON p.id=e.project_id
               WHERE e.id=? AND p.owner_user_id=?""",
            (episode_id, user_id),
        ).fetchone()
    if not row:
        raise SelfServiceClosureError("EPISODE_NOT_FOUND", "Episode not found.", status_code=404)
    return row


def _task(db: sqlite3.Connection, episode_id: str, task_id: str | None) -> sqlite3.Row:
    if task_id:
        row = db.execute(
            "SELECT * FROM xmax_thirty_second_tasks WHERE id=? AND episode_id=?",
            (task_id, episode_id),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT t.* FROM xmax_thirty_second_tasks t
               WHERE t.episode_id=?
                 AND EXISTS (SELECT 1 FROM xmax_thirty_second_segments s WHERE s.task_id=t.id)
               ORDER BY COALESCE(t.updated_at,t.created_at) DESC LIMIT 1""",
            (episode_id,),
        ).fetchone()
    if not row:
        raise SelfServiceClosureError("PROCESSING_UNITS_REQUIRED", "Prepare processing Units before final selection.")
    return row


def _file(path_value: str | None, code: str) -> Path:
    path = Path(str(path_value or ""))
    if not path.is_file() or path.stat().st_size <= 0:
        raise SelfServiceClosureError(code, "A required local media asset is missing.")
    return path.resolve()


def _attempt_public(row: sqlite3.Row) -> dict[str, Any]:
    render = _object_json(row["render_json"])
    quality = render.get("quality_evaluation") if isinstance(render.get("quality_evaluation"), dict) else {}
    raw_output = render.get("raw_output") if isinstance(render.get("raw_output"), dict) else {}
    normalization = render.get("normalization") if isinstance(render.get("normalization"), dict) else {}
    output_path = Path(str(row["output_path"] or ""))
    playable = bool(
        row["status"] == "settled"
        and row["settlement_status"] == "settled"
        and row["output_path"]
        and output_path.is_file()
        and output_path.stat().st_size > 0
    )
    return {
        "id": row["id"],
        "unit_id": row["unit_id"],
        "status": row["status"],
        "settlement_status": row["settlement_status"],
        "processing_mode": (_object_json(row["render_json"]).get("input_snapshot") or {}).get("processing_mode", "level1_direct"),
        "output_url": row["output_url"],
        "output_duration": row["output_duration"],
        "output_sha256": row["output_sha256"],
        "consumed_points": row["consumed_points"],
        "quality_status": quality.get("quality_status"),
        "raw_output_persisted": raw_output.get("status") == "persisted",
        "normalization_status": normalization.get("status"),
        "normalization_error_code": normalization.get("error_code"),
        "output_saved": bool(row["output_path"] and row["output_url"]),
        "playable": playable,
        "created_at": row["created_at"],
    }


def get_workspace(
    user_id: str,
    episode_id: str,
    task_id: str | None = None,
    *,
    verified_project_id: str | None = None,
) -> dict[str, Any]:
    with get_connection() as db:
        episode = _owned_episode(
            db, user_id, episode_id, verified_project_id=verified_project_id
        )
        task = _task(db, episode_id, task_id)
        units = db.execute(
            "SELECT * FROM xmax_thirty_second_segments WHERE task_id=? ORDER BY start_time,sequence",
            (task["id"],),
        ).fetchall()
        attempts = db.execute(
            "SELECT * FROM xmax_browser_attempts WHERE user_id=? AND task_id=? ORDER BY created_at DESC",
            (user_id, task["id"]),
        ).fetchall()
        selections = {
            row["unit_id"]: dict(row)
            for row in db.execute(
                "SELECT * FROM episode_unit_selections WHERE user_id=? AND episode_id=? AND task_id=?",
                (user_id, episode_id, task["id"]),
            ).fetchall()
        }
        finals = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM episode_final_outputs WHERE user_id=? AND episode_id=? ORDER BY created_at DESC",
                (user_id, episode_id),
            ).fetchall()
        ]
    attempts_by_unit: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_unit.setdefault(attempt["unit_id"], []).append(_attempt_public(attempt))
    unit_items = []
    for unit in units:
        diagnostics = _object_json(unit["diagnostics_json"])
        route = diagnostics.get("input_route") if isinstance(diagnostics.get("input_route"), dict) else {}
        selection = selections.get(unit["id"])
        unit_items.append({
            "unit_id": unit["id"],
            "sequence": unit["sequence"],
            "start_time": unit["start_time"],
            "end_time": unit["end_time"],
            "duration": unit["duration"],
            "status": unit["status"],
            "execution_status": unit["execution_status"],
            "quality_status": unit["quality_status"],
            "processing_mode": route.get("processing_mode", "level1_direct"),
            "source_url": unit["source_url"],
            "crop_preview_url": route.get("preprocessed_source_url"),
            "preprocessed_source_sha256": route.get("preprocessed_source_sha256"),
            "crop": route.get("crop"),
            "scale": route.get("scale"),
            "attempts": attempts_by_unit.get(unit["id"], []),
            "selection": ({
                "selected_source_type": selection["selected_source_type"],
                "selected_attempt_id": selection["selected_attempt_id"],
                "selected_at": selection["selected_at"],
            } if selection else None),
        })
    final_items = []
    for item in finals:
        item.pop("output_path", None)
        item["unit_selection_snapshot"] = json.loads(item.pop("unit_selection_snapshot_json") or "[]")
        item["output_mode"] = _output_mode(item)
        final_items.append(item)
    latest_final_output = next(
        (
            item
            for item in final_items
            if item.get("status") == "ready" and item.get("output_url") and item.get("output_mode") == "localized"
        ),
        None,
    )
    latest_visual_output = next(
        (
            item
            for item in final_items
            if item.get("status") == "ready" and item.get("output_url") and item.get("output_mode") == "visual-only"
        ),
        None,
    )
    return {
        "project": {"id": episode["project_id"], "name": episode["project_name"]},
        "episode": {"id": episode["id"], "title": episode["title"], "duration": episode["duration"]},
        "task_id": task["id"],
        "units": unit_items,
        "selection_complete": bool(unit_items) and all(item["selection"] for item in unit_items),
        "final_outputs": final_items,
        "latest_final_output": latest_final_output,
        "latest_visual_output": latest_visual_output,
    }


def select_unit(
    user_id: str,
    episode_id: str,
    unit_id: str,
    *,
    selected_source_type: str,
    selected_attempt_id: str | None,
    verified_project_id: str | None = None,
) -> dict[str, Any]:
    if selected_source_type not in {GENERATED, ORIGINAL}:
        raise SelfServiceClosureError("UNIT_SELECTION_INVALID", "Choose a generated result or the original Unit.", status_code=422)
    timestamp = now_iso()
    task_id: str
    with get_connection() as db:
        episode = _owned_episode(
            db, user_id, episode_id, verified_project_id=verified_project_id
        )
        unit = db.execute(
            """SELECT s.*,t.episode_id,t.project_id FROM xmax_thirty_second_segments s
               JOIN xmax_thirty_second_tasks t ON t.id=s.task_id
               WHERE s.id=? AND t.episode_id=?""",
            (unit_id, episode_id),
        ).fetchone()
        if not unit:
            raise SelfServiceClosureError("UNIT_NOT_FOUND", "Unit not found.", status_code=404)
        task_id = str(unit["task_id"])
        attempt_id = selected_attempt_id if selected_source_type == GENERATED else None
        if selected_source_type == GENERATED:
            attempt = db.execute(
                """SELECT * FROM xmax_browser_attempts
                   WHERE id=? AND user_id=? AND project_id=? AND episode_id=? AND task_id=? AND unit_id=?""",
                (attempt_id, user_id, episode["project_id"], episode_id, unit["task_id"], unit_id),
            ).fetchone()
            if not attempt or not _attempt_public(attempt)["playable"]:
                raise SelfServiceClosureError("GENERATED_ATTEMPT_NOT_PLAYABLE", "The selected result is not playable.")
        existing = db.execute("SELECT * FROM episode_unit_selections WHERE unit_id=?", (unit_id,)).fetchone()
        if existing and (
            existing["selected_source_type"] == selected_source_type
            and existing["selected_attempt_id"] == attempt_id
        ):
            pass
        else:
            db.execute(
                """INSERT INTO episode_unit_selections
                   (unit_id,user_id,project_id,episode_id,task_id,selected_source_type,
                    selected_attempt_id,selected_at,selected_by)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(unit_id) DO UPDATE SET
                     user_id=excluded.user_id,project_id=excluded.project_id,episode_id=excluded.episode_id,
                     task_id=excluded.task_id,selected_source_type=excluded.selected_source_type,
                     selected_attempt_id=excluded.selected_attempt_id,selected_at=excluded.selected_at,
                     selected_by=excluded.selected_by""",
                (unit_id, user_id, episode["project_id"], episode_id, task_id, selected_source_type,
                 attempt_id, timestamp, user_id),
            )
    return get_workspace(
        user_id, episode_id, task_id, verified_project_id=verified_project_id
    )


def _selected_timeline(db: sqlite3.Connection, user_id: str, episode_id: str, task: sqlite3.Row) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT s.*,sel.selected_source_type,sel.selected_attempt_id,
                  a.output_path AS attempt_output_path,a.output_duration AS attempt_output_duration,
                  a.output_sha256 AS attempt_output_sha256,a.status AS attempt_status,
                  a.settlement_status AS attempt_settlement_status
           FROM xmax_thirty_second_segments s
           LEFT JOIN episode_unit_selections sel ON sel.unit_id=s.id AND sel.user_id=?
           LEFT JOIN xmax_browser_attempts a ON a.id=sel.selected_attempt_id
           WHERE s.task_id=? ORDER BY s.start_time,s.sequence""",
        (user_id, task["id"]),
    ).fetchall()
    if not rows:
        raise SelfServiceClosureError("PROCESSING_UNITS_REQUIRED", "No processing Units are available.")
    missing = [row["id"] for row in rows if not row["selected_source_type"]]
    if missing:
        raise SelfServiceClosureError("UNIT_SELECTION_INCOMPLETE", f"Select a result for every Unit: {', '.join(missing)}")
    previous_end = 0.0
    timeline = []
    for row in rows:
        start = float(row["start_time"])
        end = float(row["end_time"])
        if end <= start or abs(start - previous_end) > 0.1:
            raise SelfServiceClosureError("UNIT_TIMELINE_INVALID", "Unit ranges are not continuous and ordered.")
        previous_end = end
        if row["selected_source_type"] == GENERATED:
            if row["attempt_status"] != "settled" or row["attempt_settlement_status"] != "settled":
                raise SelfServiceClosureError("GENERATED_ATTEMPT_NOT_SETTLED", "A selected generated result is not settled.")
            path = _file(row["attempt_output_path"], "GENERATED_ATTEMPT_FILE_MISSING")
            actual = float(row["attempt_output_duration"] or media_service.probe_media_metadata(path)["duration"])
        else:
            path = None
            actual = end - start
        timeline.append({
            "unit_id": row["id"], "sequence": row["sequence"], "start_time": start, "end_time": end,
            "target_duration": end - start, "selected_source_type": row["selected_source_type"],
            "selected_attempt_id": row["selected_attempt_id"], "path": path, "actual_duration": actual,
            "source_sha256": row["attempt_output_sha256"] if path else None,
        })
    expected = float(task["source_duration"] or 0)
    if expected > 0 and abs(previous_end - expected) > 0.1:
        raise SelfServiceClosureError("UNIT_TIMELINE_INCOMPLETE", "Unit ranges do not cover the Episode.")
    return timeline


def create_final_output(
    user_id: str,
    episode_id: str,
    task_id: str | None = None,
    *,
    verified_project_id: str | None = None,
) -> dict[str, Any]:
    with get_connection() as db:
        episode = _owned_episode(
            db, user_id, episode_id, verified_project_id=verified_project_id
        )
        task = _task(db, episode_id, task_id)
        timeline = _selected_timeline(db, user_id, episode_id, task)
    duration = float(episode["duration"] or task["source_duration"] or 0)
    readiness = audio_localization_service.provider_free_finalization_readiness(episode_id, duration)
    if not readiness.get("ready"):
        raise SelfServiceClosureError("FORMAL_AUDIO_ASSETS_NOT_READY", "The current formal overseas mix is not ready.")
    formal_mix = readiness.get("formal_mix") or {}
    audio_path = _file(formal_mix.get("audio_path"), "FORMAL_AUDIO_MIX_MISSING")
    source_path = _file(episode["video_path"], "EPISODE_SOURCE_MISSING")
    output_id = f"final-{uuid.uuid4().hex[:20]}"
    output_dir = OUTPUTS_DIR / episode["project_id"] / episode_id / "final-outputs" / output_id
    output_path = output_dir / f"{output_id}.mp4"
    work_dir = output_dir / "work"
    snapshot = [{key: item[key] for key in (
        "unit_id", "sequence", "start_time", "end_time", "selected_source_type", "selected_attempt_id", "source_sha256"
    )} for item in timeline]
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            """INSERT INTO episode_final_outputs
               (id,user_id,project_id,episode_id,task_id,status,unit_selection_snapshot_json,
                audio_output_id,audio_revision,subtitle_revision,created_at,updated_at)
               VALUES(?,?,?,?,?,'rendering',?,?,?,?,?,?)""",
            (output_id, user_id, episode["project_id"], episode_id, task["id"], _json(snapshot),
             str(formal_mix.get("dubbed_output_id") or formal_mix.get("task_id") or "formal-mix"),
             str(readiness["audio_revision"]), str(readiness["subtitle_revision"]), timestamp, timestamp),
        )
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        visual_paths: list[Path] = []
        actual_durations: list[float] = []
        target_durations: list[float] = []
        for item in timeline:
            if item["selected_source_type"] == ORIGINAL:
                clip_path = work_dir / f"unit-{int(item['sequence']):03d}-original.mp4"
                clip = media_service.trim_video(source_path, clip_path, item["start_time"], item["end_time"])
                visual_paths.append(clip_path)
                actual_durations.append(float(clip["duration"]))
            else:
                visual_paths.append(item["path"])
                actual_durations.append(float(item["actual_duration"]))
            target_durations.append(float(item["target_duration"]))
        subtitle_context = audio_localization_service.current_subtitle_render_context(episode_id)
        translated = list(subtitle_context["segments"])
        common = media_service.common_video_resolution(visual_paths)
        cues = audio_localization_service.build_renderable_subtitle_cues(
            translated,
            int(common["width"]),
            int(common["height"]),
            target_language=str(subtitle_context["target_language"]),
        )
        overlays = audio_localization_service._write_overseas_overlays(
            translated, work_dir / "subtitle-overlays", int(common["width"]), int(common["height"]),
            hide_original_subtitles=True,
            original_subtitle_region_start_percent=(
                audio_localization_service.DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT
            ),
            subtitle_position="low", cues=cues,
        )
        media_service.render_localized_xmax_timeline(
            visual_paths, target_durations, actual_durations, audio_path, output_path, overlays,
            hide_original_subtitles=True,
            original_subtitle_region_start_percent=(
                audio_localization_service.DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT
            ),
        )
        metadata = media_service.probe_media_metadata(output_path)
        decode = media_service.decode_media_to_null(output_path)
        if metadata.get("video_codec") != "h264" or metadata.get("audio_codec") != "aac" or not decode["passed"]:
            raise SelfServiceClosureError("FINAL_OUTPUT_VALIDATION_FAILED", "The final MP4 failed codec or decode validation.")
        completed = now_iso()
        output_url = storage_asset_url(output_path) or f"/outputs/{output_path.relative_to(OUTPUTS_DIR).as_posix()}"
        with get_connection() as db:
            db.execute(
                """UPDATE episode_final_outputs SET status='ready',output_path=?,output_url=?,output_sha256=?,
                   file_size=?,duration=?,width=?,height=?,fps=?,video_codec=?,audio_codec=?,updated_at=?,completed_at=?
                   WHERE id=? AND user_id=?""",
                (str(output_path), output_url, _sha256_file(output_path), output_path.stat().st_size,
                 metadata["duration"], metadata["width"], metadata["height"], metadata["fps"],
                 metadata["video_codec"], metadata["audio_codec"], completed, completed, output_id, user_id),
            )
    except Exception as error:
        with get_connection() as db:
            db.execute(
                "UPDATE episode_final_outputs SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND user_id=?",
                (getattr(error, "code", "FINAL_RENDER_FAILED"), str(error)[:1000], now_iso(), output_id, user_id),
            )
        raise
    return get_final_output(user_id, output_id)


def create_visual_final_output(
    user_id: str,
    episode_id: str,
    task_id: str | None = None,
    *,
    verified_project_id: str | None = None,
) -> dict[str, Any]:
    with get_connection() as db:
        episode = _owned_episode(
            db, user_id, episode_id, verified_project_id=verified_project_id
        )
        task = _task(db, episode_id, task_id)
        timeline = _selected_timeline(db, user_id, episode_id, task)
    source_path = _file(episode["video_path"], "EPISODE_SOURCE_MISSING")
    source_sha256 = _sha256_file(source_path)
    snapshot = [{key: item[key] for key in (
        "unit_id", "sequence", "start_time", "end_time", "selected_source_type", "selected_attempt_id", "source_sha256"
    )} for item in timeline]
    fingerprint = hashlib.sha256(_json({
        "task_id": task["id"],
        "source_sha256": source_sha256,
        "unit_selection_snapshot": snapshot,
    }).encode("utf-8")).hexdigest()
    audio_revision = f"{VISUAL_ONLY_REVISION_PREFIX}{fingerprint}"
    with get_connection() as db:
        existing = db.execute(
            """SELECT id,output_path FROM episode_final_outputs
               WHERE user_id=? AND episode_id=? AND task_id=? AND status='ready' AND audio_revision=?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, episode_id, task["id"], audio_revision),
        ).fetchone()
    if existing and Path(str(existing["output_path"] or "")).is_file():
        return get_final_output(user_id, existing["id"])

    output_id = f"visual-final-{uuid.uuid4().hex[:20]}"
    output_dir = OUTPUTS_DIR / episode["project_id"] / episode_id / "final-outputs" / output_id
    output_path = output_dir / f"{output_id}.mp4"
    work_dir = output_dir / "work"
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            """INSERT INTO episode_final_outputs
               (id,user_id,project_id,episode_id,task_id,status,unit_selection_snapshot_json,
                audio_output_id,audio_revision,subtitle_revision,created_at,updated_at)
               VALUES(?,?,?,?,?,'rendering',?,?,?,?,?,?)""",
            (
                output_id, user_id, episode["project_id"], episode_id, task["id"], _json(snapshot),
                f"source-audio:{source_sha256}", audio_revision, "none", timestamp, timestamp,
            ),
        )
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        visual_paths: list[Path] = []
        actual_durations: list[float] = []
        target_durations: list[float] = []
        for item in timeline:
            if item["selected_source_type"] == ORIGINAL:
                clip_path = work_dir / f"unit-{int(item['sequence']):03d}-original.mp4"
                clip = media_service.trim_video(source_path, clip_path, item["start_time"], item["end_time"])
                visual_paths.append(clip_path)
                actual_durations.append(float(clip["duration"]))
            else:
                visual_paths.append(item["path"])
                actual_durations.append(float(item["actual_duration"]))
            target_durations.append(float(item["target_duration"]))
        source_metadata = media_service.probe_media_metadata(source_path)
        audio_path = source_path
        if not bool(source_metadata.get("has_audio")):
            audio_path = _write_visual_only_silence(
                work_dir / "source-audio-silence.wav",
                sum(target_durations),
            )
        media_service.render_localized_xmax_timeline(
            visual_paths,
            target_durations,
            actual_durations,
            audio_path,
            output_path,
            [],
            hide_original_subtitles=False,
        )
        metadata = media_service.probe_media_metadata(output_path)
        decode = media_service.decode_media_to_null(output_path)
        if metadata.get("video_codec") != "h264" or metadata.get("audio_codec") != "aac" or not decode["passed"]:
            raise SelfServiceClosureError("VISUAL_FINAL_OUTPUT_VALIDATION_FAILED", "The visual-only MP4 failed codec or decode validation.")
        completed = now_iso()
        output_url = storage_asset_url(output_path) or f"/outputs/{output_path.relative_to(OUTPUTS_DIR).as_posix()}"
        with get_connection() as db:
            db.execute(
                """UPDATE episode_final_outputs SET status='ready',output_path=?,output_url=?,output_sha256=?,
                   file_size=?,duration=?,width=?,height=?,fps=?,video_codec=?,audio_codec=?,updated_at=?,completed_at=?
                   WHERE id=? AND user_id=?""",
                (
                    str(output_path), output_url, _sha256_file(output_path), output_path.stat().st_size,
                    metadata["duration"], metadata["width"], metadata["height"], metadata["fps"],
                    metadata["video_codec"], metadata["audio_codec"], completed, completed, output_id, user_id,
                ),
            )
    except Exception as error:
        with get_connection() as db:
            db.execute(
                "UPDATE episode_final_outputs SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND user_id=?",
                (getattr(error, "code", "VISUAL_FINAL_RENDER_FAILED"), str(error)[:1000], now_iso(), output_id, user_id),
            )
        raise
    return get_final_output(user_id, output_id)


def get_final_output(user_id: str, output_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT * FROM episode_final_outputs WHERE id=? AND user_id=?",
            (output_id, user_id),
        ).fetchone()
    if not row:
        raise SelfServiceClosureError("FINAL_OUTPUT_NOT_FOUND", "Final output not found.", status_code=404)
    item = dict(row)
    item.pop("output_path", None)
    item["unit_selection_snapshot"] = json.loads(item.pop("unit_selection_snapshot_json") or "[]")
    item["output_mode"] = _output_mode(item)
    return item


def final_output_path(user_id: str, output_id: str) -> Path:
    with get_connection() as db:
        row = db.execute(
            "SELECT output_path,status FROM episode_final_outputs WHERE id=? AND user_id=?",
            (output_id, user_id),
        ).fetchone()
    if not row or row["status"] != "ready":
        raise SelfServiceClosureError("FINAL_OUTPUT_NOT_FOUND", "Final output not found.", status_code=404)
    return _file(row["output_path"], "FINAL_OUTPUT_FILE_MISSING")
