"""Duration-constrained translation and TTS fitting for Episode dialogue slots."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

from ..database import ROOT, bool_int, get_connection, json_dump, json_load, now_iso
from . import localization_text_authority, media_service, provider_service, tts_input_revision_service


SLOT_SAFETY_SECONDS = 0.02
MAX_TTS_SPEED = 2.0
MAX_SHORTENING_RETRIES = 1
ESTIMATED_SPOKEN_WORDS_PER_SECOND = 2.6
CAPPED_SPEED_WARNING_STATUS = "capped_speed_warning"
FIT_STATUSES = {"fit", "shortened_and_fit", "speed_adjusted_and_fit", CAPPED_SPEED_WARNING_STATUS}
VOICE_ROLE_TYPES = {"dialogue", "offscreen_dialogue", "narration", "inner_monologue"}
FOLLOWING_SILENCE_SEGMENT_TYPES = {"offscreen_dialogue", "narration", "inner_monologue"}
MIN_COMPLETE_DIALOGUE_SLOT_MS = 600
SHORT_DIALOGUE_SAFETY_GAP_MS = 50
OUTPUTS_DIR = ROOT / "outputs"

_NEGATION_PATTERN = re.compile(
    r"\b(?:no|not|never|neither|nor|nothing|nobody|nowhere|cannot|can't|won't|"
    r"wouldn't|shouldn't|couldn't|don't|doesn't|didn't|isn't|aren't|wasn't|weren't)\b",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(
    r"(?<!\w)(?:[$£€¥]\s*)?\d+(?:[.,]\d+)*(?:\s*(?:%|k|m|bn|grand|million|billion))?(?!\w)",
    re.IGNORECASE,
)
_RELATIONSHIP_GROUPS = (
    {"girlfriend", "partner"},
    {"boyfriend", "partner"},
    {"wife", "spouse"},
    {"husband", "spouse"},
    {"mother", "mom", "mum"},
    {"father", "dad"},
    {"daughter"},
    {"son"},
    {"sister"},
    {"brother"},
)
_FILLER_PATTERNS = (
    r"\b(?:actually|basically|really|very|literally|honestly|seriously|simply)\b",
    r"\b(?:you know|I mean|kind of|sort of)\b",
    r"\b(?:as a matter of fact|in fact)\b",
)
_PHRASE_REPLACEMENTS = (
    (r"\bI am\b", "I'm"),
    (r"\byou are\b", "you're"),
    (r"\bwe are\b", "we're"),
    (r"\bthey are\b", "they're"),
    (r"\bI will\b", "I'll"),
    (r"\byou will\b", "you'll"),
    (r"\bdo not\b", "don't"),
    (r"\bdoes not\b", "doesn't"),
    (r"\bdid not\b", "didn't"),
    (r"\bcannot\b", "can't"),
    (r"\bwill not\b", "won't"),
    (r"\bwould like to\b", "want to"),
    (r"\bin order to\b", "to"),
    (r"\bfound out that\b", "learned"),
    (r"\bfound out\b", "learned"),
    (r"\bwas also yours\b", "was yours"),
    (r"\bfor that\b", ""),
    (r"\beven\b", ""),
)


class DurationConstrainedTTSError(RuntimeError):
    def __init__(self, code: str, message: str, *, segment_ids: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.segment_ids = segment_ids or []


def dubbing_speaker_for_segment(segment: dict[str, Any]) -> str:
    role = str(segment.get("segment_type") or "dialogue")
    speaker = str(segment.get("speaker_label") or segment.get("speaker") or "").strip()
    if speaker:
        return speaker
    return "Narrator" if role == "narration" else "Speaker 01"


def plan_extreme_short_slot_expansions(
    segments: list[dict[str, Any]],
    transform_units: list[dict[str, Any]],
    video_duration_ms: int,
    *,
    selected_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Expand complete on-screen lines without crossing dialogue or Transform Unit boundaries."""
    ordered = sorted(
        (dict(item) for item in segments),
        key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)),
    )
    units = sorted(
        (
            {
                "transform_unit_id": str(item.get("transform_unit_id") or ""),
                "start_ms": round(float(item["source_start"]) * 1000),
                "end_ms": round(float(item["source_end"]) * 1000),
            }
            for item in transform_units
            if float(item.get("source_end") or 0) > float(item.get("source_start") or 0)
        ),
        key=lambda item: (item["start_ms"], item["end_ms"]),
    )
    actions: list[dict[str, Any]] = []
    for segment in ordered:
        segment_id = str(segment["id"])
        if selected_ids is not None and segment_id not in selected_ids:
            continue
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        if (
            end_ms - start_ms >= MIN_COMPLETE_DIALOGUE_SLOT_MS
            or str(segment.get("segment_type") or "") != "dialogue"
            or str(segment.get("review_status") or "") in {"auto_merged", "manually_omitted"}
            or not str(segment.get("source_text") or "").strip()
            or not str(segment.get("final_translation") or "").strip()
        ):
            continue
        unit = next(
            (
                item
                for item in units
                if item["start_ms"] <= start_ms < item["end_ms"] and end_ms <= item["end_ms"]
            ),
            None,
        )
        if not unit:
            continue
        boundary_ms = min(int(unit["end_ms"]), int(video_duration_ms))
        next_start_ms = min(
            (
                int(item["start_ms"])
                for item in ordered
                if int(item["start_ms"]) > start_ms
                and int(item["start_ms"]) < int(unit["end_ms"])
            ),
            default=boundary_ms,
        )
        expanded_end_ms = min(boundary_ms, next_start_ms) - SHORT_DIALOGUE_SAFETY_GAP_MS
        if expanded_end_ms <= end_ms:
            continue
        actions.append({
            "kind": "expand",
            "segment_id": segment_id,
            "transform_unit_id": unit["transform_unit_id"],
            "start_ms": start_ms,
            "old_end_ms": end_ms,
            "end_ms": expanded_end_ms,
            "safety_gap_ms": SHORT_DIALOGUE_SAFETY_GAP_MS,
        })
    return actions


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT e.*, p.processing_mode FROM episodes e JOIN projects p ON p.id=e.project_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
    if not row:
        raise DurationConstrainedTTSError("EPISODE_NOT_FOUND", "Episode not found.")
    return dict(row)


