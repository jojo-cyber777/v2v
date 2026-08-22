"""Persistent orchestration for the one-click overseas Episode workflow."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..database import get_connection, json_dump, json_load, now_iso, row_to_dict
from . import audio_localization_service
from .xmax_visual_result_validity import evaluate_visual_result, segment_preserved_by_user

STAGES = (
    "preparing",
    "analyzing_shots",
    "transforming_shots",
    "merging_visual",
    "separating_audio",
    "adapting_dialogue",
    "rendering_subtitles",
    "muxing_final",
    "completed",
)
STAGE_PROGRESS = {
    "preparing": 2,
    "analyzing_shots": 8,
    "transforming_shots": 12,
    "merging_visual": 60,
    "separating_audio": 68,
    "adapting_dialogue": 76,
    "rendering_subtitles": 84,
    "muxing_final": 92,
    "completed": 100,
    "failed": 100,
}
ACTIVE_VISUAL_STATUSES = {
    "pending",
    "running",
    "local_ready",
    "uploading",
    "interrupted",
    "failed",
    "merge_pending",
    "merging",
    "merge_failed",
}
ACTIVE_AUDIO_STATUSES = {
    "pending",
    "preparing_assets",
    "building_tts_timeline",
    "rendering_subtitles",
    "separating_audio",
    "mixing_audio",
    "muxing_final",
}


class OverseasEpisodeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    episode = row_to_dict(row)
    if not episode:
        raise OverseasEpisodeError("EPISODE_NOT_FOUND", "Episode not found.")
    return episode


def _task_row(task_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        row = db.execute("SELECT * FROM overseas_episode_tasks WHERE id=?", (task_id,)).fetchone()
    return row_to_dict(row)


def _valid_file(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    return path.is_file() and path.stat().st_size > 0


def _segment_preserved_by_user(row: Any) -> bool:
    return segment_preserved_by_user(row)


def _visual_segment_rows(task_id: str) -> list[Any]:
    with get_connection() as db:
        return db.execute(
            "SELECT status, execution_status, quality_status, shot_type, preserve_original, "
            "use_source_for_merge, mapping_status, source_path, output_path, diagnostics_json "
            "FROM xmax_thirty_second_segments WHERE task_id=? ORDER BY sequence",
            (task_id,),
        ).fetchall()


def _visual_task_ready(task_id: str) -> bool:
    return _visual_unit_readiness(task_id)["visual_ready"]


def _visual_unit_readiness(task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {
            "visual_ready": False,
            "can_merge_final": False,
            "invalid_segment_ids": [],
            "automatic_fallback_count": 0,
            "real_output_count": 0,
            "explicit_source_count": 0,
            "total_units": 0,
        }
    with get_connection() as db:
        task = db.execute("SELECT * FROM xmax_thirty_second_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return {
            "visual_ready": False,
            "can_merge_final": False,
            "invalid_segment_ids": [],
            "automatic_fallback_count": 0,
            "real_output_count": 0,
            "explicit_source_count": 0,
            "total_units": 0,
        }
    evaluation = evaluate_visual_result(task, _visual_segment_rows(task_id), valid_file=_valid_file)
    visual_ready = bool(
        evaluation["total_units"] > 0
        and not evaluation["invalid_segment_ids"]
        and evaluation["automatic_fallback_count"] == 0
    )
    return {
        "visual_ready": visual_ready,
        "can_merge_final": visual_ready,
        "invalid_segment_ids": list(evaluation["invalid_segment_ids"]),
        "automatic_fallback_count": int(evaluation["automatic_fallback_count"]),
        "real_output_count": int(evaluation["real_output_count"]),
        "explicit_source_count": int(evaluation["explicit_source_count"]),
        "total_units": int(evaluation["total_units"]),
    }


def _visual_result_evaluation(task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {
            "result_valid": False,
            "classification": "missing_visual_task",
            "source_only": False,
            "real_output_count": 0,
            "explicit_source_count": 0,
            "automatic_fallback_count": 0,
            "invalid_segment_ids": [],
            "total_units": 0,
        }
    with get_connection() as db:
        task = db.execute("SELECT * FROM xmax_thirty_second_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return {
            "result_valid": False,
            "classification": "missing_visual_task",
            "source_only": False,
            "real_output_count": 0,
            "explicit_source_count": 0,
            "automatic_fallback_count": 0,
            "invalid_segment_ids": [],
            "total_units": 0,
        }
    return evaluate_visual_result(task, _visual_segment_rows(task_id), valid_file=_valid_file)


def _latest_visual_revision_id(episode_id: str) -> str | None:
    with get_connection() as db:
        row = db.execute(
            "SELECT id FROM xmax_thirty_second_tasks WHERE episode_id=? "
            "ORDER BY created_at DESC, updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    return str(row["id"]) if row else None


def _visual_task_resumable(task_id: str | None) -> bool:
    if not task_id:
        return False
    with get_connection() as db:
        task = db.execute(
            "SELECT status FROM xmax_thirty_second_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    return bool(task and task["status"] in ACTIVE_VISUAL_STATUSES and not _visual_task_ready(task_id))


def _visual_counts(task_id: str | None) -> dict[str, int]:
    if not task_id:
        return {"completed": 0, "total": 0, "fallback": 0, "failed": 0, "awaiting": 0}
    rows = _visual_segment_rows(task_id)
    transform_rows = [
        row
        for row in rows
        if row["shot_type"] == "single_character" and not bool(row["preserve_original"])
    ]
    completed = sum(
        1
        for row in transform_rows
        if (
            (row["execution_status"] or row["status"]) == "completed"
            and row["quality_status"] == "approved"
            and _valid_file(row["output_path"])
        )
    )
    fallback = sum(1 for row in transform_rows if _segment_preserved_by_user(row))
    failed = sum(
        1
        for row in transform_rows
        if not _segment_preserved_by_user(row)
        and (row["execution_status"] or row["status"]) == "failed"
    )
    return {
        "completed": completed,
        "total": len(transform_rows),
        "fallback": fallback,
        "failed": failed,
        "awaiting": max(0, len(transform_rows) - completed - fallback - failed),
    }


def _persisted_visual_counts(task_id: str | None) -> dict[str, int]:
    """Summarize saved Unit states without touching media files."""
    if not task_id:
        return {"completed": 0, "total": 0, "fallback": 0, "failed": 0, "awaiting": 0}
    rows = _visual_segment_rows(task_id)
    transform_rows = [
        row
        for row in rows
        if row["shot_type"] == "single_character" and not bool(row["preserve_original"])
    ]
    completed = sum(
        1
        for row in transform_rows
        if (
            (row["execution_status"] or row["status"]) == "completed"
            and row["quality_status"] == "approved"
            and bool(row["output_path"])
        )
    )
    fallback = sum(1 for row in transform_rows if _segment_preserved_by_user(row))
    failed = sum(
        1
        for row in transform_rows
        if not _segment_preserved_by_user(row)
        and (row["execution_status"] or row["status"]) == "failed"
    )
    return {
        "completed": completed,
        "total": len(transform_rows),
        "fallback": fallback,
        "failed": failed,
        "awaiting": max(0, len(transform_rows) - completed - fallback - failed),
    }


def _diagnostics(row: dict[str, Any]) -> dict[str, Any]:
    value = json_load(row.get("diagnostics_json"), {})
    return value if isinstance(value, dict) else {}


def _append_event(
    diagnostics: dict[str, Any],
    stage: str,
    *,
    message: str = "",
    reused: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = list(diagnostics.get("events") or [])
    events.append(
        {
            "stage": stage,
            "timestamp": now_iso(),
            "message": message,
            "reused": reused,
            "details": details or {},
        }
    )
    return {**diagnostics, "events": events[-100:]}


def _update_task(task_id: str, **values: Any) -> None:
    allowed = {
        "status",
        "stage",
        "progress",
        "progress_message",
        "visual_task_id",
        "audio_task_id",
        "transform_completed",
        "transform_total",
        "fallback_count",
        "final_output_path",
        "final_output_url",
        "error_code",
        "error_message",
        "diagnostics_json",
        "completed_at",
    }
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"Unsupported overseas task fields: {sorted(invalid)}")
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in values)
    with get_connection() as db:
        db.execute(
            f"UPDATE overseas_episode_tasks SET {assignments} WHERE id=?",
            (*values.values(), task_id),
        )


def _latest_completed_audio(
    episode_id: str,
    finalization: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM audio_localization_tasks WHERE episode_id=? AND status='completed' "
            "ORDER BY COALESCE(completed_at, updated_at, created_at) DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        item = row_to_dict(row)
        source_task_id = str((item or {}).get("source_task_id") or "")
        if (
            item
            and item.get("output_url")
            and _valid_file(item.get("output_path"))
            and (not source_task_id or _visual_task_ready(source_task_id))
            and (finalization is None or _completed_audio_matches_current_inputs(item, finalization))
        ):
            return item
    return None


def _latest_visual(episode_id: str, *, completed_only: bool) -> dict[str, Any] | None:
    status_filter = "status='completed'" if completed_only else (
        "status IN (" + ",".join("?" for _ in ACTIVE_VISUAL_STATUSES) + ")"
    )
    params: tuple[Any, ...] = (episode_id,) if completed_only else (
        episode_id,
        *sorted(ACTIVE_VISUAL_STATUSES),
    )
    with get_connection() as db:
        rows = db.execute(
            f"SELECT * FROM xmax_thirty_second_tasks WHERE episode_id=? AND {status_filter} "
            "ORDER BY COALESCE(completed_at, updated_at, created_at) DESC",
            params,
        ).fetchall()
    for row in rows:
        item = row_to_dict(row)
        if not item:
            continue
        if completed_only:
            if (
                item.get("final_output_url")
                and _valid_file(item.get("final_output_path"))
                and _visual_task_ready(item["id"])
            ):
                return item
            continue
        with get_connection() as db:
            segments = db.execute(
                "SELECT shot_type, mapping_status, preserve_original, source_path "
                "FROM xmax_thirty_second_segments "
                "WHERE task_id=? ORDER BY sequence",
                (item["id"],),
            ).fetchall()
        if (
            segments
            and all(str(segment["shot_type"] or "unknown") != "unknown" for segment in segments)
            and all(
                (
                    segment["shot_type"] == "single_character"
                    and segment["mapping_status"] == "manual_confirmed"
                )
                or (
                    segment["shot_type"] in {"multi_character", "no_character", "transition"}
                    and segment["mapping_status"] == "preserve_original"
                    and bool(segment["preserve_original"])
                )
                for segment in segments
            )
        ):
            return item
    return None


def _latest_active_audio(episode_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in ACTIVE_AUDIO_STATUSES)
    with get_connection() as db:
        row = db.execute(
            f"SELECT * FROM audio_localization_tasks WHERE episode_id=? AND status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id, *sorted(ACTIVE_AUDIO_STATUSES)),
        ).fetchone()
    return row_to_dict(row)


def _audio_stage(status: str) -> str:
    return {
        "pending": "separating_audio",
        "preparing_assets": "separating_audio",
        "building_tts_timeline": "adapting_dialogue",
        "rendering_subtitles": "rendering_subtitles",
        "separating_audio": "separating_audio",
        "mixing_audio": "muxing_final",
        "muxing_final": "muxing_final",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "failed",
    }.get(status, "separating_audio")


def _stage_not_before(current: str, proposed: str) -> str:
    if current not in STAGES or proposed not in STAGES:
        return proposed
    return STAGES[max(STAGES.index(current), STAGES.index(proposed))]


def _sync_linked_audio(row: dict[str, Any]) -> dict[str, Any]:
    audio_task_id = row.get("audio_task_id")
    if not audio_task_id or row.get("status") != "running":
        return row
    try:
        audio = audio_localization_service.get_audio_localization_task(audio_task_id)
    except audio_localization_service.AudioLocalizationError:
        return row
    diagnostics = _diagnostics(row)
    if diagnostics.get("audio_created_this_run"):
        diagnostics["provider_calls_this_run"] = int(audio.get("provider_calls") or 0)
        diagnostics["translation_provider_calls_this_run"] = int(
            audio.get("translation_provider_calls") or 0
        )
        diagnostics["tts_provider_calls_this_run"] = int(audio.get("tts_provider_calls") or 0)
    linked_visual_ready = not row.get("visual_task_id") or _visual_task_ready(str(row["visual_task_id"]))
    if audio["status"] == "completed" and audio.get("output_url") and _valid_file(audio.get("output_path")) and linked_visual_ready:
        diagnostics = _append_event(
            diagnostics,
            "completed",
            message="The final overseas MP4 was persisted.",
            details={"audio_task_id": audio_task_id},
        )
        _update_task(
            row["id"],
            status="completed",
            stage="completed",
            progress=100,
            progress_message="Overseas version completed.",
            final_output_path=audio["output_path"],
            final_output_url=audio["output_url"],
            error_code=None,
            error_message=None,
            diagnostics_json=json_dump(diagnostics),
            completed_at=audio.get("completed_at") or now_iso(),
        )
        return _task_row(row["id"]) or row
    if audio["status"] in {"failed", "interrupted"}:
        diagnostics = _append_event(
            diagnostics,
            "failed",
            message=audio.get("error_message") or "Audio Localization failed.",
            details={
                "audio_task_id": audio_task_id,
                "error_code": audio.get("error_code"),
            },
        )
        _update_task(
            row["id"],
            status="failed",
            stage="failed",
            progress=100,
            progress_message="Overseas version failed during audio localization.",
            error_code=audio.get("error_code") or "AUDIO_LOCALIZATION_FAILED",
            error_message=audio.get("error_message") or "Audio Localization failed.",
            diagnostics_json=json_dump(diagnostics),
            completed_at=now_iso(),
        )
        return _task_row(row["id"]) or row
    proposed = _audio_stage(str(audio["status"]))
    stage = _stage_not_before(str(row["stage"]), proposed)
    progress = max(
        int(row.get("progress") or 0),
        STAGE_PROGRESS.get(stage, 68),
        min(99, int(audio.get("progress") or 0)),
    )
    if stage != row["stage"] or progress != row["progress"] or audio.get("progress_message") != row.get("progress_message"):
        diagnostics = _append_event(
            diagnostics,
            stage,
            message=str(audio.get("progress_message") or ""),
            details={"audio_status": audio["status"], "audio_progress": audio.get("progress")},
        )
        _update_task(
            row["id"],
            stage=stage,
            progress=progress,
            progress_message=str(audio.get("progress_message") or ""),
            diagnostics_json=json_dump(diagnostics),
        )
        return _task_row(row["id"]) or row
    return row


def _sync_visual_ready_state(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") != "running" or not row.get("visual_task_id"):
        return row
    if row.get("stage") not in {"analyzing_shots", "transforming_shots", "merging_visual", "failed"}:
        return row
    readiness = _visual_unit_readiness(str(row["visual_task_id"]))
    if not readiness["visual_ready"] or row.get("audio_task_id"):
        return row
    if row.get("stage") == "merging_visual" and row.get("progress_message") == "Visual transformation completed. Final merge is ready.":
        return row
    counts = _visual_counts(str(row["visual_task_id"]))
    diagnostics = _append_event(
        _diagnostics(row),
        "visual_ready",
        message="All visual units are valid and ready for the explicit final merge.",
        reused=True,
        details={
            "visual_task_id": row["visual_task_id"],
            "visual_ready": True,
            **counts,
        },
    )
    _update_task(
        row["id"],
        stage="merging_visual",
        progress=STAGE_PROGRESS["merging_visual"],
        progress_message="Visual transformation completed. Final merge is ready.",
        transform_completed=counts["completed"],
        transform_total=counts["total"],
        fallback_count=counts["fallback"],
        error_code=None,
        error_message=None,
        diagnostics_json=json_dump(diagnostics),
    )
    return _task_row(row["id"]) or row


def _provider_free_finalization_readiness(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    duration = float(episode.get("duration") or 0)
    try:
        return audio_localization_service.audio_assets_readiness(episode_id, duration)
    except (
        audio_localization_service.AudioLocalizationError,
        audio_localization_service.duration_constrained_tts_service.DurationConstrainedTTSError,
        sqlite3.Error,
        OSError,
    ) as error:
        return {
            "ready": False,
            "can_auto_prepare": False,
            "tts_ready": False,
            "stems_ready": False,
            "formal_mix_ready": False,
            "dubbed_output_ready": False,
            "audio_asset_state": "blocked",
            "audio_revision": None,
            "subtitle_revision": None,
            "background_audio_revision": None,
            "mix_settings_revision": None,
            "auto_prepare_blockers": [],
            "blockers": [
                {
                    "asset": "audio_localization",
                    "segment_id": None,
                    "error_code": getattr(error, "code", "FINAL_ASSET_PREFLIGHT_FAILED"),
                    "message": str(error),
                }
            ],
        }


def _completed_audio_matches_current_inputs(audio: dict[str, Any], finalization: dict[str, Any]) -> bool:
    settings = json_load(audio.get("settings_json"), {})
    stem_manifest = finalization.get("stem_manifest") or {}
    return bool(
        _valid_file(audio.get("output_path"))
        and settings.get("audio_revision") == finalization.get("audio_revision")
        and settings.get("subtitle_revision") == finalization.get("subtitle_revision")
        and settings.get("mix_settings_revision") == finalization.get("mix_settings_revision")
        and settings.get("source_media_sha256") == stem_manifest.get("source_media_sha256")
    )


def _parsed_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _latest_ready_final_output(episode_id: str) -> dict[str, Any] | None:
    try:
        with get_connection() as db:
            row = db.execute(
                """SELECT id, completed_at, updated_at, created_at
                   FROM episode_final_outputs
                   WHERE episode_id=? AND status='ready' AND output_url IS NOT NULL
                   ORDER BY COALESCE(completed_at, updated_at, created_at) DESC
                   LIMIT 1""",
                (episode_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row_to_dict(row)


def _current_response_state(row: dict[str, Any], *, visual_ready: bool) -> dict[str, Any]:
    error_code = row.get("error_code")
    error_message = row.get("error_message")
    latest_final = _latest_ready_final_output(row["episode_id"])
    final_ready_at = _parsed_timestamp(
        (latest_final or {}).get("completed_at")
        or (latest_final or {}).get("updated_at")
        or (latest_final or {}).get("created_at")
    )
    task_updated_at = _parsed_timestamp(row.get("updated_at"))
    if final_ready_at and task_updated_at and final_ready_at >= task_updated_at:
        return {
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "progress_message": "Final output is ready.",
            "completed_at": (latest_final or {}).get("completed_at") or row.get("completed_at"),
            "error_code": None,
            "error_message": None,
        }

    if error_code == "LOCALIZATION_TRANSLATION_REQUIRED":
        try:
            audio_localization_service.current_subtitle_render_context(row["episode_id"])
        except audio_localization_service.AudioLocalizationError:
            pass
        else:
            return {
                "status": "running" if visual_ready and row.get("status") == "failed" else row["status"],
                "stage": "merging_visual" if visual_ready and row.get("status") == "failed" else row["stage"],
                "progress": max(int(row.get("progress") or 0), STAGE_PROGRESS["merging_visual"])
                if visual_ready and row.get("status") == "failed"
                else int(row.get("progress") or 0),
                "progress_message": "Visual transformation completed. Final merge is ready."
                if visual_ready and row.get("status") == "failed"
                else row.get("progress_message") or "",
                "completed_at": row.get("completed_at"),
                "error_code": None,
                "error_message": None,
            }
    return {
        "status": row["status"],
        "stage": row["stage"],
        "progress": int(row.get("progress") or 0),
        "progress_message": row.get("progress_message") or "",
        "completed_at": row.get("completed_at"),
        "error_code": error_code,
        "error_message": error_message,
    }


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    diagnostics = _diagnostics(row)
    visual_counts = _visual_counts(row.get("visual_task_id"))
    has_visual_segments = visual_counts["total"] > 0
    visual_evaluation = _visual_result_evaluation(row.get("visual_task_id"))
    current_visual_revision_id = _latest_visual_revision_id(row["episode_id"])
    visual_revision_current = bool(
        row.get("visual_task_id") and row.get("visual_task_id") == current_visual_revision_id
    )
    result_valid = bool(visual_evaluation["result_valid"] and visual_revision_current)
    visual_resumable = _visual_task_resumable(row.get("visual_task_id"))
    visual_readiness = _visual_unit_readiness(row.get("visual_task_id"))
    response_state = _current_response_state(
        row,
        visual_ready=bool(visual_readiness["visual_ready"]),
    )
    finalization = (
        _provider_free_finalization_readiness(row["episode_id"])
        if visual_readiness["visual_ready"] and row.get("status") == "running"
        else {
            "ready": False,
            "can_auto_prepare": False,
            "tts_ready": False,
            "stems_ready": False,
            "formal_mix_ready": False,
            "dubbed_output_ready": False,
            "audio_asset_state": "blocked",
            "audio_revision": None,
            "subtitle_revision": None,
            "background_audio_revision": None,
            "mix_settings_revision": None,
            "auto_prepare_blockers": [],
            "blockers": [],
        }
    )
    return {
        "task_id": row["id"],
        "project_id": row["project_id"],
        "episode_id": row["episode_id"],
        "status": response_state["status"],
        "stage": response_state["stage"],
        "progress": response_state["progress"],
        "progress_message": response_state["progress_message"],
        "visual_task_id": row.get("visual_task_id"),
        "audio_task_id": row.get("audio_task_id"),
        "transform_completed": visual_counts["completed"] if has_visual_segments else int(row.get("transform_completed") or 0),
        "transform_total": visual_counts["total"] if has_visual_segments else int(row.get("transform_total") or 0),
        "fallback_count": visual_counts["fallback"] if has_visual_segments else int(row.get("fallback_count") or 0),
        "visual_status_counts": {
            "transformed": visual_counts["completed"],
            "failed": visual_counts["failed"],
            "awaiting_user_action": visual_counts["awaiting"],
            "source_by_user": visual_counts["fallback"],
        },
        "final_output_url": row.get("final_output_url"),
        "result_valid": result_valid,
        "result_classification": (
            visual_evaluation["classification"]
            if visual_revision_current
            else "stale_visual_revision"
        ),
        "current_visual_revision_id": current_visual_revision_id,
        "visual_ready": bool(visual_readiness["visual_ready"]),
        "can_merge_final": bool(
            visual_readiness["can_merge_final"]
            and row.get("status") == "running"
            and not row.get("audio_task_id")
            and not row.get("final_output_url")
        ),
        "final_assets_ready": bool(finalization["ready"]),
        "can_prepare_final_audio": bool(finalization.get("can_auto_prepare")),
        "audio_asset_state": finalization.get("audio_asset_state") or "blocked",
        "final_merge_blockers": list(finalization["blockers"]),
        "audio_assets_to_prepare": list(finalization.get("auto_prepare_blockers") or []),
        "audio_revision": finalization.get("audio_revision"),
        "subtitle_revision": finalization.get("subtitle_revision"),
        "background_audio_revision": finalization.get("background_audio_revision"),
        "mix_settings_revision": finalization.get("mix_settings_revision"),
        "error_code": response_state["error_code"],
        "error_message": response_state["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": response_state["completed_at"],
        "requires_client_visual_execution": (
            visual_resumable
            and row["stage"] in {"analyzing_shots", "transforming_shots", "merging_visual", "failed"}
        ),
        "diagnostics": diagnostics,
        "provider_calls_this_run": int(diagnostics.get("provider_calls_this_run") or 0),
        "xmax_sessions_this_run": int(diagnostics.get("xmax_sessions_this_run") or 0),
    }


def _persisted_status_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return the orchestration status without probing media or audio assets."""
    diagnostics = _diagnostics(row)
    counts = _persisted_visual_counts(row.get("visual_task_id"))
    completed = counts["completed"]
    total = counts["total"]
    fallback = counts["fallback"]
    visual_ready = bool(total > 0 and completed + fallback >= total)
    response_state = _current_response_state(row, visual_ready=visual_ready)
    current_visual_revision_id = _latest_visual_revision_id(row["episode_id"])
    visual_revision_current = bool(
        row.get("visual_task_id") and row.get("visual_task_id") == current_visual_revision_id
    )
    completed_task = row.get("status") == "completed"
    final_output_ready = bool(completed_task and row.get("final_output_url"))
    return {
        "task_id": row["id"],
        "project_id": row["project_id"],
        "episode_id": row["episode_id"],
        "status": response_state["status"],
        "stage": response_state["stage"],
        "progress": response_state["progress"],
        "progress_message": response_state["progress_message"],
        "visual_task_id": row.get("visual_task_id"),
        "audio_task_id": row.get("audio_task_id"),
        "transform_completed": completed,
        "transform_total": total,
        "fallback_count": fallback,
        "visual_status_counts": {
            "transformed": completed,
            "failed": counts["failed"],
            "awaiting_user_action": counts["awaiting"],
            "source_by_user": fallback,
        },
        "final_output_url": row.get("final_output_url"),
        "result_valid": bool(final_output_ready and visual_revision_current),
        "result_classification": (
            "valid" if final_output_ready and visual_revision_current
            else "stale_visual_revision" if row.get("visual_task_id") and not visual_revision_current
            else "incomplete"
        ),
        "current_visual_revision_id": current_visual_revision_id,
        "visual_ready": visual_ready,
        "can_merge_final": bool(
            visual_ready
            and row.get("status") == "running"
            and not row.get("audio_task_id")
            and not row.get("final_output_url")
        ),
        # Full audio readiness remains an explicit finalization check. Status polling
        # must not hash source media or invoke ffprobe across TTS/mix assets.
        "final_assets_ready": completed_task,
        "can_prepare_final_audio": bool(visual_ready and row.get("status") == "running"),
        "audio_asset_state": "ready" if completed_task else "validation_deferred",
        "final_merge_blockers": [],
        "audio_assets_to_prepare": [],
        "audio_revision": None,
        "subtitle_revision": None,
        "background_audio_revision": None,
        "mix_settings_revision": None,
        "error_code": response_state["error_code"],
        "error_message": response_state["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": response_state["completed_at"],
        "requires_client_visual_execution": bool(
            row.get("status") == "running"
            and row.get("visual_task_id")
            and not visual_ready
            and row.get("stage") in {"analyzing_shots", "transforming_shots", "merging_visual", "failed"}
        ),
        "diagnostics": diagnostics,
        "provider_calls_this_run": int(diagnostics.get("provider_calls_this_run") or 0),
        "xmax_sessions_this_run": int(diagnostics.get("xmax_sessions_this_run") or 0),
    }


def get_task(task_id: str) -> dict[str, Any]:
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    row = _sync_visual_ready_state(row)
    row = _sync_linked_audio(row)
    return _payload(row)


def get_task_status(task_id: str) -> dict[str, Any]:
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    return _persisted_status_payload(row)


def get_latest_task(episode_id: str) -> dict[str, Any] | None:
    _episode(episode_id)
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM overseas_episode_tasks WHERE episode_id=? ORDER BY created_at DESC",
            (episode_id,),
        ).fetchall()
    items = [row_to_dict(row) for row in rows]
    current_visual_revision_id = _latest_visual_revision_id(episode_id)
    for item in items:
        if not item or item.get("visual_task_id") != current_visual_revision_id:
            continue
        synced = _sync_visual_ready_state(_sync_linked_audio(item))
        payload = _payload(synced)
        if payload["visual_ready"] or payload["requires_client_visual_execution"]:
            return payload
    for item in items:
        if not item:
            continue
        synced = _sync_linked_audio(item)
        payload = _payload(synced)
        if payload["status"] == "completed" and payload["result_valid"]:
            return payload
    return None


