"""Subtitle parsing, validation and export helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class SubtitleValidationError(ValueError):
    pass


TIME_PATTERN = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})")


def _time_to_ms(value: str) -> int:
    match = TIME_PATTERN.fullmatch(value.strip())
    if not match:
        raise SubtitleValidationError(f"Invalid subtitle timestamp: {value}")
    parts = {key: int(raw) for key, raw in match.groupdict().items()}
    return (((parts["h"] * 60 + parts["m"]) * 60) + parts["s"]) * 1000 + parts["ms"]


def decode_subtitle(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise SubtitleValidationError("Subtitle encoding is not supported. Please use UTF-8.")


def parse_subtitle(content: bytes, suffix: str, duration_seconds: float | None = None) -> tuple[list[dict[str, Any]], str]:
    text, encoding = decode_subtitle(content)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if suffix.lower() == ".vtt":
        normalized = re.sub(r"^WEBVTT[^\n]*\n+", "", normalized)
    blocks = re.split(r"\n{2,}", normalized)
    segments: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.split("\n") if line.strip()]
        timeline_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timeline_index is None:
            continue
        timeline = lines[timeline_index].split("-->")
        if len(timeline) != 2:
            raise SubtitleValidationError("Invalid subtitle timeline.")
        start_ms = _time_to_ms(timeline[0].strip().split(" ")[0])
        end_ms = _time_to_ms(timeline[1].strip().split(" ")[0])
        source_text = "\n".join(lines[timeline_index + 1 :]).strip()
        if not source_text:
            raise SubtitleValidationError("A subtitle cue has no text.")
        if end_ms <= start_ms:
            raise SubtitleValidationError("Subtitle cue end time must be after start time.")
        if duration_seconds and end_ms > round(duration_seconds * 1000) + 1000:
            raise SubtitleValidationError("Subtitle timeline exceeds the video duration.")
        segments.append({"start_ms": start_ms, "end_ms": end_ms, "text": source_text})
    if not segments:
        raise SubtitleValidationError("No valid subtitle cues were found.")
    for previous, current in zip(segments, segments[1:]):
        if current["start_ms"] < previous["start_ms"]:
            raise SubtitleValidationError("Subtitle cues are not ordered by time.")
    return segments, encoding


def _format_time(milliseconds: int, separator: str) -> str:
    milliseconds = max(0, int(milliseconds))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def write_srt(segments: list[dict[str, Any]], path: Path, translated: bool = False, bilingual: bool = False) -> None:
    blocks = []
    for index, segment in enumerate(segments, 1):
        source = segment.get("source_text", "")
        target = segment.get("final_translation", "")
        text = f"{source}\n{target}" if bilingual and target else (target if translated and target else source)
        if not text or not segment.get("subtitle_enabled", True):
            continue
        blocks.append(
            f"{index}\n{_format_time(segment['start_ms'], ',')} --> {_format_time(segment['end_ms'], ',')}\n{text}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def write_vtt(segments: list[dict[str, Any]], path: Path, translated: bool = True) -> None:
    blocks = ["WEBVTT"]
    for segment in segments:
        text = segment.get("final_translation", "") if translated else segment.get("source_text", "")
        if not text or not segment.get("subtitle_enabled", True):
            continue
        blocks.append(
            f"{_format_time(segment['start_ms'], '.')} --> {_format_time(segment['end_ms'], '.')}\n{text}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

