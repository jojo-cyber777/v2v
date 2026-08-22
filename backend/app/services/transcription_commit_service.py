"""Validated, atomic persistence for local STT results."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..database import get_connection, json_dump, now_iso
from .transcript_invalidation_service import invalidate_after_transcript_change


SPEAKER_PATTERN = re.compile(r"^SPEAKER_(?:\d{2,}|UNKNOWN)$")


class TranscriptionCommitError(RuntimeError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail[:4000]


def validate_transcription_result(result: dict[str, Any], audio_duration: float) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", "The local STT result is not an object.")
    for key in ("provider", "model", "detected_language", "timestamp_source", "metadata"):
        if not result.get(key):
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"The local STT result is missing {key}.")
    if result.get("provider") != "local_whisper_pyannote":
        raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", "The atomic local commit received the wrong provider result.")
    rows = result.get("segments")
    if not isinstance(rows, list) or not rows:
        raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", "The local STT result contains no segments.")
    limit_ms = round((float(audio_duration) + 0.05) * 1000)
    previous_end = 0
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} is not an object.")
        try:
            start_ms, end_ms = int(raw["start_ms"]), int(raw["end_ms"])
        except (KeyError, TypeError, ValueError) as error:
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} has invalid timestamps.") from error
        speaker = str(raw.get("speaker") or "")
        text = str(raw.get("text") or "").strip()
        if start_ms < 0 or end_ms <= start_ms or start_ms < previous_end or end_ms > limit_ms:
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} has an invalid time range.")
        if not text or not SPEAKER_PATTERN.fullmatch(speaker):
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} has invalid text or speaker_id.")
        words = raw.get("words") or []
        if not isinstance(words, list):
            raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} words are invalid.")
        for word in words:
            if not isinstance(word, dict):
                raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} contains an invalid word.")
            try:
                word_start = round(float(word["start"]) * 1000)
                word_end = round(float(word["end"]) * 1000)
            except (KeyError, TypeError, ValueError) as error:
                raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} word timestamps are invalid.") from error
            if word_start < start_ms - 50 or word_end > end_ms + 50 or word_end <= word_start:
                raise TranscriptionCommitError("LOCAL_STT_OUTPUT_INVALID", f"Segment {index} word timestamps are out of range.")
        normalized.append(dict(raw, start_ms=start_ms, end_ms=end_ms, text=text, speaker=speaker))
        previous_end = end_ms
    return normalized


def commit_transcription_result_atomic(
    *,
    episode: dict[str, Any],
    task_id: str,
    result: dict[str, Any],
    audio_duration: float,
    source_language: str,
    overwrite_user_edits: bool,
    subtitle_path: Path,
    transcript_json_path: Path,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    rows = validate_transcription_result(result, audio_duration)
    timestamp = now_iso()
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_json_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_text = "".join(
        f"{index}\n{_srt_time(row['start_ms'])} --> {_srt_time(row['end_ms'])}\n{row['text']}\n\n"
        for index, row in enumerate(rows, 1)
    )
    subtitle_path.write_text(subtitle_text, encoding="utf-8")

    try:
        with get_connection() as db:
            existing = db.execute("SELECT * FROM transcript_documents WHERE episode_id=?", (episode["id"],)).fetchone()
            if existing and existing["user_edited"] and not overwrite_user_edits:
                raise TranscriptionCommitError(
                    "TRANSCRIPT_OVERWRITE_CONFIRMATION_REQUIRED",
                    "The transcript contains user edits. Confirm overwrite before replacing it.",
                )
            document_id = existing["id"] if existing else f"tdoc-{uuid.uuid4().hex[:12]}"
            old_rows = db.execute(
                "SELECT * FROM transcript_segments WHERE document_id=? ORDER BY seq", (document_id,)
            ).fetchall() if existing else []
            old_segment_ids = [row["id"] for row in old_rows]
            source_id = f"ssrc-{uuid.uuid4().hex[:12]}"
            metadata = dict(result.get("metadata") or {})
            metadata["segment_details"] = [
                {"speaker_id": row["speaker"], "words": row.get("words", []), "overlap": bool(row.get("overlap"))}
                for row in rows
            ]
            db.execute(
                "INSERT INTO subtitle_sources (id, episode_id, source_type, selected, priority, language, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, 'speech_only', 1, 200, ?, ?, ?, ?)",
                (source_id, episode["id"], result["detected_language"], json_dump({
                    "provider": result["provider"], "model": result["model"],
                    "provider_request_id": result.get("provider_request_id"),
                    "timestamp_source": result["timestamp_source"],
                }), timestamp, timestamp),
            )
            db.execute("UPDATE subtitle_sources SET selected=CASE WHEN id=? THEN 1 ELSE 0 END WHERE episode_id=?", (source_id, episode["id"]))
            transcript_text = str(result.get("transcript") or "".join(row["text"] for row in rows))
            if existing:
                db.execute(
                    "UPDATE transcript_documents SET source_id=?, source_text_origin='speech_transcription', source_language=?, "
                    "selected_source_mode='speech_primary', source_priority='speech_transcription', comparison_mode='source_review', "
                    "revision=revision+1, provider=?, model=?, status='completed', user_edited=0, detected_language=?, "
                    "transcript_text=?, provider_request_id=?, timestamp_source=?, audio_duration=?, speaker_count=?, segment_count=?, "
                    "provider_metadata_json=?, updated_at=? WHERE id=?",
                    (source_id, source_language, result["provider"], result["model"], result["detected_language"],
                     transcript_text, result.get("provider_request_id"), result["timestamp_source"], audio_duration,
                     result.get("speaker_count", 0), len(rows), json_dump(metadata), timestamp, document_id),
                )
            else:
                db.execute(
                    "INSERT INTO transcript_documents (id, episode_id, source_id, source_text_origin, source_language, selected_source_mode, "
                    "source_priority, comparison_mode, revision, provider, model, status, user_edited, detected_language, transcript_text, "
                    "provider_request_id, timestamp_source, audio_duration, speaker_count, segment_count, provider_metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'speech_transcription', ?, 'speech_primary', 'speech_transcription', 'source_review', 1, ?, ?, "
                    "'completed', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (document_id, episode["id"], source_id, source_language, result["provider"], result["model"],
                     result["detected_language"], transcript_text, result.get("provider_request_id"), result["timestamp_source"],
                     audio_duration, result.get("speaker_count", 0), len(rows), json_dump(metadata), timestamp, timestamp),
                )
            current_ids: list[str] = []
            for index, row in enumerate(rows, 1):
                segment_id = old_rows[index - 1]["id"] if index <= len(old_rows) else f"seg-{uuid.uuid4().hex[:12]}"
                current_ids.append(segment_id)
                if index <= len(old_rows):
                    db.execute(
                        "UPDATE transcript_segments SET seq=?, start_ms=?, end_ms=?, source_text=?, source_text_origin='speech_transcription', "
                        "speaker=?, speaker_label=?, segment_type=?, literal_translation='', localized_translation='', final_translation='', "
                        "translation_status='not_started', subtitle_enabled=1, review_status='pending', user_edited=0, translation_edited=0, "
                        "stale_for_voice=1, confidence=?, timestamp_source=?, updated_at=? WHERE id=?",
                        (index, row["start_ms"], row["end_ms"], row["text"], row["speaker"], row["speaker"],
                         row.get("segment_type", "dialogue"), row.get("confidence"), row.get("timestamp_source"), timestamp, segment_id),
                    )
                else:
                    db.execute(
                        "INSERT INTO transcript_segments (id, document_id, seq, start_ms, end_ms, source_text, source_text_origin, speaker, "
                        "speaker_label, segment_type, confidence, timestamp_source, subtitle_enabled, review_status, stale_for_voice, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'speech_transcription', ?, ?, ?, ?, ?, 1, 'pending', 1, ?, ?)",
                        (segment_id, document_id, index, row["start_ms"], row["end_ms"], row["text"], row["speaker"], row["speaker"],
                         row.get("segment_type", "dialogue"), row.get("confidence"), row.get("timestamp_source"), timestamp, timestamp),
                    )
            if fault_injector:
                fault_injector("after_segment_write")
            invalidate_after_transcript_change(
                db,
                episode_id=episode["id"],
                transcript_document_id=document_id,
                segment_ids=old_segment_ids,
                timestamp=timestamp,
                fault_injector=fault_injector,
            )
            if len(old_rows) > len(rows):
                surplus = [row["id"] for row in old_rows[len(rows):]]
                db.execute(f"DELETE FROM transcript_segments WHERE id IN ({','.join('?' for _ in surplus)})", surplus)
            db.execute(
                "INSERT INTO subtitle_outputs (id, episode_id, task_id, output_type, file_path, file_format, metadata_json, created_at) "
                "VALUES (?, ?, ?, 'source_subtitles', ?, 'srt', '{}', ?)",
                (f"sout-{uuid.uuid4().hex[:12]}", episode["id"], task_id, str(subtitle_path), timestamp),
            )
            db.execute(
                "UPDATE ai_tasks SET status='completed', progress=100, stage='completed', output_path=?, provider_request_id=?, "
                "attempt_count=?, usage_json=?, completed_at=?, updated_at=? WHERE id=?",
                (str(subtitle_path), result.get("provider_request_id"), result.get("attempt_count", 1),
                 json_dump(metadata), timestamp, timestamp, task_id),
            )
            document = dict(db.execute("SELECT * FROM transcript_documents WHERE id=?", (document_id,)).fetchone())
            persisted = [dict(row) for row in db.execute(
                "SELECT * FROM transcript_segments WHERE document_id=? ORDER BY seq", (document_id,)
            ).fetchall()]
            transcript_json_path.write_text(
                json.dumps({"document": document, "segments": persisted}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return {"document": document, "segments": persisted, "source_id": source_id}
    except TranscriptionCommitError:
        raise
    except Exception as error:
        raise TranscriptionCommitError("LOCAL_STT_COMMIT_FAILED", "The local transcript commit failed.", repr(error)) from error


def _srt_time(value_ms: int) -> str:
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

