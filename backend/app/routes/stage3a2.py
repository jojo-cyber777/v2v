"""Stage 3 A2-1 real ElevenLabs dubbing and MVP ducking mix routes."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from ..database import ROOT, bool_int, get_connection, json_dump, json_load, now_iso
from ..services import (
    audio_localization_service,
    duration_constrained_tts_service,
    language_capabilities,
    localization_text_authority,
    media_service,
    provider_service,
    subtitle_service,
    task_state,
    tencent_tts_voice_catalog,
    tts_input_revision_service,
)
from . import xmax_thirty_second


router = APIRouter(prefix="/api", tags=["stage-3a2"])
OUTPUTS_DIR = ROOT / "outputs"
ACTIVE_TASK_STATUSES = {"pending", "running", "queued", "generating_voice", "aligning", "mixing", "rendering_preview"}
logger = logging.getLogger(__name__)
TASK_TERMINAL_STATUSES = {"completed", "partial", "failed", "paused", "cancelled", "interrupted"}
SAFE_DIALOGUE_GAP_SECONDS = 0.02
MAX_MILD_TEMPO_RATIO = 2.0
DURATION_FIT_STATUSES = {
    "fit",
    "shortened_and_fit",
    "speed_adjusted_and_fit",
    duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS,
}
VOICE_UNFINISHED_STATUSES = {"missing_voice", "failed", "text_changed", "timing_exception"}
EXTREME_SHORT_SLOT_SECONDS = 0.35
EXTREME_SHORT_MAX_GAP_SECONDS = 0.12
TENCENT_TTS_CHUNK_CHAR_LIMIT = 450
TENCENT_VOICE_PREVIEW_TEXT = "Hello, this is a voice preview."
TTS_PROVIDERS = ("tencent", "elevenlabs", "edge")


class BindingItem(BaseModel):
    speaker_label: str
    character_id: str | None = None
    voice_id: str
    scope: str = "episode"


class BindingBatch(BaseModel):
    bindings: list[BindingItem]


class TtsProviderBody(BaseModel):
    provider: str = Field(min_length=1, max_length=40)


class VoiceGenerateBody(BaseModel):
    mode: str = "all_unfinished"
    segment_ids: list[str] = Field(default_factory=list)
    auto_mix: bool = True
    stop_on_provider_failure: bool = False
    adjust_timeline: bool = True


class VoicePreviewBody(BaseModel):
    segment_id: str
    voice_id: str | None = None


class ShorterTextBody(BaseModel):
    shorter_text: str | None = None


class MixPreviewBody(BaseModel):
    segment_ids: list[str] = Field(default_factory=list)
    dialogue_original_volume: float | None = Field(default=None, ge=0, le=1)
    non_dialogue_original_volume: float | None = Field(default=None, ge=0, le=1)
    dub_voice_volume: float | None = Field(default=None, ge=0, le=2)
    fade_ms: int | None = Field(default=None, ge=0, le=30)
    include_target_subtitles: bool | None = None


class SpeakerReassignBody(BaseModel):
    speaker_label: str = Field(min_length=1, max_length=80)


class DialogueRoleBody(BaseModel):
    segment_type: str


class VoiceResolutionBody(BaseModel):
    action: str


class MixSettingsBody(BaseModel):
    dialogue_original_volume: float = Field(default=0.05, ge=0, le=1)
    non_dialogue_original_volume: float = Field(default=0.95, ge=0, le=1)
    dub_voice_volume: float = Field(default=1.9, ge=0, le=2)
    fade_ms: int = Field(default=10, ge=0, le=30)
    include_target_subtitles: bool = True


class AudioLocalizationBody(BaseModel):
    source_task_id: str | None = None
    target_language: str = "english"
    voice: str = "default"
    original_audio_volume: float = Field(default=10, ge=0, le=100)
    voice_over_volume: float = Field(default=100, ge=0, le=200)
    keep_original_audio: bool = True
    subtitles_enabled: bool = True
    hide_original_subtitles: bool = True
    original_subtitle_region_start_percent: float = Field(default=85.5, ge=0, le=95)
    original_subtitle_region_height_percent: float = Field(default=14.5, ge=3, le=30)
    subtitle_position: str = "low"


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT e.*, p.processing_mode, p.name AS project_name FROM episodes e "
            "JOIN projects p ON p.id=e.project_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Episode not found.")
    return dict(row)


def _episode_tts_provider(episode: dict[str, Any]) -> str:
    settings = json_load(episode.get("settings_override"), {})
    selected = str(settings.get("tts_provider") or "").strip().lower()
    if selected in TTS_PROVIDERS:
        return selected
    return provider_service.tts_configuration().provider


def _episode_tts_configuration(episode: dict[str, Any]) -> provider_service.TTSConfiguration:
    return provider_service.tts_configuration(_episode_tts_provider(episode))


def _tts_provider_options() -> list[dict[str, Any]]:
    labels = {
        "tencent": "Tencent Cloud TTS",
        "elevenlabs": "ElevenLabs",
        "edge": "Edge TTS",
    }
    return [
        provider_service.tts_configuration(provider).public_status()
        | {"id": provider, "display_name": labels[provider]}
        for provider in TTS_PROVIDERS
    ]


def _video_path(episode: dict[str, Any]) -> Path:
    path = Path(str(episode.get("video_path") or ""))
    if not path.is_file():
        raise HTTPException(status_code=400, detail="The current episode video file does not exist.")
    return path


def _document(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        row = db.execute("SELECT * FROM transcript_documents WHERE episode_id=?", (episode_id,)).fetchone()
    return dict(row) if row else None


def _target_language(episode_id: str) -> str:
    try:
        return localization_text_authority.resolve_episode_text_authority(episode_id).language
    except localization_text_authority.LocalizationTextAuthorityError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": error.code, "message": str(error)},
        ) from error


def _voice_catalog_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["labels"] = json_load(item.get("labels_json"), {})
    item["metadata"] = json_load(item.get("metadata_json"), {})
    return item


def _voice_is_language_compatible(row: Any, target_language: str) -> bool:
    return language_capabilities.voice_row_matches_target_language(
        _voice_catalog_item(row),
        target_language,
    )


def _segments(episode_id: str) -> list[dict[str, Any]]:
    document = _document(episode_id)
    if not document:
        return []
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM transcript_segments WHERE document_id=? ORDER BY seq",
            (document["id"],),
        ).fetchall()
    try:
        authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    except localization_text_authority.LocalizationTextAuthorityError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": error.code, "message": str(error)},
        ) from error
    result = []
    for row in rows:
        item = dict(row)
        item["subtitle_enabled"] = bool(item["subtitle_enabled"])
        item["dubbing_speaker"] = duration_constrained_tts_service.dubbing_speaker_for_segment(item)
        result.append(authority.apply_to_segment(item))
    return result


def _tts_text(segment: dict[str, Any]) -> str:
    return localization_text_authority.effective_text(segment)


def _is_explicitly_omitted(segment: dict[str, Any]) -> bool:
    return str(segment.get("review_status") or "") == "manually_omitted"


def _is_tts_eligible(segment: dict[str, Any]) -> bool:
    return bool(_tts_text(segment)) and not _is_explicitly_omitted(segment)


def _retains_original_dialogue(segment: dict[str, Any]) -> bool:
    return str(segment.get("review_status") or "") == "preserve_original_audio"


def _dialogue_ducking_segments(
    segments: list[dict[str, Any]],
    current_tts_segment_ids: set[str],
) -> list[dict[str, Any]]:
    """Keep source dialogue suppressed even when a line is omitted or not TTS-ready."""
    result: list[dict[str, Any]] = []
    for segment in segments:
        if _retains_original_dialogue(segment):
            continue
        if not str(segment.get("source_text") or "").strip():
            continue
        if int(segment.get("end_ms") or 0) <= int(segment.get("start_ms") or 0):
            continue
        result.append({
            **segment,
            "force_silence": str(segment["id"]) not in current_tts_segment_ids,
        })
    return result


def plan_extreme_short_dialogue_actions(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge tiny adjacent lines by voice role without silently omitting dialogue."""
    working = [dict(item) for item in sorted(segments, key=lambda item: (int(item["start_ms"]), int(item["seq"])))]
    actions: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for index, segment in enumerate(working):
        segment_id = str(segment["id"])
        if segment_id in consumed or not _is_tts_eligible(segment):
            continue
        duration = (int(segment["end_ms"]) - int(segment["start_ms"])) / 1000
        if duration > EXTREME_SHORT_SLOT_SECONDS:
            continue
        speaker = duration_constrained_tts_service.dubbing_speaker_for_segment(segment)
        candidates: list[tuple[int, dict[str, Any]]] = []
        if index > 0:
            previous = working[index - 1]
            gap = (int(segment["start_ms"]) - int(previous["end_ms"])) / 1000
            if (
                str(previous["id"]) not in consumed
                and _is_tts_eligible(previous)
                and duration_constrained_tts_service.dubbing_speaker_for_segment(previous) == speaker
                and gap <= EXTREME_SHORT_MAX_GAP_SECONDS
            ):
                candidates.append((0, previous))
        if index + 1 < len(working):
            following = working[index + 1]
            gap = (int(following["start_ms"]) - int(segment["end_ms"])) / 1000
            if (
                str(following["id"]) not in consumed
                and _is_tts_eligible(following)
                and duration_constrained_tts_service.dubbing_speaker_for_segment(following) == speaker
                and gap <= EXTREME_SHORT_MAX_GAP_SECONDS
            ):
                candidates.append((1, following))
        if not candidates:
            continue
        _priority, target = min(candidates, key=lambda item: item[0])
        ordered = sorted([segment, target], key=lambda item: int(item["start_ms"]))
        target.update({
            "start_ms": min(int(segment["start_ms"]), int(target["start_ms"])),
            "end_ms": max(int(segment["end_ms"]), int(target["end_ms"])),
            "source_text": " ".join(str(item.get("source_text") or "").strip() for item in ordered).strip(),
            "final_translation": " ".join(str(item.get("final_translation") or "").strip() for item in ordered).strip(),
        })
        actions.append({
            "kind": "merge",
            "source_id": segment_id,
            "target_id": str(target["id"]),
            "start_ms": target["start_ms"],
            "end_ms": target["end_ms"],
            "source_text": target["source_text"],
            "final_translation": target["final_translation"],
        })
        consumed.add(segment_id)
    return actions