def _segments(episode_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        document = db.execute(
            "SELECT * FROM transcript_documents WHERE episode_id=? AND status='completed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if not document:
            raise DurationConstrainedTTSError("TRANSCRIPT_REQUIRED", "A completed transcript is required.")
        rows = db.execute(
            "SELECT * FROM transcript_segments WHERE document_id=? AND subtitle_enabled=1 ORDER BY start_ms, seq",
            (document["id"],),
        ).fetchall()
    try:
        authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    except localization_text_authority.LocalizationTextAuthorityError as error:
        raise DurationConstrainedTTSError(error.code, str(error)) from error
    result: list[dict[str, Any]] = []
    for row in rows:
        item = authority.apply_to_segment(dict(row))
        if not localization_text_authority.effective_text(item):
            continue
        item["dubbing_speaker"] = dubbing_speaker_for_segment(item)
        item["target_language"] = authority.language
        result.append(item)
    return result


def _binding_map(episode: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with get_connection() as db:
        project_rows = db.execute(
            "SELECT * FROM speaker_voice_bindings WHERE project_id=? AND episode_id IS NULL "
            "AND is_project_default=1 ORDER BY updated_at",
            (episode["project_id"],),
        ).fetchall()
        episode_rows = db.execute(
            "SELECT * FROM speaker_voice_bindings WHERE episode_id=? AND is_episode_override=1 ORDER BY updated_at",
            (episode["id"],),
        ).fetchall()
    resolved = {row["speaker_label"]: dict(row) for row in project_rows}
    resolved.update({row["speaker_label"]: dict(row) for row in episode_rows})
    return resolved


def _slot(segment: dict[str, Any]) -> dict[str, float]:
    start_ms = int(segment["start_ms"])
    end_ms = int(segment["end_ms"])
    available_end_ms = max(end_ms, int(segment.get("_available_end_ms") or end_ms))
    slot_duration = max(0.0, (end_ms - start_ms) / 1000)
    available_duration = max(0.0, (available_end_ms - start_ms) / 1000)
    usable_duration = max(0.001, available_duration - SLOT_SAFETY_SECONDS)
    return {
        "slot_duration": slot_duration,
        "usable_duration": usable_duration,
        "safety_margin": min(SLOT_SAFETY_SECONDS, max(0.0, slot_duration - usable_duration)),
    }


def _assign_contiguous_silence_windows(segments: list[dict[str, Any]]) -> None:
    ordered = sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    for index, segment in enumerate(ordered):
        end_ms = int(segment["end_ms"])
        next_start_ms = int(ordered[index + 1]["start_ms"]) if index + 1 < len(ordered) else end_ms
        can_use_following_silence = (
            str(segment.get("segment_type") or "dialogue").strip().lower()
            in FOLLOWING_SILENCE_SEGMENT_TYPES
        )
        segment["_available_end_ms"] = (
            max(end_ms, next_start_ms)
            if can_use_following_silence
            else end_ms
        )


def _is_minimal_spoken_line(text: str) -> bool:
    words = _normalized_words(text)
    return len(words) <= 2 and len("".join(words)) <= 16


def _output_directory(episode_id: str) -> Path:
    with get_connection() as db:
        row = db.execute("SELECT project_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise DurationConstrainedTTSError("EPISODE_NOT_FOUND", "Episode not found.")
    return OUTPUTS_DIR / row["project_id"] / episode_id / "dubbing" / "duration-constrained"


def _source_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _normalized_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text)


def _word_budget(usable_duration: float) -> int:
    return max(1, math.floor(max(0.0, usable_duration) * ESTIMATED_SPOKEN_WORDS_PER_SECOND))


def _normalized_fact(value: str) -> str:
    return re.sub(r"[\s,.]", "", value).lower()


def _required_facts(text: str) -> dict[str, Any]:
    lower = text.lower()
    numbers = {_normalized_fact(match.group(0)) for match in _NUMBER_PATTERN.finditer(text)}
    relationships = [
        sorted(group)
        for group in _RELATIONSHIP_GROUPS
        if any(re.search(rf"\b{re.escape(term)}\b", lower) for term in group)
    ]
    names = {
        token.lower()
        for token in re.findall(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+([A-Z][a-z]{2,})\b", text)
    }
    return {
        "numbers": numbers,
        "has_negation": bool(_NEGATION_PATTERN.search(text)),
        "relationships": relationships,
        "names": names,
    }


def _missing_required_facts(current_text: str, candidate_text: str) -> list[str]:
    required = _required_facts(current_text)
    candidate_lower = candidate_text.lower()
    candidate_numbers = {
        _normalized_fact(match.group(0)) for match in _NUMBER_PATTERN.finditer(candidate_text)
    }
    missing: list[str] = []
    for number in sorted(required["numbers"] - candidate_numbers):
        missing.append(f"number:{number}")
    if required["has_negation"] and not _NEGATION_PATTERN.search(candidate_text):
        missing.append("negation")
    for group in required["relationships"]:
        if not any(re.search(rf"\b{re.escape(term)}\b", candidate_lower) for term in group):
            missing.append(f"relationship:{'/'.join(group)}")
    for name in sorted(required["names"]):
        if not re.search(rf"\b{re.escape(name)}\b", candidate_lower):
            missing.append(f"name:{name}")
    return missing


def _validate_compression_candidate(
    source_text: str,
    current_text: str,
    candidate_text: str,
    usable_duration: float,
    *,
    provider_meaning_preserved: bool = True,
) -> dict[str, Any]:
    del source_text  # The source is required by the provider prompt; local checks preserve explicit English facts.
    normalized = re.sub(r"\s+", " ", candidate_text).strip()
    words = _normalized_words(normalized)
    max_words = _word_budget(usable_duration)
    missing_facts = _missing_required_facts(current_text, normalized) if normalized else []
    reasons: list[str] = []
    if not normalized:
        reasons.append("empty")
    if normalized and len(normalized) >= len(current_text.strip()):
        reasons.append("not_shorter")
    if len(words) > max_words:
        reasons.append("estimated_duration_exceeded")
    if not provider_meaning_preserved:
        reasons.append("provider_meaning_not_preserved")
    if missing_facts:
        reasons.append("key_facts_missing")
    return {
        "valid": not reasons,
        "text": normalized,
        "word_count": len(words),
        "max_words": max_words,
        "estimated_duration": len(words) / ESTIMATED_SPOKEN_WORDS_PER_SECOND,
        "missing_facts": missing_facts,
        "reasons": reasons,
    }


def _simple_rule_compression(
    source_text: str,
    current_text: str,
    usable_duration: float,
) -> dict[str, Any]:
    candidate = re.sub(r"\s+", " ", current_text).strip()
    applied_rules: list[str] = []
    for pattern, replacement in _PHRASE_REPLACEMENTS:
        updated = re.sub(pattern, replacement, candidate, flags=re.IGNORECASE)
        if updated != candidate:
            candidate = updated
            applied_rules.append(pattern)
    for pattern in _FILLER_PATTERNS:
        updated = re.sub(pattern, "", candidate, flags=re.IGNORECASE)
        if updated != candidate:
            candidate = updated
            applied_rules.append(pattern)
    candidate = re.sub(r"\s+([,.;!?])", r"\1", candidate)
    candidate = re.sub(r",\s*,+", ",", candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" ,")

    validation = _validate_compression_candidate(
        source_text,
        current_text,
        candidate,
        usable_duration,
    )
    if validation["valid"]:
        return {**validation, "strategy": "simple_rules", "applied_rules": applied_rules}

    # A leading scene-setting clause is the least important removable unit. Only drop it
    # when every explicit number, relationship, name, and negation remains intact.
    clauses = [part.strip() for part in re.split(r"(?<=[,;:])\s+", candidate) if part.strip()]
    if len(clauses) > 1:
        without_lead = " ".join(clauses[1:]).strip(" ,")
        lead_validation = _validate_compression_candidate(
            source_text,
            current_text,
            without_lead,
            usable_duration,
        )
        if lead_validation["valid"]:
            return {
                **lead_validation,
                "strategy": "simple_rules_drop_lead_context",
                "applied_rules": [*applied_rules, "drop_leading_context_clause"],
            }
    return {**validation, "strategy": "simple_rules", "applied_rules": applied_rules}


def _output_usage(row: Any) -> dict[str, Any]:
    return json_load(row["usage_json"] if row else "{}", {})


def _output_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _audio_path(row: Any, *, prefer_fitted: bool = False) -> Path | None:
    values = (
        (row["aligned_audio_path"], row["raw_audio_path"])
        if prefer_fitted
        else (row["raw_audio_path"], row["aligned_audio_path"])
    )
    for value in values:
        path = Path(str(value or ""))
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def _matching_outputs(
    episode_id: str,
    segment_id: str,
    text: str,
    binding: dict[str, Any],
) -> list[Any]:
    with get_connection() as db:
        return db.execute(
            "SELECT * FROM voice_segment_outputs WHERE episode_id=? AND transcript_segment_id=? "
            "AND output_kind='segment' AND source_text_hash=? AND voice_id=? AND model=? "
            "ORDER BY updated_at DESC",
            (episode_id, segment_id, _source_hash(text), binding["voice_id"], binding["model"]),
        ).fetchall()


def _current_output(
    episode_id: str,
    segment: dict[str, Any],
    binding: dict[str, Any],
) -> Any | None:
    for row in _matching_outputs(
        episode_id, segment["id"], localization_text_authority.effective_text(segment), binding
    ):
        if row["status"] in {"completed", "pending_regeneration", "manual_review_required"} and _audio_path(row):
            return row
    return None


def _current_revision_output(
    episode_id: str,
    segment: dict[str, Any],
    binding: dict[str, Any],
) -> Any | None:
    expected_hash = tts_input_revision_service.build_tts_input_revision(segment, binding).input_hash
    for row in _matching_outputs(
        episode_id, segment["id"], localization_text_authority.effective_text(segment), binding
    ):
        if (
            row["status"] == "completed"
            and row["tts_input_hash"] == expected_hash
            and _audio_path(row)
        ):
            return row
    return None


def _merge_usage(row: Any, values: dict[str, Any]) -> dict[str, Any]:
    return {**_output_usage(row), **values}


def _fit_output(
    row: Any,
    segment: dict[str, Any],
    *,
    text_changed: bool,
    retry_count: int,
) -> dict[str, Any]:
    source_path = _audio_path(row)
    if not source_path:
        raise DurationConstrainedTTSError(
            "TTS_AUDIO_MISSING",
            f"The TTS file for segment {segment['id']} is missing.",
            segment_ids=[segment["id"]],
        )
    window = _slot(segment)
    directory = _output_directory(str(row["episode_id"])) / "aligned"
    output_path = directory / f"{segment['id']}-{str(row['source_text_hash'])[:8]}-{row['id']}.wav"
    fit = media_service.fit_tts_audio_to_slot(
        source_path,
        output_path,
        window["usable_duration"],
        maximum_speed=MAX_TTS_SPEED,
    )
    if not fit["fit"]:
        return {
            **fit,
            **window,
            "fit_status": "manual_review_required",
            "retry_count": retry_count,
            "text_changed": text_changed,
        }

    speed_adjustment = float(fit["speed_adjustment"])
    capped_speed_warning = bool(fit.get("capped_speed_warning"))
    fit_status = (
        CAPPED_SPEED_WARNING_STATUS
        if capped_speed_warning
        else "shortened_and_fit"
        if text_changed
        else ("speed_adjusted_and_fit" if speed_adjustment > 1.0001 else "fit")
    )
    usage = _merge_usage(
        row,
        {
            "duration_fit_status": fit_status,
            "status": fit_status,
            "needs_shorter_text": False,
            "needs_attention": capped_speed_warning,
            "warning_code": "TTS_DURATION_CAPPED" if capped_speed_warning else None,
            "warning_message": (
                "The voice line exceeds its original dialogue slot after applying the maximum 2.00x speed."
                if capped_speed_warning
                else None
            ),
            "original_tts_duration": float(fit["original_duration"]),
            "decoded_tts_duration": float(fit["decoded_duration"]),
            "final_tts_duration": float(fit["final_duration"]),
            "slot_duration": window["slot_duration"],
            "available_duration": window["usable_duration"],
            "usable_duration": window["usable_duration"],
            "safety_margin": window["safety_margin"],
            "speed_adjustment": speed_adjustment,
            "required_speed": float(fit.get("duration_ratio") or speed_adjustment),
            "maximum_speed": MAX_TTS_SPEED,
            "slot_overrun_seconds": float(fit.get("slot_overrun_seconds") or 0.0),
            "shortening_retry_count": retry_count,
            "text_shortened": text_changed,
            "final_text": localization_text_authority.effective_text(segment),
        },
    )
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "UPDATE voice_segment_outputs SET aligned_audio_path=?, aligned_duration=?, speed_adjustment=?, "
            "duration_warning=?, status='completed', error_code=NULL, error_message=NULL, usage_json=?, "
            "completed_at=COALESCE(completed_at, ?), updated_at=? WHERE id=?",
            (
                str(output_path),
                float(fit["final_duration"]),
                speed_adjustment,
                bool_int(capped_speed_warning),
                json_dump(usage),
                timestamp,
                timestamp,
                row["id"],
            ),
        )
    return {
        **fit,
        **window,
        "fit_status": fit_status,
        "retry_count": retry_count,
        "text_changed": text_changed,
        "output_id": row["id"],
        "audio_path": str(output_path),
    }


def _tts_idempotency_key(segment_id: str, text: str, binding: dict[str, Any]) -> str:
    material = {
        "operation": "duration_constrained_tts",
        "segment_id": segment_id,
        "text": text,
        "voice_id": binding["voice_id"],
        "model": binding["model"],
        "format": provider_service.tts_configuration().output_format,
    }
    return "tts-fit:" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _obtain_tts_output(
    episode: dict[str, Any],
    segment: dict[str, Any],
    text: str,
    binding: dict[str, Any],
    tts_provider: Any,
) -> tuple[Any, int]:
    for row in _matching_outputs(episode["id"], segment["id"], text, binding):
        if row["status"] in {"completed", "pending_regeneration", "manual_review_required"} and _audio_path(row):
            return row, 0

    config = provider_service.tts_configuration()
    source_hash = _source_hash(text)
    output_id = f"vso-{uuid.uuid4().hex[:12]}"
    idempotency_key = _tts_idempotency_key(segment["id"], text, binding)
    timestamp = now_iso()
    slot_duration = _slot(segment)["slot_duration"]
    with get_connection() as db:
        db.execute(
            "INSERT INTO voice_segment_outputs (id, episode_id, transcript_segment_id, output_kind, provider, "
            "model, voice_id, source_text_hash, idempotency_key, target_language, output_format, target_duration, "
            "status, started_at, created_at, updated_at, heartbeat_at) "
            "VALUES (?, ?, ?, 'segment', ?, ?, ?, ?, ?, ?, ?, ?, 'generating', ?, ?, ?, ?)",
            (
                output_id,
                episode["id"],
                segment["id"],
                binding["provider"],
                binding["model"],
                binding["voice_id"],
                source_hash,
                idempotency_key,
                segment["target_language"],
                config.output_format,
                slot_duration,
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        )

    suffix = config.output_format.split("_", 1)[0].lower()
    if suffix not in {"mp3", "wav", "pcm", "opus", "ulaw", "alaw"}:
        suffix = "audio"
    directory = _output_directory(episode["id"]) / "raw"
    raw_path = directory / f"{segment['id']}-{source_hash[:8]}-{output_id}.{suffix}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = tts_provider.generate_speech(text, binding["voice_id"])
        raw_path.write_bytes(result["audio"])
        generated_duration = media_service.media_duration_seconds(raw_path)
        usage = {
            **(result.get("usage") or {}),
            "duration_fit_status": "pending_measurement",
            "status": "pending_measurement",
            "target_text": text,
            "slot_duration": slot_duration,
        }
        completed_at = now_iso()
        with get_connection() as db:
            db.execute(
                "UPDATE voice_segment_outputs SET raw_audio_path=?, generated_duration=?, aligned_duration=?, "
                "status='completed', provider_request_id=?, usage_json=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    str(raw_path),
                    generated_duration,
                    generated_duration,
                    result.get("provider_request_id"),
                    json_dump(usage),
                    completed_at,
                    completed_at,
                    output_id,
                ),
            )
            row = db.execute("SELECT * FROM voice_segment_outputs WHERE id=?", (output_id,)).fetchone()
        return row, 1
    except Exception as error:
        with get_connection() as db:
            db.execute(
                "UPDATE voice_segment_outputs SET status='failed', error_code=?, error_message=?, "
                "completed_at=?, updated_at=? WHERE id=?",
                (
                    getattr(error, "code", "TTS_GENERATION_FAILED"),
                    provider_service.redact_sensitive_text(str(error)),
                    now_iso(),
                    now_iso(),
                    output_id,
                ),
            )
        raise


def _mark_manual_review(
    row: Any | None,
    segment: dict[str, Any],
    *,
    actual_duration: float | None,
    retry_count: int,
    attempted_text: str,
) -> None:
    if not row:
        return
    window = _slot(segment)
    usage = _merge_usage(
        row,
        {
            "duration_fit_status": "manual_review_required",
            "status": "manual_review_required",
            "needs_shorter_text": segment.get("localization_mode") != localization_text_authority.DUB_ONLY,
            "original_tts_duration": actual_duration,
            "final_tts_duration": actual_duration,
            "slot_duration": window["slot_duration"],
            "available_duration": window["usable_duration"],
            "usable_duration": window["usable_duration"],
            "safety_margin": window["safety_margin"],
            "shortening_retry_count": retry_count,
            "attempted_text": attempted_text,
            "final_text": localization_text_authority.effective_text(segment),
        },
    )
    with get_connection() as db:
        db.execute(
            "UPDATE voice_segment_outputs SET status='manual_review_required', duration_warning=1, "
            "error_code='TTS_DURATION_UNFIT', error_message=?, usage_json=?, updated_at=? WHERE id=?",
            (
                "The voice line cannot fit its original dialogue slot without exceeding the 2.00x speed limit.",
                json_dump(usage),
                now_iso(),
                row["id"],
            ),
        )


def _commit_shortened_text(segment: dict[str, Any], text: str, output_id: str) -> None:
    timestamp = now_iso()
    with get_connection() as db:
        db.execute(
            "UPDATE transcript_segments SET final_translation=?, translation_status='completed', "
            "translation_edited=0, stale_for_voice=0, updated_at=? WHERE id=?",
            (text, timestamp, segment["id"]),
        )
        db.execute(
            "UPDATE voice_segment_outputs SET status='stale', updated_at=? "
            "WHERE transcript_segment_id=? AND output_kind='segment' AND id<>? "
            "AND status IN ('completed','pending_regeneration','manual_review_required')",
            (timestamp, segment["id"], output_id),
        )
        db.execute(
            "UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? "
            "AND output_type IN ('translated_subtitles','translated_subtitle_preview','subtitle_preview')",
            (segment["episode_id"],),
        )


def _context(segments: list[dict[str, Any]], index: int) -> tuple[str | None, str | None]:
    previous_text = localization_text_authority.effective_text(segments[index - 1]) if index > 0 else None
    next_text = (
        localization_text_authority.effective_text(segments[index + 1])
        if index + 1 < len(segments)
        else None
    )
    return previous_text, next_text


def ensure_episode_duration_fit(
    episode_id: str,
    *,
    segment_ids: list[str] | None = None,
    translation_provider: Any | None = None,
    tts_provider: Any | None = None,
    allow_provider_calls: bool = True,
) -> dict[str, Any]:
    episode = _episode(episode_id)
    all_segments = _segments(episode_id)
    _assign_contiguous_silence_windows(all_segments)
    segments = all_segments
    if segment_ids is not None:
        selected = set(segment_ids)
        segments = [item for item in all_segments if item["id"] in selected]
    bindings = _binding_map(episode)
    translator = translation_provider
    voice_provider = tts_provider
    results: list[dict[str, Any]] = []
    translation_calls = 0
    tts_calls = 0

    for index, segment in enumerate(segments):
        segment["episode_id"] = episode_id
        binding = bindings.get(segment["dubbing_speaker"])
        if not binding:
            results.append(
                {
                    "segment_id": segment["id"],
                    "seq": segment["seq"],
                    "fit_status": "failed",
                    "error_code": "VOICE_BINDING_REQUIRED",
                    **_slot(segment),
                }
            )
            continue

        current_text = localization_text_authority.effective_text(segment)
        current_row = (
            _current_output(episode_id, segment, binding)
            if allow_provider_calls
            else _current_revision_output(episode_id, segment, binding)
        )
        if current_row is None:
            if not allow_provider_calls:
                results.append(
                    {
                        "segment_id": segment["id"],
                        "seq": segment["seq"],
                        "fit_status": "failed",
                        "error_code": "CURRENT_TTS_ASSET_REQUIRED",
                        **_slot(segment),
                    }
                )
                continue
            voice_provider = voice_provider or provider_service.get_tts_provider()
            current_row, calls = _obtain_tts_output(
                episode,
                segment,
                current_text,
                binding,
                voice_provider,
            )
            tts_calls += calls

        fitted = _fit_output(current_row, segment, text_changed=False, retry_count=0)
        if fitted["fit"]:
            results.append(
                {
                    "segment_id": segment["id"],
                    "seq": segment["seq"],
                    "text": current_text,
                    **fitted,
                }
            )
            continue

        all_index = next(
            item_index
            for item_index, item in enumerate(all_segments)
            if item["id"] == segment["id"]
        )
        previous_text, next_text = _context(all_segments, all_index)
        attempted_text = current_text
        latest_row = current_row
        final_result: dict[str, Any] | None = None
        seen_texts = {current_text}
        compression_attempts: list[dict[str, Any]] = []
        last_measured_duration = float(fitted["decoded_duration"])
        can_shorten = (
            segment.get("localization_mode") != localization_text_authority.DUB_ONLY
            and not _is_minimal_spoken_line(current_text)
        )
        for retry in range(1, MAX_SHORTENING_RETRIES + 1) if can_shorten and allow_provider_calls else ():
            translator = translator or provider_service.get_translation_provider()
            translation_calls += 1
            try:
                candidate = translator.suggest_shorter_text(
                    segment["source_text"],
                    attempted_text,
                    segment["dubbing_speaker"],
                    _slot(segment)["usable_duration"],
                    previous_text,
                    next_text,
                    segment["target_language"],
                    actual_tts_duration=last_measured_duration,
                    retry_index=retry,
                    max_retries=MAX_SHORTENING_RETRIES,
                )
            except provider_service.ProviderError as error:
                compression_attempts.append(
                    {
                        "attempt": retry,
                        "strategy": "provider",
                        "valid": False,
                        "error_code": error.code,
                        "error_message": provider_service.redact_sensitive_text(str(error)),
                    }
                )
                if error.code != "TRANSLATION_INVALID_OUTPUT":
                    break
                continue
            candidate_text = str(candidate["shorter_text"]).strip()
            validation = _validate_compression_candidate(
                segment["source_text"],
                current_text,
                candidate_text,
                _slot(segment)["usable_duration"],
                provider_meaning_preserved=bool(candidate.get("meaning_preserved")),
            )
            compression_attempts.append(
                {
                    "attempt": retry,
                    "strategy": "provider",
                    **validation,
                }
            )
            if not validation["valid"]:
                if (
                    candidate_text
                    and candidate_text not in seen_texts
                    and len(candidate_text) < len(attempted_text)
                    and not validation["missing_facts"]
                ):
                    attempted_text = candidate_text
                    seen_texts.add(candidate_text)
                continue
            if candidate_text in seen_texts:
                continue
            seen_texts.add(candidate_text)
            segment["final_translation"] = candidate_text
            segment["localization_text"] = candidate_text
            voice_provider = voice_provider or provider_service.get_tts_provider()
            latest_row, calls = _obtain_tts_output(
                episode,
                segment,
                candidate_text,
                binding,
                voice_provider,
            )
            tts_calls += calls
            fitted = _fit_output(latest_row, segment, text_changed=True, retry_count=retry)
            last_measured_duration = float(fitted["decoded_duration"])
            attempted_text = candidate_text
            if fitted["fit"]:
                _commit_shortened_text(segment, candidate_text, str(latest_row["id"]))
                final_result = {
                    "segment_id": segment["id"],
                    "seq": segment["seq"],
                    "text": candidate_text,
                    "previous_text": current_text,
                    "compression_strategy": "provider",
                    "compression_attempts": compression_attempts,
                    **fitted,
                }
                break

        if final_result:
            results.append(final_result)
            continue

        fallback = {
            "text": current_text,
            "valid": False,
            "strategy": (
                "one_shortening_attempt_exhausted"
                if can_shorten
                else "minimal_text_no_further_compression"
            ),
        }
        compression_attempts.append(
            {
                "attempt": MAX_SHORTENING_RETRIES + 1,
                **fallback,
            }
        )

        if segment.get("localization_mode") != localization_text_authority.DUB_ONLY:
            segment["final_translation"] = current_text
        segment["localization_text"] = current_text
        original_segment = {**segment, "localization_text": current_text}
        _mark_manual_review(
            current_row,
            original_segment,
            actual_duration=last_measured_duration,
            retry_count=MAX_SHORTENING_RETRIES if can_shorten else 0,
            attempted_text=attempted_text,
        )
        results.append(
            {
                "segment_id": segment["id"],
                "seq": segment["seq"],
                "text": current_text,
                "attempted_text": attempted_text,
                "fit_status": "manual_review_required",
                "actual_tts_duration": last_measured_duration,
                "retry_count": MAX_SHORTENING_RETRIES if can_shorten else 0,
                "compression_strategy": str(fallback["strategy"]),
                "compression_attempts": compression_attempts,
                "error_code": (
                    "TTS_ORIGINAL_TEXT_UNFIT"
                    if segment.get("localization_mode") == localization_text_authority.DUB_ONLY
                    else "TTS_MINIMAL_TEXT_UNFIT" if not can_shorten
                    else "TRANSLATION_COMPRESSION_UNFIT"
                ),
                **_slot(segment),
            }
        )

    blocked = [item["segment_id"] for item in results if item["fit_status"] not in FIT_STATUSES]
    return {
        "episode_id": episode_id,
        "all_fit": not blocked and len(results) == len(segments),
        "segment_count": len(segments),
        "fit_count": len(results) - len(blocked),
        "blocked_segment_ids": blocked,
        "translation_provider_calls": translation_calls,
        "tts_provider_calls": tts_calls,
        "provider_calls": translation_calls + tts_calls,
        "segments": results,
    }


def provider_free_episode_readiness(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    segments = _segments(episode_id)
    _assign_contiguous_silence_windows(segments)
    bindings = _binding_map(episode)
    blockers: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    subtitle_manifest: list[dict[str, Any]] = []

    for segment in segments:
        binding = bindings.get(segment["dubbing_speaker"])
        if not binding:
            blockers.append(
                {
                    "asset": "voice_binding",
                    "segment_id": segment["id"],
                    "error_code": "VOICE_BINDING_REQUIRED",
                }
            )
            continue
        row = _current_revision_output(episode_id, segment, binding)
        if row is None:
            blockers.append(
                {
                    "asset": "tts",
                    "segment_id": segment["id"],
                    "error_code": "CURRENT_TTS_ASSET_REQUIRED",
                }
            )
            continue
        window = _slot(segment)
        output_usage = _output_usage(row)
        declared_fit_status = str(
            output_usage.get("duration_fit_status") or output_usage.get("status") or ""
        ).strip()
        aligned_path_value = str(_output_value(row, "aligned_audio_path") or "").strip()
        aligned_duration = float(_output_value(row, "aligned_duration") or 0.0)
        target_duration = float(_output_value(row, "target_duration") or 0.0)
        duration_warning = bool(_output_value(row, "duration_warning", 0))
        output_error_code = str(_output_value(row, "error_code") or "").strip()
        legacy_aligned_completion = (
            str(_output_value(row, "status") or "").strip().lower() == "completed"
            and bool(aligned_path_value)
            and aligned_duration > 0
            and target_duration > 0
            and aligned_duration <= target_duration + 0.005
            and not duration_warning
            and not output_error_code
        )
        fit_status = declared_fit_status
        if fit_status not in FIT_STATUSES and legacy_aligned_completion:
            fit_status = (
                "speed_adjusted_and_fit"
                if float(_output_value(row, "speed_adjustment") or 1.0) > 1.000001
                else "fit"
            )

        expects_aligned_output = bool(aligned_path_value) or declared_fit_status in FIT_STATUSES
        if expects_aligned_output:
            capped_speed_warning = fit_status == CAPPED_SPEED_WARNING_STATUS
            if (
                (duration_warning and not capped_speed_warning)
                or output_error_code
                or fit_status not in FIT_STATUSES
            ):
                blockers.append(
                    {
                        "asset": "tts_timing",
                        "segment_id": segment["id"],
                        "error_code": "TTS_DURATION_EXCEEDED",
                    }
                )
                continue
            audio_path = Path(aligned_path_value) if aligned_path_value else None
            try:
                aligned_audio_exists = bool(
                    audio_path and audio_path.is_file() and audio_path.stat().st_size > 0
                )
            except OSError:
                aligned_audio_exists = False
            if not aligned_audio_exists:
                blockers.append(
                    {
                        "asset": "tts",
                        "segment_id": segment["id"],
                        "error_code": "TTS_AUDIO_MISSING",
                    }
                )
                continue
            try:
                actual_duration = media_service.media_duration_seconds(audio_path)
            except (OSError, media_service.MediaToolError):
                blockers.append(
                    {
                        "asset": "tts",
                        "segment_id": segment["id"],
                        "error_code": "TTS_AUDIO_NOT_READABLE",
                    }
                )
                continue
            if (
                aligned_duration <= 0
                or (
                    not capped_speed_warning
                    and aligned_duration > float(window["slot_duration"]) + 0.005
                )
                or (
                    not capped_speed_warning
                    and actual_duration > float(window["slot_duration"]) + 0.005
                )
            ):
                blockers.append(
                    {
                        "asset": "tts_timing",
                        "segment_id": segment["id"],
                        "error_code": "TTS_DURATION_EXCEEDED",
                        "aligned_duration": aligned_duration,
                        "actual_duration": actual_duration,
                        "available_duration": float(window["slot_duration"]),
                    }
                )
                continue
        else:
            audio_path = _audio_path(row)
            if not audio_path:
                blockers.append(
                    {
                        "asset": "tts",
                        "segment_id": segment["id"],
                        "error_code": "TTS_AUDIO_MISSING",
                    }
                )
                continue
            try:
                actual_duration = media_service.media_duration_seconds(audio_path)
            except media_service.MediaToolError:
                blockers.append(
                    {
                        "asset": "tts",
                        "segment_id": segment["id"],
                        "error_code": "TTS_AUDIO_NOT_READABLE",
                    }
                )
                continue

        usable_duration = float(window["usable_duration"])
        required_speed = actual_duration / usable_duration if usable_duration > 0 else float("inf")
        if not expects_aligned_output and required_speed > MAX_TTS_SPEED + 1e-6:
            blockers.append(
                {
                    "asset": "tts_timing",
                    "segment_id": segment["id"],
                    "error_code": "TTS_DURATION_EXCEEDED",
                    "required_speed": required_speed,
                    "maximum_speed": MAX_TTS_SPEED,
                }
            )
            continue
        manifest.append(
            {
                "segment_id": segment["id"],
                "tts_input_hash": row["tts_input_hash"],
                "output_id": row["id"],
                "audio_path": str(audio_path),
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "required_speed": required_speed,
            }
        )
        subtitle_manifest.append(
            {
                "segment_id": segment["id"],
                "text": localization_text_authority.effective_text(segment),
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "enabled": bool(segment.get("subtitle_enabled", True)),
            }
        )

    audio_revision = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    subtitle_revision = hashlib.sha256(
        json.dumps(subtitle_manifest, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "ready": bool(segments) and not blockers and len(manifest) == len(segments),
        "segment_count": len(segments),
        "audio_revision": audio_revision,
        "subtitle_revision": subtitle_revision,
        "manifest": manifest,
        "subtitle_manifest": subtitle_manifest,
        "blockers": blockers,
    }


def episode_duration_fit_audit(episode_id: str) -> dict[str, Any]:
    episode = _episode(episode_id)
    segments = _segments(episode_id)
    _assign_contiguous_silence_windows(segments)
    bindings = _binding_map(episode)
    results: list[dict[str, Any]] = []
    for segment in segments:
        binding = bindings.get(segment["dubbing_speaker"])
        row = _current_revision_output(episode_id, segment, binding) if binding else None
        usage = _output_usage(row)
        fit_status = str(usage.get("duration_fit_status") or "")
        path = (
            _audio_path(row, prefer_fitted=True)
            if row and fit_status in FIT_STATUSES
            else _audio_path(row, prefer_fitted=False) if row else None
        )
        actual_duration = media_service.media_duration_seconds(path) if path else None
        window = _slot(segment)
        if fit_status not in FIT_STATUSES and fit_status != "manual_review_required":
            fit_status = "manual_review_required"
        results.append(
            {
                "segment_id": segment["id"],
                "seq": segment["seq"],
                "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"],
                "source_text": segment["source_text"],
                "final_text": localization_text_authority.effective_text(segment),
                "audio_path": str(path) if path else None,
                "original_tts_duration": usage.get("original_tts_duration") or (float(row["generated_duration"]) if row and row["generated_duration"] else None),
                "final_tts_duration": actual_duration,
                "speed_adjustment": usage.get("speed_adjustment") or (float(row["speed_adjustment"]) if row else 1.0),
                "retry_count": int(usage.get("shortening_retry_count") or 0),
                "fit_status": fit_status,
                "duration_warning": bool(_output_value(row, "duration_warning", 0)),
                "needs_attention": bool(usage.get("needs_attention")),
                "warning_code": usage.get("warning_code"),
                **window,
            }
        )
    blocked = [item["segment_id"] for item in results if item["fit_status"] not in FIT_STATUSES]
    return {
        "episode_id": episode_id,
        "all_fit": not blocked and bool(results),
        "segment_count": len(results),
        "fit_count": len(results) - len(blocked),
        "blocked_segment_ids": blocked,
        "segments": results,
    }


def render_episode_tts_timeline(
    episode_id: str,
    output_path: Path,
    duration: float,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    audit = episode_duration_fit_audit(episode_id)
    if not audit["all_fit"] and not allow_partial:
        raise DurationConstrainedTTSError(
            "TTS_DURATION_REVIEW_REQUIRED",
            "One or more English voice lines do not fit their original dialogue slots.",
            segment_ids=list(audit["blocked_segment_ids"]),
        )
    eligible = [item for item in audit["segments"] if item["fit_status"] in FIT_STATUSES]
    if not eligible:
        raise DurationConstrainedTTSError(
            "TTS_DURATION_REVIEW_REQUIRED",
            "No English voice lines fit their original dialogue slots.",
            segment_ids=list(audit["blocked_segment_ids"]),
        )
    segments = [
        {
            "seq": item["seq"],
            "segment_id": item["segment_id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "audio_path": item["audio_path"],
            "allow_slot_overrun": item["fit_status"] == CAPPED_SPEED_WARNING_STATUS,
        }
        for item in eligible
    ]
    rendered = media_service.render_tts_only_audio(segments, output_path, duration)
    return {
        **rendered,
        "audit": audit,
        "mixed_count": len(segments),
        "omitted_segment_ids": list(audit["blocked_segment_ids"]),
    }
