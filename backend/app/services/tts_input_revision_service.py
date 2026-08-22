"""Canonical TTS input revisions shared by generation, mixing, and finalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import localization_text_authority


@dataclass(frozen=True)
class TtsInputRevision:
    payload: dict[str, str | int]
    input_hash: str


def _canonical_text(value: Any) -> str:
    # Case remains significant; only null handling and surrounding whitespace are normalized.
    return str(value or "").strip()


def _canonical_milliseconds(value: Any) -> int:
    try:
        # Timeline fields are persisted in integer milliseconds. Fractional input is truncated
        # toward zero so every caller follows the existing production rule.
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid TTS timeline millisecond value: {value!r}") from exc


def build_tts_input_revision(
    segment: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> TtsInputRevision:
    """Return the canonical payload and SHA-256 revision for one TTS segment."""
    common_payload: dict[str, str | int] = {
        "transcript_segment_id": _canonical_text(segment.get("id")),
        "speaker_id": _canonical_text(segment.get("dubbing_speaker")),
        "provider": _canonical_text(binding.get("provider")),
        "model": _canonical_text(binding.get("model")),
        "resolved_voice_id": _canonical_text(binding.get("voice_id")),
        "start_time": _canonical_milliseconds(segment.get("start_ms")),
        "end_time": _canonical_milliseconds(segment.get("end_ms")),
    }
    if _canonical_text(segment.get("localization_mode")) == localization_text_authority.DUB_ONLY:
        payload: dict[str, str | int] = {
            "transcript_segment_id": common_payload["transcript_segment_id"],
            "source_text": _canonical_text(localization_text_authority.effective_text(segment)),
            "source_language": _canonical_text(segment.get("localization_language")),
            "language_mode": localization_text_authority.DUB_ONLY,
            "speaker_id": common_payload["speaker_id"],
            "provider": common_payload["provider"],
            "model": common_payload["model"],
            "resolved_voice_id": common_payload["resolved_voice_id"],
            "start_time": common_payload["start_time"],
            "end_time": common_payload["end_time"],
        }
    else:
        payload = {
            "transcript_segment_id": common_payload["transcript_segment_id"],
            "final_translation": _canonical_text(segment.get("final_translation")),
            "speaker_id": common_payload["speaker_id"],
            "provider": common_payload["provider"],
            "model": common_payload["model"],
            "resolved_voice_id": common_payload["resolved_voice_id"],
            "start_time": common_payload["start_time"],
            "end_time": common_payload["end_time"],
        }
    if _canonical_text(binding.get("provider")) == "edge":
        payload.update(
            {
                "rate": _canonical_text(binding.get("rate") or "+0%"),
                "volume": _canonical_text(binding.get("volume") or "+0%"),
                "pitch": _canonical_text(binding.get("pitch") or "+0Hz"),
            }
        )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return TtsInputRevision(
        payload=payload,
        input_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