def _prepare_extreme_short_dialogue(
    episode_id: str,
    selected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    episode = _episode(episode_id)
    segments = _segments(episode_id)
    try:
        source_path = xmax_thirty_second._source_path(episode)
        plan = xmax_thirty_second._load_persistent_scene_plan(episode, source_path, 0.35)
    except Exception:
        plan = None
    expansion_actions = duration_constrained_tts_service.plan_extreme_short_slot_expansions(
        segments,
        list((plan or {}).get("transform_units") or []),
        round(float(episode.get("duration") or 0) * 1000),
        selected_ids=selected_ids,
    )
    expanded_ids = {str(action["segment_id"]) for action in expansion_actions}
    merge_actions = [] if selected_ids is not None else [
        action
        for action in plan_extreme_short_dialogue_actions(segments)
        if str(action["source_id"]) not in expanded_ids
        and str(action["target_id"]) not in expanded_ids
    ]
    actions = [*expansion_actions, *merge_actions]
    if not actions:
        return []
    timestamp = now_iso()
    with get_connection() as db:
        for action in actions:
            if action["kind"] == "expand":
                segment_id = str(action["segment_id"])
                db.execute(
                    "UPDATE transcript_segments SET end_ms=?, stale_for_voice=1, updated_at=? WHERE id=?",
                    (action["end_ms"], timestamp, segment_id),
                )
                db.execute(
                    "UPDATE voice_segment_outputs SET status='stale', error_code='TTS_TIMELINE_CHANGED', "
                    "error_message='Dialogue timing changed; regenerate this voice segment.', updated_at=? "
                    "WHERE episode_id=? AND transcript_segment_id=?",
                    (timestamp, episode_id, segment_id),
                )
                continue
            affected_ids = [str(action["source_id"])]
            target_id = str(action["target_id"])
            affected_ids.append(target_id)
            db.execute(
                "UPDATE transcript_segments SET start_ms=?, end_ms=?, source_text=?, final_translation=?, "
                "translation_status='completed', review_status='approved', stale_for_voice=1, updated_at=? WHERE id=?",
                (
                    action["start_ms"],
                    action["end_ms"],
                    action["source_text"],
                    action["final_translation"],
                    timestamp,
                    target_id,
                ),
            )
            db.execute(
                "UPDATE transcript_segments SET subtitle_enabled=0, final_translation='', review_status=?, "
                "stale_for_voice=1, updated_at=? WHERE id=?",
                ("auto_merged", timestamp, action["source_id"]),
            )
            placeholders = ",".join("?" for _ in affected_ids)
            db.execute(
                f"UPDATE voice_segment_outputs SET status='stale', error_code='TIMELINE_AUTO_ADJUSTED', "
                f"error_message='An extreme short dialogue slot was merged or safely omitted.', updated_at=? "
                f"WHERE episode_id=? AND transcript_segment_id IN ({placeholders}) AND status='completed'",
                (timestamp, episode_id, *affected_ids),
            )
        _invalidate_dubbed_outputs(db, episode_id, timestamp)
        db.execute(
            "UPDATE transcript_documents SET revision=revision+1, updated_at=? WHERE episode_id=?",
            (timestamp, episode_id),
        )
        db.execute(
            "UPDATE subtitle_outputs SET status='stale' "
            "WHERE episode_id=? AND output_type IN ('translated_subtitles', 'subtitle_preview', 'translated_subtitle_preview')",
            (episode_id,),
        )
    return actions


def _public_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(OUTPUTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return f"/outputs/{relative.as_posix()}"


def _output_directory(episode: dict[str, Any]) -> Path:
    path = OUTPUTS_DIR / episode["project_id"] / episode["id"] / "dubbing"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _local_output_path(value: str | None) -> Path | None:
    if not value:
        return None
    if value.startswith("/outputs/"):
        return OUTPUTS_DIR.joinpath(*value[len("/outputs/"):].split("/"))
    path = Path(value)
    return path if path.is_absolute() else None


def _verified_previous_tts_only(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT usage_json, created_at FROM voice_generation_tasks WHERE episode_id=? AND task_type='dubbing_preview' AND status='completed' ORDER BY created_at DESC",
            (episode_id,),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        usage = json_load(row["usage_json"], {})
        path = _local_output_path(usage.get("tts_only_path") or usage.get("tts_only_url"))
        segment_ids = list(usage.get("mixed_segment_ids") or [])
        if not path or not path.is_file() or path.stat().st_size <= 0 or not segment_ids:
            continue
        try:
            duration = media_service.media_duration_seconds(path)
        except Exception:
            continue
        if duration <= 0:
            continue
        candidates.append({"path": path, "segment_ids": segment_ids, "mixed_count": int(usage.get("mixed_count") or 0), "created_at": row["created_at"], "duration": duration})
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["mixed_count"], item["created_at"]), reverse=True)
    return candidates[0]


def _recover_file_timeline(episode: dict[str, Any], current_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover missing legacy timeline rows from existing local transcription/subtitle artifacts only."""
    timeline_path = _output_directory(episode).parent / "subtitles" / "source_transcript.json"
    if not timeline_path.is_file():
        return current_segments
    try:
        document = json.loads(timeline_path.read_text(encoding="utf-8"))
        source_segments = list(document.get("segments") or [])
    except (OSError, ValueError, TypeError):
        return current_segments
    if len(source_segments) <= len(current_segments):
        return current_segments
    target_cues: list[dict[str, Any]] = []
    for subtitle_name in ("preview_subtitles.srt", "translated_subtitles_en.srt", "translated_subtitles.srt"):
        subtitle_path = timeline_path.parent / subtitle_name
        if subtitle_path.is_file():
            try:
                target_cues, _ = subtitle_service.parse_subtitle(subtitle_path.read_bytes(), ".srt")
            except Exception:
                target_cues = []
            if target_cues:
                break
    current_by_id = {item["id"]: item for item in current_segments}
    recovered: list[dict[str, Any]] = []
    for index, source in enumerate(source_segments):
        segment_id = source.get("id")
        if segment_id in current_by_id:
            recovered.append(current_by_id[segment_id])
            continue
        final_translation = str(source.get("final_translation") or "").strip()
        if not final_translation and index < len(target_cues):
            final_translation = str(target_cues[index].get("text") or "").strip()
        if not segment_id or not final_translation:
            continue
        recovered.append({
            "id": segment_id,
            "seq": int(source.get("seq") or index + 1),
            "start_ms": int(source.get("start_ms") or 0),
            "end_ms": int(source.get("end_ms") or 0),
            "source_text": source.get("source_text") or "",
            "final_translation": final_translation,
            "subtitle_enabled": bool(source.get("subtitle_enabled", True)),
            "segment_type": source.get("segment_type") or "dialogue",
            "speaker_label": source.get("speaker_label") or source.get("speaker") or "Speaker 01",
            "dubbing_speaker": source.get("speaker_label") or source.get("speaker") or "Speaker 01",
        })
    return recovered if len(recovered) > len(current_segments) else current_segments


def _ensure_mix_settings(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM audio_mix_settings WHERE episode_id=?", (episode_id,)).fetchone()
        if not row:
            timestamp = now_iso()
            db.execute(
                "INSERT INTO audio_mix_settings (episode_id, dialogue_original_volume, non_dialogue_original_volume, dub_voice_volume, fade_ms, include_target_subtitles, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, 0.05, 0.95, 1.9, 10, 1, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM audio_mix_settings WHERE episode_id=?", (episode_id,)).fetchone()
        elif (
            math.isclose(float(row["dialogue_original_volume"]), 0.15, abs_tol=1e-9)
            and math.isclose(float(row["non_dialogue_original_volume"]), 1.0, abs_tol=1e-9)
            and math.isclose(float(row["dub_voice_volume"]), 1.0, abs_tol=1e-9)
            and int(row["fade_ms"]) == 80
        ):
            db.execute(
                "UPDATE audio_mix_settings SET dialogue_original_volume=?, non_dialogue_original_volume=?, "
                "dub_voice_volume=?, fade_ms=?, updated_at=? WHERE episode_id=?",
                (0.05, 0.95, 1.9, 10, now_iso(), episode_id),
            )
            row = db.execute("SELECT * FROM audio_mix_settings WHERE episode_id=?", (episode_id,)).fetchone()
    item = dict(row)
    item["include_target_subtitles"] = bool(item["include_target_subtitles"])
    return item


def _task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM voice_generation_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Voice generation task not found.")
    item = dict(row)
    item["usage"] = json_load(item.pop("usage_json", "{}"), {})
    item["segment_ids"] = json_load(item.pop("segment_ids_json", "[]"), [])
    item["status"] = task_state.canonical_status(item.get("status"))
    return item


def _create_task(episode: dict[str, Any], task_type: str, total_count: int) -> dict[str, Any]:
    task_id = f"vgt-{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    target_language = _target_language(episode["id"])
    with get_connection() as db:
        db.execute(
            "INSERT INTO voice_generation_tasks (id, project_id, episode_id, task_type, target_language, total_count, status, created_at, updated_at, heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (task_id, episode["project_id"], episode["id"], task_type, target_language, total_count, timestamp, timestamp, timestamp),
        )
    return _task(task_id)


def _active_task(episode_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
    with get_connection() as db:
        row = db.execute(
            f"SELECT id FROM voice_generation_tasks WHERE episode_id=? AND status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (episode_id, *sorted(ACTIVE_TASK_STATUSES)),
        ).fetchone()
    return _task(row["id"]) if row else None


def _require_no_active_task(episode_id: str) -> None:
    active = _active_task(episode_id)
    if active:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_TASK_ALREADY_RUNNING",
                "message": "A voice generation or dubbing task is already running for this episode.",
                "task_id": active["id"],
            },
        )


def _update_task(task_id: str, **updates: Any) -> None:
    if not updates:
        return
    updates = task_state.heartbeat_update(updates)
    if "last_heartbeat" not in updates and updates.get("status") not in TASK_TERMINAL_STATUSES:
        updates["last_heartbeat"] = updates["heartbeat_at"]
    assignments = ", ".join(f"{key}=?" for key in updates)
    with get_connection() as db:
        db.execute(f"UPDATE voice_generation_tasks SET {assignments} WHERE id=?", (*updates.values(), task_id))


def _set_task_segments(task_id: str, segment_ids: list[str]) -> None:
    _update_task(task_id, segment_ids_json=json_dump(segment_ids), control_requested="")


def _task_remaining_segments(task: dict[str, Any], episode_id: str) -> list[str]:
    ids = set(task.get("segment_ids") or [])
    if not ids:
        return []
    segments = [item for item in _segments(episode_id) if item["id"] in ids]
    bindings = _binding_map(_episode(episode_id))
    with get_connection() as db:
        outputs = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND output_kind='segment' ORDER BY updated_at DESC",
            (episode_id,),
        ).fetchall()
    return [item["id"] for item in segments if (_current_output(item, bindings.get(item["dubbing_speaker"]), list(outputs)) or {}).get("status") != "completed"]


def _current_task_for_assets(
    task: dict[str, Any] | None,
    *,
    tts_unfinished_segment_ids: list[str],
    mix_ready: bool,
) -> dict[str, Any] | None:
    """Exclude terminal task failures superseded by current TTS assets."""
    if not task or task.get("status") != "failed":
        return task
    unfinished = set(tts_unfinished_segment_ids)
    task_segments = set(task.get("segment_ids") or [])
    if task.get("task_type") in {"voice_preview", "voice_segments"}:
        return task if task_segments & unfinished else None
    if (
        task.get("task_type") == "dubbing_preview"
        and task.get("error_code") == "TTS_DURATION_EXCEEDED"
        and mix_ready
        and not unfinished
    ):
        return None
    return task


def _error_parts(error: Exception) -> tuple[str, str]:
    if isinstance(error, provider_service.ProviderError):
        return error.code, str(error)
    if isinstance(error, media_service.MediaToolError):
        return error.code, str(error)
    if isinstance(error, audio_localization_service.AudioLocalizationError):
        return error.code, str(error)
    return "INTERNAL_ERROR", provider_service.redact_sensitive_text(str(error))


def _provider_error_usage(error: Exception) -> dict[str, Any]:
    if not isinstance(error, provider_service.ProviderError):
        return {}
    return {"provider_error": provider_service.provider_error_diagnostics(error)}


def _timeline_window(segment: dict[str, Any], next_segment: dict[str, Any] | None, video_duration: float | None) -> dict[str, float]:
    start = max(0.0, float(segment["start_ms"]) / 1000)
    end = max(start, float(segment["end_ms"]) / 1000)
    original_duration = max(0.001, end - start)
    may_use_following_silence = segment.get("segment_type") in {
        "offscreen_dialogue", "narration", "inner_monologue",
    }
    next_start = (
        max(end, float(next_segment["start_ms"]) / 1000)
        if may_use_following_silence and next_segment
        else end
    )
    available_end = min(next_start, video_duration) if video_duration and video_duration > start else next_start
    available_duration = max(0.001, available_end - start - SAFE_DIALOGUE_GAP_SECONDS)
    return {
        "original_duration": original_duration,
        "available_duration": available_duration,
        "audio_end_ms": (start + available_duration) * 1000,
        "reserved_gap": SAFE_DIALOGUE_GAP_SECONDS,
    }


def _segment_timeline_window(episode: dict[str, Any], segment: dict[str, Any]) -> dict[str, float]:
    eligible = [item for item in _segments(episode["id"]) if _is_tts_eligible(item)]
    ordered = sorted(eligible, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    index = next((index for index, item in enumerate(ordered) if item["id"] == segment["id"]), -1)
    next_segment = ordered[index + 1] if index >= 0 and index + 1 < len(ordered) else None
    try:
        video_duration = media_service.media_duration_seconds(_video_path(episode))
    except Exception:
        video_duration = None
    return _timeline_window(segment, next_segment, video_duration)


def _voice_output(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["duration_warning"] = bool(item["duration_warning"])
    item["raw_audio_url"] = _public_path(item.get("raw_audio_path"))
    item["aligned_audio_url"] = _public_path(item.get("aligned_audio_path"))
    item["usage"] = json_load(item.pop("usage_json", "{}"), {})
    usage = item["usage"]
    item["audio_path"] = item.get("aligned_audio_path") or item.get("raw_audio_path")
    item["duration_ms"] = round(
        float(item.get("aligned_duration") or item.get("generated_duration") or 0) * 1000
    )
    item["provider_name"] = item.get("provider")
    item["character_cost"] = usage.get("character_cost", 0)
    capped_speed_warning = (
        usage.get("duration_fit_status")
        == duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
    )
    item["needs_shorter_text"] = bool(
        (item["duration_warning"] and not capped_speed_warning)
        or usage.get("needs_shorter_text")
        or item.get("error_code") == "TTS_DURATION_EXCEEDED"
    )
    item["needs_attention"] = bool(item["duration_warning"] or usage.get("needs_attention"))
    item["shorter_text"] = usage.get("shorter_text")
    item["regeneration_status"] = usage.get("status") if item["needs_shorter_text"] else None
    item["original_duration"] = usage.get("original_duration") or item.get("target_duration")
    item["available_duration"] = usage.get("available_duration") or item.get("target_duration")
    item["speed_ratio"] = usage.get("speed_ratio") or item.get("speed_adjustment")
    item["duration_fit_status"] = usage.get("duration_fit_status")
    item["original_tts_duration"] = usage.get("original_tts_duration") or item.get("generated_duration")
    item["final_tts_duration"] = usage.get("final_tts_duration") or item.get("aligned_duration")
    item["shortening_retry_count"] = int(usage.get("shortening_retry_count") or 0)
    chunk_manifest = usage.get("tts_child_chunks")
    if isinstance(chunk_manifest, dict):
        chunks = chunk_manifest.get("chunks")
        item["tts_child_chunk_count"] = int(chunk_manifest.get("chunk_count") or 0)
        item["tts_child_chunks"] = [
            {
                key: chunk.get(key)
                for key in (
                    "chunk_id", "index", "parent_segment_id", "chunk_hash", "text",
                    "character_count", "provider", "voice_type", "speed_ratio", "volume",
                    "status", "duration", "character_cost", "cache_hit", "error_code", "error_message",
                )
            }
            for chunk in chunks
            if isinstance(chunk, dict)
        ] if isinstance(chunks, list) else []
    else:
        item["tts_child_chunk_count"] = 0
        item["tts_child_chunks"] = []
    if (
        not item["duration_fit_status"]
        and item.get("status") == "completed"
        and item.get("aligned_audio_path")
        and float(item.get("aligned_duration") or 0) > 0
        and float(item.get("target_duration") or 0) > 0
        and float(item["aligned_duration"]) <= float(item["target_duration"]) + 0.005
    ):
        item["duration_fit_status"] = "fit"
        item["final_tts_duration"] = float(item["aligned_duration"])
    item["needs_shorter_text"] = bool(
        item["needs_shorter_text"] or item["duration_fit_status"] == "manual_review_required"
    )
    return item


def _duration_fit_input_duration(output: dict[str, Any]) -> float:
    usage = output.get("usage") if isinstance(output.get("usage"), dict) else {}
    silence_trim = usage.get("silence_trim") if isinstance(usage, dict) else None
    if isinstance(silence_trim, dict):
        try:
            candidate_duration = float(silence_trim.get("candidate_duration") or 0)
        except (TypeError, ValueError):
            candidate_duration = 0.0
        if math.isfinite(candidate_duration) and candidate_duration > 0:
            return candidate_duration
    try:
        return float(output.get("generated_duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _invalidate_dubbed_outputs(db: Any, episode_id: str, timestamp: str | None = None) -> None:
    db.execute(
        "UPDATE dubbed_outputs SET status='stale', updated_at=? "
        "WHERE episode_id=? AND status='completed'",
        (timestamp or now_iso(), episode_id),
    )


def _mark_voice_outputs_stale(
    db: Any,
    episode_id: str,
    segment_ids: list[str],
    reason: str,
    timestamp: str | None = None,
) -> None:
    if not segment_ids:
        return
    changed_at = timestamp or now_iso()
    placeholders = ",".join("?" for _ in segment_ids)
    db.execute(
        f"UPDATE voice_segment_outputs SET status='stale', error_code='TTS_PROVIDER_CHANGED', "
        f"error_message=?, updated_at=? WHERE episode_id=? "
        f"AND transcript_segment_id IN ({placeholders}) AND status IN ('completed','generating','queued')",
        (reason, changed_at, episode_id, *segment_ids),
    )
    db.execute(
        f"UPDATE transcript_segments SET stale_for_voice=1, updated_at=? "
        f"WHERE id IN ({placeholders})",
        (changed_at, *segment_ids),
    )
    _invalidate_dubbed_outputs(db, episode_id, changed_at)


def _output_mix_eligibility(
    segment: dict[str, Any],
    output: dict[str, Any] | None,
    binding: dict[str, Any] | None = None,
    *,
    verify_media: bool = True,
) -> tuple[bool, str | None]:
    if _is_explicitly_omitted(segment):
        return False, "Transcript segment was omitted"
    if not _tts_text(segment):
        return False, "Transcript has no current dubbing text"
    if not binding or not str(binding.get("voice_id") or "").strip():
        return False, "Transcript speaker has no resolved voice"
    if not output:
        return False, "No matching voice output"
    if output.get("status") != "completed":
        return False, "Audio is not completed"
    expected_hash = tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
    if output.get("tts_input_hash") != expected_hash:
        return False, "Audio input revision is outdated"
    if output.get("voice_id") != binding.get("voice_id"):
        return False, "Audio voice is outdated"
    if output.get("duration_fit_status") not in DURATION_FIT_STATUSES:
        return False, "Voice line has not passed its original-slot duration check"
    audio_path = output.get("aligned_audio_path")
    if not audio_path:
        return False, "Audio file is missing"
    if not verify_media:
        return True, None
    if not Path(str(audio_path)).is_file() or Path(str(audio_path)).stat().st_size <= 0:
        return False, "Audio file is missing"
    try:
        if media_service.media_duration_seconds(Path(str(audio_path))) <= 0:
            return False, "Audio file is not readable"
    except Exception:
        return False, "Audio file is not readable"
    return True, None


def _eligible_mix_output(
    segment: dict[str, Any],
    binding: dict[str, Any] | None,
    rows: list[Any],
    *,
    verify_media: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    if not binding:
        return None, "Transcript speaker has no resolved voice"
    expected_hash = tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
    candidates: list[dict[str, Any]] = []
    last_reason = "No matching voice output"
    for row in rows:
        item = dict(row)
        if item["transcript_segment_id"] != segment["id"] or item["output_kind"] != "segment":
            continue
        normalized = _voice_output(row)
        if normalized.get("tts_input_hash") != expected_hash:
            last_reason = "Audio input revision is outdated"
            continue
        eligible, reason = _output_mix_eligibility(
            segment,
            normalized,
            binding,
            verify_media=verify_media,
        )
        if not eligible:
            last_reason = reason or last_reason
            continue
        candidates.append(normalized)
    if not candidates:
        return None, last_reason
    return candidates[0], None


def _mix_output(segment: dict[str, Any], binding: dict[str, Any] | None, rows: list[Any]) -> dict[str, Any] | None:
    """Pick only the latest readable TTS for the complete current input revision."""
    output, _ = _eligible_mix_output(segment, binding, rows)
    return output


def _resolve_mix_inputs(
    episode_id: str,
    segments: list[dict[str, Any]],
    bindings: dict[str, dict[str, Any]],
    rows: list[Any],
    mix_settings: dict[str, Any] | None = None,
    *,
    verify_media: bool = True,
) -> dict[str, Any]:
    required = [
        item
        for item in sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
        if _is_tts_eligible(item)
    ]
    outputs: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    manifest_segments: list[dict[str, Any]] = []
    for segment in required:
        binding = bindings.get(segment["dubbing_speaker"])
        output, reason = _eligible_mix_output(
            segment,
            binding,
            rows,
            verify_media=verify_media,
        )
        expected_hash = (
            tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
            if binding
            else None
        )
        if output:
            outputs[segment["id"]] = output
        else:
            reasons[segment["id"]] = reason or "No matching voice output"
        manifest_segments.append({
            "transcript_segment_id": segment["id"],
            "tts_input_hash": expected_hash,
            "voice_output_id": output["id"] if output else None,
        })
    settings = mix_settings or _ensure_mix_settings(episode_id)
    manifest = {
        "version": 2,
        "episode_id": episode_id,
        "mix_settings": {
            "dialogue_original_volume": float(settings["dialogue_original_volume"]),
            "non_dialogue_original_volume": float(settings["non_dialogue_original_volume"]),
            "dub_voice_volume": float(settings["dub_voice_volume"]),
            "fade_ms": int(settings["fade_ms"]),
            "include_target_subtitles": bool(settings["include_target_subtitles"]),
        },
        "segments": manifest_segments,
    }
    revision_payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "required_segment_ids": [item["id"] for item in required],
        "eligible_segment_ids": [item["id"] for item in required if item["id"] in outputs],
        "missing_segment_ids": [item["id"] for item in required if item["id"] not in outputs],
        "reasons": reasons,
        "outputs": outputs,
        "manifest": manifest,
        "revision": hashlib.sha256(revision_payload.encode("utf-8")).hexdigest(),
    }


def _load_mix_inputs(
    episode_id: str,
    mix_settings: dict[str, Any] | None = None,
    *,
    verify_media: bool = True,
) -> dict[str, Any]:
    episode = _episode(episode_id)
    segments = _segments(episode_id)
    bindings = _binding_map(episode)
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM voice_segment_outputs "
            "WHERE episode_id=? AND output_kind='segment' ORDER BY updated_at DESC",
            (episode_id,),
        ).fetchall()
    return _resolve_mix_inputs(
        episode_id,
        segments,
        bindings,
        list(rows),
        mix_settings or _ensure_mix_settings(episode_id),
        verify_media=verify_media,
    )


def _resolve_binding_voice(
    binding: dict[str, Any],
    character_voices: dict[str, str],
) -> tuple[str | None, str | None]:
    direct_voice = str(binding.get("voice_id") or "").strip()
    if direct_voice:
        source = "voice_only_role" if not binding.get("character_id") else "speaker_direct"
        return direct_voice, source
    character_voice = character_voices.get(str(binding.get("character_id") or ""))
    if character_voice:
        return character_voice, "character"
    return None, None


def _binding_rows(episode: dict[str, Any]) -> list[dict[str, Any]]:
    active_provider = _episode_tts_configuration(episode).provider
    target_language = _target_language(episode["id"])
    with get_connection() as db:
        project_rows = db.execute(
            "SELECT * FROM speaker_voice_bindings WHERE project_id=? AND episode_id IS NULL AND is_project_default=1 ORDER BY updated_at",
            (episode["project_id"],),
        ).fetchall()
        episode_rows = db.execute(
            "SELECT * FROM speaker_voice_bindings WHERE episode_id=? AND is_episode_override=1 ORDER BY updated_at",
            (episode["id"],),
        ).fetchall()
        character_rows = db.execute(
            "SELECT id, voice_id, voice_profile_id, gender FROM characters WHERE project_id=?",
            (episode["project_id"],),
        ).fetchall()
        voice_rows = db.execute(
            "SELECT voice_id, name, labels_json, metadata_json FROM voice_catalog_cache WHERE provider=?",
            (active_provider,),
        ).fetchall()
    resolved = {row["speaker_label"]: dict(row) for row in project_rows}
    resolved.update({row["speaker_label"]: dict(row) for row in episode_rows})
    catalog_rows = [_voice_catalog_item(row) for row in voice_rows]
    compatible_catalog_rows = [
        row for row in catalog_rows
        if language_capabilities.voice_row_matches_target_language(row, target_language)
    ]
    catalog = {str(row["voice_id"]): row for row in catalog_rows}
    character_voices = {
        str(row["id"]): str(row["voice_profile_id"] or row["voice_id"] or "").strip()
        for row in character_rows
    }
    character_genders = {str(row["id"]): str(row["gender"] or "") for row in character_rows}
    for item in resolved.values():
        if str(item.get("provider") or "").strip() == active_provider:
            voice_id, source = _resolve_binding_voice(item, character_voices)
        else:
            voice_id, source = None, None
        catalog_item = catalog.get(voice_id or "")
        compatible = bool(
            catalog_item
            and language_capabilities.voice_row_matches_target_language(catalog_item, target_language)
        )
        replacement_id = None
        if voice_id and not compatible:
            replacement_id = language_capabilities.recommended_voice_id(
                compatible_catalog_rows,
                target_language,
                character_genders.get(str(item.get("character_id") or "")),
            )
            voice_id = replacement_id
            source = "language_default" if replacement_id else None
        item["voice_id"] = voice_id or ""
        item["voice_resolution_source"] = source
        item["voice_language_compatible"] = bool(voice_id)
        item["auto_rematched"] = bool(replacement_id)
        item["target_language"] = target_language
        item["is_project_default"] = bool(item["is_project_default"])
        item["is_episode_override"] = bool(item["is_episode_override"])
        catalog_item = catalog.get(item["voice_id"])
        if catalog_item:
            metadata = catalog_item["metadata"]
            item["voice_type"] = str(metadata.get("voice_type") or item["voice_id"].split(":")[-1])
            item["voice_name"] = catalog_item["name"]
            item["catalog_version"] = metadata.get("catalog_version")
            item["voice_known"] = True
        elif active_provider == "tencent" and item["voice_id"]:
            voice_type = item["voice_id"].split(":")[-1]
            item["voice_type"] = voice_type
            item["voice_name"] = f"未知腾讯音色 ID {voice_type}"
            item["catalog_version"] = None
            item["voice_known"] = False
    return list(resolved.values())


def _binding_map(episode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["speaker_label"]: item
        for item in _binding_rows(episode)
        if str(item.get("voice_id") or "").strip()
    }


def _resolve_segment_voices(
    episode: dict[str, Any],
    segments: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    bindings = _binding_map(episode)
    speakers = sorted({str(item["dubbing_speaker"]) for item in segments})
    missing = [speaker for speaker in speakers if speaker not in bindings]
    return bindings, missing


def _speaker_profiles(
    document: dict[str, Any] | None,
    segments: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize detected transcript speakers separately from TTS voices."""
    binding_map = {
        str(item.get("speaker_label") or ""): item
        for item in bindings
        if item.get("speaker_label")
    }
    profiles: dict[str, dict[str, Any]] = {}
    for segment in segments:
        speaker = str(segment["dubbing_speaker"])
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or 0)
        profile = profiles.setdefault(speaker, {
            "speaker_id": speaker,
            "source_provider": str((document or {}).get("provider") or ""),
            "source_model": str((document or {}).get("model") or ""),
            "detected_language": str((document or {}).get("detected_language") or ""),
            "segment_count": 0,
            "total_duration_ms": 0,
            "first_appearance_ms": start_ms,
        })
        profile["segment_count"] += 1
        profile["total_duration_ms"] += max(0, end_ms - start_ms)
        profile["first_appearance_ms"] = min(profile["first_appearance_ms"], start_ms)

    result: list[dict[str, Any]] = []
    for speaker, profile in sorted(
        profiles.items(), key=lambda item: (item[1]["first_appearance_ms"], item[0])
    ):
        binding = binding_map.get(speaker) or {}
        voice_id = str(binding.get("voice_id") or "").strip()
        result.append(profile | {
            "character_id": binding.get("character_id"),
            "tts_provider": str(binding.get("provider") or "") if voice_id else None,
            "tts_model": str(binding.get("model") or "") if voice_id else None,
            "voice_id": voice_id or None,
            "voice_name": binding.get("voice_name") if voice_id else None,
            "binding_status": "bound" if voice_id else "unbound",
        })
    return result


def _current_output(segment: dict[str, Any], binding: dict[str, Any] | None, rows: list[Any]) -> dict[str, Any] | None:
    if not binding or not _tts_text(segment):
        return None
    expected_hash = tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
    stale_fallback = None
    for row in rows:
        item = dict(row)
        if item["transcript_segment_id"] != segment["id"] or item["output_kind"] != "segment":
            continue
        normalized = _voice_output(row)
        if (
            item.get("tts_input_hash") == expected_hash
            and item["voice_id"] == binding["voice_id"]
            and item["model"] == binding["model"]
        ):
            if normalized.get("status") == "completed":
                audio_path = normalized.get("aligned_audio_path") or normalized.get("raw_audio_path")
                try:
                    readable = bool(
                        audio_path
                        and Path(str(audio_path)).is_file()
                        and Path(str(audio_path)).stat().st_size > 0
                        and media_service.media_duration_seconds(Path(str(audio_path))) > 0
                    )
                except Exception:
                    readable = False
                if not readable:
                    normalized["status"] = "failed"
                    normalized["error_code"] = "TTS_AUDIO_MISSING"
                    normalized["error_message"] = "The generated voice audio file is missing or unreadable."
            return normalized
        if stale_fallback is None and normalized["status"] in {"completed", "failed", "pending_regeneration", "interrupted", "stale"}:
            if normalized.get("needs_shorter_text") or normalized.get("regeneration_status") == "pending_regeneration":
                normalized["status"] = "pending_regeneration"
            elif normalized["status"] == "completed":
                normalized["status"] = "stale"
            stale_fallback = normalized
    return stale_fallback


def _voice_segment_status(
    segment: dict[str, Any],
    output: dict[str, Any] | None,
    *,
    mix_eligible: bool,
) -> str:
    if _is_explicitly_omitted(segment):
        return "omitted"
    if str(segment.get("review_status") or "") == "auto_omitted":
        return "timing_exception"
    if not output:
        return "missing_voice"
    if output.get("error_code") == "TTS_TEXT_CHANGED" or output.get("status") == "stale":
        return "text_changed"
    if output.get("duration_fit_status") == "manual_review_required" or output.get("needs_shorter_text"):
        return "timing_exception"
    if output.get("status") == "completed" or mix_eligible:
        return "ready"
    if output.get("status") in {
        "completed", "failed", "interrupted", "orphaned", "pending_regeneration",
    }:
        return "failed"
    return "missing_voice"


def _public_audio_assets_readiness(episode_id: str, duration: float) -> dict[str, Any]:
    try:
        audio_assets = audio_localization_service.audio_assets_readiness(episode_id, duration)
    except (
        audio_localization_service.AudioLocalizationError,
        duration_constrained_tts_service.DurationConstrainedTTSError,
        media_service.MediaToolError,
        OSError,
    ) as error:
        return {
            "ready": False,
            "can_auto_prepare": False,
            "tts_ready": False,
            "stems_ready": False,
            "formal_mix_ready": False,
            "dubbed_output_ready": False,
            "quick_preview_ready": False,
            "audio_asset_state": "blocked",
            "audio_revision": None,
            "subtitle_revision": None,
            "mix_settings_revision": None,
            "formal_mix_task_id": None,
            "formal_mix_source": None,
            "quick_preview_task_id": None,
            "blockers": [{
                "asset": "audio_localization",
                "segment_id": None,
                "error_code": getattr(error, "code", "AUDIO_ASSET_PREFLIGHT_FAILED"),
                "message": str(error),
            }],
            "assets_to_prepare": [],
        }
    return {
        "ready": bool(audio_assets["ready"]),
        "can_auto_prepare": bool(audio_assets["can_auto_prepare"]),
        "tts_ready": bool(audio_assets["tts_ready"]),
        "stems_ready": bool(audio_assets["stems_ready"]),
        "formal_mix_ready": bool(audio_assets["formal_mix_ready"]),
        "dubbed_output_ready": bool(audio_assets["dubbed_output_ready"]),
        "quick_preview_ready": bool(audio_assets["quick_preview_ready"]),
        "audio_asset_state": str(audio_assets["audio_asset_state"]),
        "audio_revision": audio_assets.get("audio_revision"),
        "subtitle_revision": audio_assets.get("subtitle_revision"),
        "mix_settings_revision": audio_assets.get("mix_settings_revision"),
        "formal_mix_task_id": (audio_assets.get("formal_mix") or {}).get("task_id"),
        "formal_mix_source": (audio_assets.get("formal_mix") or {}).get("asset_source"),
        "quick_preview_task_id": (audio_assets.get("quick_preview") or {}).get("task_id"),
        "blockers": list(audio_assets.get("blockers") or []),
        "assets_to_prepare": list(audio_assets.get("auto_prepare_blockers") or []),
    }


def _state(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    tts_config = _episode_tts_configuration(episode)
    active_provider = tts_config.provider
    target_language = _target_language(episode_id)
    if active_provider == "tencent":
        _ensure_local_tencent_voice_catalog()
    segments = _segments(episode_id)
    bindings = _binding_rows(episode)
    speaker_profiles = _speaker_profiles(document, segments, bindings)
    eligible = [item for item in segments if _is_tts_eligible(item)]
    binding_map, missing_voice_speakers = _resolve_segment_voices(episode, eligible)
    with get_connection() as db:
        voices = db.execute(
            "SELECT * FROM voice_catalog_cache WHERE provider=? ORDER BY name COLLATE NOCASE",
            (active_provider,),
        ).fetchall()
        outputs = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? ORDER BY updated_at DESC",
            (episode_id,),
        ).fetchall()
        latest_task = db.execute(
            "SELECT * FROM voice_generation_tasks WHERE episode_id=? ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        latest_mix_task = db.execute(
            "SELECT * FROM voice_generation_tasks WHERE episode_id=? AND task_type='dubbing_preview' ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        dubbed = db.execute(
            "SELECT * FROM dubbed_outputs WHERE episode_id=? AND status='completed' "
            "AND mix_mode='demucs_dialogue_replacement' ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        orphan_count = db.execute("SELECT COUNT(*) AS count FROM voice_output_orphans WHERE episode_id=?", (episode_id,)).fetchone()["count"]
        task_rows = db.execute("SELECT task_type, usage_json FROM voice_generation_tasks WHERE episode_id=?", (episode_id,)).fetchall()
        characters = db.execute(
            "SELECT id, character_code, name, gender, priority_level, confirmed, voice_id, voice_profile_id "
            "FROM characters WHERE project_id=? ORDER BY character_code, created_at",
            (episode["project_id"],),
        ).fetchall()
    output_rows = list(outputs)
    mix_settings = _ensure_mix_settings(episode_id)
    mix_inputs = _resolve_mix_inputs(episode_id, segments, binding_map, output_rows, mix_settings)
    try:
        video_duration = media_service.media_duration_seconds(_video_path(episode))
    except Exception:
        video_duration = None
    ordered_eligible = sorted(eligible, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    timeline_windows = {
        item["id"]: _timeline_window(item, ordered_eligible[index + 1] if index + 1 < len(ordered_eligible) else None, video_duration)
        for index, item in enumerate(ordered_eligible)
    }
    segment_items = []
    completed_count = 0
    failed_count = 0
    warning_count = 0
    stale_count = 0
    voice_status_counts = {
        "ready": 0,
        "missing_voice": 0,
        "failed": 0,
        "text_changed": 0,
        "timing_exception": 0,
        "omitted": 0,
    }
    tts_unfinished_segment_ids: list[str] = []
    mix_eligible_segment_ids = list(mix_inputs["eligible_segment_ids"])
    mix_ineligible_reasons = dict(mix_inputs["reasons"])
    eligible_ids = {item["id"] for item in eligible}
    for segment in segments:
        tts_eligible = segment["id"] in eligible_ids
        binding = binding_map.get(segment["dubbing_speaker"])
        current_output = _current_output(segment, binding, output_rows)
        if tts_eligible:
            mix_output = mix_inputs["outputs"].get(segment["id"])
            mix_reason = mix_inputs["reasons"].get(segment["id"])
        else:
            mix_output, mix_reason = None, "Transcript is not ready for TTS"
        output = mix_output or current_output
        if output and output["status"] in {"stale", "orphaned"}:
            stale_count += 1
        original_duration = max(0.0, (int(segment["end_ms"]) - int(segment["start_ms"])) / 1000)
        window = timeline_windows.get(segment["id"], {
            "original_duration": original_duration,
            "available_duration": original_duration,
        })
        if output:
            output["original_duration"] = window["original_duration"]
            output["available_duration"] = window["available_duration"]
            fit_input_duration = _duration_fit_input_duration(output)
            if fit_input_duration > 0:
                output["speed_ratio"] = fit_input_duration / window["available_duration"]
                capped_speed_warning = (
                    output.get("duration_fit_status")
                    == duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
                )
                output["needs_shorter_text"] = bool(
                    not capped_speed_warning
                    and fit_input_duration > window["available_duration"]
                    and output["speed_ratio"] > MAX_MILD_TEMPO_RATIO
                )
                output["duration_warning"] = bool(
                    capped_speed_warning or output["needs_shorter_text"]
                )
                output["needs_attention"] = output["duration_warning"]
        if output and (output["needs_shorter_text"] or output.get("needs_attention")):
            warning_count += 1
        mix_eligible = mix_output is not None
        if not mix_eligible:
            mix_ineligible_reasons[segment["id"]] = mix_reason or "Voice output is not eligible"
        voice_status = _voice_segment_status(segment, output, mix_eligible=mix_eligible)
        voice_status_counts[voice_status] += 1
        if tts_eligible and voice_status in VOICE_UNFINISHED_STATUSES:
            tts_unfinished_segment_ids.append(segment["id"])
        if tts_eligible and voice_status == "ready":
            completed_count += 1
        if tts_eligible and voice_status in {"failed", "timing_exception"}:
            failed_count += 1
        segment_items.append({
            "id": segment["id"], "transcript_segment_id": segment["id"],
            "seq": segment["seq"], "start_ms": segment["start_ms"], "end_ms": segment["end_ms"],
            "speaker_label": segment["dubbing_speaker"], "segment_type": segment["segment_type"],
            "source_text": segment["source_text"], "final_translation": segment["final_translation"],
            "localization_text": _tts_text(segment),
            "localization_mode": segment.get("localization_mode"),
            "target_duration": window["original_duration"], "available_duration": window["available_duration"],
            "voice_id": binding["voice_id"] if binding else (mix_output.get("voice_id") if mix_output else None), "output": output,
            "tts_eligible": tts_eligible, "voice_status": voice_status,
            "mix_eligible": mix_eligible, "mix_disabled_reason": mix_reason,
        })
    dubbed_item = dict(dubbed) if dubbed else None
    if dubbed_item and (
        dubbed_item.get("mix_revision") != mix_inputs["revision"]
        or bool(mix_inputs["missing_segment_ids"])
        or not Path(str(dubbed_item.get("audio_path") or "")).is_file()
        or not Path(str(dubbed_item.get("preview_video_path") or "")).is_file()
    ):
        with get_connection() as db:
            db.execute(
                "UPDATE dubbed_outputs SET status='stale', updated_at=? WHERE id=? AND status='completed'",
                (now_iso(), dubbed_item["id"]),
            )
        dubbed_item = None
    latest = _task(latest_task["id"]) if latest_task else None
    if latest:
        live_status = task_state.validate_live_status(
            latest.get("status"), latest.get("heartbeat_at") or latest.get("last_heartbeat") or latest.get("updated_at"),
            task_state.TASK_TIMEOUT_SECONDS.get(latest.get("task_type", "voice_segments"), 120),
        )
        if live_status == "interrupted" and latest.get("status") == "running":
            _update_task(
                latest["id"], status="interrupted", error_code="TASK_INTERRUPTED",
                error_message="任务已中断，请继续失败项或重新开始。", completed_at=now_iso(),
            )
            latest = _task(latest["id"])
    latest = _current_task_for_assets(
        latest,
        tts_unfinished_segment_ids=tts_unfinished_segment_ids,
        mix_ready=bool(mix_inputs["required_segment_ids"]) and not mix_inputs["missing_segment_ids"],
    )
    config = tts_config.public_status()
    if not config["configured"]:
        status = "disabled"
    elif latest and latest["status"] == "interrupted":
        status = "interrupted"
    elif latest and latest["status"] in ACTIVE_TASK_STATUSES:
        status = "generating"
    elif latest and latest["status"] == "failed":
        status = "failed"
    elif latest and latest["status"] == "paused":
        status = "paused"
    elif latest and latest["status"] == "cancelled":
        status = "cancelled"
    elif eligible and completed_count == len(eligible) and dubbed_item:
        status = "completed"
    elif completed_count or failed_count:
        status = "partial"
    else:
        status = "ready"
    if dubbed_item:
        dubbed_item["include_target_subtitles"] = bool(dubbed_item["include_target_subtitles"])
        dubbed_item["audio_url"] = _public_path(dubbed_item.get("audio_path"))
        dubbed_item["preview_video_url"] = _public_path(dubbed_item.get("preview_video_path"))
        dubbed_item["mix_input_manifest"] = json_load(dubbed_item.pop("mix_input_manifest_json", "{}"), {})
        mix_usage = json_load(latest_mix_task["usage_json"], {}) if latest_mix_task else {}
        dubbed_item["eligible_count"] = mix_usage.get("eligible_count")
        dubbed_item["mixed_count"] = mix_usage.get("mixed_count")
    public_audio_assets = _public_audio_assets_readiness(
        episode_id,
        float(video_duration or episode.get("duration") or 0),
    )
    usage_summary = {"provider_calls": 0, "cache_hits": 0, "character_cost": 0}
    for task_row in task_rows:
        if task_row["task_type"] not in {"voice_preview", "voice_segments"}:
            continue
        usage = json_load(task_row["usage_json"], {})
        for key in usage_summary:
            usage_summary[key] += int(usage.get(key) or 0)
    task_summary = {
        "pending_count": int(bool(latest and latest["status"] == "pending")),
        "running_count": int(bool(latest and latest["status"] == "running")),
        "interrupted_count": int(bool(latest and latest["status"] == "interrupted")),
    }
    missing_translation_count = len([item for item in segments if item["subtitle_enabled"] and not _tts_text(item)])
    return {
        "episode_id": episode_id,
        "project_id": episode["project_id"],
        "processing_mode": episode["processing_mode"],
        "tts_provider": config,
        "selected_tts_provider": active_provider,
        "tts_providers": _tts_provider_options(),
        "status": status,
        "language_mode": segments[0].get("localization_mode") if segments else None,
        "text_authority": segments[0].get("localization_text_field") if segments else None,
        "target_language": target_language,
        "target_locale": language_capabilities.normalize_target_language(target_language).locales[0],
        "voices": [
            _voice_catalog_item(row)
            for row in voices
            if _voice_is_language_compatible(row, target_language)
        ],
        "bindings": bindings,
        "speaker_profiles": speaker_profiles,
        "characters": [dict(row) | {"confirmed": bool(row["confirmed"])} for row in characters],
        "speakers": sorted(
            {item["dubbing_speaker"] for item in segments}
            | {
                str(item["speaker_label"])
                for item in bindings
                if item.get("speaker_label") and not item.get("character_id")
            }
        ),
        "segments": segment_items,
        "tts_unfinished_segment_ids": tts_unfinished_segment_ids,
        "mix_required_segment_ids": mix_inputs["required_segment_ids"],
        "mix_eligible_segment_ids": mix_eligible_segment_ids,
        "mix_missing_segment_ids": mix_inputs["missing_segment_ids"],
        "mix_input_manifest": mix_inputs["manifest"],
        "mix_revision": mix_inputs["revision"],
        "mix_ready": bool(mix_inputs["required_segment_ids"]) and not mix_inputs["missing_segment_ids"],
        "mix_ineligible_reasons": mix_ineligible_reasons,
        "mix_settings": mix_settings,
        "latest_task": latest,
        "summary": {
            "total_count": len(eligible), "completed_count": completed_count,
            "failed_count": failed_count, "duration_warning_count": warning_count,
            "needs_shorter_count": warning_count, "qualified_count": max(0, completed_count - warning_count),
            **task_summary,
            "transcript_count": len(segments), "eligible_count": len(eligible),
            "speaker_count": len(speaker_profiles),
            "dialogue_segment_count": len(segments),
            "voice_status_counts": voice_status_counts,
            "all_speakers_have_voice": not missing_voice_speakers,
            "missing_voice_speakers": missing_voice_speakers,
            "missing_translation_count": missing_translation_count, "stale_count": stale_count + orphan_count,
            "orphan_count": orphan_count, "provider_calls": usage_summary["provider_calls"],
            "cache_hits": usage_summary["cache_hits"], "character_cost": usage_summary["character_cost"],
        },
        "dubbed_output": dubbed_item,
        "audio_assets": public_audio_assets,
        "capabilities": {
            "tts": "real", "mixing": "demucs_dialogue_replacement",
            "voice_separation": "demucs", "lip_sync": "disabled",
        },
    }


def _save_voice_catalog(voices: list[dict[str, Any]], provider: str) -> None:
    timestamp = now_iso()
    with get_connection() as db:
        for voice in voices:
            labels = dict(voice.get("labels") or {})
            metadata = dict(voice.get("metadata") or {})
            if provider == "edge":
                locale = str(voice.get("locale") or "").strip()
                language = str(voice.get("language") or "").strip().lower()
                gender = str(voice.get("gender") or "Unknown").strip()
                labels.update({"language": language, "locale": locale, "gender": gender})
                metadata.update({
                    "display_name": voice.get("display_name") or voice["name"],
                    "locale": locale,
                    "language": language,
                    "gender_or_role": gender,
                    "model": voice.get("model") or "edge-neural",
                })
            db.execute(
                "INSERT INTO voice_catalog_cache (voice_id, provider, name, category, labels_json, description, preview_url, metadata_json, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(voice_id) DO UPDATE SET provider=excluded.provider, name=excluded.name, category=excluded.category, labels_json=excluded.labels_json, "
                "description=excluded.description, preview_url=excluded.preview_url, metadata_json=excluded.metadata_json, fetched_at=excluded.fetched_at",
                (
                    voice["voice_id"], provider, voice["name"], voice.get("category"), json_dump(labels),
                    voice.get("description"), voice.get("preview_url"),
                    json_dump({
                        **metadata,
                        "verified_languages": voice.get("verified_languages", []),
                        "high_quality_base_model_ids": voice.get("high_quality_base_model_ids", []),
                    }),
                    timestamp,
                ),
            )
        voice_ids = [str(voice["voice_id"]) for voice in voices]
        if voice_ids:
            placeholders = ",".join("?" for _ in voice_ids)
            db.execute(
                f"DELETE FROM voice_catalog_cache WHERE provider=? AND voice_id NOT IN ({placeholders})",
                (provider, *voice_ids),
            )


def _ensure_local_tencent_voice_catalog() -> dict[str, Any]:
    try:
        catalog = tencent_tts_voice_catalog.load_tencent_voice_catalog()
    except tencent_tts_voice_catalog.TencentVoiceCatalogError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "technical": error.detail},
        ) from error
    rows = tencent_tts_voice_catalog.provider_voice_rows(catalog)
    with get_connection() as db:
        cached = db.execute(
            "SELECT COUNT(*) AS count FROM voice_catalog_cache WHERE provider='tencent' "
            "AND json_extract(metadata_json, '$.catalog_version')=?",
            (catalog["catalog_version"],),
        ).fetchone()
    if int(cached["count"] or 0) != len(rows):
        _save_voice_catalog(rows, "tencent")
    return catalog


def _fail_task(task_id: str, error: Exception) -> None:
    code, message = _error_parts(error)
    with get_connection() as db:
        row = db.execute(
            "SELECT usage_json FROM voice_generation_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    usage = json_load(row["usage_json"], {}) if row else {}
    usage.update(_provider_error_usage(error))
    if isinstance(error, provider_service.ProviderError):
        usage.update({
            "provider_calls": max(0, int(error.attempt_count or 0)),
            "character_cost": 0,
        })
    _update_task(
        task_id,
        status="failed",
        error_code=code,
        error_message=message,
        usage_json=json_dump(usage),
        completed_at=now_iso(),
    )


def _segment_binding(
    episode: dict[str, Any],
    segment: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    voice_override: str | None,
    tts_config: provider_service.TTSConfiguration,
) -> dict[str, Any]:
    if voice_override:
        with get_connection() as db:
            voice = db.execute(
                "SELECT voice_id, name, labels_json, metadata_json FROM voice_catalog_cache WHERE voice_id=? AND provider=?",
                (voice_override, tts_config.provider),
            ).fetchone()
        if not voice or not _voice_is_language_compatible(voice, _target_language(episode["id"])):
            raise provider_service.ProviderError(
                "VOICE_LANGUAGE_INCOMPATIBLE",
                "The selected preview voice is not compatible with the current target language.",
            )
        return {"voice_id": voice_override, "provider": tts_config.provider, "model": tts_config.model}
    binding = bindings.get(segment["dubbing_speaker"])
    if not binding:
        raise provider_service.ProviderError(
            "VOICE_BINDING_REQUIRED", f"Select a voice for {segment['dubbing_speaker']} before generating speech."
        )
    return binding


def _provider_speed_ratio_hint(episode_id: str, segment_id: str) -> float:
    """Reuse the latest measured fit multiplier without issuing an exploratory TTS call."""
    with get_connection() as db:
        rows = db.execute(
            "SELECT speed_adjustment, usage_json FROM voice_segment_outputs "
            "WHERE episode_id=? AND transcript_segment_id=? "
            "AND generated_duration IS NOT NULL ORDER BY updated_at DESC",
            (episode_id, segment_id),
        ).fetchall()
    for row in rows:
        usage = json_load(row["usage_json"], {})
        candidates = (
            row["speed_adjustment"],
            usage.get("speed_adjustment"),
            usage.get("speed_ratio"),
        )
        valid_ratios: list[float] = []
        for candidate in candidates:
            try:
                ratio = float(candidate)
            except (TypeError, ValueError):
                continue
            if math.isfinite(ratio) and 1.0 <= ratio <= MAX_MILD_TEMPO_RATIO:
                valid_ratios.append(ratio)
        if valid_ratios:
            return max(valid_ratios)
    return 1.0


def _edge_silence_trim_candidate_path(raw_path: Path) -> Path:
    return raw_path.with_name(f"{raw_path.stem}-silence-trim-candidate.wav")


def _aligned_output_path(episode: dict[str, Any], output: dict[str, Any]) -> Path:
    segment_id = str(output["transcript_segment_id"])
    revision = str(output.get("tts_input_hash") or "current")[:12]
    output_suffix = str(output["id"])[-6:]
    return _output_directory(episode) / "aligned" / f"{segment_id}-{revision}-{output_suffix}.wav"


def _normalize_tencent_wav_boundaries(source_path: Path, output_path: Path) -> dict[str, Any]:
    """Remove only leading/trailing provider silence; preserve all spoken and internal audio."""
    ffmpeg = media_service.resolve_ffmpeg_binary()
    if not ffmpeg:
        raise media_service.MediaToolError(
            "FFMPEG_NOT_FOUND",
            "FFmpeg is required to normalize Tencent TTS WAV boundaries.",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(source_path),
        "-af",
        (
            "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,"
            "areverse,"
            "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB,"
            "areverse"
        ),
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise media_service.MediaToolError(
            "TTS_AUDIO_NORMALIZATION_FAILED",
            "Tencent TTS WAV boundary normalization failed.",
            provider_service.redact_sensitive_text(result.stderr)[-4000:],
        )
    source_duration = media_service.media_duration_seconds(source_path)
    normalized_duration = media_service.media_duration_seconds(output_path)
    if normalized_duration <= 0 or normalized_duration > source_duration + 0.005:
        output_path.unlink(missing_ok=True)
        raise media_service.MediaToolError(
            "TTS_AUDIO_NORMALIZATION_INVALID",
            "Tencent TTS WAV boundary normalization produced an invalid duration.",
        )
    return {
        "provider_audio_path": str(source_path),
        "provider_audio_duration": source_duration,
        "boundary_normalized_duration": normalized_duration,
    }


def _normalize_tts_chunk_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[^\S\n]+", " ", normalized)
    normalized = re.sub(r"\n+", "\n", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized).strip()
    normalized = re.sub(r"\s+([,.?!;:])", r"\1", normalized)
    return normalized


def _split_tencent_tts_text(text: str, limit: int = TENCENT_TTS_CHUNK_CHAR_LIMIT) -> list[str]:
    """Split without rewriting, preferring sentence, comma, then word boundaries."""
    if limit < 1:
        raise ValueError("Tencent TTS chunk limit must be positive.")
    remaining = _normalize_tts_chunk_text(text)
    if not remaining:
        raise provider_service.ProviderError("TTS_TEXT_EMPTY", "The final translation is empty.")
    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        sentence_breaks = [
            match.end()
            for match in re.finditer(r"(?:[.?!;:](?=\s|$)|\n)", window)
            if match.end() <= limit
        ]
        comma_breaks = [
            match.end()
            for match in re.finditer(r",(?=\s|$)", window)
            if match.end() <= limit
        ]
        word_breaks = [
            match.start()
            for match in re.finditer(r"\s+", window)
            if 0 < match.start() <= limit
        ]
        split_at = (
            max(sentence_breaks)
            if sentence_breaks
            else max(comma_breaks)
            if comma_breaks
            else max(word_breaks)
            if word_breaks
            else 0
        )
        if split_at <= 0:
            raise provider_service.ProviderError(
                "TTS_CHUNK_WORD_TOO_LONG",
                f"One word exceeds the Tencent TTS {limit}-character request limit.",
            )
        chunk = _normalize_tts_chunk_text(remaining[:split_at])
        if not chunk or len(chunk) > limit:
            raise provider_service.ProviderError(
                "TTS_CHUNK_INVALID",
                "Unable to split the Tencent TTS text within the provider request limit.",
            )
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    final_chunk = _normalize_tts_chunk_text(remaining)
    if final_chunk:
        chunks.append(final_chunk)
    if not chunks or any(len(chunk) > limit for chunk in chunks):
        raise provider_service.ProviderError(
            "TTS_CHUNK_INVALID",
            "Unable to split the Tencent TTS text within the provider request limit.",
        )
    reconstructed = _normalize_tts_chunk_text(" ".join(chunks))
    source_normalized = _normalize_tts_chunk_text(remaining if len(chunks) == 1 else text).replace("\n", " ")
    source_normalized = _normalize_tts_chunk_text(source_normalized)
    if reconstructed.replace("\n", " ") != source_normalized.replace("\n", " "):
        raise provider_service.ProviderError(
            "TTS_CHUNK_CONTENT_MISMATCH",
            "Tencent TTS chunking changed the source text.",
        )
    return chunks


def _tencent_tts_chunk_hash(
    *,
    provider: str,
    voice_type: str,
    chunk_text: str,
    speed_ratio: float,
    parent_revision: str,
) -> str:
    material = {
        "provider": provider,
        "voice_type": voice_type,
        "chunk_text": chunk_text,
        "speed": round(float(speed_ratio), 6),
        "parent_revision": parent_revision,
    }
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _concat_tencent_tts_chunks(
    chunk_paths: list[Path],
    output_path: Path,
    sample_rate: int,
) -> float:
    if not chunk_paths:
        raise media_service.MediaToolError("TTS_CHUNKS_EMPTY", "Tencent TTS returned no audio chunks.")
    ffmpeg = media_service.resolve_ffmpeg_binary()
    if not ffmpeg:
        raise media_service.MediaToolError(
            "FFMPEG_NOT_FOUND",
            "FFmpeg is required to concatenate Tencent TTS chunks.",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), "-y"]
    for chunk_path in chunk_paths:
        command.extend(["-i", str(chunk_path)])
    inputs = "".join(f"[{index}:a]" for index in range(len(chunk_paths)))
    command.extend([
        "-filter_complex",
        f"{inputs}concat=n={len(chunk_paths)}:v=0:a=1[outa]",
        "-map",
        "[outa]",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise media_service.MediaToolError(
            "TTS_CHUNK_CONCAT_FAILED",
            "Unable to concatenate Tencent TTS chunks.",
            provider_service.redact_sensitive_text(result.stderr)[-4000:],
        )
    duration = media_service.media_duration_seconds(output_path)
    if duration <= 0:
        output_path.unlink(missing_ok=True)
        raise media_service.MediaToolError(
            "TTS_CHUNK_CONCAT_INVALID",
            "The concatenated Tencent TTS audio is not readable.",
        )
    return duration


def _previous_tencent_chunk_manifest(
    episode_id: str,
    segment_id: str,
    tts_input_hash: str,
    output_id: str,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT usage_json FROM voice_segment_outputs "
            "WHERE episode_id=? AND transcript_segment_id=? AND tts_input_hash=? AND id<>? "
            "ORDER BY updated_at DESC",
            (episode_id, segment_id, tts_input_hash, output_id),
        ).fetchall()
    for row in rows:
        manifest = json_load(row["usage_json"], {}).get("tts_child_chunks")
        if isinstance(manifest, dict) and isinstance(manifest.get("chunks"), list):
            return manifest
    return None


def _persist_tencent_chunk_manifest(output_id: str, manifest: dict[str, Any]) -> None:
    with get_connection() as db:
        current = db.execute(
            "SELECT usage_json FROM voice_segment_outputs WHERE id=?",
            (output_id,),
        ).fetchone()
        usage = json_load(current["usage_json"] if current else "{}", {})
        usage["tts_child_chunks"] = manifest
        db.execute(
            "UPDATE voice_segment_outputs SET usage_json=?, heartbeat_at=?, updated_at=? WHERE id=?",
            (json_dump(usage), now_iso(), now_iso(), output_id),
        )


def _generate_tencent_chunked_audio(
    *,
    episode_id: str,
    segment: dict[str, Any],
    text: str,
    voice_id: str,
    speed_ratio: float,
    parent_revision: str,
    output_id: str,
    output_path: Path,
    tts_config: provider_service.TTSConfiguration,
) -> dict[str, Any]:
    chunks = _split_tencent_tts_text(text)
    previous = _previous_tencent_chunk_manifest(
        episode_id,
        segment["id"],
        parent_revision,
        output_id,
    )
    previous_by_hash = {
        str(item.get("chunk_hash")): item
        for item in (previous or {}).get("chunks", [])
        if isinstance(item, dict) and item.get("chunk_hash")
    }
    chunk_directory = output_path.parent / "chunks" / f"{segment['id']}-{parent_revision[:16]}"
    chunk_directory.mkdir(parents=True, exist_ok=True)
    provider = provider_service.get_tts_provider(tts_config)
    provider_speed = provider_service.tencent_speed_for_ratio(speed_ratio)
    manifest: dict[str, Any] = {
        "version": 1,
        "parent_segment_id": segment["id"],
        "parent_revision": parent_revision,
        "provider": "tencent",
        "voice_type": voice_id,
        "speaker": segment.get("dubbing_speaker"),
        "speed_ratio": float(speed_ratio),
        "provider_speed": provider_speed,
        "volume": 0.0,
        "chunk_char_limit": TENCENT_TTS_CHUNK_CHAR_LIMIT,
        "chunk_count": len(chunks),
        "provider_call_count": 0,
        "cache_hit_count": 0,
        "chunks": [],
    }
    request_ids: list[str] = []
    character_cost = 0
    chunk_paths: list[Path] = []
    for index, chunk_text in enumerate(chunks):
        chunk_hash = _tencent_tts_chunk_hash(
            provider="tencent",
            voice_type=voice_id,
            chunk_text=chunk_text,
            speed_ratio=speed_ratio,
            parent_revision=parent_revision,
        )
        chunk_path = chunk_directory / f"{index + 1:03d}-{chunk_hash[:16]}.wav"
        previous_item = previous_by_hash.get(chunk_hash)
        chunk_item: dict[str, Any] = {
            "chunk_id": f"{segment['id']}-chunk-{index + 1:03d}",
            "index": index,
            "parent_segment_id": segment["id"],
            "chunk_hash": chunk_hash,
            "text": chunk_text,
            "character_count": len(chunk_text),
            "provider": "tencent",
            "voice_type": voice_id,
            "speed_ratio": float(speed_ratio),
            "volume": 0.0,
            "status": "pending",
            "audio_path": str(chunk_path),
        }
        manifest["chunks"].append(chunk_item)
        reusable = False
        if previous_item and previous_item.get("status") == "ready":
            previous_path = Path(str(previous_item.get("audio_path") or ""))
            try:
                reusable = bool(
                    previous_path.is_file()
                    and previous_path.stat().st_size > 0
                    and media_service.media_duration_seconds(previous_path) > 0
                )
            except Exception:
                reusable = False
            if reusable:
                chunk_item.update({
                    "status": "ready",
                    "audio_path": str(previous_path),
                    "duration": float(previous_item.get("duration") or media_service.media_duration_seconds(previous_path)),
                    "provider_request_id": previous_item.get("provider_request_id"),
                    "character_cost": int(previous_item.get("character_cost") or 0),
                    "cache_hit": True,
                })
                manifest["cache_hit_count"] += 1
                chunk_paths.append(previous_path)
                _persist_tencent_chunk_manifest(output_id, manifest)
                continue
        try:
            manifest["provider_call_count"] += 1
            result = provider.generate_speech(
                chunk_text,
                voice_id,
                session_id=f"{segment['id']}-{parent_revision[:10]}-{index + 1:03d}-{chunk_hash[:8]}",
                speed_ratio=speed_ratio,
            )
            provider_path = chunk_path.with_name(f"{chunk_path.stem}-provider.wav")
            provider_path.write_bytes(result["audio"])
            _normalize_tencent_wav_boundaries(provider_path, chunk_path)
            chunk_duration = media_service.media_duration_seconds(chunk_path)
            request_id = str(result.get("provider_request_id") or "")
            chunk_cost = int(result.get("usage", {}).get("character_cost") or len(chunk_text))
            if request_id:
                request_ids.append(request_id)
            character_cost += chunk_cost
            chunk_item.update({
                "status": "ready",
                "duration": chunk_duration,
                "provider_request_id": request_id or None,
                "character_cost": chunk_cost,
                "cache_hit": False,
            })
            chunk_paths.append(chunk_path)
            _persist_tencent_chunk_manifest(output_id, manifest)
        except Exception as error:
            code, message = _error_parts(error)
            chunk_item.update({
                "status": "failed",
                "error_code": code,
                "error_message": message,
                "cache_hit": False,
            })
            _persist_tencent_chunk_manifest(output_id, manifest)
            if isinstance(error, provider_service.ProviderError):
                error.attempt_count = max(
                    int(error.attempt_count or 0),
                    int(manifest["provider_call_count"]),
                )
            raise
    combined_duration = _concat_tencent_tts_chunks(
        chunk_paths,
        output_path,
        tts_config.sample_rate,
    )
    manifest["status"] = "ready"
    manifest["combined_audio_path"] = str(output_path)
    manifest["combined_duration"] = combined_duration
    _persist_tencent_chunk_manifest(output_id, manifest)
    return {
        "provider_call": int(manifest["provider_call_count"]),
        "cache_hit": int(manifest["cache_hit_count"]),
        "provider_request_id": request_ids[-1] if request_ids else None,
        "usage": {
            "character_cost": character_cost,
            "tts_child_chunks": manifest,
        },
    }


def _existing_cache(
    episode_id: str,
    segment_id: str,
    output_kind: str,
    voice_id: str,
    model: str,
    tts_input_hash: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? AND status='completed' "
            "AND tts_input_hash=? AND voice_id=? AND model=? "
            "ORDER BY CASE WHEN idempotency_key=? THEN 0 ELSE 1 END, completed_at DESC",
            (episode_id, segment_id, tts_input_hash, voice_id, model, idempotency_key),
        ).fetchall()
    for row in rows:
        item = dict(row)
        usage = json_load(item.get("usage_json"), {})
        capped_speed_warning = (
            usage.get("duration_fit_status")
            == duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
        )
        if (
            (bool(item.get("duration_warning")) and not capped_speed_warning)
            or item.get("error_code") == "TTS_DURATION_EXCEEDED"
        ):
            continue
        raw_exists = bool(item.get("raw_audio_path") and Path(item["raw_audio_path"]).is_file())
        aligned_exists = bool(item.get("aligned_audio_path") and Path(item["aligned_audio_path"]).is_file())
        if item["output_kind"] == output_kind and raw_exists and (output_kind == "preview" or aligned_exists):
            return item
        if output_kind == "segment" and item["output_kind"] == "preview" and raw_exists:
            return item
    return None


def _tts_idempotency_key(
    segment_id: str,
    target_text: str,
    voice_id: str,
    model_id: str,
    generation_settings: dict[str, Any],
    *,
    tts_input_hash: str | None = None,
) -> str:
    material = {
        "segment_id": segment_id, "target_text": target_text, "voice_id": voice_id,
        "model_id": model_id, "generation_settings": generation_settings,
        "tts_input_hash": tts_input_hash,
    }
    return "tts:" + hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _record_output_start(
    episode_id: str,
    segment: dict[str, Any],
    output_kind: str,
    binding: dict[str, Any],
    source_hash: str,
    tts_input_hash: str,
    idempotency_key: str,
    output_format: str,
) -> str:
    output_id = f"vso-{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "INSERT INTO voice_segment_outputs (id, episode_id, transcript_segment_id, output_kind, provider, model, voice_id, source_text_hash, tts_input_hash, idempotency_key, target_language, output_format, target_duration, status, started_at, created_at, updated_at, heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generating', ?, ?, ?, ?)",
            (
                output_id, episode_id, segment["id"], output_kind, binding["provider"], binding["model"], binding["voice_id"],
                source_hash, tts_input_hash, idempotency_key, _target_language(episode_id), output_format,
                (segment["end_ms"] - segment["start_ms"]) / 1000, timestamp, timestamp, timestamp, timestamp,
            ),
        )
    return output_id


def _generate_one(
    episode: dict[str, Any], segment: dict[str, Any], output_kind: str,
    bindings: dict[str, dict[str, Any]], voice_override: str | None,
    tts_provider: provider_service.TTSProvider | None = None,
) -> dict[str, Any]:
    tts_config = _episode_tts_configuration(episode)
    binding = _segment_binding(episode, segment, bindings, voice_override, tts_config)
    text = _tts_text(segment)
    if not text:
        raise provider_service.ProviderError("TTS_TEXT_EMPTY", "The current dubbing text is empty.")
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tts_input_hash = tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
    speed_ratio = (
        _provider_speed_ratio_hint(episode["id"], segment["id"])
        if tts_config.provider == "tencent"
        else 1.0
    )
    generation_settings = {"output_kind": output_kind, "format": tts_config.output_format}
    if tts_config.provider == "tencent":
        generation_settings["speed_ratio"] = speed_ratio
        if len(_normalize_tts_chunk_text(text)) > TENCENT_TTS_CHUNK_CHAR_LIMIT:
            generation_settings["chunk_char_limit"] = TENCENT_TTS_CHUNK_CHAR_LIMIT
            generation_settings["chunk_strategy"] = "sentence_comma_word"
    idempotency_key = _tts_idempotency_key(
        segment["id"],
        text,
        binding["voice_id"],
        binding["model"],
        generation_settings,
        tts_input_hash=tts_input_hash,
    )
    if output_kind == "segment":
        with get_connection() as db:
            existing_rows = db.execute(
                "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? AND output_kind='segment' ORDER BY updated_at DESC",
                (episode["id"], segment["id"]),
            ).fetchall()
        for row in existing_rows:
            if row["idempotency_key"] == idempotency_key and row["status"] in {"generating", "queued", "running"}:
                raise provider_service.ProviderError("TTS_TASK_ALREADY_RUNNING", "This TTS request is already running.")
    cached = _existing_cache(
        episode["id"],
        segment["id"],
        output_kind,
        binding["voice_id"],
        binding["model"],
        tts_input_hash,
        idempotency_key,
    )
    if cached and cached["output_kind"] == output_kind:
        return {"provider_call": 0, "cache_hit": 1, "output_id": cached["id"]}
    output_id = _record_output_start(
        episode["id"],
        segment,
        output_kind,
        binding,
        source_hash,
        tts_input_hash,
        idempotency_key,
        tts_config.output_format,
    )
    directory = _output_directory(episode)
    suffix = tts_config.output_format.split("_", 1)[0]
    if suffix not in {"mp3", "wav", "pcm", "opus", "ulaw", "alaw"}:
        suffix = "audio"
    raw_path = directory / ("previews" if output_kind == "preview" else "segments") / (
        f"{segment['id']}-{tts_input_hash[:12]}-{output_id[-6:]}.{suffix}"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    provider_call = 0
    usage: dict[str, Any] = {}
    request_id = None
    try:
        if cached and cached.get("raw_audio_path") and Path(cached["raw_audio_path"]).is_file():
            shutil.copy2(Path(cached["raw_audio_path"]), raw_path)
            cache_hit = 1
        else:
            if (
                tts_config.provider == "tencent"
                and len(_normalize_tts_chunk_text(text)) > TENCENT_TTS_CHUNK_CHAR_LIMIT
            ):
                chunked = _generate_tencent_chunked_audio(
                    episode_id=episode["id"],
                    segment=segment,
                    text=text,
                    voice_id=binding["voice_id"],
                    speed_ratio=speed_ratio,
                    parent_revision=tts_input_hash,
                    output_id=output_id,
                    output_path=raw_path,
                    tts_config=tts_config,
                )
                provider_call = chunked["provider_call"]
                cache_hit = chunked["cache_hit"]
                usage = chunked["usage"]
                request_id = chunked["provider_request_id"]
            else:
                provider = tts_provider or provider_service.get_tts_provider(tts_config)
                result = provider.generate_speech(
                    text,
                    binding["voice_id"],
                    session_id=f"{segment['id']}-{tts_input_hash[:16]}",
                    speed_ratio=speed_ratio,
                )
                if tts_config.provider == "tencent":
                    provider_raw_path = raw_path.with_name(f"{raw_path.stem}-provider.wav")
                    provider_raw_path.write_bytes(result["audio"])
                    boundary_usage = _normalize_tencent_wav_boundaries(provider_raw_path, raw_path)
                else:
                    raw_path.write_bytes(result["audio"])
                    boundary_usage = {}
                provider_call = 1
                cache_hit = 0
                usage = {**result.get("usage", {}), **boundary_usage}
                request_id = result.get("provider_request_id")
        generated_duration = media_service.media_duration_seconds(raw_path)
        target_duration = _segment_timeline_window(episode, segment)["available_duration"]
        aligned_path = None
        aligned_duration = generated_duration
        speed_adjustment = 1.0
        duration_warning = False
        if output_kind == "segment":
            candidate_aligned_path = directory / "aligned" / (
                f"{segment['id']}-{tts_input_hash[:12]}-{output_id[-6:]}.wav"
            )
            silence_trim_candidate_path = (
                _edge_silence_trim_candidate_path(raw_path)
                if str(binding.get("provider") or tts_config.provider).strip().lower() == "edge"
                else None
            )
            alignment = media_service.align_tts_audio(
                raw_path,
                candidate_aligned_path,
                target_duration,
                maximum_ratio=MAX_MILD_TEMPO_RATIO,
                silence_trim_candidate_path=silence_trim_candidate_path,
            )
            if alignment.get("silence_trim"):
                usage = {**usage, "silence_trim": alignment["silence_trim"]}
            aligned_duration = alignment["aligned_duration"]
            speed_adjustment = alignment["speed_adjustment"]
            duration_warning = alignment["duration_warning"]
            capped_speed_warning = bool(alignment.get("capped_speed_warning"))
            aligned_path = candidate_aligned_path if not duration_warning or capped_speed_warning else None
            if duration_warning:
                usage = {
                    **usage,
                    "status": (
                        duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
                        if capped_speed_warning
                        else "pending_regeneration"
                    ),
                    "duration_fit_status": (
                        duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
                        if capped_speed_warning
                        else "manual_review_required"
                    ),
                    "needs_shorter_text": (
                        not capped_speed_warning
                        and segment.get("localization_mode") != localization_text_authority.DUB_ONLY
                    ),
                    "needs_attention": True,
                    "warning_code": "TTS_DURATION_CAPPED" if capped_speed_warning else "TTS_DURATION_UNFIT",
                    "shorter_text": None,
                    "original_text": segment["source_text"], "target_text": _tts_text(segment),
                    "original_duration": _segment_timeline_window(episode, segment)["original_duration"],
                    "available_duration": target_duration, "speed_ratio": alignment["duration_ratio"],
                    "maximum_speed": MAX_MILD_TEMPO_RATIO,
                    "slot_overrun_seconds": alignment.get("slot_overrun_seconds", 0.0),
                }
        timestamp = now_iso()
        with get_connection() as db:
            db.execute(
                "UPDATE voice_segment_outputs SET status='stale', error_code='TTS_SUPERSEDED', "
                "error_message='A newer TTS revision completed for this segment.', updated_at=? "
                "WHERE episode_id=? AND transcript_segment_id=? AND output_kind=? AND id<>? "
                "AND status='completed'",
                (timestamp, episode["id"], segment["id"], output_kind, output_id),
            )
            db.execute(
                "UPDATE voice_segment_outputs SET raw_audio_path=?, aligned_audio_path=?, generated_duration=?, aligned_duration=?, speed_adjustment=?, duration_warning=?, status='completed', provider_request_id=?, usage_json=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    str(raw_path), str(aligned_path) if aligned_path else None, generated_duration, aligned_duration,
                    speed_adjustment, bool_int(duration_warning), request_id, json_dump(usage), timestamp, timestamp, output_id,
                ),
            )
            if output_kind == "segment":
                db.execute("UPDATE transcript_segments SET stale_for_voice=0 WHERE id=?", (segment["id"],))
                _invalidate_dubbed_outputs(db, episode["id"], timestamp)
        return {"provider_call": provider_call, "cache_hit": cache_hit, "usage": usage, "output_id": output_id}
    except Exception as error:
        code, message = _error_parts(error)
        failure_usage = _provider_error_usage(error)
        with get_connection() as db:
            current = db.execute(
                "SELECT usage_json FROM voice_segment_outputs WHERE id=?",
                (output_id,),
            ).fetchone()
            stored_usage = json_load(current["usage_json"] if current else "{}", {})
            db.execute(
                "UPDATE voice_segment_outputs SET status='failed', error_code=?, error_message=?, usage_json=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    code,
                    message,
                    json_dump({**stored_usage, **failure_usage}),
                    now_iso(),
                    now_iso(),
                    output_id,
                ),
            )
        raise


def _run_voice_task(
    task_id: str, episode_id: str, segment_ids: list[str], output_kind: str,
    voice_override: str | None = None,
    auto_mix: bool = True,
    stop_on_provider_failure: bool = False,
) -> None:
    episode = _episode(episode_id)
    tts_provider = provider_service.get_tts_provider(_episode_tts_configuration(episode))
    segments = [item for item in _segments(episode_id) if item["id"] in set(segment_ids)]
    bindings = _binding_map(episode)
    existing = _task(task_id)
    completed = int(existing.get("completed_count") or 0)
    failed = int(existing.get("failed_count") or 0)
    provider_calls = 0
    cache_hits = 0
    character_cost = 0
    first_error: tuple[str, str] | None = None
    first_error_usage: dict[str, Any] = {}
    _update_task(task_id, status="generating_voice", started_at=existing.get("started_at") or now_iso(), completed_at=None, error_code=None, error_message=None)
    for index, segment in enumerate(segments, 1):
        control = _task(task_id).get("control_requested")
        if control == "pause":
            _update_task(task_id, status="paused", control_requested="", current_segment_id=None, error_code=None, error_message=None)
            return
        if control == "cancel":
            _update_task(task_id, status="cancelled", control_requested="", current_segment_id=None, completed_at=now_iso())
            return
        _update_task(
            task_id, current_segment_id=segment["id"], status="generating_voice",
            progress=max(1, round((index - 1) / max(1, len(segments)) * 100)),
        )
        try:
            result = _generate_one(
                episode, segment, output_kind, bindings, voice_override, tts_provider
            )
            completed += 1
            provider_calls += result.get("provider_call", 0)
            cache_hits += result.get("cache_hit", 0)
            cost = result.get("usage", {}).get("character_cost")
            if isinstance(cost, int):
                character_cost += cost
            if output_kind == "segment":
                _update_task(task_id, status="aligning")
        except Exception as error:
            failed += 1
            if isinstance(error, provider_service.ProviderError):
                provider_calls += max(0, int(error.attempt_count or 0))
            if first_error is None:
                first_error = _error_parts(error)
                first_error_usage = _provider_error_usage(error)
            if stop_on_provider_failure and isinstance(error, provider_service.ProviderError):
                _update_task(task_id, completed_count=completed, failed_count=failed)
                break
        _update_task(task_id, completed_count=completed, failed_count=failed)
    status = "completed" if failed == 0 else ("partial" if completed else "failed")
    usage = {
        "provider_calls": provider_calls,
        "cache_hits": cache_hits,
        "character_cost": character_cost,
        **first_error_usage,
    }
    error_code = "VOICE_SEGMENTS_PARTIAL" if status == "partial" else (first_error[0] if first_error else None)
    error_message = None
    if status == "partial":
        error_message = f"{failed} voice segment(s) failed. First error: {first_error[1] if first_error else 'unknown error'}"
    elif status == "failed" and first_error:
        error_message = first_error[1]
    _update_task(
        task_id, status=status, progress=100, current_segment_id=None, completed_count=completed,
        failed_count=failed, error_code=error_code,
        error_message=error_message,
        usage_json=json_dump(usage), completed_at=now_iso(),
    )
    if output_kind == "segment" and auto_mix:
        _auto_rebuild_mix_preview(episode_id)


def recover_pending_voice_tasks() -> None:
    """A restart must never silently issue another paid request."""
    with get_connection() as db:
        db.execute(
            "UPDATE voice_generation_tasks SET status='paused', control_requested='', "
            "error_code='TASK_INTERRUPTED', error_message='The task was paused after the server restarted.', "
            "updated_at=? WHERE status IN ('queued','generating_voice','aligning','mixing','rendering_preview')",
            (now_iso(),),
        )


def _run_mix_task(
    task_id: str,
    episode_id: str,
    selected_ids: list[str] | None = None,
    settings_override: dict[str, Any] | None = None,
    expected_mix_revision: str | None = None,
    expected_mix_manifest: dict[str, Any] | None = None,
) -> None:
    episode = _episode(episode_id)
    try:
        _update_task(task_id, status="mixing", progress=20, started_at=now_iso())
        with get_connection() as db:
            _invalidate_dubbed_outputs(db, episode_id)
        mix_inputs = _load_mix_inputs(episode_id)
        if expected_mix_revision and mix_inputs["revision"] != expected_mix_revision:
            raise media_service.MediaToolError(
                "DUBBING_MIX_INPUT_CHANGED",
                "The TTS inputs changed before the mix started. Refresh and create the preview again.",
            )
        if expected_mix_manifest and mix_inputs["manifest"] != expected_mix_manifest:
            raise media_service.MediaToolError(
                "DUBBING_MIX_INPUT_CHANGED",
                "The TTS input manifest changed before the mix started. Refresh and create the preview again.",
            )
        if mix_inputs["missing_segment_ids"]:
            raise media_service.MediaToolError(
                "DUBBING_SEGMENTS_INCOMPLETE",
                "Latest TTS audio is missing for segment(s): "
                + ", ".join(mix_inputs["missing_segment_ids"]),
            )
        segments = _segments(episode_id)
        directory = _output_directory(episode)
        version = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        mix_segments = []
        missing_segment_ids: list[str] = []
        blocked_segments: list[str] = []
        completed_output_count = 0
        required_ids = set(mix_inputs["required_segment_ids"])
        if selected_ids and set(selected_ids) != required_ids:
            raise media_service.MediaToolError(
                "DUBBING_MIX_SELECTION_STALE",
                "The selected voice segments no longer match the current TTS manifest.",
            )
        ordered_segments = [
            item for item in sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
            if item["id"] in required_ids
        ]
        try:
            video_duration = media_service.media_duration_seconds(_video_path(episode))
        except Exception:
            video_duration = None
        timeline_windows = {
            item["id"]: _timeline_window(item, ordered_segments[index + 1] if index + 1 < len(ordered_segments) else None, video_duration)
            for index, item in enumerate(ordered_segments)
        }
        for segment in ordered_segments:
            output = mix_inputs["outputs"].get(segment["id"])
            if not output or output["status"] != "completed":
                missing_segment_ids.append(segment["id"])
                logger.error(
                    "A2 missing latest TTS segment_id=%s speaker=%s start_ms=%s end_ms=%s output_status=%s",
                    segment["id"], segment["dubbing_speaker"], segment["start_ms"], segment["end_ms"],
                    output.get("status") if output else None,
                )
                continue
            window = timeline_windows[segment["id"]]
            generated_duration = float(output.get("generated_duration") or 0)
            if output.get("duration_fit_status") not in DURATION_FIT_STATUSES:
                blocked_segments.append(segment["id"])
                continue
            fitted_audio_path = output.get("aligned_audio_path")
            fitted_audio = Path(str(fitted_audio_path)) if fitted_audio_path else None
            logger.info(
                "A2 TTS timeline speaker=%s seq=%s target_text=%r original_duration=%.3f tts_duration=%.3f",
                segment["dubbing_speaker"], segment["seq"], _tts_text(segment), window["original_duration"], generated_duration,
            )
            logger.info(
                "A2 TTS file segment_id=%s path=%s exists=%s adelay_ms=%s",
                segment["id"], fitted_audio_path, bool(fitted_audio_path and fitted_audio.is_file()), int(segment["start_ms"]),
            )
            if not fitted_audio or not fitted_audio.is_file() or fitted_audio.stat().st_size <= 0:
                missing_segment_ids.append(segment["id"])
                continue
            fitted_duration = media_service.media_duration_seconds(fitted_audio)
            capped_speed_warning = (
                output.get("duration_fit_status")
                == duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
            )
            if fitted_duration > window["original_duration"] + 0.005 and not capped_speed_warning:
                raw_audio_path = Path(str(output.get("raw_audio_path") or ""))
                if raw_audio_path.is_file() and raw_audio_path.stat().st_size > 0:
                    repaired_audio = fitted_audio.with_name(
                        f"{fitted_audio.stem}-current-fit{fitted_audio.suffix}"
                    )
                    silence_trim_candidate_path = (
                        _edge_silence_trim_candidate_path(raw_audio_path)
                        if str(output.get("provider") or "").strip().lower() == "edge"
                        else None
                    )
                    repair = media_service.align_tts_audio(
                        raw_audio_path,
                        repaired_audio,
                        window["available_duration"],
                        maximum_ratio=MAX_MILD_TEMPO_RATIO,
                        silence_trim_candidate_path=silence_trim_candidate_path,
                    )
                    repair_capped_warning = bool(repair.get("capped_speed_warning"))
                    if (not repair["duration_warning"] or repair_capped_warning) and repaired_audio.is_file():
                        fit_status = (
                            duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
                            if repair_capped_warning
                            else "speed_adjusted_and_fit"
                            if float(repair["speed_adjustment"]) > 1.0001
                            else "fit"
                        )
                        usage = dict(output.get("usage") or {})
                        if repair.get("silence_trim"):
                            usage["silence_trim"] = repair["silence_trim"]
                        usage.update({
                            "duration_fit_status": fit_status,
                            "final_tts_duration": repair["aligned_duration"],
                            "available_duration": window["available_duration"],
                            "speed_ratio": repair["duration_ratio"],
                            "atempo_chain": repair.get("atempo_chain"),
                            "status": fit_status,
                            "needs_shorter_text": False,
                            "needs_attention": repair_capped_warning,
                            "warning_code": "TTS_DURATION_CAPPED" if repair_capped_warning else None,
                            "maximum_speed": MAX_MILD_TEMPO_RATIO,
                            "slot_overrun_seconds": repair.get("slot_overrun_seconds", 0.0),
                            "derived_repair": {
                                "reason": "aligned_file_duration_mismatch",
                                "previous_path": str(fitted_audio),
                                "previous_duration": fitted_duration,
                            },
                        })
                        with get_connection() as db:
                            changed = db.execute(
                                "UPDATE voice_segment_outputs SET aligned_audio_path=?, aligned_duration=?, "
                                "speed_adjustment=?, duration_warning=?, error_code=NULL, error_message=NULL, "
                                "usage_json=?, updated_at=? WHERE id=? AND status='completed' AND tts_input_hash=?",
                                (
                                    str(repaired_audio), repair["aligned_duration"], repair["speed_adjustment"],
                                    bool_int(repair_capped_warning),
                                    json_dump(usage), now_iso(), output["id"], output["tts_input_hash"],
                                ),
                            ).rowcount
                        if changed != 1:
                            raise media_service.MediaToolError(
                                "DUBBING_MIX_INPUT_CHANGED",
                                "The current TTS asset changed while repairing its derived timeline file.",
                            )
                        fitted_audio = repaired_audio
                        fitted_duration = media_service.media_duration_seconds(fitted_audio)
                        capped_speed_warning = repair_capped_warning
            if fitted_duration > window["original_duration"] + 0.005 and not capped_speed_warning:
                blocked_segments.append(segment["id"])
                continue
            audio_end_ms = int(segment["start_ms"] + round(fitted_duration * 1000))
            completed_output_count += 1
            mix_segments.append({
                "seq": segment["seq"], "segment_id": segment["id"], "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"], "audio_end_ms": audio_end_ms, "audio_path": str(fitted_audio),
                "aligned_duration": fitted_duration, "generated_duration": generated_duration or fitted_duration,
                "speed_adjustment": float(output.get("speed_adjustment") or 1.0),
            })
            logger.info(
                "A2 fitted TTS selected seq=%s start_ms=%s end_ms=%s audio_end_ms=%s tts_duration=%.3f speed_adjustment=%.6f",
                segment["seq"], segment["start_ms"], segment["end_ms"], audio_end_ms, fitted_duration,
                float(output.get("speed_adjustment") or 1.0),
            )
        logger.info("A2 mix candidates completed=%s mixed=%s blocked=%s missing=%s", completed_output_count, len(mix_segments), len(blocked_segments), len(missing_segment_ids))
        if blocked_segments:
            raise media_service.MediaToolError(
                "TTS_DURATION_EXCEEDED",
                f"Regenerate these dialogue segments with shorter English text: {', '.join(blocked_segments)}.",
            )
        if missing_segment_ids:
            raise media_service.MediaToolError(
                "DUBBING_SEGMENTS_INCOMPLETE",
                f"Completed TTS audio is missing for segment(s): {', '.join(missing_segment_ids)}.",
            )
        if completed_output_count != len(mix_segments):
            raise media_service.MediaToolError(
                "DUBBING_SEGMENTS_INCOMPLETE",
                f"Only {len(mix_segments)} of {completed_output_count} completed TTS segment(s) were mixed.",
            )
        if completed_output_count != len(ordered_segments):
            raise media_service.MediaToolError(
                "DUBBING_SEGMENTS_INCOMPLETE",
                f"Only {completed_output_count} of {len(ordered_segments)} selected TTS segment(s) were available.",
            )
        if not mix_segments:
            raise media_service.MediaToolError("DUBBING_SEGMENTS_REQUIRED", "Generate at least one current voice segment before mixing.")
        if not video_duration or video_duration <= 0:
            raise media_service.MediaToolError("DUBBING_VIDEO_DURATION_MISSING", "The source video duration is unavailable.")
        settings = settings_override or _ensure_mix_settings(episode_id)
        ducking_segments = _dialogue_ducking_segments(
            segments,
            {str(item["segment_id"]) for item in mix_segments},
        )
        mix_path = directory / f"a2-mix-demucs-{version}.wav"
        tts_only_path = directory / f"a2-tts-only-{version}.wav"
        tts_only_result = media_service.render_tts_only_audio(
            mix_segments,
            tts_only_path,
            video_duration,
            fade_ms=int(settings["fade_ms"]),
        )
        tts_readiness = duration_constrained_tts_service.provider_free_episode_readiness(episode_id)
        if not tts_readiness["ready"]:
            raise media_service.MediaToolError(
                "DUBBING_SEGMENTS_INCOMPLETE",
                "The current TTS revision is not ready for a Demucs-backed preview.",
            )
        _update_task(task_id, status="separating_audio", progress=42)
        demucs_stems = audio_localization_service.prepare_dubbing_preview_stems(
            episode_id,
            task_id=task_id,
            source_video=_video_path(episode),
            duration=video_duration,
            audio_revision=str(tts_readiness["audio_revision"]),
            mix_revision=str(mix_inputs["revision"]),
        )
        _update_task(
            task_id,
            status="mixing",
            progress=60,
            usage_json=json_dump({
                "demucs_stems": demucs_stems,
                "mix_revision": mix_inputs["revision"],
                "mix_input_manifest": mix_inputs["manifest"],
            }),
        )
        mix_result = media_service.mix_demucs_localization_audio(
            Path(str(demucs_stems["no_vocals"]["path"])),
            tts_only_path,
            mix_path,
            video_duration,
            vocals_path=Path(str(demucs_stems["vocals"]["path"])),
            dialogue_segments=ducking_segments,
            settings=settings,
        )
        _update_task(task_id, status="rendering_preview", progress=70)
        language_value = _target_language(episode_id)
        target_capability = language_capabilities.normalize_target_language(language_value)
        with get_connection() as db:
            subtitle_style_row = db.execute("SELECT * FROM subtitle_styles WHERE episode_id=?", (episode_id,)).fetchone()
        subtitle_style = dict(subtitle_style_row) if subtitle_style_row else None
        subtitle_path: Path | None = None
        if settings["include_target_subtitles"]:
            subtitle_segments = [
                segment for segment in _segments(episode_id)
                if segment.get("subtitle_enabled") and _tts_text(segment)
            ]
            video_metadata = media_service.probe_media_metadata(_video_path(episode))
            subtitle_cues = audio_localization_service.build_renderable_subtitle_cues(
                subtitle_segments,
                int(video_metadata["width"]),
                int(video_metadata["height"]),
                preferred_font_size=int((subtitle_style or {}).get("font_size") or 48),
                target_language=target_capability.id,
            )
            subtitle_path = directory / f"a2-subtitles-{target_capability.code}-{version}.ass"
            audio_localization_service._write_overseas_ass(
                subtitle_segments,
                subtitle_path,
                int(video_metadata["width"]),
                int(video_metadata["height"]),
                subtitle_position={"bottom": "low", "middle": "standard", "top": "high"}.get(
                    str((subtitle_style or {}).get("position") or "bottom"),
                    "low",
                ),
                cues=subtitle_cues,
            )
            subtitle_style = {
                **(subtitle_style or {}),
                "use_embedded_ass_style": True,
                "max_lines": 2,
            }
        preview_path = directory / f"a2-preview-two-track-{version}.mp4"
        preview_result = media_service.render_dubbed_preview(_video_path(episode), mix_path, preview_path, subtitle_path, subtitle_style)
        final_mix_inputs = _load_mix_inputs(episode_id)
        if final_mix_inputs["revision"] != mix_inputs["revision"] or final_mix_inputs["missing_segment_ids"]:
            raise media_service.MediaToolError(
                "DUBBING_MIX_INPUT_CHANGED",
                "A TTS segment changed while the preview was rendering. The outdated preview was not published.",
            )
        timestamp = now_iso()
        output_id = f"dub-{uuid.uuid4().hex[:12]}"
        with get_connection() as db:
            _invalidate_dubbed_outputs(db, episode_id, timestamp)
            db.execute(
                "INSERT INTO dubbed_outputs (id, episode_id, task_id, audio_path, preview_video_path, mix_mode, target_language, include_target_subtitles, status, actual_duration, mix_input_manifest_json, mix_revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'demucs_dialogue_replacement', ?, ?, 'completed', ?, ?, ?, ?, ?)",
                (
                    output_id, episode_id, task_id, str(mix_path), str(preview_path),
                    _target_language(episode_id), bool_int(bool(subtitle_path)), preview_result["duration"],
                    json_dump(final_mix_inputs["manifest"]), final_mix_inputs["revision"], timestamp, timestamp,
                ),
            )
        _update_task(
            task_id, status="completed", progress=100, completed_count=len(mix_segments), completed_at=now_iso(),
            error_code=None, error_message=None,
            usage_json=json_dump({
                "eligible_count": len(ordered_segments), "mixed_count": len(mix_segments),
                "mixed_segments": len(mix_segments), "mixed_segment_ids": [item["segment_id"] for item in mix_segments],
                "mix_duration": mix_result["duration"], "preview_url": _public_path(preview_path),
                "audio_url": _public_path(mix_path), "output_path": _public_path(preview_path),
                 "tts_only_url": _public_path(tts_only_result["path"]),
                 "tts_only_path": _public_path(tts_only_result["path"]),
                 "background_audio_path": _public_path(Path(str(demucs_stems["no_vocals"]["path"]))),
                 "ducked_audio_path": _public_path(Path(str(demucs_stems["no_vocals"]["path"]))),
                 "output_audio_path": _public_path(mix_path),
                 "preview_video_path": _public_path(preview_path),
                 "ducked_interval_count": len(ducking_segments),
                 "ducked_forced_silence_count": sum(1 for item in ducking_segments if item.get("force_silence")),
                 "amix_inputs": mix_result["amix_inputs"],
                 "amix_normalize": mix_result["normalize"],
                 "mix_revision": final_mix_inputs["revision"],
                 "mix_input_manifest": final_mix_inputs["manifest"],
                 "dubbed_output_id": output_id,
                 "demucs_stems": demucs_stems,
             }),
         )
    except Exception as error:
        _fail_task(task_id, error)


def _auto_rebuild_mix_preview(episode_id: str) -> str | None:
    """Rebuild once when every required segment has a current readable TTS revision."""
    mix_inputs = _load_mix_inputs(episode_id)
    selected_ids = list(mix_inputs["required_segment_ids"])
    if not selected_ids or mix_inputs["missing_segment_ids"]:
        return None
    with get_connection() as db:
        current = db.execute(
            "SELECT id FROM dubbed_outputs "
            "WHERE episode_id=? AND status='completed' AND mix_revision=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (episode_id, mix_inputs["revision"]),
        ).fetchone()
    if current or _active_task(episode_id):
        return None
    episode = _episode(episode_id)
    task = _create_task(episode, "dubbing_preview", len(selected_ids))
    _set_task_segments(task["id"], selected_ids)
    _run_mix_task(
        task["id"],
        episode_id,
        selected_ids,
        _ensure_mix_settings(episode_id),
        mix_inputs["revision"],
        mix_inputs["manifest"],
    )
    return task["id"]


@router.post("/episodes/{episode_id}/dubbing-segments/{segment_id}/shorter-candidate")
def create_shorter_candidate(episode_id: str, segment_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    if localization_text_authority.resolve_episode_text_authority(episode_id).language_mode == localization_text_authority.DUB_ONLY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUB_ONLY_SHORTENING_DISABLED",
                "message": "Original-language dubbing cannot call translation to shorten transcript text.",
            },
        )
    segments = [item for item in _segments(episode_id) if _is_tts_eligible(item)]
    ordered = sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    index = next((value for value, item in enumerate(ordered) if item["id"] == segment_id), -1)
    if index < 0:
        raise HTTPException(status_code=404, detail="Transcript segment not found.")
    segment = ordered[index]
    bindings = _binding_map(episode)
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? AND output_kind='segment' ORDER BY updated_at DESC",
            (episode_id, segment_id),
        ).fetchall()
    output = _current_output(segment, bindings.get(segment["dubbing_speaker"]), list(rows))
    if not output or not output.get("needs_shorter_text"):
        raise HTTPException(status_code=409, detail={"code": "SHORTER_CANDIDATE_NOT_REQUIRED", "message": "This line does not currently require shorter text."})
    previous_text = ordered[index - 1]["final_translation"] if index else None
    next_text = ordered[index + 1]["final_translation"] if index + 1 < len(ordered) else None
    window = _segment_timeline_window(episode, segment)
    try:
        adapter = provider_service.get_translation_provider()
        if not hasattr(adapter, "suggest_shorter_text"):
            raise provider_service.ProviderError("TRANSLATION_PROVIDER_DISABLED", "DeepSeek shortening is not configured.")
        candidate = adapter.suggest_shorter_text(
            segment["source_text"], segment["final_translation"], segment["dubbing_speaker"],
            window["available_duration"], previous_text, next_text, _target_language(episode_id),
        )
    except provider_service.ProviderError as error:
        raise HTTPException(status_code=400, detail={"code": error.code, "message": str(error), "technical": error.detail}) from error
    usage = dict(output.get("usage") or {})
    usage.update({
        "shorter_text": candidate["shorter_text"], "meaning_preserved": candidate["meaning_preserved"],
        "candidate_status": "pending_confirmation", "candidate_provider": candidate["provider"],
        "candidate_model": candidate["model"], "candidate_request_id": candidate.get("provider_request_id"),
    })
    with get_connection() as db:
        db.execute(
            "UPDATE voice_segment_outputs SET usage_json=?, updated_at=? WHERE id=?",
            (json_dump(usage), now_iso(), output["id"]),
        )
    return _state(episode_id)


