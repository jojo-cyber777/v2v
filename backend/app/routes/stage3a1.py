"""Stage 3 A1 transcription, translation and subtitle routes."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..database import ROOT, bool_int, get_connection, json_dump, json_load, now_iso, row_to_dict
from ..services import (
    audio_localization_service,
    duration_constrained_tts_service,
    language_capabilities,
    localization_text_authority,
    media_service,
    provider_service,
    subtitle_service,
    task_state,
    transcription_commit_service,
)
from . import xmax_thirty_second


router = APIRouter(prefix="/api", tags=["stage-3a1"])
OUTPUTS_DIR = ROOT / "outputs"
SOURCE_TYPES = {"embedded_soft_subtitle", "external_subtitle", "burned_in_subtitle", "speech_only", "unknown"}
SOURCE_MODES = {"subtitle_primary", "speech_primary", "side_by_side"}
SOURCE_PRIORITIES = {"speech_transcription", "subtitle_source", "side_by_side_review"}
RENDER_MODES = {
    "replace_soft_subtitle", "mask_burned_subtitle_and_add_target",
    "keep_original_and_add_target", "target_only", "no_target_subtitle",
}
TRANSLATION_STYLES = {"literal", "natural", "short_drama_localization", "commercial_voiceover"}
TRANSCRIPTION_PROVIDERS = ("tencent", "elevenlabs", "local_whisper_pyannote")


class SourceSelection(BaseModel):
    source_id: str | None = None
    source_type: str = "auto"
    selected_source_mode: str = "subtitle_primary"
    source_priority: str | None = None
    comparison_mode: str | None = None
    overwrite_user_edits: bool = False


class TranscriptionStart(BaseModel):
    source_language: str = "auto"
    overwrite_user_edits: bool = False
    provider: str | None = None


class TranscriptionProviderSelection(BaseModel):
    provider: str


class TranslationStart(BaseModel):
    source_language: str = "auto"
    target_language: str = "english"
    translation_style: str = "natural"
    segment_ids: list[str] = Field(default_factory=list)


class SegmentBody(BaseModel):
    start_ms: int
    end_ms: int
    source_text: str
    source_text_origin: str = "manual"
    speaker: str = "Speaker 1"
    segment_type: str = "dialogue"
    literal_translation: str = ""
    localized_translation: str = ""
    final_translation: str = ""
    translation_status: str = "not_started"
    subtitle_enabled: bool = True
    review_status: str = "pending"


class SegmentUpdate(SegmentBody):
    id: str


class SegmentBatch(BaseModel):
    segments: list[SegmentUpdate]


class SplitBody(BaseModel):
    at_ms: int | None = None


class MergeBody(BaseModel):
    segment_ids: list[str] = Field(min_length=2)
    confirmed_source_text: str | None = None
    confirmed_speaker: str | None = None
    create_new_segment: bool = False


class StyleBody(BaseModel):
    render_mode: str = "replace_soft_subtitle"
    font_family: str = "Arial"
    font_size: int = Field(default=42, ge=18, le=72)
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    background_color: str = "#00000099"
    position: str = "bottom"
    safe_margin_percent: int = Field(default=8, ge=0, le=30)
    max_lines: int = Field(default=2, ge=1, le=4)
    mask_height_percent: int = Field(default=22, ge=10, le=35)


class SubtitlePreviewBody(StyleBody):
    target_language: str | None = None
    mask_enabled: bool = True
    mask_height_ratio: float | None = Field(default=None, ge=0.1, le=0.35)


class BatchReviewBody(BaseModel):
    action: str


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    episode = row_to_dict(row)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def _episode_transcription_provider(episode: dict[str, Any]) -> str:
    settings = json_load(episode.get("settings_override"), {})
    selected = str(settings.get("stt_provider") or "").strip().lower()
    if selected in TRANSCRIPTION_PROVIDERS:
        return selected
    return provider_service.transcription_configuration().provider


def _transcription_provider_options() -> list[dict[str, Any]]:
    labels = {
        "tencent": "Tencent ASR",
        "elevenlabs": "ElevenLabs Scribe",
        "local_whisper_pyannote": "Local Whisper + Pyannote（本地免费）",
    }
    return [
        provider_service.transcription_configuration(provider).public_status()
        | {"id": provider, "display_name": labels[provider]}
        for provider in TRANSCRIPTION_PROVIDERS
    ]


def _video_path(episode: dict[str, Any]) -> Path:
    value = episode.get("video_path")
    if not value or not Path(value).is_file():
        raise HTTPException(status_code=400, detail="Upload the current episode video first.")
    return Path(value)


def _episode_output(episode: dict[str, Any]) -> Path:
    path = OUTPUTS_DIR / episode["project_id"] / episode["id"]
    path.mkdir(parents=True, exist_ok=True)
    return path


def _language_code(value: str | None) -> str:
    return language_capabilities.language_code(value)


def _font_catalog() -> list[dict[str, str]]:
    known = [
        ("Arial", "Arial"), ("Segoe UI", "Segoe UI"), ("Microsoft YaHei", "Microsoft YaHei"),
        ("Noto Sans CJK SC", "Noto Sans CJK SC"), ("DejaVu Sans", "DejaVu Sans"),
        ("Liberation Sans", "Liberation Sans"),
    ]
    available_files = set()
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if windows_fonts.is_dir():
        available_files = {item.stem.lower() for item in windows_fonts.glob("*.ttf")}
    detected: set[str] = set()
    if shutil.which("fc-list"):
        try:
            result = subprocess.run([shutil.which("fc-list") or "fc-list", ": family"], capture_output=True, text=True, timeout=5, check=False)
            detected = {line.split(",", 1)[0].strip() for line in result.stdout.splitlines() if line.strip()}
        except (OSError, subprocess.SubprocessError):
            detected = set()
    catalog: list[dict[str, str]] = []
    for key, label in known:
        stem = key.lower().replace(" ", "")
        if key in detected or not available_files or any(stem in item.replace(" ", "") for item in available_files):
            catalog.append({"id": key, "label": label})
    return catalog or [{"id": "Arial", "label": "Arial"}]


def _mark_voice_outputs_stale(db: Any, episode_id: str, segment_ids: list[str], reason: str) -> None:
    if not segment_ids:
        return
    placeholders = ",".join("?" for _ in segment_ids)
    timestamp = now_iso()
    db.execute(
        f"UPDATE voice_segment_outputs SET status='stale', error_code='VOICE_OUTPUT_STALE', error_message=?, updated_at=? "
        f"WHERE episode_id=? AND transcript_segment_id IN ({placeholders}) AND status IN ('completed','generating','queued')",
        (reason, timestamp, episode_id, *segment_ids),
    )
    db.execute(
        f"UPDATE transcript_segments SET stale_for_voice=1 WHERE id IN ({placeholders})",
        segment_ids,
    )
    _invalidate_dubbed_outputs(db, episode_id, timestamp)


def _invalidate_dubbed_outputs(db: Any, episode_id: str, timestamp: str | None = None) -> None:
    db.execute(
        "UPDATE dubbed_outputs SET status='stale', updated_at=? "
        "WHERE episode_id=? AND status='completed'",
        (timestamp or now_iso(), episode_id),
    )


def _archive_voice_outputs(db: Any, episode_id: str, segment_ids: list[str], reason: str) -> None:
    if not segment_ids:
        return
    placeholders = ",".join("?" for _ in segment_ids)
    rows = db.execute(
        f"SELECT id, transcript_segment_id, output_kind, status FROM voice_segment_outputs "
        f"WHERE episode_id=? AND transcript_segment_id IN ({placeholders})",
        (episode_id, *segment_ids),
    ).fetchall()
    for row in rows:
        db.execute(
            "INSERT OR REPLACE INTO voice_output_orphans (id, episode_id, transcript_segment_id, output_kind, status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"orphan-{row['id']}", episode_id, row["transcript_segment_id"], row["output_kind"], row["status"], reason, now_iso()),
        )
    _invalidate_dubbed_outputs(db, episode_id)


def _public_output_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        relative = Path(path).resolve().relative_to(OUTPUTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return f"/outputs/{relative.as_posix()}"


def _create_task(episode: dict[str, Any], task_type: str, provider: str | None = None, model: str | None = None, idempotency_key: str | None = None) -> str:
    task_id = f"ait-{uuid.uuid4().hex[:12]}"
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "INSERT INTO ai_tasks (id, project_id, episode_id, task_type, provider, model, status, progress, stage, idempotency_key, created_at, updated_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 'pending', ?, ?, ?, ?)",
            (task_id, episode["project_id"], episode["id"], task_type, provider, model, idempotency_key, timestamp, timestamp, timestamp),
        )
    return task_id


def _update_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    values = task_state.heartbeat_update(values)
    assignments = ", ".join(f"{key}=?" for key in values)
    with get_connection() as db:
        db.execute(f"UPDATE ai_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))


def _task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
    item = row_to_dict(row)
    if not item:
        raise HTTPException(status_code=404, detail="AI task not found")
    item["usage"] = json_load(item.pop("usage_json", "{}"), {})
    item["status"] = task_state.validate_live_status(
        item.get("status"), item.get("heartbeat_at") or item.get("updated_at"),
        task_state.TASK_TIMEOUT_SECONDS.get(item.get("task_type", "translation"), 120),
    )
    if item["status"] == "interrupted" and task_state.canonical_status(row["status"]) == "running":
        _update_task(task_id, status="interrupted", error_code="TASK_INTERRUPTED", error_message="任务已中断，请重新开始。", completed_at=now_iso())
    return item


def _refresh_local_transcription_heartbeat(task_id: str, state: dict[str, Any]) -> None:
    status = str(state.get("status") or "").strip().lower()
    if status not in {"queued", "running"}:
        return
    try:
        sidecar_progress = float(state.get("progress") or 0)
    except (TypeError, ValueError):
        sidecar_progress = 0.0
    progress = max(35, min(75, round(35 + max(0.0, min(100.0, sidecar_progress)) * 0.4)))
    _update_task(task_id, status="running", progress=progress, stage="transcribing")


def _translation_idempotency_key(
    episode_id: str,
    payload: TranslationStart,
    *,
    provider: str | None,
    model: str | None,
) -> str:
    document = _document(episode_id)
    segments = _segments(document["id"]) if document else []
    wanted = set(payload.segment_ids)
    if wanted:
        segments = [item for item in segments if item["id"] in wanted]
    material = {
        "operation": "translation",
        "episode_id": episode_id,
        "transcript_document_id": document["id"] if document else None,
        "transcript_revision": int(document.get("revision") or 0) if document else 0,
        "source_language": payload.source_language.strip().lower(),
        "target_language": payload.target_language.strip().lower(),
        "translation_style": payload.translation_style,
        "provider": (provider or "").strip().lower(),
        "model": (model or "").strip(),
        "segments": [
            {
                "id": item["id"],
                "seq": item["seq"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "source_text": item["source_text"],
                "speaker": item.get("speaker_label") or item.get("speaker") or "Speaker 01",
                "segment_type": item.get("segment_type") or "dialogue",
            }
            for item in segments if not item["translation_edited"]
        ],
    }
    return "translation:" + hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _document(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        row = db.execute("SELECT * FROM transcript_documents WHERE episode_id=?", (episode_id,)).fetchone()
    item = row_to_dict(row)
    if item:
        item["user_edited"] = bool(item["user_edited"])
        item["provider_metadata"] = json_load(item.pop("provider_metadata_json", "{}"), {})
    return item


def _segments(document_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM transcript_segments WHERE document_id=? ORDER BY seq", (document_id,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["subtitle_enabled"] = bool(item["subtitle_enabled"])
        item["user_edited"] = bool(item["user_edited"])
        item["translation_edited"] = bool(item["translation_edited"])
        item["stale_for_voice"] = bool(item["stale_for_voice"])
        item["speaker_label"] = item.get("speaker_label") or item.get("speaker") or "Speaker 01"
        duration = max(0.1, (item["end_ms"] - item["start_ms"]) / 1000)
        text = item.get("final_translation") or item.get("source_text") or ""
        item["reading_speed"] = round(len(text.replace(" ", "")) / duration, 1)
        item["reading_speed_warning"] = item["reading_speed"] > 12
        result.append(item)
    return result


def _write_transcript_json(episode: dict[str, Any], document: dict[str, Any], segments: list[dict[str, Any]]) -> Path:
    path = _episode_output(episode) / "subtitles" / "source_transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"document": document, "segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _replace_document(
    episode: dict[str, Any], rows: list[dict[str, Any]], origin: str, language: str,
    source_id: str | None, provider: str | None = None, model: str | None = None,
    overwrite_user_edits: bool = False,
    *,
    transcript_text: str = "",
    detected_language: str | None = None,
    provider_request_id: str | None = None,
    timestamp_source: str | None = None,
    audio_duration: float | None = None,
    speaker_count: int = 0,
    provider_metadata: dict[str, Any] | None = None,
    source_priority: str | None = None,
    comparison_mode: str = "source_review",
) -> dict[str, Any]:
    existing = _document(episode["id"])
    if existing and existing["user_edited"] and not overwrite_user_edits:
        raise HTTPException(status_code=409, detail="The transcript contains user edits. Confirm overwrite before replacing it.")
    timestamp = now_iso()
    document_id = existing["id"] if existing else f"tdoc-{uuid.uuid4().hex[:12]}"
    transcript_text = transcript_text or "".join(str(row.get("text", "")) for row in rows)
    detected_language = detected_language or (language if language != "auto" else None)
    source_priority = source_priority or (
        "speech_transcription" if origin == "speech_transcription" else "subtitle_source"
    )
    segment_count = len(rows)
    with get_connection() as db:
        if existing:
            old_segment_ids = [row["id"] for row in db.execute("SELECT id FROM transcript_segments WHERE document_id=?", (document_id,)).fetchall()]
            _archive_voice_outputs(db, episode["id"], old_segment_ids, "Transcript document replaced.")
            _mark_voice_outputs_stale(db, episode["id"], old_segment_ids, "Transcript document replaced.")
            db.execute("DELETE FROM transcript_segments WHERE document_id=?", (document_id,))
            db.execute(
                "UPDATE transcript_documents SET source_id=?, source_text_origin=?, source_language=?, source_priority=?, comparison_mode=?, revision=revision+1, provider=?, model=?, status='completed', user_edited=0, detected_language=?, transcript_text=?, provider_request_id=?, timestamp_source=?, audio_duration=?, speaker_count=?, segment_count=?, provider_metadata_json=?, updated_at=? WHERE id=?",
                (source_id, origin, language, source_priority, comparison_mode, provider, model, detected_language, transcript_text, provider_request_id, timestamp_source, audio_duration, speaker_count, segment_count, json_dump(provider_metadata or {}), timestamp, document_id),
            )
        else:
            db.execute(
                "INSERT INTO transcript_documents (id, episode_id, source_id, source_text_origin, source_language, source_priority, comparison_mode, provider, model, status, detected_language, transcript_text, provider_request_id, timestamp_source, audio_duration, speaker_count, segment_count, provider_metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, episode["id"], source_id, origin, language, source_priority, comparison_mode, provider, model, detected_language, transcript_text, provider_request_id, timestamp_source, audio_duration, speaker_count, segment_count, json_dump(provider_metadata or {}), timestamp, timestamp),
            )
        for index, row in enumerate(rows, 1):
            db.execute(
                "INSERT INTO transcript_segments (id, document_id, seq, start_ms, end_ms, source_text, source_text_origin, speaker, speaker_label, segment_type, confidence, timestamp_source, subtitle_enabled, review_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)",
                (f"seg-{uuid.uuid4().hex[:12]}", document_id, index, int(row["start_ms"]), int(row["end_ms"]), row.get("text", ""), origin, row.get("speaker", "Speaker 01"), row.get("speaker", "Speaker 01"), row.get("segment_type", "dialogue"), row.get("confidence"), row.get("timestamp_source") or timestamp_source, timestamp, timestamp),
            )
    document = _document(episode["id"]) or {}
    segments = _segments(document_id)
    transcript_path = _write_transcript_json(episode, document, segments)
    with get_connection() as db:
        db.execute(
            "INSERT INTO subtitle_outputs (id, episode_id, output_type, file_path, file_format, metadata_json, created_at) VALUES (?, ?, 'source_transcript', ?, 'json', '{}', ?)",
            (f"sout-{uuid.uuid4().hex[:12]}", episode["id"], str(transcript_path), timestamp),
        )
        db.execute("UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? AND output_type IN ('translated_subtitles', 'translated_subtitle_preview', 'subtitle_preview')", (episode["id"],))
        db.execute("UPDATE subtitle_styles SET preview_stale=1, preview_stale_reason='Transcript source changed; regenerate the preview.', updated_at=? WHERE episode_id=?", (timestamp, episode["id"]))
    return document


def _source_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["selected"] = bool(item["selected"])
    item["manual_override"] = bool(item["manual_override"])
    item["metadata"] = json_load(item.pop("metadata_json", "{}"), {})
    item["file_url"] = _public_output_path(item.get("file_path"))
    with get_connection() as db:
        streams = db.execute("SELECT * FROM subtitle_streams WHERE source_id=? ORDER BY stream_index", (item["id"],)).fetchall()
    item["streams"] = [dict(stream) | {"metadata": json_load(stream["metadata_json"], {})} for stream in streams]
    return item


def _ensure_style(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM subtitle_styles WHERE episode_id=?", (episode_id,)).fetchone()
        if not row:
            episode = db.execute("SELECT orientation FROM episodes WHERE id=?", (episode_id,)).fetchone()
            orientation = episode["orientation"] if episode else None
            font_size = 48 if orientation == "portrait" else (44 if orientation == "square" else 42)
            style_id = f"sstyle-{uuid.uuid4().hex[:12]}"
            timestamp = now_iso()
            db.execute(
                "INSERT INTO subtitle_styles (id, episode_id, font_size, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (style_id, episode_id, font_size, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM subtitle_styles WHERE id=?", (style_id,)).fetchone()
    item = dict(row)
    legacy_modes = {
        "add_target_subtitle": "replace_soft_subtitle",
        "bilingual_subtitle": "keep_original_and_add_target",
        "mask_burned_subtitle": "mask_burned_subtitle_and_add_target",
        "keep_original_only": "no_target_subtitle",
        "no_subtitle": "no_target_subtitle",
        "ai_remove_burned_subtitle_disabled": "mask_burned_subtitle_and_add_target",
    }
    if item.get("render_mode") in legacy_modes:
        item["render_mode"] = legacy_modes[item["render_mode"]]
        with get_connection() as db:
            db.execute("UPDATE subtitle_styles SET render_mode=?, updated_at=? WHERE episode_id=?", (item["render_mode"], now_iso(), episode_id))
    if item.get("render_mode") == "target_only" and int(item.get("mask_height_percent") or 0) in {18, 20}:
        item["mask_height_percent"] = 22
    with get_connection() as db:
        source_types = {row["source_type"] for row in db.execute("SELECT source_type FROM subtitle_sources WHERE episode_id=?", (episode_id,)).fetchall()}
    allowed_modes = {"keep_original_and_add_target", "target_only", "no_target_subtitle"}
    if source_types & {"embedded_soft_subtitle", "external_subtitle"}:
        allowed_modes.add("replace_soft_subtitle")
    if "burned_in_subtitle" in source_types:
        allowed_modes.add("mask_burned_subtitle_and_add_target")
    if item.get("render_mode") not in allowed_modes:
        item["render_mode"] = "no_target_subtitle"
        with get_connection() as db:
            db.execute("UPDATE subtitle_styles SET render_mode=?, updated_at=? WHERE episode_id=?", (item["render_mode"], now_iso(), episode_id))
    item["preview_stale"] = bool(item.get("preview_stale"))
    item["available_fonts"] = _font_catalog()
    if item.get("font_family") not in {font["id"] for font in item["available_fonts"]}:
        item["font_family"] = item["available_fonts"][0]["id"]
        item["font_fallback"] = True
    else:
        item["font_fallback"] = False
    return item


def _state(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    with get_connection() as db:
        source_rows = db.execute("SELECT * FROM subtitle_sources WHERE episode_id=? ORDER BY selected DESC, priority DESC, created_at DESC", (episode_id,)).fetchall()
        translation = db.execute("SELECT td.* FROM translation_documents td JOIN transcript_documents d ON d.id=td.transcript_document_id WHERE d.episode_id=?", (episode_id,)).fetchone()
        outputs = db.execute("SELECT * FROM subtitle_outputs WHERE episode_id=? ORDER BY created_at DESC", (episode_id,)).fetchall()
        latest_task = db.execute("SELECT * FROM ai_tasks WHERE episode_id=? ORDER BY created_at DESC LIMIT 1", (episode_id,)).fetchone()
        transcription_task = db.execute("SELECT * FROM ai_tasks WHERE episode_id=? AND task_type='transcription' ORDER BY created_at DESC LIMIT 1", (episode_id,)).fetchone()
    authority = None
    authority_error = None
    segments = _segments(document["id"]) if document else []
    if document:
        try:
            authority = localization_text_authority.resolve_episode_text_authority(episode_id)
            segments = authority.apply_to_segments(segments)
        except localization_text_authority.LocalizationTextAuthorityError as error:
            authority_error = {"code": error.code, "message": str(error)}
            authority = None
    source_priority = (document or {}).get("source_priority") or (
        "speech_transcription" if (document or {}).get("source_text_origin") == "speech_transcription" else "subtitle_source"
    )
    comparison_mode = (document or {}).get("comparison_mode") or (
        "side_by_side_review" if (document or {}).get("selected_source_mode") == "side_by_side" else "source_review"
    )
    subtitle_available = any(
        item["source_type"] in {"embedded_soft_subtitle", "external_subtitle"}
        and ((item["stream_count"] or 0) > 0 or bool(item["file_path"]))
        for item in source_rows
    )
    active_source_label = {
        "speech_transcription": "Speech transcription",
        "subtitle_source": "Subtitle file / track",
        "side_by_side_review": "Side-by-side review",
    }.get(source_priority, source_priority)
    selected_provider = _episode_transcription_provider(episode)
    provider_status = provider_service.transcription_provider_status(
        provider_service.transcription_configuration(selected_provider)
    )
    transcription_task_item = _task(transcription_task["id"]) if transcription_task else None
    real_status = "disabled"
    if provider_status["configured"]:
        real_status = "failed" if transcription_task_item and transcription_task_item.get("status") in {"failed", "interrupted"} else "real"
    has_burned_in = any(item["source_type"] == "burned_in_subtitle" for item in source_rows)
    render_modes = {"keep_original_and_add_target", "target_only", "no_target_subtitle"}
    if subtitle_available:
        render_modes.add("replace_soft_subtitle")
    if has_burned_in:
        render_modes.add("mask_burned_subtitle_and_add_target")
    return {
        "episode_id": episode_id,
        "language_mode": authority.language_mode if authority else None,
        "text_authority": authority.text_field if authority else None,
        "localization_language": authority.language if authority else None,
        "localization_error": authority_error,
        "project_id": episode["project_id"],
        "video_duration": episode.get("duration"),
        "video_url": f"/uploads/{Path(episode['video_path']).name}" if episode.get("video_path") else None,
        "sources": [_source_dict(row) for row in source_rows],
        "document": document,
        "source_priority": source_priority,
        "comparison_mode": comparison_mode,
        "active_source": {"id": source_priority, "label": active_source_label},
        "subtitle_source_available": subtitle_available,
        "subtitle_source_reason": None if subtitle_available else "No readable embedded subtitle track or external SRT/VTT is available.",
        "segments": segments,
        "translation": row_to_dict(translation),
        "style": _ensure_style(episode_id),
        "outputs": [dict(row) | {"file_url": _public_output_path(row["file_path"]), "metadata": json_load(row["metadata_json"], {})} for row in outputs],
        "latest_task": _task(latest_task["id"]) if latest_task else None,
        "media_tools": media_service.media_tool_status(),
        "transcription_provider": provider_status,
        "selected_transcription_provider": selected_provider,
        "transcription_providers": _transcription_provider_options(),
        "transcription_summary": {
            "status": real_status,
            "audio_duration": document.get("audio_duration") if document and document.get("audio_duration") is not None else episode.get("duration"),
            "detected_language": document.get("detected_language") if document else None,
            "segment_count": len(segments),
            "speaker_count": len({item["speaker_label"] for item in segments if item.get("speaker_label")}),
            "timestamp_source": document.get("timestamp_source") if document else None,
        },
        "translation_provider": provider_service.translation_configuration().public_status(),
        "tts_provider": provider_service.tts_configuration().public_status(),
        "render_modes": sorted(render_modes),
        "available_fonts": _font_catalog(),
        "a1_notice": "Transcription, translation and subtitles only. Voice generation and audio replacement are not part of A1.",
    }


def _fail_task(task_id: str, error: Exception) -> None:
    if isinstance(error, media_service.MediaToolError):
        code, detail = error.code, error.stderr
    elif isinstance(error, provider_service.ProviderError):
        code, detail = error.code, error.detail
    elif isinstance(error, transcription_commit_service.TranscriptionCommitError):
        code, detail = error.code, error.detail
    else:
        code, detail = "INTERNAL_ERROR", provider_service.redact_sensitive_text(repr(error))
    _update_task(
        task_id, status="failed", error_code=code, error_message=str(error), stderr=detail,
        completed_at=now_iso(), stage="failed",
        provider_request_id=getattr(error, "request_id", None),
        attempt_count=getattr(error, "attempt_count", 0),
    )


@router.get("/episodes/{episode_id}/transcription-subtitles")
def get_transcription_subtitles(episode_id: str) -> dict[str, Any]:
    return _state(episode_id)


@router.put("/episodes/{episode_id}/transcription-provider")
def select_transcription_provider(
    episode_id: str,
    payload: TranscriptionProviderSelection,
) -> dict[str, Any]:
    episode = _episode(episode_id)
    provider = payload.provider.strip().lower()
    if provider not in TRANSCRIPTION_PROVIDERS:
        raise HTTPException(status_code=400, detail={
            "code": "TRANSCRIPTION_PROVIDER_INVALID",
            "message": "Select a supported transcription provider.",
        })
    settings = json_load(episode.get("settings_override"), {})
    settings["stt_provider"] = provider
    with get_connection() as db:
        db.execute(
            "UPDATE episodes SET settings_override=?, updated_at=? WHERE id=?",
            (json_dump(settings), now_iso(), episode_id),
        )
    return _state(episode_id)


@router.get("/ai-tasks/{task_id}")
def get_ai_task(task_id: str) -> dict[str, Any]:
    return _task(task_id)


@router.post("/episodes/{episode_id}/subtitle-detect")
def detect_subtitle_streams(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    video_path = _video_path(episode)
    task_id = _create_task(episode, "subtitle_detection")
    _update_task(task_id, status="running", progress=10, stage="probing", started_at=now_iso(), input_path=str(video_path))
    try:
        probe = media_service.probe_media(video_path)
        timestamp = now_iso()
        source_id = f"ssrc-{uuid.uuid4().hex[:12]}"
        source_type = probe["subtitle_source_type"]
        output_path: Path | None = None
        with get_connection() as db:
            db.execute("DELETE FROM subtitle_sources WHERE episode_id=? AND source_type IN ('embedded_soft_subtitle', 'unknown') AND manual_override=0", (episode_id,))
            db.execute(
                "INSERT INTO subtitle_sources (id, episode_id, source_type, selected, priority, stream_count, metadata_json, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (source_id, episode_id, source_type, 300 if source_type == "embedded_soft_subtitle" else 0, probe["subtitle_stream_count"], json_dump({"message": probe["message"], "audio_stream_count": probe["audio_stream_count"]}), timestamp, timestamp),
            )
            for stream in probe["subtitle_streams"]:
                db.execute(
                    "INSERT INTO subtitle_streams (id, source_id, episode_id, stream_index, codec_name, language, title, disposition_default, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"sstr-{uuid.uuid4().hex[:12]}", source_id, episode_id, stream["index"], stream["codec_name"], stream["language"], stream["title"], bool_int(stream["default"]), json_dump(stream["metadata"]), timestamp),
                )
        if probe["subtitle_streams"]:
            output_path = _episode_output(episode) / "subtitles" / "embedded_source.srt"
            extraction = media_service.extract_subtitle_stream(video_path, probe["subtitle_streams"][0]["index"], output_path)
            with get_connection() as db:
                db.execute("UPDATE subtitle_sources SET file_path=?, file_format='srt', language=?, updated_at=? WHERE id=?", (str(output_path), probe["subtitle_streams"][0]["language"], now_iso(), source_id))
            if not _document(episode_id):
                rows, _ = subtitle_service.parse_subtitle(output_path.read_bytes(), ".srt", episode.get("duration"))
                _replace_document(episode, rows, "embedded_subtitle", probe["subtitle_streams"][0]["language"] or "auto", source_id)
            _update_task(task_id, stderr=extraction["stderr"])
        _update_task(task_id, status="completed", progress=100, stage="completed", completed_at=now_iso(), output_path=str(output_path) if output_path else None)
    except Exception as error:
        _fail_task(task_id, error)
    return _state(episode_id)


@router.post("/episodes/{episode_id}/external-subtitles")
async def upload_external_subtitles(
    episode_id: str, file: UploadFile = File(...), select_source: bool = Form(False), overwrite_user_edits: bool = Form(False)
) -> dict[str, Any]:
    episode = _episode(episode_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise HTTPException(status_code=400, detail="Only SRT and VTT subtitle files are supported in A1.")
    content = await file.read()
    try:
        rows, encoding = subtitle_service.parse_subtitle(content, suffix, episode.get("duration"))
    except subtitle_service.SubtitleValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    directory = _episode_output(episode) / "subtitles" / "external"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4().hex[:8]}-{Path(file.filename or 'subtitles').name}"
    target.write_bytes(content)
    timestamp = now_iso()
    source_id = f"ssrc-{uuid.uuid4().hex[:12]}"
    with get_connection() as db:
        db.execute(
            "INSERT INTO subtitle_sources (id, episode_id, source_type, selected, priority, file_path, file_format, metadata_json, created_at, updated_at) VALUES (?, ?, 'external_subtitle', 0, 400, ?, ?, ?, ?, ?)",
            (source_id, episode_id, str(target), suffix.lstrip("."), json_dump({"filename": file.filename, "encoding": encoding, "cue_count": len(rows)}), timestamp, timestamp),
        )
    if select_source:
        _replace_document(episode, rows, "external_subtitle", "auto", source_id, overwrite_user_edits=overwrite_user_edits)
        with get_connection() as db:
            db.execute("UPDATE subtitle_sources SET selected=CASE WHEN id=? THEN 1 ELSE 0 END WHERE episode_id=?", (source_id, episode_id))
    return _state(episode_id)


@router.put("/episodes/{episode_id}/subtitle-source")
def select_subtitle_source(episode_id: str, payload: SourceSelection) -> dict[str, Any]:
    episode = _episode(episode_id)
    if payload.selected_source_mode not in SOURCE_MODES:
        raise HTTPException(status_code=400, detail="Invalid source comparison mode.")
    requested_priority = payload.source_priority
    if requested_priority not in SOURCE_PRIORITIES:
        requested_priority = {
            "subtitle_primary": "subtitle_source",
            "speech_primary": "speech_transcription",
            "side_by_side": "side_by_side_review",
        }.get(payload.selected_source_mode, "speech_transcription")
    comparison_mode = payload.comparison_mode or (
        "side_by_side_review" if payload.selected_source_mode == "side_by_side" else "source_review"
    )
    with get_connection() as db:
        if requested_priority == "side_by_side_review":
            document = db.execute("SELECT * FROM transcript_documents WHERE episode_id=?", (episode_id,)).fetchone()
            if not document:
                raise HTTPException(status_code=400, detail="Create a transcript before opening side-by-side review.")
            db.execute("UPDATE transcript_documents SET comparison_mode=?, updated_at=? WHERE episode_id=?", (comparison_mode, now_iso(), episode_id))
            return _state(episode_id)
        if requested_priority == "speech_transcription":
            row = db.execute("SELECT * FROM subtitle_sources WHERE episode_id=? AND source_type='speech_only' ORDER BY created_at DESC LIMIT 1", (episode_id,)).fetchone()
        elif payload.source_id:
            row = db.execute("SELECT * FROM subtitle_sources WHERE id=? AND episode_id=?", (payload.source_id, episode_id)).fetchone()
        else:
            mapped = {"soft": "embedded_soft_subtitle", "external": "external_subtitle", "burned": "burned_in_subtitle"}.get(payload.source_type, payload.source_type)
            if mapped in {"embedded_soft_subtitle", "external_subtitle"}:
                row = db.execute("SELECT * FROM subtitle_sources WHERE episode_id=? AND source_type=? AND (stream_count > 0 OR file_path IS NOT NULL) ORDER BY created_at DESC LIMIT 1", (episode_id, mapped)).fetchone()
            else:
                row = db.execute("SELECT * FROM subtitle_sources WHERE episode_id=? AND source_type=? ORDER BY created_at DESC LIMIT 1", (episode_id, mapped)).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail={"code": "SUBTITLE_SOURCE_UNAVAILABLE", "message": "No readable embedded subtitle track or external SRT/VTT is available."})
    source = dict(row)
    if source.get("file_path"):
        try:
            rows, _ = subtitle_service.parse_subtitle(Path(source["file_path"]).read_bytes(), f".{source.get('file_format') or 'srt'}", episode.get("duration"))
        except (OSError, subtitle_service.SubtitleValidationError) as error:
            raise HTTPException(status_code=422, detail=f"Unable to load the selected subtitle source: {error}") from error
        origin = "external_subtitle" if source["source_type"] == "external_subtitle" else "embedded_subtitle"
        _replace_document(
            episode, rows, origin, source.get("language") or "auto", source["id"],
            overwrite_user_edits=payload.overwrite_user_edits, source_priority="subtitle_source", comparison_mode=comparison_mode,
        )
    with get_connection() as db:
        db.execute("UPDATE subtitle_sources SET selected=CASE WHEN id=? THEN 1 ELSE 0 END WHERE episode_id=?", (source["id"], episode_id))
        db.execute("UPDATE transcript_documents SET selected_source_mode='subtitle_primary', source_priority='subtitle_source', comparison_mode=?, updated_at=? WHERE episode_id=?", (comparison_mode, now_iso(), episode_id))
    return _state(episode_id)


@router.post("/episodes/{episode_id}/extract-audio")
def extract_episode_audio(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    video_path = _video_path(episode)
    task_id = _create_task(episode, "audio_extraction")
    _update_task(task_id, status="running", progress=10, stage="extracting_audio", started_at=now_iso(), input_path=str(video_path))
    try:
        output_path = _episode_output(episode) / "audio" / "source_audio.wav"
        result = media_service.extract_audio(video_path, output_path)
        _update_task(task_id, status="completed", progress=100, stage="completed", stderr=result["stderr"], output_path=str(output_path), completed_at=now_iso())
    except Exception as error:
        _fail_task(task_id, error)
    return _task(task_id)


def _run_transcription(
    task_id: str,
    episode_id: str,
    language: str,
    overwrite: bool,
    provider_override: str | None = None,
) -> None:
    episode = _episode(episode_id)
    config = provider_service.transcription_configuration(provider_override)
    try:
        _update_task(task_id, status="running", progress=5, stage="validating_configuration", started_at=now_iso())
        provider_service.validate_configuration(config, "transcription")
        video_path = _video_path(episode)
        audio_path = _episode_output(episode) / "audio" / "source_audio.wav"
        if not audio_path.is_file():
            _update_task(task_id, progress=15, stage="extracting_audio", input_path=str(video_path))
            extraction = media_service.extract_audio(video_path, audio_path)
            _update_task(task_id, stderr=extraction["stderr"])
        _update_task(task_id, progress=35, stage="transcribing", input_path=str(audio_path))
        audio_duration = media_service.wav_duration_seconds(audio_path)
        if config.provider == "local_whisper_pyannote":
            result = provider_service.transcribe_audio(
                audio_path,
                language,
                config,
                status_callback=lambda state: _refresh_local_transcription_heartbeat(task_id, state),
            )
        else:
            result = provider_service.transcribe_audio(audio_path, language, config)
        rows = result["segments"]
        if not rows:
            raise provider_service.ProviderError("TRANSCRIPTION_EMPTY", "The provider returned no transcript segments.")
        previous_end = -1
        for row in rows:
            if row["start_ms"] >= row["end_ms"]:
                raise provider_service.ProviderError("TIMESTAMPS_INVALID", "A transcript segment has an invalid time range.")
            if row["start_ms"] < previous_end:
                raise provider_service.ProviderError("TIMESTAMP_OVERLAP", "Transcript segment timestamps overlap.")
            if row["end_ms"] > round((audio_duration + 1) * 1000):
                raise provider_service.ProviderError("TIMESTAMP_OUT_OF_RANGE", "A provider timestamp exceeds the extracted audio duration.")
            previous_end = row["end_ms"]
        _update_task(task_id, progress=80, stage="saving_transcript")
        if config.provider == "local_whisper_pyannote":
            transcription_commit_service.commit_transcription_result_atomic(
                episode=episode,
                task_id=task_id,
                result=result,
                audio_duration=audio_duration,
                source_language=language,
                overwrite_user_edits=overwrite,
                subtitle_path=_episode_output(episode) / "subtitles" / f"source_subtitles-{task_id}.srt",
                transcript_json_path=_episode_output(episode) / "subtitles" / f"source_transcript-{task_id}.json",
            )
            return
        source_id = f"ssrc-{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        with get_connection() as db:
            db.execute("INSERT INTO subtitle_sources (id, episode_id, source_type, selected, priority, language, metadata_json, created_at, updated_at) VALUES (?, ?, 'speech_only', 1, 200, ?, ?, ?, ?)", (source_id, episode_id, result["detected_language"], json_dump({"provider": config.provider, "model": config.model, "provider_request_id": result.get("provider_request_id"), "timestamp_source": result["timestamp_source"]}), timestamp, timestamp))
            db.execute("UPDATE subtitle_sources SET selected=CASE WHEN id=? THEN 1 ELSE 0 END WHERE episode_id=?", (source_id, episode_id))
        document = _replace_document(
            episode, rows, "speech_transcription", result["detected_language"], source_id,
            config.provider, config.model, overwrite,
            transcript_text=result["transcript"], detected_language=result["detected_language"],
            provider_request_id=result.get("provider_request_id"), timestamp_source=result["timestamp_source"],
            audio_duration=audio_duration, speaker_count=result["speaker_count"], provider_metadata=result.get("metadata"),
            source_priority="speech_transcription", comparison_mode="source_review",
        )
        with get_connection() as db:
            db.execute("UPDATE transcript_documents SET selected_source_mode='speech_primary', source_priority='speech_transcription', comparison_mode='source_review', updated_at=? WHERE episode_id=?", (now_iso(), episode_id))
        segments = _segments(document["id"])
        subtitle_path = _episode_output(episode) / "subtitles" / "source_subtitles.srt"
        subtitle_service.write_srt(segments, subtitle_path)
        with get_connection() as db:
            db.execute("INSERT INTO subtitle_outputs (id, episode_id, task_id, output_type, file_path, file_format, metadata_json, created_at) VALUES (?, ?, ?, 'source_subtitles', ?, 'srt', '{}', ?)", (f"sout-{uuid.uuid4().hex[:12]}", episode_id, task_id, str(subtitle_path), now_iso()))
        _update_task(
            task_id, status="completed", progress=100, stage="completed", output_path=str(subtitle_path),
            provider_request_id=result.get("provider_request_id"), attempt_count=result.get("attempt_count", 1),
            usage_json=json_dump(result.get("metadata", {})), completed_at=now_iso(),
        )
    except Exception as error:
        _fail_task(task_id, error)


@router.post("/episodes/{episode_id}/transcribe")
def start_transcription(episode_id: str, payload: TranscriptionStart, background_tasks: BackgroundTasks) -> dict[str, Any]:
    episode = _episode(episode_id)
    _video_path(episode)
    existing = _document(episode_id)
    if existing and existing["user_edited"] and not payload.overwrite_user_edits:
        raise HTTPException(status_code=409, detail="The transcript contains user edits. Confirm overwrite before starting transcription.")
    provider_override = payload.provider or _episode_transcription_provider(episode)
    config = provider_service.transcription_configuration(provider_override)
    task_id = _create_task(episode, "transcription", config.provider or None, config.model or None)
    if not config.configured:
        missing = ", ".join(config.public_status()["missing"])
        invalid_key = bool(
            config.provider == "elevenlabs"
            and config.api_key
            and not config.api_key_has_valid_transport_format
        )
        _update_task(
            task_id, status="disabled", stage="configuration",
            error_code="API_KEY_INVALID" if invalid_key else "TRANSCRIPTION_PROVIDER_DISABLED",
            error_message="The ElevenLabs API key format is invalid." if invalid_key else f"Transcription provider is not configured. Missing: {missing}.",
            completed_at=now_iso(),
        )
        return _task(task_id)
    background_tasks.add_task(
        _run_transcription,
        task_id,
        episode_id,
        payload.source_language,
        payload.overwrite_user_edits,
        provider_override,
    )
    return _task(task_id)


def _run_translation(task_id: str, episode_id: str, payload: TranslationStart) -> None:
    config = provider_service.translation_configuration()
    try:
        _update_task(task_id, status="running", progress=10, stage="validating_configuration", started_at=now_iso())
        provider_service.validate_configuration(config, "translation")
        document = _document(episode_id)
        if not document:
            raise provider_service.ProviderError("TRANSCRIPT_REQUIRED", "Create or import a transcript before translation.")
        segments = _segments(document["id"])
        if payload.segment_ids:
            wanted = set(payload.segment_ids)
            segments = [segment for segment in segments if segment["id"] in wanted]
        segments = [segment for segment in segments if not segment["translation_edited"]]
        if not segments:
            raise provider_service.ProviderError(
                "NO_SEGMENTS_SELECTED",
                "No unedited transcript segments were selected for translation; human-edited translations were preserved.",
            )
        _update_task(task_id, progress=35, stage="translating")
        translation_result = provider_service.translate_segments(
            segments, payload.source_language, payload.target_language, payload.translation_style
        )
        translated = translation_result["translations"]
        timestamp = now_iso()
        with get_connection() as db:
            db.execute("UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? AND output_type IN ('translated_subtitles', 'translated_subtitle_preview')", (episode_id,))
            db.execute(
                "INSERT INTO translation_documents (id, transcript_document_id, source_language, target_language, translation_style, provider, model, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?) ON CONFLICT(transcript_document_id) DO UPDATE SET source_language=excluded.source_language, target_language=excluded.target_language, translation_style=excluded.translation_style, provider=excluded.provider, model=excluded.model, status='processing', error_message=NULL, updated_at=excluded.updated_at",
                (f"trdoc-{uuid.uuid4().hex[:12]}", document["id"], payload.source_language, payload.target_language, payload.translation_style, config.provider, config.model, timestamp, timestamp),
            )
            for item in translated:
                db.execute("UPDATE transcript_segments SET literal_translation=?, localized_translation=?, final_translation=?, translation_status='completed', translation_edited=0, stale_for_voice=1, updated_at=? WHERE id=? AND document_id=?", (item["literal"], item["localized"], item["final"], timestamp, item["id"], document["id"]))
            db.execute("UPDATE translation_documents SET status='completed', updated_at=? WHERE transcript_document_id=?", (timestamp, document["id"]))
        _update_task(task_id, progress=75, stage="exporting_subtitles")
        all_segments = _segments(document["id"])
        episode = _episode(episode_id)
        directory = _episode_output(episode) / "subtitles"
        language_code = _language_code(payload.target_language)
        srt_path = directory / f"translated_subtitles_{language_code}.srt"
        vtt_path = directory / f"translated_subtitles_{language_code}.vtt"
        subtitle_service.write_srt(all_segments, srt_path, translated=True)
        subtitle_service.write_vtt(all_segments, vtt_path, translated=True)
        output_metadata = {
            "provider": translation_result["provider"],
            "model": translation_result["model"],
            "target_language": payload.target_language,
            "provider_request_id": translation_result.get("provider_request_id"),
        }
        with get_connection() as db:
            for output_type, path, fmt in (("translated_subtitles", srt_path, "srt"), ("translated_subtitles", vtt_path, "vtt")):
                db.execute(
                    "INSERT INTO subtitle_outputs (id, episode_id, task_id, output_type, file_path, file_format, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"sout-{uuid.uuid4().hex[:12]}", episode_id, task_id, output_type, str(path), fmt, json_dump(output_metadata), now_iso()),
                )
        _update_task(task_id, progress=88, stage="rendering_translated_subtitle_preview")
        preview_path = _episode_output(episode) / f"translated_subtitle_preview_{language_code}.mp4"
        preview_result = media_service.render_subtitle_preview(
            _video_path(episode), srt_path, preview_path, _ensure_style(episode_id)
        )
        with get_connection() as db:
            db.execute(
                "INSERT INTO subtitle_outputs (id, episode_id, task_id, output_type, file_path, file_format, status, metadata_json, created_at) VALUES (?, ?, ?, 'translated_subtitle_preview', ?, 'mp4', 'completed', ?, ?)",
                (
                    f"sout-{uuid.uuid4().hex[:12]}", episode_id, task_id, str(preview_path),
                    json_dump(output_metadata | {"original_audio_preserved": True}), now_iso(),
                ),
            )
        _update_task(
            task_id, status="completed", progress=100, stage="completed", output_path=str(preview_path),
            provider_request_id=translation_result.get("provider_request_id"),
            attempt_count=translation_result.get("attempt_count", 1),
            usage_json=json_dump(translation_result.get("usage", {})),
            stderr=preview_result["stderr"], completed_at=now_iso(),
        )
    except Exception as error:
        document = _document(episode_id)
        if document:
            with get_connection() as db:
                db.execute("UPDATE translation_documents SET status='failed', error_message=?, updated_at=? WHERE transcript_document_id=?", (str(error), now_iso(), document["id"]))
        _fail_task(task_id, error)


@router.post("/episodes/{episode_id}/translate")
def start_translation(episode_id: str, payload: TranslationStart, background_tasks: BackgroundTasks) -> dict[str, Any]:
    episode = _episode(episode_id)
    try:
        authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    except localization_text_authority.LocalizationTextAuthorityError as error:
        if error.code != "LOCALIZATION_ASR_REQUIRED":
            raise HTTPException(status_code=409, detail={"code": error.code, "message": str(error)}) from error
        authority = None
    if authority and authority.language_mode == localization_text_authority.DUB_ONLY:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TRANSLATION_DISABLED_FOR_DUB_ONLY",
                "message": "Translation is disabled while Dub without translation is selected.",
            },
        )
    if payload.translation_style not in TRANSLATION_STYLES:
        raise HTTPException(status_code=400, detail="Invalid translation style.")
    try:
        language_capabilities.normalize_target_language(payload.target_language)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "TARGET_LANGUAGE_UNSUPPORTED", "message": "This target language is not enabled in the current A1 workflow."})
    if not _document(episode_id):
        raise HTTPException(status_code=400, detail="Create or import a transcript before translation.")
    config = provider_service.translation_configuration()
    idempotency_key = _translation_idempotency_key(
        episode_id,
        payload,
        provider=config.provider,
        model=config.model,
    )
    with get_connection() as db:
        cached_task = db.execute(
            "SELECT id, status FROM ai_tasks WHERE episode_id=? AND task_type='translation' AND idempotency_key=? ORDER BY created_at DESC LIMIT 1",
            (episode_id, idempotency_key),
        ).fetchone()
    if cached_task and task_state.canonical_status(cached_task["status"]) in {"pending", "running", "completed"}:
        return _task(cached_task["id"])
    task_id = _create_task(episode, "translation", config.provider or None, config.model or None, idempotency_key)
    if not config.configured:
        missing = ", ".join(config.public_status()["missing"])
        invalid_key = bool(config.api_key and not config.api_key_has_valid_transport_format)
        _update_task(
            task_id, status="disabled", stage="configuration",
            error_code="API_KEY_INVALID" if invalid_key else "TRANSLATION_PROVIDER_DISABLED",
            error_message="The DeepSeek API key format is invalid." if invalid_key else f"Translation provider is not configured. Missing: {missing}.",
            completed_at=now_iso(),
        )
        return _task(task_id)
    background_tasks.add_task(_run_translation, task_id, episode_id, payload)
    return _task(task_id)


def _validate_segment(body: SegmentBody, duration: float | None) -> None:
    if body.start_ms < 0 or body.end_ms <= body.start_ms:
        raise HTTPException(status_code=400, detail="Segment end time must be after start time.")
    if duration and body.end_ms > round(duration * 1000) + 1000:
        raise HTTPException(status_code=400, detail="Segment timeline exceeds the video duration.")
    if not body.source_text.strip():
        raise HTTPException(status_code=400, detail="Segment source text cannot be empty.")


@router.post("/episodes/{episode_id}/transcript-segments")
def add_segment(episode_id: str, payload: SegmentBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    _validate_segment(payload, episode.get("duration"))
    document = _document(episode_id)
    timestamp = now_iso()
    if not document:
        document_id = f"tdoc-{uuid.uuid4().hex[:12]}"
        with get_connection() as db:
            db.execute("INSERT INTO transcript_documents (id, episode_id, source_text_origin, source_language, status, user_edited, created_at, updated_at) VALUES (?, ?, 'manual', 'auto', 'completed', 1, ?, ?)", (document_id, episode_id, timestamp, timestamp))
        document = _document(episode_id)
    assert document
    with get_connection() as db:
        seq = db.execute("SELECT COALESCE(MAX(seq), 0)+1 AS seq FROM transcript_segments WHERE document_id=?", (document["id"],)).fetchone()["seq"]
        db.execute("INSERT INTO transcript_segments (id, document_id, seq, start_ms, end_ms, source_text, source_text_origin, speaker, segment_type, literal_translation, localized_translation, final_translation, translation_status, subtitle_enabled, review_status, user_edited, translation_edited, stale_for_voice, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)", (f"seg-{uuid.uuid4().hex[:12]}", document["id"], seq, payload.start_ms, payload.end_ms, payload.source_text.strip(), payload.source_text_origin, payload.speaker, payload.segment_type, payload.literal_translation, payload.localized_translation, payload.final_translation, payload.translation_status, bool_int(payload.subtitle_enabled), payload.review_status, bool_int(bool(payload.final_translation)), timestamp, timestamp))
        db.execute("UPDATE transcript_documents SET user_edited=1, revision=revision+1, updated_at=? WHERE id=?", (timestamp, document["id"]))
    return _state(episode_id)


@router.put("/episodes/{episode_id}/transcript-segments")
def save_segments(episode_id: str, payload: SegmentBatch) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=404, detail="Transcript document not found.")
    timestamp = now_iso()
    any_source_changed = False
    any_translation_changed = False
    translation_changed_ids: list[str] = []
    timeline_or_speaker_changed_ids: list[str] = []
    try:
        source_path = xmax_thirty_second._source_path(episode)
        plan = xmax_thirty_second._load_persistent_scene_plan(episode, source_path, 0.35)
    except Exception:
        plan = None
    submitted_segments = {
        body.id: body.model_dump()
        for body in payload.segments
    }
    planning_segments = [
        {**segment, **submitted_segments.get(str(segment["id"]), {})}
        for segment in _segments(document["id"])
    ]
    expansion_actions = duration_constrained_tts_service.plan_extreme_short_slot_expansions(
        planning_segments,
        list((plan or {}).get("transform_units") or []),
        round(float(episode.get("duration") or 0) * 1000),
        selected_ids={body.id for body in payload.segments},
    )
    expanded_end_by_id = {
        str(action["segment_id"]): int(action["end_ms"])
        for action in expansion_actions
    }
    with get_connection() as db:
        for body in payload.segments:
            if body.id in expanded_end_by_id:
                body.end_ms = expanded_end_by_id[body.id]
            _validate_segment(body, episode.get("duration"))
            old = db.execute("SELECT * FROM transcript_segments WHERE id=? AND document_id=?", (body.id, document["id"])).fetchone()
            if not old:
                raise HTTPException(status_code=404, detail=f"Transcript segment {body.id} not found.")
            source_changed = old["source_text"] != body.source_text or old["start_ms"] != body.start_ms or old["end_ms"] != body.end_ms
            translation_changed = old["final_translation"] != body.final_translation
            timeline_or_speaker_changed = (
                old["start_ms"] != body.start_ms
                or old["end_ms"] != body.end_ms
                or old["speaker"] != body.speaker
                or old["segment_type"] != body.segment_type
            )
            any_source_changed = any_source_changed or source_changed
            any_translation_changed = any_translation_changed or translation_changed
            if translation_changed:
                translation_changed_ids.append(body.id)
            if timeline_or_speaker_changed and not translation_changed:
                timeline_or_speaker_changed_ids.append(body.id)
            translation_status = "stale" if source_changed and old["translation_status"] == "completed" else body.translation_status
            db.execute(
                "UPDATE transcript_segments SET start_ms=?, end_ms=?, source_text=?, source_text_origin=?, speaker=?, speaker_label=?, segment_type=?, literal_translation=?, localized_translation=?, final_translation=?, translation_status=?, subtitle_enabled=?, review_status=?, user_edited=1, translation_edited=?, stale_for_voice=?, updated_at=? WHERE id=?",
                (body.start_ms, body.end_ms, body.source_text.strip(), body.source_text_origin, body.speaker, body.speaker, body.segment_type, body.literal_translation, body.localized_translation, body.final_translation, translation_status, bool_int(body.subtitle_enabled), body.review_status, bool_int(translation_changed or bool(old["translation_edited"])), bool_int(translation_changed or timeline_or_speaker_changed or bool(old["stale_for_voice"])), timestamp, body.id),
            )
        db.execute("UPDATE transcript_documents SET user_edited=1, revision=revision+1, updated_at=? WHERE id=?", (timestamp, document["id"]))
        if any_source_changed:
            db.execute("UPDATE translation_documents SET status='stale', updated_at=? WHERE transcript_document_id=?", (timestamp, document["id"]))
        if any_translation_changed:
            if translation_changed_ids:
                placeholders = ",".join("?" for _ in translation_changed_ids)
                db.execute(
                    f"UPDATE voice_segment_outputs SET status='failed', error_code='TTS_TEXT_CHANGED', error_message='Translation text changed; confirm the new text before retrying.', updated_at=? WHERE transcript_segment_id IN ({placeholders}) AND status IN ('completed','stale')",
                    (timestamp, *translation_changed_ids),
                )
        if timeline_or_speaker_changed_ids:
            placeholders = ",".join("?" for _ in timeline_or_speaker_changed_ids)
            db.execute(
                f"UPDATE voice_segment_outputs SET status='failed', error_code='TTS_TIMELINE_OR_SPEAKER_CHANGED', "
                f"error_message='Timeline or speaker changed; regenerate this voice segment.', updated_at=? "
                f"WHERE transcript_segment_id IN ({placeholders}) AND status IN ('completed','stale')",
                (timestamp, *timeline_or_speaker_changed_ids),
            )
        if translation_changed_ids or timeline_or_speaker_changed_ids:
            _invalidate_dubbed_outputs(db, episode_id, timestamp)
        if any_source_changed or any_translation_changed:
            db.execute("UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? AND output_type IN ('translated_subtitles', 'subtitle_preview', 'translated_subtitle_preview')", (episode_id,))
    fresh_document = _document(episode_id) or {}
    _write_transcript_json(episode, fresh_document, _segments(document["id"]))
    return _state(episode_id)


@router.delete("/episodes/{episode_id}/transcript-segments/{segment_id}")
def delete_segment(episode_id: str, segment_id: str) -> dict[str, Any]:
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=404, detail="Transcript document not found.")
    with get_connection() as db:
        _archive_voice_outputs(db, episode_id, [segment_id], "Transcript segment deleted.")
        deleted = db.execute("DELETE FROM transcript_segments WHERE id=? AND document_id=?", (segment_id, document["id"])).rowcount
        if not deleted:
            raise HTTPException(status_code=404, detail="Transcript segment not found.")
        rows = db.execute("SELECT id FROM transcript_segments WHERE document_id=? ORDER BY start_ms, seq", (document["id"],)).fetchall()
        for index, row in enumerate(rows, 1):
            db.execute("UPDATE transcript_segments SET seq=? WHERE id=?", (index, row["id"]))
        db.execute("UPDATE transcript_documents SET user_edited=1, revision=revision+1, updated_at=? WHERE id=?", (now_iso(), document["id"]))
    return _state(episode_id)


@router.post("/episodes/{episode_id}/transcript-segments/{segment_id}/split")
def split_segment(episode_id: str, segment_id: str, payload: SplitBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=404, detail="Transcript document not found.")
    with get_connection() as db:
        row = db.execute("SELECT * FROM transcript_segments WHERE id=? AND document_id=?", (segment_id, document["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Transcript segment not found.")
    at_ms = payload.at_ms or ((row["start_ms"] + row["end_ms"]) // 2)
    if at_ms <= row["start_ms"] or at_ms >= row["end_ms"]:
        raise HTTPException(status_code=400, detail="Split time must be inside the segment.")
    words = row["source_text"].split()
    pivot = max(1, len(words) // 2)
    left = " ".join(words[:pivot]) or row["source_text"]
    right = " ".join(words[pivot:]) or row["source_text"]
    timestamp = now_iso()
    with get_connection() as db:
        _archive_voice_outputs(db, episode_id, [segment_id], "Transcript segment split; the old timeline is no longer active.")
        _mark_voice_outputs_stale(db, episode_id, [segment_id], "Transcript segment split; regenerate the new segments.")
        db.execute("UPDATE transcript_segments SET end_ms=?, source_text=?, user_edited=1, translation_status='stale', stale_for_voice=1, updated_at=? WHERE id=?", (at_ms, left, timestamp, segment_id))
        db.execute("UPDATE transcript_segments SET seq=seq+10000 WHERE document_id=? AND seq>?", (document["id"], row["seq"]))
        db.execute("UPDATE transcript_segments SET seq=seq-9999 WHERE document_id=? AND seq>10000", (document["id"],))
        db.execute("INSERT INTO transcript_segments (id, document_id, seq, start_ms, end_ms, source_text, source_text_origin, speaker, segment_type, subtitle_enabled, review_status, user_edited, stale_for_voice, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)", (f"seg-{uuid.uuid4().hex[:12]}", document["id"], row["seq"] + 1, at_ms, row["end_ms"], right, row["source_text_origin"], row["speaker"], row["segment_type"], row["subtitle_enabled"], row["review_status"], timestamp, timestamp))
        db.execute("UPDATE transcript_documents SET user_edited=1, revision=revision+1, updated_at=? WHERE id=?", (timestamp, document["id"]))
    return _state(episode_id)


@router.post("/episodes/{episode_id}/transcript-segments/merge")
def merge_segments(episode_id: str, payload: MergeBody) -> dict[str, Any]:
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=404, detail="Transcript document not found.")
    placeholders = ",".join("?" for _ in payload.segment_ids)
    with get_connection() as db:
        rows = db.execute(f"SELECT * FROM transcript_segments WHERE document_id=? AND id IN ({placeholders}) ORDER BY seq", (document["id"], *payload.segment_ids)).fetchall()
        if len(rows) != len(payload.segment_ids):
            raise HTTPException(status_code=404, detail="One or more transcript segments were not found.")
        first = rows[0]
        timestamp = now_iso()
        confirmed_merge = bool(
            payload.create_new_segment
            or payload.confirmed_source_text is not None
            or payload.confirmed_speaker is not None
        )
        if confirmed_merge:
            source_text = str(payload.confirmed_source_text or "").strip()
            speaker = str(payload.confirmed_speaker or "").strip()
            if not payload.create_new_segment or not source_text or not speaker:
                raise HTTPException(
                    status_code=400,
                    detail="Confirmed merge requires create_new_segment, confirmed_source_text, and confirmed_speaker.",
                )
            if len(rows) != 2 or int(rows[1]["seq"]) != int(rows[0]["seq"]) + 1:
                raise HTTPException(status_code=400, detail="Confirmed merge requires exactly two adjacent transcript segments.")
        _archive_voice_outputs(db, episode_id, list(payload.segment_ids), "Transcript segments merged; the old timeline is no longer active.")
        _mark_voice_outputs_stale(db, episode_id, list(payload.segment_ids), "Transcript segments merged; regenerate the merged segment.")
        merged_segment_id = first["id"]
        if confirmed_merge:
            merged_segment_id = f"seg-{uuid.uuid4().hex[:12]}"
            db.execute(
                f"DELETE FROM transcript_segments WHERE id IN ({placeholders})",
                tuple(payload.segment_ids),
            )
            db.execute(
                "INSERT INTO transcript_segments "
                "(id, document_id, seq, start_ms, end_ms, source_text, source_text_origin, speaker, speaker_label, "
                "segment_type, literal_translation, localized_translation, final_translation, translation_status, "
                "subtitle_enabled, review_status, user_edited, translation_edited, stale_for_voice, confidence, "
                "timestamp_source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, '', '', '', 'stale', ?, 'pending', 1, 0, 1, NULL, "
                "'manual_boundary_correction', ?, ?)",
                (
                    merged_segment_id,
                    document["id"],
                    first["seq"],
                    min(row["start_ms"] for row in rows),
                    max(row["end_ms"] for row in rows),
                    source_text,
                    speaker,
                    speaker,
                    first["segment_type"],
                    bool_int(all(bool(row["subtitle_enabled"]) for row in rows)),
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                "UPDATE translation_documents SET status='stale', updated_at=? WHERE transcript_document_id=?",
                (timestamp, document["id"]),
            )
            db.execute(
                "UPDATE subtitle_outputs SET status='stale' WHERE episode_id=?",
                (episode_id,),
            )
        else:
            db.execute("UPDATE transcript_segments SET start_ms=?, end_ms=?, source_text=?, final_translation=?, user_edited=1, translation_edited=1, translation_status='stale', stale_for_voice=1, updated_at=? WHERE id=?", (min(row["start_ms"] for row in rows), max(row["end_ms"] for row in rows), " ".join(row["source_text"] for row in rows), " ".join(row["final_translation"] for row in rows if row["final_translation"]), timestamp, first["id"]))
            for row in rows[1:]:
                db.execute("DELETE FROM transcript_segments WHERE id=?", (row["id"],))
        remaining = db.execute("SELECT id FROM transcript_segments WHERE document_id=? ORDER BY start_ms, seq", (document["id"],)).fetchall()
        for index, row in enumerate(remaining, 1):
            db.execute("UPDATE transcript_segments SET seq=? WHERE id=?", (index, row["id"]))
        db.execute(
            "UPDATE transcript_documents SET user_edited=1, segment_count=?, revision=revision+1, updated_at=? WHERE id=?",
            (len(remaining), timestamp, document["id"]),
        )
    return {**_state(episode_id), "merged_segment_id": merged_segment_id}


@router.post("/episodes/{episode_id}/transcript-review/batch")
def batch_review(episode_id: str, payload: BatchReviewBody) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=400, detail="Create or import a transcript before batch review.")
    allowed = {"approve_transcription", "adopt_recommended_translations", "approve_no_warnings", "process_exceptions", "save"}
    if payload.action not in allowed:
        raise HTTPException(status_code=400, detail="Invalid batch review action.")
    segments = _segments(document["id"])
    style = _ensure_style(episode_id)
    exceptions = [
        item for item in segments
        if item["reading_speed_warning"] or (item.get("confidence") is not None and item["confidence"] < 0.75)
        or item["translation_status"] in {"failed", "stale"} or not item["final_translation"].strip()
    ]
    changed = 0
    affected_voice_ids: list[str] = []
    if payload.action == "process_exceptions":
        result = {"action": payload.action, "affected_count": len(exceptions), "exception_segment_ids": [item["id"] for item in exceptions]}
        state = _state(episode_id)
        state["batch_result"] = result
        return state
    with get_connection() as db:
        for item in segments:
            if payload.action == "approve_transcription":
                if not item["user_edited"] and item["review_status"] != "approved":
                    db.execute("UPDATE transcript_segments SET review_status='approved', updated_at=? WHERE id=?", (now_iso(), item["id"]))
                    changed += 1
            elif payload.action == "adopt_recommended_translations":
                if not item["translation_edited"] and (item["localized_translation"] or item["literal_translation"]):
                    recommendation = item["localized_translation"] or item["literal_translation"]
                    if recommendation != item["final_translation"]:
                        db.execute("UPDATE transcript_segments SET final_translation=?, translation_status='completed', stale_for_voice=1, updated_at=? WHERE id=?", (recommendation, now_iso(), item["id"]))
                        affected_voice_ids.append(item["id"])
                        changed += 1
            elif payload.action == "approve_no_warnings":
                has_warning = item["reading_speed_warning"] or (item.get("confidence") is not None and item["confidence"] < 0.75) or item["translation_status"] in {"failed", "stale"}
                if not has_warning and item["review_status"] != "approved":
                    db.execute("UPDATE transcript_segments SET review_status='approved', updated_at=? WHERE id=?", (now_iso(), item["id"]))
                    changed += 1
        if affected_voice_ids:
            _mark_voice_outputs_stale(db, episode_id, affected_voice_ids, "Recommended translation adopted; regenerate this segment.")
        if changed:
            db.execute("UPDATE transcript_documents SET user_edited=1, revision=revision+1, updated_at=? WHERE id=?", (now_iso(), document["id"]))
    result = {"action": payload.action, "affected_count": changed, "exception_segment_ids": [item["id"] for item in exceptions]}
    state = _state(episode_id)
    state["batch_result"] = result
    return state


@router.put("/episodes/{episode_id}/subtitle-style")
def save_subtitle_style(episode_id: str, payload: StyleBody) -> dict[str, Any]:
    _episode(episode_id)
    legacy_mode_aliases = {
        "add_target_subtitle": "replace_soft_subtitle",
        "bilingual_subtitle": "keep_original_and_add_target",
        "mask_burned_subtitle": "mask_burned_subtitle_and_add_target",
        "keep_original_only": "no_target_subtitle",
        "no_subtitle": "no_target_subtitle",
        "ai_remove_burned_subtitle_disabled": "mask_burned_subtitle_and_add_target",
    }
    normalized_mode = legacy_mode_aliases.get(payload.render_mode, payload.render_mode)
    if normalized_mode not in RENDER_MODES:
        raise HTTPException(status_code=400, detail="Invalid subtitle render mode.")
    available_fonts = {font["id"] for font in _font_catalog()}
    if payload.font_family not in available_fonts:
        raise HTTPException(status_code=400, detail={"code": "SUBTITLE_FONT_UNAVAILABLE", "message": "The selected subtitle font is not available to FFmpeg/libass."})
    previous = _ensure_style(episode_id)
    values = payload.model_dump()
    values["render_mode"] = normalized_mode
    values["updated_at"] = now_iso()
    changed = any(previous.get(key) != values.get(key) for key in values if key != "updated_at")
    with get_connection() as db:
        db.execute(
            "UPDATE subtitle_styles SET render_mode=?, font_family=?, font_size=?, text_color=?, outline_color=?, outline_width=?, background_color=?, position=?, safe_margin_percent=?, max_lines=?, mask_height_percent=?, font_key=?, preview_stale=?, preview_stale_reason=?, updated_at=? WHERE episode_id=?",
            (values["render_mode"], values["font_family"], values["font_size"], values["text_color"], values["outline_color"], values["outline_width"], values["background_color"], values["position"], values["safe_margin_percent"], values["max_lines"], values["mask_height_percent"], values["font_family"], bool_int(changed), "Subtitle settings changed; regenerate the preview." if changed else previous.get("preview_stale_reason"), values["updated_at"], episode_id),
        )
    return _state(episode_id)


@router.post("/episodes/{episode_id}/subtitle-export")
def export_subtitles(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    document = _document(episode_id)
    if not document:
        raise HTTPException(status_code=400, detail="Create or import a transcript before export.")
    authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    segments = authority.apply_to_segments(_segments(document["id"]))
    directory = _episode_output(episode) / "subtitles"
    source_path = directory / "source_subtitles.srt"
    language_code = _language_code(authority.language)
    target_srt = directory / f"translated_subtitles_{language_code}.srt"
    target_vtt = directory / f"translated_subtitles_{language_code}.vtt"
    uses_translation = authority.language_mode != localization_text_authority.DUB_ONLY
    subtitle_service.write_srt(segments, target_srt, translated=uses_translation)
    subtitle_service.write_vtt(segments, target_vtt, translated=uses_translation)
    timestamp = now_iso()
    with get_connection() as db:
        outputs = (("translated_subtitles", target_srt, "srt"), ("translated_subtitles", target_vtt, "vtt"))
        if style["render_mode"] != "target_only":
            subtitle_service.write_srt(segments, source_path)
            outputs = (("source_subtitles", source_path, "srt"),) + outputs
        for output_type, path, fmt in outputs:
            db.execute("INSERT INTO subtitle_outputs (id, episode_id, output_type, file_path, file_format, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?)", (f"sout-{uuid.uuid4().hex[:12]}", episode_id, output_type, str(path), fmt, timestamp))
    return _state(episode_id)


def _run_preview(task_id: str, episode_id: str, requested_style: dict[str, Any] | None = None) -> None:
    try:
        episode = _episode(episode_id)
        video_path = _video_path(episode)
        document = _document(episode_id)
        if not document:
            raise subtitle_service.SubtitleValidationError("Create or import a transcript before preview.")
        style = requested_style or _ensure_style(episode_id)
        if style.get("mask_height_ratio") is not None:
            style["mask_height_percent"] = round(float(style["mask_height_ratio"]) * 100)
        if style.get("render_mode") == "target_only" and not style.get("mask_height_percent"):
            style["mask_height_percent"] = 22
        authority = localization_text_authority.resolve_episode_text_authority(episode_id)
        segments = authority.apply_to_segments(_segments(document["id"]))
        target_language = authority.language
        directory = _episode_output(episode) / "subtitles"
        directory.mkdir(parents=True, exist_ok=True)
        mode = style["render_mode"]
        bilingual = (
            authority.language_mode != localization_text_authority.DUB_ONLY
            and mode == "keep_original_and_add_target"
            and document.get("source_text_origin") in {"external_subtitle", "embedded_subtitle"}
        )
        translated = mode != "no_target_subtitle"
        subtitle_path = directory / (
            "preview_subtitles.ass" if translated and not bilingual else "preview_subtitles.srt"
        )
        subtitle_input: Path | None = subtitle_path if translated or bilingual else None
        subtitle_cues: list[dict[str, Any]] = []
        render_style = dict(style)
        if subtitle_input:
            if translated and not bilingual:
                metadata = media_service.probe_media_metadata(video_path)
                subtitle_cues = audio_localization_service.build_renderable_subtitle_cues(
                    segments,
                    int(metadata["width"]),
                    int(metadata["height"]),
                    preferred_font_size=int(style["font_size"]),
                    target_language=target_language,
                )
                audio_localization_service._write_overseas_ass(
                    segments,
                    subtitle_input,
                    int(metadata["width"]),
                    int(metadata["height"]),
                    subtitle_position={"bottom": "low", "middle": "standard", "top": "high"}.get(
                        str(style.get("position") or "bottom"),
                        "low",
                    ),
                    cues=subtitle_cues,
                )
                render_style["use_embedded_ass_style"] = True
                render_style["safe_margin_percent"] = round(
                    audio_localization_service.SUBTITLE_SAFE_MARGIN_RATIO * 100
                )
                render_style["max_lines"] = 2
            else:
                subtitle_service.write_srt(segments, subtitle_input, translated=translated, bilingual=bilingual)
        version = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output_name = f"subtitle-preview-target-only-{version}.mp4" if mode == "target_only" else f"subtitle-preview-{version}.mp4"
        output_path = _episode_output(episode) / output_name
        _update_task(task_id, status="running", progress=20, stage="rendering_subtitle_preview", started_at=now_iso())
        with get_connection() as db:
            db.execute("UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? AND output_type='subtitle_preview' AND status='completed'", (episode_id,))
        result = media_service.render_subtitle_preview(video_path, subtitle_input, output_path, render_style)
        timestamp = now_iso()
        with get_connection() as db:
            db.execute("INSERT INTO subtitle_outputs (id, episode_id, task_id, output_type, file_path, file_format, status, metadata_json, created_at) VALUES (?, ?, ?, 'subtitle_preview', ?, 'mp4', 'completed', ?, ?)", (f"sout-{uuid.uuid4().hex[:12]}", episode_id, task_id, str(output_path), json_dump({"render_mode": mode, "target_language": target_language, "mask_enabled": mode == "target_only", "mask_height_ratio": round(float(style.get("mask_height_percent") or 22) / 100, 4), "notice": "Preview rendered from the current subtitle settings.", "subtitle_cue_manifest": audio_localization_service.subtitle_cue_manifest(subtitle_cues) if subtitle_cues else None}), timestamp))
            db.execute("UPDATE subtitle_styles SET preview_stale=0, preview_stale_reason=NULL, updated_at=? WHERE episode_id=?", (timestamp, episode_id))
        _update_task(task_id, status="completed", progress=100, stage="completed", stderr=result["stderr"], output_path=str(output_path), completed_at=timestamp)
    except Exception as error:
        _fail_task(task_id, error)


@router.post("/episodes/{episode_id}/subtitle-preview")
def create_subtitle_preview(episode_id: str, background_tasks: BackgroundTasks, payload: SubtitlePreviewBody | None = None) -> dict[str, Any]:
    episode = _episode(episode_id)
    _video_path(episode)
    task_id = _create_task(episode, "subtitle_preview")
    background_tasks.add_task(_run_preview, task_id, episode_id, payload.model_dump() if payload else None)
    return _task(task_id)