def _insert_task(
    episode: dict[str, Any],
    *,
    stage: str,
    status: str = "running",
    visual_task_id: str | None = None,
    audio_task_id: str | None = None,
    final_output_path: str | None = None,
    final_output_url: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    task_id = f"overseas-episode-{uuid.uuid4().hex[:16]}"
    timestamp = now_iso()
    counts = _visual_counts(visual_task_id)
    progress = STAGE_PROGRESS[stage]
    with get_connection() as db:
        db.execute(
            """
            INSERT INTO overseas_episode_tasks (
                id, project_id, episode_id, status, stage, progress, progress_message,
                visual_task_id, audio_task_id, transform_completed, transform_total,
                fallback_count, final_output_path, final_output_url, error_code,
                error_message, diagnostics_json, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                episode["project_id"],
                episode["id"],
                status,
                stage,
                progress,
                (
                    "Overseas version completed."
                    if status == "completed"
                    else "Overseas version failed."
                    if status == "failed"
                    else "Preparing the Episode workflow."
                ),
                visual_task_id,
                audio_task_id,
                counts["completed"],
                counts["total"],
                counts["fallback"],
                final_output_path,
                final_output_url,
                error_code,
                error_message,
                json_dump(diagnostics or {}),
                timestamp,
                timestamp,
                timestamp if status in {"completed", "failed"} else None,
            ),
        )
    return get_task(task_id)


def _completed_reuse_diagnostics(audio: dict[str, Any]) -> dict[str, Any]:
    settings = json_load(audio.get("settings_json"), {})
    duration_fit = settings.get("duration_fit") if isinstance(settings, dict) else {}
    duration_fit = duration_fit if isinstance(duration_fit, dict) else {}
    diagnostics: dict[str, Any] = {
        "events": [],
        "reused_assets": {
            "visual": True,
            "audio_localization": True,
            "demucs_stems": True,
            "duration_fitted_tts": duration_fit.get("all_fit") is True,
        },
        "duration_fit_audit_available": bool(duration_fit),
        "duration_fit_blocked_segment_ids": list(duration_fit.get("blocked_segment_ids") or []),
        "provider_calls_this_run": 0,
        "xmax_sessions_this_run": 0,
        "audio_created_this_run": False,
    }
    for stage in STAGES:
        diagnostics = _append_event(
            diagnostics,
            stage,
            message="Reused a verified existing asset.",
            reused=True,
            details={"audio_task_id": audio["id"]} if stage == "completed" else {},
        )
    return diagnostics


def _attach_audio_task(task_id: str, visual_task_id: str) -> tuple[dict[str, Any], bool]:
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    existing_audio = _latest_active_audio(row["episode_id"])
    if existing_audio:
        if existing_audio.get("source_task_id") != visual_task_id:
            message = (
                "Another Audio Localization task is active for a different visual result. "
                "Finish or stop that task before starting this overseas version."
            )
            fail_task(task_id, "AUDIO_TASK_CONFLICT", message, stage="separating_audio")
            raise OverseasEpisodeError("AUDIO_TASK_CONFLICT", message)
        audio = audio_localization_service.get_audio_localization_task(existing_audio["id"])
        created = False
    else:
        try:
            audio, created = audio_localization_service.create_audio_localization_task(
                row["episode_id"],
                source_task_id=visual_task_id,
                target_language="english",
                voice="default",
                hide_original_subtitles=True,
                original_subtitle_region_start_percent=audio_localization_service.DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
                original_subtitle_region_height_percent=audio_localization_service.DEFAULT_BURNED_SUBTITLE_REGION_HEIGHT_PERCENT,
                subtitle_position="low",
                allow_provider_calls=False,
            )
        except audio_localization_service.AudioLocalizationError as error:
            fail_task(task_id, error.code, str(error), stage="separating_audio")
            raise OverseasEpisodeError(error.code, str(error)) from error
    diagnostics = _diagnostics(row)
    diagnostics["audio_created_this_run"] = bool(created)
    diagnostics = _append_event(
        diagnostics,
        "separating_audio",
        message="Audio Localization task created." if created else "Resumed an existing Audio Localization task.",
        reused=not created,
        details={"audio_task_id": audio["id"]},
    )
    _update_task(
        task_id,
        status="running",
        stage="separating_audio",
        progress=STAGE_PROGRESS["separating_audio"],
        progress_message="Preparing dialogue-separated audio and reusable localization assets.",
        visual_task_id=visual_task_id,
        audio_task_id=audio["id"],
        error_code=None,
        error_message=None,
        diagnostics_json=json_dump(diagnostics),
    )
    return get_task(task_id), created


def create_or_resume_task(episode_id: str) -> tuple[dict[str, Any], str | None]:
    episode = _episode(episode_id)
    with get_connection() as db:
        active = db.execute(
            "SELECT * FROM overseas_episode_tasks WHERE episode_id=? AND status='running' "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    if active:
        payload = get_task(active["id"])
        action = "start_audio" if payload.get("audio_task_id") else (
            "run_visual" if payload.get("requires_client_visual_execution") else None
        )
        return payload, action

    with get_connection() as db:
        completed = db.execute(
            "SELECT * FROM overseas_episode_tasks WHERE episode_id=? AND status='completed' "
            "ORDER BY completed_at DESC, created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    if completed and _valid_file(completed["final_output_path"]):
        completed_item = row_to_dict(completed) or {}
        visual_task_id = str(completed_item.get("visual_task_id") or "")
        if not visual_task_id or _visual_task_ready(visual_task_id):
            return _payload(completed_item), None

    finalization = _provider_free_finalization_readiness(episode_id)
    audio = _latest_completed_audio(episode_id, finalization)
    if audio:
        payload = _insert_task(
            episode,
            stage="completed",
            status="completed",
            visual_task_id=audio.get("source_task_id"),
            audio_task_id=audio["id"],
            final_output_path=audio["output_path"],
            final_output_url=audio["output_url"],
            diagnostics=_completed_reuse_diagnostics(audio),
        )
        return payload, None

    visual = _latest_visual(episode_id, completed_only=True)
    if visual:
        diagnostics = _append_event(
            {
                "events": [],
                "reused_assets": {"visual": True},
                "provider_calls_this_run": 0,
                "xmax_sessions_this_run": 0,
            },
            "preparing",
            message="Reused the latest verified visual merge.",
            reused=True,
            details={"visual_task_id": visual["id"]},
        )
        task = _insert_task(
            episode,
            stage="separating_audio",
            visual_task_id=visual["id"],
            diagnostics=diagnostics,
        )
        task, created = _attach_audio_task(task["task_id"], visual["id"])
        return task, "start_audio" if created else None

    visual = _latest_visual(episode_id, completed_only=False)
    if visual:
        diagnostics = _append_event(
            {
                "events": [],
                "reused_assets": {"completed_shots": True},
                "provider_calls_this_run": 0,
                "xmax_sessions_this_run": 0,
            },
            "analyzing_shots",
            message="Loaded the persisted Shot plan and character mappings.",
            reused=True,
            details={"visual_task_id": visual["id"]},
        )
        diagnostics = _append_event(
            diagnostics,
            "transforming_shots",
            message="Ready to continue only incomplete Transform Units.",
        )
        task = _insert_task(
            episode,
            stage="transforming_shots",
            visual_task_id=visual["id"],
            diagnostics=diagnostics,
        )
        return task, "run_visual"

    diagnostics = _append_event(
        {"events": [], "provider_calls_this_run": 0, "xmax_sessions_this_run": 0},
        "analyzing_shots",
        message="No persisted character mapping was available.",
    )
    diagnostics = _append_event(
        diagnostics,
        "failed",
        message="Create and confirm the Episode Shot mappings before using one-click generation.",
    )
    task = _insert_task(
        episode,
        stage="failed",
        status="failed",
        diagnostics=diagnostics,
        error_code="SHOT_MAPPING_REQUIRED",
        error_message="No persisted Shot mapping is available for this Episode.",
    )
    return task, None


def update_visual_progress(
    task_id: str,
    *,
    visual_task_id: str,
    stage: str,
    transform_completed: int,
    transform_total: int,
    fallback_count: int,
    provider_calls: int = 0,
    xmax_sessions: int = 0,
) -> dict[str, Any]:
    if stage not in {"analyzing_shots", "transforming_shots", "merging_visual"}:
        raise OverseasEpisodeError("OVERSEAS_STAGE_INVALID", "The visual stage is not valid.")
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    if row["status"] != "running":
        return _persisted_status_payload(row)
    with get_connection() as db:
        visual = db.execute(
            "SELECT id, episode_id FROM xmax_thirty_second_tasks WHERE id=?",
            (visual_task_id,),
        ).fetchone()
    if not visual or visual["episode_id"] != row["episode_id"]:
        raise OverseasEpisodeError("VISUAL_TASK_MISMATCH", "The visual task does not belong to this Episode.")
    if row.get("visual_task_id") and row["visual_task_id"] != visual_task_id:
        raise OverseasEpisodeError("VISUAL_TASK_MISMATCH", "The orchestration task is linked to another visual task.")
    total = max(0, int(transform_total))
    completed = min(total, max(0, int(transform_completed)))
    fallback = max(0, int(fallback_count))
    diagnostics = _diagnostics(row)
    diagnostics["provider_calls_this_run"] = max(
        int(diagnostics.get("provider_calls_this_run") or 0),
        int(provider_calls),
    )
    diagnostics["xmax_sessions_this_run"] = max(
        int(diagnostics.get("xmax_sessions_this_run") or 0),
        int(xmax_sessions),
    )
    diagnostics = _append_event(
        diagnostics,
        stage,
        message=f"Transform progress {completed}/{total}.",
        details={"completed": completed, "total": total, "fallback_count": fallback},
    )
    stage_base = STAGE_PROGRESS[stage]
    progress = stage_base
    if stage == "transforming_shots" and total:
        progress = 12 + round(43 * completed / total)
    _update_task(
        task_id,
        stage=stage,
        progress=progress,
        progress_message=(
            "Merging validated visual units."
            if stage == "merging_visual"
            else f"Transforming visual units {completed}/{total}."
        ),
        visual_task_id=visual_task_id,
        transform_completed=completed,
        transform_total=total,
        fallback_count=fallback,
        diagnostics_json=json_dump(diagnostics),
    )
    return get_task_status(task_id)


def complete_visual_stage(task_id: str, visual_task_id: str) -> tuple[dict[str, Any], bool]:
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    with get_connection() as db:
        visual_row = db.execute(
            "SELECT * FROM xmax_thirty_second_tasks WHERE id=? AND episode_id=?",
            (visual_task_id, row["episode_id"]),
        ).fetchone()
    visual = row_to_dict(visual_row)
    if (
        not visual
        or visual.get("status") != "completed"
        or not visual.get("final_output_url")
        or not _valid_file(visual.get("final_output_path"))
        or not _visual_task_ready(visual_task_id)
    ):
        raise OverseasEpisodeError(
            "VISUAL_RESULT_NOT_READY",
            "The persisted visual merge is not complete or readable.",
        )
    counts = _visual_counts(visual_task_id)
    finalization = _provider_free_finalization_readiness(row["episode_id"])
    if not finalization.get("can_auto_prepare"):
        blockers = ", ".join(
            str(item.get("segment_id") or item.get("asset") or item.get("error_code"))
            for item in finalization["blockers"]
        )
        raise OverseasEpisodeError(
            "FINAL_ASSETS_NOT_READY",
            f"Final merge is blocked by missing or stale assets: {blockers}.",
        )
    diagnostics = _diagnostics(row)
    diagnostics = _append_event(
        diagnostics,
        "merging_visual",
        message="The validated visual timeline merge completed.",
        details={"visual_task_id": visual_task_id, **counts},
    )
    diagnostics["final_input_revisions"] = {
        "visual_task_id": visual_task_id,
        "audio_revision": finalization.get("audio_revision"),
        "subtitle_revision": finalization.get("subtitle_revision"),
        "background_audio_revision": finalization.get("background_audio_revision"),
        "mix_settings_revision": finalization.get("mix_settings_revision"),
        "audio_assets_to_prepare": list(finalization.get("auto_prepare_blockers") or []),
    }
    _update_task(
        task_id,
        stage="merging_visual",
        progress=STAGE_PROGRESS["merging_visual"],
        progress_message="Visual merge completed.",
        visual_task_id=visual_task_id,
        transform_completed=counts["completed"],
        transform_total=counts["total"],
        fallback_count=counts["fallback"],
        diagnostics_json=json_dump(diagnostics),
    )
    completed_audio = _latest_completed_audio(row["episode_id"], finalization)
    if (
        completed_audio
        and completed_audio.get("source_task_id") == visual_task_id
        and _completed_audio_matches_current_inputs(completed_audio, finalization)
    ):
        diagnostics = _completed_reuse_diagnostics(completed_audio)
        _update_task(
            task_id,
            status="completed",
            stage="completed",
            progress=100,
            progress_message="Overseas version completed.",
            audio_task_id=completed_audio["id"],
            final_output_path=completed_audio["output_path"],
            final_output_url=completed_audio["output_url"],
            diagnostics_json=json_dump(diagnostics),
            error_code=None,
            error_message=None,
            completed_at=completed_audio.get("completed_at") or now_iso(),
        )
        return get_task(task_id), False
    return _attach_audio_task(task_id, visual_task_id)


def run_audio_stage(task_id: str, audio_task_id: str) -> None:
    audio_localization_service.run_audio_localization_task(audio_task_id)
    row = _task_row(task_id)
    if row:
        _sync_linked_audio(row)


def fail_task(task_id: str, code: str, message: str, *, stage: str | None = None) -> dict[str, Any]:
    row = _task_row(task_id)
    if not row:
        raise OverseasEpisodeError("OVERSEAS_TASK_NOT_FOUND", "Overseas Episode task not found.")
    diagnostics = _append_event(
        _diagnostics(row),
        "failed",
        message=message,
        details={"failed_stage": stage or row.get("stage"), "error_code": code},
    )
    _update_task(
        task_id,
        status="failed",
        stage="failed",
        progress=100,
        progress_message="Overseas version failed.",
        error_code=code[:120],
        error_message=message[:1000],
        diagnostics_json=json_dump(diagnostics),
        completed_at=now_iso(),
    )
    return get_task(task_id)