@router.put("/episodes/{episode_id}/dubbing-segments/{segment_id}/shorter-candidate")
def accept_shorter_candidate(episode_id: str, segment_id: str, payload: ShorterTextBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    if localization_text_authority.resolve_episode_text_authority(episode_id).language_mode == localization_text_authority.DUB_ONLY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUB_ONLY_SHORTENING_DISABLED",
                "message": "Original-language transcript text cannot be replaced by a translation candidate.",
            },
        )
    shorter_text = (payload.shorter_text or "").strip()
    segment = next((item for item in _segments(episode_id) if item["id"] == segment_id), None)
    if not segment:
        raise HTTPException(status_code=404, detail="Transcript segment not found.")
    if not shorter_text or shorter_text == segment["final_translation"].strip():
        raise HTTPException(status_code=400, detail={"code": "SHORTER_TEXT_UNCHANGED", "message": "Confirm a changed shorter English line before retrying."})
    with get_connection() as db:
        row = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? AND output_kind='segment' ORDER BY updated_at DESC LIMIT 1",
            (episode_id, segment_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No failed voice output exists for this line.")
        usage = json_load(row["usage_json"], {})
        if not usage.get("needs_shorter_text") and not bool(row["duration_warning"]):
            raise HTTPException(status_code=409, detail={"code": "SHORTER_CANDIDATE_NOT_REQUIRED", "message": "This line is already eligible for mixing."})
        usage.update({
            "shorter_text": shorter_text, "confirmed_shorter_text": shorter_text,
            "candidate_status": "accepted", "status": "pending_regeneration",
            "needs_shorter_text": True, "target_text": segment["final_translation"].strip(),
        })
        db.execute(
            "UPDATE transcript_segments SET final_translation=?, stale_for_voice=1 WHERE id=?",
            (shorter_text, segment_id),
        )
        db.execute(
            "UPDATE voice_segment_outputs SET status='pending_regeneration', error_code=NULL, error_message=NULL, usage_json=?, updated_at=? WHERE id=?",
            (json_dump(usage), now_iso(), row["id"]),
        )
        _invalidate_dubbed_outputs(db, episode_id)
    return _state(episode_id)


@router.delete("/episodes/{episode_id}/dubbing-segments/{segment_id}/shorter-candidate")
def discard_shorter_candidate(episode_id: str, segment_id: str) -> dict[str, Any]:
    _episode(episode_id)
    with get_connection() as db:
        row = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? AND output_kind='segment' ORDER BY updated_at DESC LIMIT 1",
            (episode_id, segment_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Voice output not found.")
        usage = json_load(row["usage_json"], {})
        usage["shorter_text"] = None
        usage["candidate_status"] = "discarded"
        db.execute("UPDATE voice_segment_outputs SET usage_json=?, updated_at=? WHERE id=?", (json_dump(usage), now_iso(), row["id"]))
    return _state(episode_id)


@router.get("/episodes/{episode_id}/dubbing")
def get_dubbing_state(episode_id: str) -> dict[str, Any]:
    return _state(episode_id)


@router.put("/episodes/{episode_id}/tts-provider")
def select_episode_tts_provider(episode_id: str, payload: TtsProviderBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    selected = payload.provider.strip().lower()
    if selected not in TTS_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={"code": "TTS_PROVIDER_UNSUPPORTED", "message": f"Unsupported TTS provider: {selected}."},
        )
    _require_no_active_task(episode_id)
    previous = _episode_tts_provider(episode)
    settings = json_load(episode.get("settings_override"), {})
    settings["tts_provider"] = selected
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "UPDATE episodes SET settings_override=?, updated_at=? WHERE id=?",
            (json_dump(settings), timestamp, episode_id),
        )
        if previous != selected:
            segment_rows = db.execute(
                "SELECT s.id FROM transcript_segments s "
                "JOIN transcript_documents d ON d.id=s.document_id WHERE d.episode_id=?",
                (episode_id,),
            ).fetchall()
            _mark_voice_outputs_stale(
                db,
                episode_id,
                [str(row["id"]) for row in segment_rows],
                f"TTS provider changed from {previous or 'unset'} to {selected}.",
            )
    return _state(episode_id)


@router.get("/voice-generation-tasks/{task_id}")
def get_voice_task(task_id: str) -> dict[str, Any]:
    return _task(task_id)


@router.post("/episodes/{episode_id}/tts-voices/refresh")
def refresh_voices(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    config = _episode_tts_configuration(episode)
    _require_no_active_task(episode_id)
    task = _create_task(episode, "voice_catalog", 0)
    _update_task(task["id"], status="generating_voice", started_at=now_iso())
    provider = provider_service.get_tts_provider(config)
    try:
        voices = provider.list_voices()
    except provider_service.ProviderError as error:
        _fail_task(task["id"], error)
        raise HTTPException(status_code=400, detail={"code": error.code, "message": str(error), "technical": error.detail}) from error
    _save_voice_catalog(voices, config.provider)
    _update_task(
        task["id"], status="completed", progress=100, completed_at=now_iso(),
        usage_json=json_dump({
            "voice_count": len(voices),
            "provider_calls": max(0, int(getattr(provider, "last_attempt_count", 0) or 0)),
            "billable": False,
            "character_cost": 0,
        }),
    )
    return _state(episode_id)


@router.post("/episodes/{episode_id}/tts-voices/{voice_type}/preview")
def preview_tencent_catalog_voice(episode_id: str, voice_type: int) -> dict[str, Any]:
    episode = _episode(episode_id)
    config = _episode_tts_configuration(episode)
    if config.provider != "tencent":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VOICE_PREVIEW_PROVIDER_UNSUPPORTED",
                "message": "Tencent voice preview requires TTS_PROVIDER=tencent.",
            },
        )
    catalog = _ensure_local_tencent_voice_catalog()
    voice = tencent_tts_voice_catalog.find_voice(catalog, voice_type)
    if not voice:
        raise HTTPException(
            status_code=404,
            detail={"code": "VOICE_NOT_FOUND", "message": f"未知腾讯音色 ID {voice_type}"},
        )
    if tencent_tts_voice_catalog.TEXT_TO_VOICE_ENDPOINT not in voice["supported_endpoints"]:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VOICE_NOT_TEXT_TO_VOICE_COMPATIBLE",
                "message": "The selected Tencent voice does not support TextToVoice.",
            },
        )
    cache_key = hashlib.sha256(json_dump({
        "provider": "tencent",
        "voice_type": voice_type,
        "text": TENCENT_VOICE_PREVIEW_TEXT,
        "catalog_version": catalog["catalog_version"],
        "model": config.model,
        "codec": config.codec,
        "sample_rate": config.sample_rate,
    }).encode("utf-8")).hexdigest()
    output_directory = _output_directory(episode) / "voice-catalog-previews"
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"tencent-{voice_type}-{cache_key[:16]}.wav"
    if output_path.is_file() and output_path.stat().st_size > 0:
        try:
            duration = media_service.media_duration_seconds(output_path)
        except Exception:
            duration = 0
        if duration > 0:
            return {
                "voice_id": f"tencent:{voice_type}",
                "voice_name": voice["name"],
                "catalog_version": catalog["catalog_version"],
                "preview_text": TENCENT_VOICE_PREVIEW_TEXT,
                "audio_url": _public_path(str(output_path)),
                "duration": duration,
                "cached": True,
                "provider_calls": 0,
                "provider_request_id": None,
            }
        output_path.unlink(missing_ok=True)
    provider = provider_service.get_tts_provider(config)
    try:
        generated = provider.generate_speech(
            TENCENT_VOICE_PREVIEW_TEXT,
            f"tencent:{voice_type}",
            session_id=f"voice-preview-{cache_key[:24]}",
            speed_ratio=1.0,
        )
    except provider_service.ProviderError as error:
        raise HTTPException(
            status_code=400,
            detail={"code": error.code, "message": str(error), "technical": error.detail},
        ) from error
    audio = generated.get("audio")
    if not isinstance(audio, bytes) or not audio:
        raise HTTPException(
            status_code=502,
            detail={"code": "TTS_AUDIO_INVALID", "message": "Tencent Cloud returned empty preview audio."},
        )
    temporary_path = output_path.with_suffix(".tmp.wav")
    try:
        temporary_path.write_bytes(audio)
        duration = media_service.media_duration_seconds(temporary_path)
        if duration <= 0:
            raise media_service.MediaToolError(
                "TTS_AUDIO_INVALID",
                "Tencent Cloud returned an unreadable preview audio file.",
            )
        temporary_path.replace(output_path)
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        if isinstance(error, media_service.MediaToolError):
            raise HTTPException(
                status_code=502,
                detail={"code": error.code, "message": str(error)},
            ) from error
        raise
    return {
        "voice_id": f"tencent:{voice_type}",
        "voice_name": voice["name"],
        "catalog_version": catalog["catalog_version"],
        "preview_text": TENCENT_VOICE_PREVIEW_TEXT,
        "audio_url": _public_path(str(output_path)),
        "duration": duration,
        "cached": False,
        "provider_calls": 1,
        "provider_request_id": generated.get("provider_request_id"),
    }


