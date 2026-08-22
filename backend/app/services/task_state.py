"""Shared task state and local zombie-task recovery rules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..database import get_connection, now_iso

CANONICAL_STATUSES = {"pending", "running", "completed", "failed", "cancelled", "interrupted"}
LEGACY_STATUS_MAP = {
    "queued": "pending", "preparing": "running", "splitting": "running", "submitting": "running",
    "processing": "running", "recording": "running", "generating_voice": "running", "aligning": "running",
    "mixing": "running", "rendering_preview": "running", "downloading": "running", "stopped": "cancelled",
    "disabled": "failed", "timed_out": "failed", "paused": "interrupted",
}
TASK_TIMEOUT_SECONDS = {"transcription": 120, "translation": 120, "voice_preview": 45, "voice_segments": 45, "dubbing": 120, "xmax": 30}


def canonical_status(value: str | None) -> str:
    status = str(value or "pending")
    return status if status in CANONICAL_STATUSES else LEGACY_STATUS_MAP.get(status, status)


def heartbeat_expired(value: str | None, timeout_seconds: int) -> bool:
    if not value:
        return True
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - timestamp).total_seconds() > timeout_seconds


def mark_interrupted_on_startup() -> dict[str, int]:
    """A new backend process cannot safely resume an old paid/background call."""
    timestamp = now_iso()
    counts = {
        "ai_tasks": 0,
        "voice_generation_tasks": 0,
        "voice_segment_outputs": 0,
        "xmax_realtime_batches": 0,
        "audio_localization_tasks": 0,
    }
    with get_connection() as db:
        counts["ai_tasks"] = db.execute(
            "UPDATE ai_tasks SET status='interrupted', error_code='TASK_INTERRUPTED', "
            "error_message='The task was interrupted when the backend restarted.', completed_at=?, heartbeat_at=? "
            "WHERE status IN ('queued','running')", (timestamp, timestamp),
        ).rowcount
        counts["ai_tasks"] += db.execute(
            "UPDATE ai_tasks SET status='failed', error_code=COALESCE(error_code, 'TASK_DISABLED'), "
            "error_message=COALESCE(error_message, 'The task was disabled and did not run.'), updated_at=? WHERE status='disabled'",
            (timestamp,),
        ).rowcount
        counts["voice_generation_tasks"] = db.execute(
            "UPDATE voice_generation_tasks SET status='interrupted', error_code='TASK_INTERRUPTED', "
            "error_message='The task was interrupted when the backend restarted.', completed_at=?, last_heartbeat=?, heartbeat_at=? "
            "WHERE status IN ('queued','generating_voice','aligning','mixing','rendering_preview')",
            (timestamp, timestamp, timestamp),
        ).rowcount
        counts["voice_generation_tasks"] += db.execute(
            "UPDATE voice_generation_tasks SET status='interrupted', error_code=COALESCE(error_code, 'TASK_INTERRUPTED'), "
            "error_message=COALESCE(error_message, '任务已中断，请继续失败项或重新开始。'), heartbeat_at=?, updated_at=? WHERE status='paused'",
            (timestamp, timestamp),
        ).rowcount
        counts["voice_segment_outputs"] = db.execute(
            "UPDATE voice_segment_outputs SET status='failed', error_code='TASK_INTERRUPTED', "
            "error_message='The TTS task was interrupted when the backend restarted.', completed_at=?, heartbeat_at=?, updated_at=? "
            "WHERE status IN ('queued','generating','running')",
            (timestamp, timestamp, timestamp),
        ).rowcount
        counts["xmax_realtime_batches"] = db.execute(
            "UPDATE xmax_realtime_batches SET status='interrupted', error_code='TASK_INTERRUPTED', "
            "error_message='The Xmax batch was interrupted when the backend restarted.', heartbeat_at=?, finished_at=? "
            "WHERE status IN ('starting','running','preparing','splitting','processing','recording','merging')", (timestamp, timestamp),
        ).rowcount
        counts["xmax_realtime_batches"] += db.execute(
            "UPDATE xmax_realtime_batches SET status='cancelled', heartbeat_at=? WHERE status='stopped'", (timestamp,)
        ).rowcount
        counts["audio_localization_tasks"] = db.execute(
            "UPDATE audio_localization_tasks SET status='interrupted', progress_message=?, "
            "error_code='TASK_INTERRUPTED', error_message=?, finished_at=?, updated_at=? "
            "WHERE status IN ('pending','preparing_assets','rendering_subtitles','separating_audio',"
            "'building_tts_timeline','mixing_audio','muxing_final')",
            (
                "Audio Localization was interrupted when the backend restarted.",
                "The task was interrupted when the backend restarted. Start it again explicitly.",
                timestamp,
                timestamp,
            ),
        ).rowcount
    return counts


def validate_live_status(status: str | None, heartbeat: str | None, timeout_seconds: int) -> str:
    normalized = canonical_status(status)
    if normalized == "running" and heartbeat_expired(heartbeat, timeout_seconds):
        return "interrupted"
    return normalized


def heartbeat_update(values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    timestamp = now_iso()
    values.setdefault("heartbeat_at", timestamp)
    values.setdefault("updated_at", timestamp)
    return values