@router.put("/episodes/{episode_id}/speaker-voice-bindings")
def save_bindings(episode_id: str, payload: BindingBatch) -> dict[str, Any]:
    episode = _episode(episode_id)
    config = _episode_tts_configuration(episode)
    episode_provider_override = config.provider != provider_service.tts_configuration().provider
    timestamp = now_iso()
    with get_connection() as db:
        for item in payload.bindings:
            speaker = item.speaker_label.strip()
            if not speaker or len(speaker) > 64 or any(ord(character) < 32 for character in speaker):
                raise HTTPException(status_code=400, detail="Speaker label is invalid.")
            if not item.voice_id.strip():
                raise HTTPException(status_code=400, detail={"code": "VOICE_ID_REQUIRED", "message": f"Select a real voice for {speaker}."})
            voice = db.execute(
                "SELECT voice_id, name, labels_json, metadata_json FROM voice_catalog_cache WHERE voice_id=? AND provider=?",
                (item.voice_id, config.provider),
            ).fetchone()
            if not voice:
                provider_label = {
                    "tencent": "Tencent",
                    "elevenlabs": "ElevenLabs",
                    "edge": "Edge",
                }.get(config.provider, config.provider)
                raise HTTPException(status_code=400, detail={"code": "VOICE_ID_NOT_FOUND", "message": f"The selected voice for {speaker} is not in the current {provider_label} voice catalog."})
            if not _voice_is_language_compatible(voice, _target_language(episode_id)):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "VOICE_LANGUAGE_INCOMPATIBLE",
                        "message": f"{voice['name']} is not compatible with the current target language.",
                    },
                )
            if config.provider == "tencent":
                metadata = json_load(voice["metadata_json"], {})
                if not bool(metadata.get("text_to_voice_compatible")):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "VOICE_NOT_TEXT_TO_VOICE_COMPATIBLE",
                            "message": f"{voice['name']} does not support TextToVoice.",
                        },
                    )
            affected_speakers = {speaker}
            if item.character_id:
                character = db.execute(
                    "SELECT id FROM characters WHERE id=? AND project_id=?",
                    (item.character_id, episode["project_id"]),
                ).fetchone()
                if not character:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "CHARACTER_NOT_FOUND", "message": "The selected voice character does not belong to this project."},
                    )
                if not episode_provider_override:
                    affected_speakers.update(
                        str(row["speaker_label"])
                        for row in db.execute(
                            "SELECT speaker_label FROM speaker_voice_bindings WHERE project_id=? AND character_id=?",
                            (episode["project_id"], item.character_id),
                        ).fetchall()
                    )
                    db.execute(
                        "UPDATE characters SET voice_id=?, voice_profile_id=?, updated_at=? WHERE id=?",
                        (item.voice_id, item.voice_id, timestamp, item.character_id),
                    )
                    db.execute(
                        "UPDATE speaker_voice_bindings SET voice_id=?, provider=?, model=?, updated_at=? "
                        "WHERE project_id=? AND character_id=?",
                        (item.voice_id, config.provider, config.model, timestamp, episode["project_id"], item.character_id),
                    )
            scope = "project" if item.scope == "project" and episode["processing_mode"] == "character-consistency" else "episode"
            if scope == "project":
                db.execute("DELETE FROM speaker_voice_bindings WHERE project_id=? AND episode_id IS NULL AND speaker_label=?", (episode["project_id"], speaker))
                episode_value = None
                project_default = True
                episode_override = False
            else:
                db.execute("DELETE FROM speaker_voice_bindings WHERE episode_id=? AND speaker_label=?", (episode_id, speaker))
                episode_value = episode_id
                project_default = False
                episode_override = True
            db.execute(
                "INSERT INTO speaker_voice_bindings (id, project_id, episode_id, speaker_label, character_id, provider, model, voice_id, is_project_default, is_episode_override, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"svb-{uuid.uuid4().hex[:12]}", episode["project_id"], episode_value, speaker,
                    item.character_id, config.provider, config.model, item.voice_id,
                    bool_int(project_default), bool_int(episode_override), timestamp, timestamp,
                ),
            )
            segment_ids = [
                segment["id"]
                for segment in _segments(episode_id)
                if segment["dubbing_speaker"] in affected_speakers
            ]
            if segment_ids:
                placeholders = ",".join("?" for _ in segment_ids)
                db.execute(
                    f"UPDATE voice_segment_outputs SET status='failed', error_code='TTS_VOICE_CHANGED', "
                    f"error_message='Voice binding changed; regenerate this voice segment.', updated_at=? "
                    f"WHERE episode_id=? AND transcript_segment_id IN ({placeholders}) AND status IN ('completed','stale')",
                    (timestamp, episode_id, *segment_ids),
                )
                db.execute(f"UPDATE transcript_segments SET stale_for_voice=1 WHERE id IN ({placeholders})", segment_ids)
                _invalidate_dubbed_outputs(db, episode_id, timestamp)
    return _state(episode_id)


@router.put("/episodes/{episode_id}/dubbing-segments/{segment_id}/speaker")
def reassign_dubbing_speaker(episode_id: str, segment_id: str, payload: SpeakerReassignBody) -> dict[str, Any]:
    _episode(episode_id)
    speaker = payload.speaker_label.strip()
    if not speaker:
        raise HTTPException(status_code=400, detail="Speaker label is required.")
    with get_connection() as db:
        row = db.execute(
            "SELECT ts.id FROM transcript_segments ts JOIN transcript_documents td ON td.id=ts.document_id "
            "WHERE ts.id=? AND td.episode_id=?",
            (segment_id, episode_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transcript segment not found.")
        db.execute(
            "UPDATE transcript_segments SET speaker=?, speaker_label=?, stale_for_voice=1, updated_at=? WHERE id=?",
            (speaker, speaker, now_iso(), segment_id),
        )
        db.execute(
            "UPDATE voice_segment_outputs SET status='stale', error_code='TTS_SPEAKER_CHANGED', "
            "error_message='Speaker changed; regenerate this voice segment.', updated_at=? "
            "WHERE episode_id=? AND transcript_segment_id=? "
            "AND status IN ('completed','failed','stale','pending_regeneration','manual_review_required')",
            (now_iso(), episode_id, segment_id),
        )
        _invalidate_dubbed_outputs(db, episode_id)
    return _state(episode_id)


@router.put("/episodes/{episode_id}/dubbing-segments/{segment_id}/role")
def update_dubbing_role(episode_id: str, segment_id: str, payload: DialogueRoleBody) -> dict[str, Any]:
    _episode(episode_id)
    role = payload.segment_type.strip()
    if role not in duration_constrained_tts_service.VOICE_ROLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "DUBBING_ROLE_INVALID",
                "message": "Dialogue role must be on-screen dialogue, off-screen dialogue, narration, or inner monologue.",
            },
        )
    with get_connection() as db:
        row = db.execute(
            "SELECT ts.id FROM transcript_segments ts JOIN transcript_documents td ON td.id=ts.document_id "
            "WHERE ts.id=? AND td.episode_id=?",
            (segment_id, episode_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transcript segment not found.")
        db.execute(
            "UPDATE transcript_segments SET segment_type=?, stale_for_voice=1, updated_at=? WHERE id=?",
            (role, now_iso(), segment_id),
        )
        db.execute(
            "UPDATE voice_segment_outputs SET status='stale', error_code='DUBBING_ROLE_CHANGED', "
            "error_message='Dialogue role changed; regenerate this voice segment.', updated_at=? "
            "WHERE episode_id=? AND transcript_segment_id=? AND status='completed'",
            (now_iso(), episode_id, segment_id),
        )
        _invalidate_dubbed_outputs(db, episode_id)
    return _state(episode_id)


@router.post("/episodes/{episode_id}/dubbing-segments/{segment_id}/resolution")
def resolve_dubbing_segment(episode_id: str, segment_id: str, payload: VoiceResolutionBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    segment = next((item for item in _segments(episode_id) if item["id"] == segment_id), None)
    if not segment:
        raise HTTPException(status_code=404, detail="Transcript segment not found.")
    action = payload.action.strip().lower()
    timestamp = now_iso()
    if action == "restore":
        with get_connection() as db:
            db.execute(
                "UPDATE transcript_segments SET subtitle_enabled=1, review_status='approved', "
                "stale_for_voice=1, updated_at=? WHERE id=?",
                (timestamp, segment_id),
            )
            db.execute(
                "UPDATE voice_segment_outputs SET status='stale', error_code='TTS_LINE_RESTORED', "
                "error_message='The voice line was restored and requires current TTS.', updated_at=? "
                "WHERE episode_id=? AND transcript_segment_id=? "
                "AND status IN ('completed','failed','pending_regeneration','manual_review_required','stale')",
                (timestamp, episode_id, segment_id),
            )
            _invalidate_dubbed_outputs(db, episode_id, timestamp)
        return _state(episode_id)
    if action == "omit":
        with get_connection() as db:
            db.execute(
                "UPDATE transcript_segments SET subtitle_enabled=0, review_status='manually_omitted', "
                "stale_for_voice=1, updated_at=? WHERE id=?",
                (timestamp, segment_id),
            )
            db.execute(
                "UPDATE voice_segment_outputs SET status='stale', error_code='TTS_LINE_OMITTED', "
                "error_message='The user omitted this voice line.', updated_at=? "
                "WHERE episode_id=? AND transcript_segment_id=? AND status IN ('completed','failed','pending_regeneration')",
                (timestamp, episode_id, segment_id),
            )
            _invalidate_dubbed_outputs(db, episode_id, timestamp)
        return _state(episode_id)
    if action != "use_current":
        raise HTTPException(status_code=400, detail="Invalid voice resolution action.")

    binding = _binding_map(episode).get(segment["dubbing_speaker"])
    with get_connection() as db:
        output_rows = db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? "
            "AND output_kind='segment' ORDER BY updated_at DESC",
            (episode_id, segment_id),
        ).fetchall()
    output = _current_output(segment, binding, list(output_rows))
    raw_path = Path(str(output.get("raw_audio_path") if output else ""))
    if not output or not raw_path.is_file() or raw_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "CURRENT_TTS_AUDIO_UNAVAILABLE", "message": "No current audio file is available for this line."},
        )
    existing_aligned_path = Path(str(output.get("aligned_audio_path") or ""))
    if (
        not bool(output.get("duration_warning"))
        and not bool(output.get("needs_shorter_text"))
        and existing_aligned_path.is_file()
        and existing_aligned_path.stat().st_size > 0
    ):
        existing_usage = dict(output.get("usage") or {})
        fit_status = "speed_adjusted_and_fit" if float(output.get("speed_adjustment") or 1) > 1.0001 else "fit"
        fit_input_duration = _duration_fit_input_duration(output)
        normalized_usage = {
            **existing_usage,
            "status": fit_status,
            "duration_fit_status": fit_status,
            "needs_shorter_text": False,
            "final_tts_duration": float(output.get("aligned_duration") or 0),
            "available_duration": _segment_timeline_window(episode, segment)["available_duration"],
        }
        if float(output.get("speed_adjustment") or 1) > 1.0001:
            normalized_usage["atempo_chain"] = f"atempo={float(output['speed_adjustment']):.6f}"
        if fit_input_duration > 0 and normalized_usage["available_duration"] > 0:
            normalized_usage["speed_ratio"] = fit_input_duration / normalized_usage["available_duration"]
        if json_dump(normalized_usage) != json_dump(existing_usage):
            with get_connection() as db:
                db.execute(
                    "UPDATE voice_segment_outputs SET usage_json=?, updated_at=? WHERE id=?",
                    (json_dump(normalized_usage), timestamp, output["id"]),
                )
        return _state(episode_id)
    target_duration = _segment_timeline_window(episode, segment)["available_duration"]
    aligned_path = _aligned_output_path(episode, output)
    silence_trim_candidate_path = (
        _edge_silence_trim_candidate_path(raw_path)
        if str(output.get("provider") or "").strip().lower() == "edge"
        else None
    )
    alignment = media_service.align_tts_audio(
        raw_path,
        aligned_path,
        target_duration,
        maximum_ratio=MAX_MILD_TEMPO_RATIO,
        silence_trim_candidate_path=silence_trim_candidate_path,
    )
    usage = dict(output.get("usage") or {})
    if alignment.get("silence_trim"):
        usage["silence_trim"] = alignment["silence_trim"]
    capped_speed_warning = bool(alignment.get("capped_speed_warning"))
    if alignment["duration_warning"] and not capped_speed_warning:
        usage.update({
            "status": "pending_regeneration",
            "needs_shorter_text": True,
            "available_duration": target_duration,
            "speed_ratio": alignment["duration_ratio"],
        })
        existing_usage = dict(output.get("usage") or {})
        existing_trim = existing_usage.get("silence_trim") if isinstance(existing_usage.get("silence_trim"), dict) else {}
        candidate_path = str((alignment.get("silence_trim") or {}).get("candidate_path") or "")
        already_recorded = (
            bool(candidate_path)
            and str(existing_trim.get("candidate_path") or "") == candidate_path
            and bool(existing_usage.get("needs_shorter_text"))
            and math.isclose(
                float(existing_usage.get("speed_ratio") or 0),
                float(alignment["duration_ratio"]),
                rel_tol=0,
                abs_tol=1e-9,
            )
        )
        if not already_recorded:
            with get_connection() as db:
                db.execute(
                    "UPDATE voice_segment_outputs SET usage_json=?, updated_at=? WHERE id=?",
                    (json_dump(usage), timestamp, output["id"]),
                )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CURRENT_TTS_AUDIO_TOO_LONG",
                "message": "The current audio cannot fit without crossing the permitted timeline boundary.",
            },
        )
    duration_fit_status = (
        duration_constrained_tts_service.CAPPED_SPEED_WARNING_STATUS
        if capped_speed_warning
        else "speed_adjusted_and_fit"
        if float(alignment["speed_adjustment"]) > 1.0001
        else "fit"
    )
    usage.update({
        "duration_fit_status": duration_fit_status,
        "final_tts_duration": alignment["aligned_duration"],
        "available_duration": target_duration,
        "speed_ratio": alignment["duration_ratio"],
        "atempo_chain": alignment.get("atempo_chain"),
        "status": duration_fit_status,
        "needs_shorter_text": False,
        "needs_attention": capped_speed_warning,
        "warning_code": "TTS_DURATION_CAPPED" if capped_speed_warning else None,
        "maximum_speed": MAX_MILD_TEMPO_RATIO,
        "slot_overrun_seconds": alignment.get("slot_overrun_seconds", 0.0),
        "manual_resolution": "use_current",
    })
    with get_connection() as db:
        db.execute(
            "UPDATE voice_segment_outputs SET aligned_audio_path=?, aligned_duration=?, speed_adjustment=?, "
            "duration_warning=?, status='completed', error_code=NULL, error_message=NULL, usage_json=?, updated_at=? WHERE id=?",
            (
                str(aligned_path),
                alignment["aligned_duration"],
                alignment["speed_adjustment"],
                bool_int(capped_speed_warning),
                json_dump(usage),
                timestamp,
                output["id"],
            ),
        )
        db.execute("UPDATE transcript_segments SET stale_for_voice=0, updated_at=? WHERE id=?", (timestamp, segment_id))
        _invalidate_dubbed_outputs(db, episode_id, timestamp)
    return _state(episode_id)


@router.post("/episodes/{episode_id}/tts-preview")
def create_voice_preview(episode_id: str, payload: VoicePreviewBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    episode = _episode(episode_id)
    active = _active_task(episode_id)
    if active:
        return active
    document = _document(episode_id)
    active_document_id = document["id"] if document else None
    segment = next((item for item in _segments(episode_id) if item["id"] == payload.segment_id), None)
    if not segment:
        raise HTTPException(status_code=404, detail={
            "code": "TRANSCRIPT_SEGMENT_NOT_FOUND",
            "message": "Transcript segment is not part of the current episode transcript.",
            "project_id": episode["project_id"], "episode_id": episode_id,
            "requested_segment_id": payload.segment_id, "active_transcript_id": active_document_id,
        })
    task = _create_task(episode, "voice_preview", 1)
    if not _episode_tts_configuration(episode).configured:
        _update_task(task["id"], status="failed", error_code="TTS_PROVIDER_DISABLED", error_message="The configured Text to Speech provider is not available.", completed_at=now_iso())
        return _task(task["id"])
    _set_task_segments(task["id"], [segment["id"]])
    background_tasks.add_task(_run_voice_task, task["id"], episode_id, [segment["id"]], "preview", payload.voice_id)
    return task


@router.delete("/episodes/{episode_id}/tts-previews")
def clear_voice_previews(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    preview_dir = _output_directory(episode) / "previews"
    if preview_dir.is_dir():
        shutil.rmtree(preview_dir)
    with get_connection() as db:
        db.execute("DELETE FROM voice_segment_outputs WHERE episode_id=? AND output_kind='preview'", (episode_id,))
    return _state(episode_id)


@router.post("/episodes/{episode_id}/tts-generate")
def generate_voice_segments(episode_id: str, payload: VoiceGenerateBody, background_tasks: BackgroundTasks) -> dict[str, Any]:
    episode = _episode(episode_id)
    active = _active_task(episode_id)
    if active:
        return active
    authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    if (
        authority.language_mode != localization_text_authority.DUB_ONLY
        and payload.adjust_timeline
        and payload.mode in {"all_unfinished", "selected"}
    ):
        selected_ids = set(payload.segment_ids) if payload.mode == "selected" else None
        _prepare_extreme_short_dialogue(episode_id, selected_ids)
    eligible = [item for item in _segments(episode_id) if _is_tts_eligible(item)]
    if payload.mode == "selected":
        wanted = set(payload.segment_ids)
        eligible = [item for item in eligible if item["id"] in wanted]
    elif payload.mode == "failed":
        failed_ids = set(_state(episode_id)["tts_unfinished_segment_ids"])
        eligible = [item for item in eligible if item["id"] in failed_ids]
    elif payload.mode != "all_unfinished":
        raise HTTPException(status_code=400, detail="Invalid voice generation mode.")
    elif payload.mode == "all_unfinished":
        unfinished_ids = set(_state(episode_id)["tts_unfinished_segment_ids"])
        eligible = [item for item in eligible if item["id"] in unfinished_ids]
    if not eligible:
        raise HTTPException(status_code=400, detail="No eligible transcript segments were selected.")
    _, missing = _resolve_segment_voices(episode, eligible)
    if missing:
        raise HTTPException(status_code=400, detail=f"Select voices for: {', '.join(missing)}.")
    task = _create_task(episode, "voice_segments", len(eligible))
    if not _episode_tts_configuration(episode).configured:
        _update_task(task["id"], status="failed", error_code="TTS_PROVIDER_DISABLED", error_message="The configured Text to Speech provider is not available.", completed_at=now_iso())
        return _task(task["id"])
    _set_task_segments(task["id"], [item["id"] for item in eligible])
    background_tasks.add_task(
        _run_voice_task,
        task["id"], episode_id, [item["id"] for item in eligible], "segment",
        None, payload.auto_mix, payload.stop_on_provider_failure,
    )
    return task


@router.post("/voice-generation-tasks/{task_id}/pause")
def pause_voice_task(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    if task["status"] not in ACTIVE_TASK_STATUSES:
        return task
    _update_task(task_id, control_requested="pause")
    return _task(task_id)


@router.post("/voice-generation-tasks/{task_id}/resume")
def resume_voice_task(task_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    task = _task(task_id)
    if task["status"] not in {"paused", "failed", "partial", "interrupted"}:
        return task
    remaining = _task_remaining_segments(task, task["episode_id"])
    if not remaining:
        return task
    _require_no_active_task(task["episode_id"])
    _update_task(task_id, status="queued", control_requested="", error_code=None, error_message=None, completed_at=None)
    background_tasks.add_task(_run_voice_task, task_id, task["episode_id"], remaining, "segment")
    return _task(task_id)


@router.post("/voice-generation-tasks/{task_id}/cancel")
def cancel_voice_task(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    if task["status"] in TASK_TERMINAL_STATUSES:
        return task
    if task["status"] == "queued":
        _update_task(task_id, status="cancelled", completed_at=now_iso())
    else:
        _update_task(task_id, control_requested="cancel")
    return _task(task_id)


@router.post("/voice-generation-tasks/{task_id}/reset-stuck")
def reset_stuck_voice_task(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    if task["status"] not in ACTIVE_TASK_STATUSES and task["status"] != "paused":
        return task
    _update_task(task_id, status="paused", control_requested="", current_segment_id=None, error_code="TASK_RESET", error_message="Task run lock was cleared; completed audio was preserved.")
    return _task(task_id)


@router.put("/episodes/{episode_id}/audio-mix-settings")
def save_mix_settings(episode_id: str, payload: MixSettingsBody) -> dict[str, Any]:
    _episode(episode_id)
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "INSERT INTO audio_mix_settings (episode_id, dialogue_original_volume, non_dialogue_original_volume, dub_voice_volume, fade_ms, include_target_subtitles, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(episode_id) DO UPDATE SET dialogue_original_volume=excluded.dialogue_original_volume, "
            "non_dialogue_original_volume=excluded.non_dialogue_original_volume, dub_voice_volume=excluded.dub_voice_volume, fade_ms=excluded.fade_ms, "
            "include_target_subtitles=excluded.include_target_subtitles, updated_at=excluded.updated_at",
            (
                episode_id, payload.dialogue_original_volume, payload.non_dialogue_original_volume,
                payload.dub_voice_volume, payload.fade_ms, bool_int(payload.include_target_subtitles), timestamp, timestamp,
            ),
        )
        db.execute("UPDATE dubbed_outputs SET status='stale', updated_at=? WHERE episode_id=? AND status='completed'", (timestamp, episode_id))
    return _state(episode_id)


@router.post("/episodes/{episode_id}/dubbing-preview")
def create_dubbing_preview(episode_id: str, background_tasks: BackgroundTasks, payload: MixPreviewBody | None = None) -> dict[str, Any]:
    episode = _episode(episode_id)
    _require_no_active_task(episode_id)
    _video_path(episode)
    current_settings = _ensure_mix_settings(episode_id)
    settings_override = {
        "episode_id": episode_id,
        "dialogue_original_volume": payload.dialogue_original_volume if payload and payload.dialogue_original_volume is not None else current_settings["dialogue_original_volume"],
        "non_dialogue_original_volume": payload.non_dialogue_original_volume if payload and payload.non_dialogue_original_volume is not None else current_settings["non_dialogue_original_volume"],
        "dub_voice_volume": payload.dub_voice_volume if payload and payload.dub_voice_volume is not None else current_settings["dub_voice_volume"],
        "fade_ms": payload.fade_ms if payload and payload.fade_ms is not None else current_settings["fade_ms"],
        "include_target_subtitles": payload.include_target_subtitles if payload and payload.include_target_subtitles is not None else bool(current_settings["include_target_subtitles"]),
    }
    mix_inputs = _load_mix_inputs(
        episode_id,
        settings_override,
        verify_media=False,
    )
    selected_ids = list(mix_inputs["required_segment_ids"])
    if mix_inputs["missing_segment_ids"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DUBBING_SEGMENTS_INCOMPLETE",
                "message": "Generate the latest TTS for every required segment before mixing.",
                "missing_segment_ids": mix_inputs["missing_segment_ids"],
                "reasons": {
                    segment_id: mix_inputs["reasons"].get(segment_id)
                    for segment_id in mix_inputs["missing_segment_ids"]
                },
            },
        )
    if not selected_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DUBBING_SEGMENTS_REQUIRED",
                "message": "No completed, readable voice outputs are available for mixing.",
                "reasons": mix_inputs["reasons"],
            },
        )
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "UPDATE audio_mix_settings SET dialogue_original_volume=?, non_dialogue_original_volume=?, dub_voice_volume=?, fade_ms=?, include_target_subtitles=?, updated_at=? WHERE episode_id=?",
            (settings_override["dialogue_original_volume"], settings_override["non_dialogue_original_volume"], settings_override["dub_voice_volume"], settings_override["fade_ms"], bool_int(settings_override["include_target_subtitles"]), timestamp, episode_id),
        )
    task = _create_task(episode, "dubbing_preview", len(selected_ids))
    _set_task_segments(task["id"], selected_ids)
    _update_task(task["id"], status="queued", progress=0)
    background_tasks.add_task(
        _run_mix_task,
        task["id"],
        episode_id,
        selected_ids,
        settings_override,
        mix_inputs["revision"],
        mix_inputs["manifest"],
    )
    return _task(task["id"])


@router.get("/episodes/{episode_id}/audio-localization")
def get_audio_localization(episode_id: str, source_task_id: str | None = None) -> dict[str, Any]:
    try:
        return audio_localization_service.get_audio_localization_state(episode_id, source_task_id)
    except audio_localization_service.AudioLocalizationError as error:
        status = 404 if error.code == "EPISODE_NOT_FOUND" else 422
        raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error
    except media_service.MediaToolError as error:
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error


@router.post("/episodes/{episode_id}/audio-localization")
def create_audio_localization(
    episode_id: str,
    payload: AudioLocalizationBody,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    try:
        task, created = audio_localization_service.create_audio_localization_task(
            episode_id,
            source_task_id=payload.source_task_id,
            target_language=payload.target_language,
            voice=payload.voice,
            hide_original_subtitles=payload.hide_original_subtitles,
            original_subtitle_region_start_percent=payload.original_subtitle_region_start_percent,
            original_subtitle_region_height_percent=payload.original_subtitle_region_height_percent,
            subtitle_position=payload.subtitle_position,
        )
        if created:
            background_tasks.add_task(audio_localization_service.run_audio_localization_task, task["id"])
        return task
    except audio_localization_service.AudioLocalizationError as error:
        status = 404 if error.code == "EPISODE_NOT_FOUND" else 422
        raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error
    except media_service.MediaToolError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": str(error), "stderr": error.stderr[-2000:]},
        ) from error


@router.get("/audio-localization/{task_id}")
def get_audio_localization_task(task_id: str) -> dict[str, Any]:
    try:
        return audio_localization_service.get_audio_localization_task(task_id)
    except audio_localization_service.AudioLocalizationError as error:
        status = 404 if error.code == "LOCALIZATION_TASK_NOT_FOUND" else 422
        raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error
