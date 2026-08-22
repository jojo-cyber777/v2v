"""Local-only assembly of completed ASR, translation, and TTS assets."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import unicodedata
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..database import ROOT, get_connection, json_dump, json_load, now_iso
from . import (
    duration_constrained_tts_service,
    language_capabilities,
    localization_text_authority,
    media_service,
    provider_service,
)


OUTPUTS_DIR = ROOT / "outputs"
DEMUCS_PYTHON_EXECUTABLE_ENV = "DEMUCS_PYTHON_EXECUTABLE"


def default_demucs_python(root: Path = ROOT, platform_name: str = os.name) -> Path:
    if platform_name == "nt":
        return root / ".tools" / "demucs-py311" / "Scripts" / "python.exe"
    return root / ".demucs-venv" / "bin" / "python"


DEMUCS_PYTHON = default_demucs_python()
DEMUCS_WORKER = ROOT / "backend" / "workers" / "demucs_worker.py"
DEFAULT_AUDIO_MODE = "demucs_dialogue_replacement"
DUBBING_PREVIEW_DEMUCS_MODE = "dubbing_preview_demucs"
ACTIVE_TASK_STATUSES = {
    "pending",
    "preparing_assets",
    "rendering_subtitles",
    "separating_audio",
    "building_tts_timeline",
    "mixing_audio",
    "muxing_final",
}
DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT = 85.5
DEFAULT_BURNED_SUBTITLE_REGION_HEIGHT_PERCENT = 14.5
SUBTITLE_MAX_WIDTH_RATIO = 0.80
SUBTITLE_SAFE_MARGIN_RATIO = 0.10
SUBTITLE_POSITIONS = {"low": 0.08, "standard": 0.13, "high": 0.18}
SUBTITLE_CUE_LAYOUT_VERSION = "safe-cues-v3-font-matched"
SUBTITLE_FONT_FILES: dict[str, tuple[str, ...]] = {
    "Arial": (r"C:\Windows\Fonts\arial.ttf",),
    "DejaVu Sans": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",),
    "Liberation Sans": ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",),
    "Noto Sans": ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",),
    "Noto Sans CJK SC": (
        r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ),
    "Noto Sans SC": (r"C:\Windows\Fonts\NotoSansSC-VF.ttf",),
    "Noto Sans CJK TC": ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",),
    "Noto Sans TC": (),
    "Noto Sans CJK JP": ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",),
    "Noto Sans JP": (),
    "Noto Sans CJK KR": ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",),
    "Noto Sans KR": (),
    "Noto Sans Thai": ("/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",),
    "Noto Sans Arabic": ("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",),
    "Microsoft YaHei": (r"C:\Windows\Fonts\msyh.ttc",),
    "Microsoft JhengHei": (r"C:\Windows\Fonts\msjh.ttc",),
    "SimSun": (r"C:\Windows\Fonts\simsun.ttc",),
    "Yu Gothic": (r"C:\Windows\Fonts\YuGothM.ttc", r"C:\Windows\Fonts\YuGothR.ttc"),
    "Meiryo": (r"C:\Windows\Fonts\meiryo.ttc",),
    "MS Gothic": (r"C:\Windows\Fonts\msgothic.ttc",),
    "Malgun Gothic": (r"C:\Windows\Fonts\malgun.ttf",),
    "Leelawadee UI": (r"C:\Windows\Fonts\LeelawUI.ttf",),
    "Tahoma": (r"C:\Windows\Fonts\tahoma.ttf",),
}
SUBTITLE_GLYPH_FALLBACK_FAMILIES = (
    "Noto Sans",
    "Noto Sans CJK JP",
    "Noto Sans CJK SC",
    "Noto Sans CJK TC",
    "Noto Sans CJK KR",
    "Noto Sans Arabic",
    "Noto Sans Thai",
)
NATURAL_BREAK_WORDS = {
    "and", "but", "or", "so", "because", "if", "when", "while", "that", "which", "who", "then",
}
logger = logging.getLogger(__name__)
_active_task_ids: set[str] = set()
_active_task_lock = threading.Lock()


class AudioLocalizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _public_output_path(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        relative = path.resolve().relative_to(OUTPUTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return f"/outputs/{relative.as_posix()}"


def _episode(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT e.*, p.name AS project_name FROM episodes e JOIN projects p ON p.id=e.project_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
    if not row:
        raise AudioLocalizationError("EPISODE_NOT_FOUND", "Episode not found.")
    return dict(row)


def _visual_source(episode_id: str, source_task_id: str | None) -> dict[str, Any]:
    with get_connection() as db:
        if source_task_id:
            row = db.execute(
                "SELECT id, final_output_path, final_output_url, completed_at FROM xmax_thirty_second_tasks "
                "WHERE id=? AND episode_id=? AND status='completed'",
                (source_task_id, episode_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT id, final_output_path, final_output_url, completed_at FROM xmax_thirty_second_tasks "
                "WHERE episode_id=? AND status='completed' AND final_output_path IS NOT NULL "
                "ORDER BY completed_at DESC, updated_at DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
    if row:
        path = Path(str(row["final_output_path"] or ""))
        if path.is_file() and path.stat().st_size > 0 and not _is_localized_visual_path(path):
            return {
                "path": path,
                "url": row["final_output_url"] or _public_output_path(path),
                "kind": "xmax_final",
                "task_id": row["id"],
                "completed_at": row["completed_at"],
            }
    episode = _episode(episode_id)
    path = Path(str(episode.get("video_path") or ""))
    if not path.is_file() or path.stat().st_size <= 0:
        raise AudioLocalizationError("LOCALIZATION_VIDEO_MISSING", "No readable Episode video is available.")
    if _is_localized_visual_path(path):
        raise AudioLocalizationError(
            "LOCALIZATION_VISUAL_SOURCE_INVALID",
            "The Episode source points to an already localized export. Use the original Episode video instead.",
        )
    return {
        "path": path,
        "url": f"/uploads/{path.name}",
        "kind": "episode_source",
        "task_id": None,
        "completed_at": None,
    }


def _is_localized_visual_path(path: Path) -> bool:
    normalized_parts = {part.lower() for part in path.parts}
    return "audio-localization" in normalized_parts or path.name.lower().startswith("localized-")


def _transcript_assets(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        document = db.execute(
            "SELECT * FROM transcript_documents WHERE episode_id=? AND status='completed' ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if not document:
            return {"document": None, "segments": [], "translation": None}
        segments = db.execute(
            "SELECT * FROM transcript_segments WHERE document_id=? ORDER BY seq",
            (document["id"],),
        ).fetchall()
        translation = db.execute(
            "SELECT * FROM translation_documents WHERE transcript_document_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (document["id"],),
        ).fetchone()
    try:
        authority = localization_text_authority.resolve_episode_text_authority(episode_id)
    except localization_text_authority.LocalizationTextAuthorityError as error:
        if error.code == "EPISODE_NOT_FOUND":
            authority = localization_text_authority.LocalizationTextAuthority(
                language_mode=localization_text_authority.TRANSLATE_DUB,
                text_field="final_translation",
                language=language_capabilities.normalize_target_language(
                    str(translation["target_language"] if translation else "english")
                ).id,
                transcript_document_id=str(document["id"]),
                translation_document_id=str(translation["id"]) if translation else None,
            )
        else:
            raise AudioLocalizationError(error.code, str(error)) from error
    return {
        "document": dict(document),
        "segments": authority.apply_to_segments(dict(row) for row in segments),
        "translation": dict(translation) if translation else None,
        "text_authority": authority,
    }


def _tts_timeline(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT id, usage_json, completed_at FROM voice_generation_tasks "
            "WHERE episode_id=? AND task_type='dubbing_preview' AND status='completed' "
            "ORDER BY completed_at DESC, updated_at DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        usage = json_load(row["usage_json"], {})
        value = usage.get("tts_only_path") or usage.get("tts_only_url")
        if not value:
            continue
        if str(value).startswith("/outputs/"):
            path = OUTPUTS_DIR.joinpath(*str(value)[len("/outputs/"):].split("/"))
        else:
            path = Path(str(value))
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        try:
            duration = media_service.media_duration_seconds(path)
        except media_service.MediaToolError:
            continue
        return {
            "path": path,
            "url": _public_output_path(path),
            "duration": duration,
            "mixed_count": int(usage.get("mixed_count") or 0),
            "segment_ids": list(usage.get("mixed_segment_ids") or []),
            "task_id": row["id"],
            "completed_at": row["completed_at"],
        }
    return None


def _latest_localized_output(output_directory: Path) -> Path | None:
    candidates = sorted(
        (
            path for path in output_directory.glob("localized-*.mp4")
            if path.is_file() and path.stat().st_size > 0
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in candidates:
        try:
            metadata = media_service.probe_media_metadata(path)
        except media_service.MediaToolError:
            continue
        if (
            float(metadata.get("duration") or 0) > 0
            and int(metadata.get("width") or 0) > 0
            and int(metadata.get("height") or 0) > 0
        ):
            return path
    return None


def _format_ass_time(milliseconds: int) -> str:
    total = max(0, int(milliseconds))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def _fontconfig_match(font_family: str) -> tuple[Path, str] | None:
    executable = shutil.which("fc-match")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-f", "%{file}|%{family}\n", font_family],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path_value, _, family_value = result.stdout.splitlines()[0].partition("|")
    path = Path(path_value.strip())
    if not path.is_file():
        return None
    return path, (family_value.split(",", 1)[0].strip() or font_family)


def _font_supports_text(font_path: Path, text: str) -> bool:
    try:
        font = ImageFont.truetype(str(font_path), 48)
        missing = bytes(font.getmask(chr(0x10FFFF)))
        for character in text:
            category = unicodedata.category(character)
            if character.isspace() or category.startswith(("C", "M", "Z")):
                continue
            if bytes(font.getmask(character)) == missing:
                return False
    except (OSError, ValueError):
        return False
    return True


@lru_cache(maxsize=128)
def _resolved_subtitle_font(language_id: str, sample_text: str) -> tuple[Path, str]:
    capability = language_capabilities.normalize_target_language(language_id)
    checked: set[Path] = set()
    candidate_families = tuple(dict.fromkeys((*capability.font_families, *SUBTITLE_GLYPH_FALLBACK_FAMILIES)))
    for family in candidate_families:
        candidates = [
            (Path(value), family)
            for value in SUBTITLE_FONT_FILES.get(family, ())
        ]
        fontconfig = _fontconfig_match(family)
        if fontconfig:
            candidates.append(fontconfig)
        for font_path, resolved_family in candidates:
            normalized_path = font_path.resolve() if font_path.exists() else font_path
            if normalized_path in checked or not font_path.is_file():
                continue
            checked.add(normalized_path)
            if _font_supports_text(font_path, sample_text):
                return font_path, resolved_family
    raise AudioLocalizationError(
        "LOCALIZATION_SUBTITLE_FONT_UNAVAILABLE",
        f"No installed subtitle font covers the current {capability.id} text. "
        f"Install one of: {', '.join(candidate_families)}.",
    )


def _subtitle_font_spec(target_language: str = "english", text: str = "") -> tuple[Path, str]:
    capability = language_capabilities.normalize_target_language(target_language)
    sample = "".join(sorted(set(text)))
    return _resolved_subtitle_font(capability.id, sample)


@lru_cache(maxsize=512)
def _font_for_size(font_path: str | int, font_size: int | None = None) -> ImageFont.FreeTypeFont:
    if font_size is None:
        font_size = int(font_path)
        font_path = str(_subtitle_font_spec()[0])
    return ImageFont.truetype(str(font_path), font_size)


def _subtitle_font_range(height: int, preferred_font_size: int | None = None) -> tuple[int, int]:
    default_size = max(28, round(height * 0.064))
    if preferred_font_size is None:
        hinting_adjustment = 1 if height >= 720 else 0
        return default_size, max(12, round(height * 0.052) - hinting_adjustment)
    base_size = max(12, int(preferred_font_size))
    safe_minimum = max(12, min(30, round(height * 0.052)))
    return base_size, min(base_size, safe_minimum)


def _subtitle_stroke_width(height: int) -> int:
    return max(3, round(height * 3 / 544))


def _text_width(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, stroke_width: int = 3) -> int:
    box = font.getbbox(text, stroke_width=stroke_width)
    return max(0, box[2] - box[0])


@lru_cache(maxsize=32_768)
def _text_width_for_size(text: str, font_size: int, stroke_width: int, font_path: str) -> int:
    return _text_width(text, _font_for_size(font_path, font_size), stroke_width)


def _break_score(words: list[str], index: int, first_width: int, second_width: int, max_width: int) -> float:
    score = abs(first_width - second_width)
    previous = words[index - 1].rstrip("\"')]}").lower()
    following = words[index].lstrip("\"'([{").lower()
    if previous.endswith((".", ",", "?", "!", ";", ":")):
        score -= max_width * 0.40
    if following.strip(".,?!;:") in NATURAL_BREAK_WORDS:
        score -= max_width * 0.24
    return score


def _subtitle_units(text: str, break_mode: str) -> tuple[list[str], str]:
    if break_mode != "grapheme":
        return text.split(" "), " "
    units: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("M") or character in {"\u200d", "\ufe0f"}:
            if units:
                units[-1] += character
            else:
                units.append(character)
            continue
        if character.isspace():
            if units:
                units[-1] += " "
            continue
        units.append(character)
    return units, ""


def _fit_subtitle_lines(
    text: str,
    width: int,
    height: int,
    preferred_font_size: int | None = None,
    *,
    font_path: Path,
    break_mode: str = "word",
) -> dict[str, Any] | None:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return None
    max_width = max(1, int(width * SUBTITLE_MAX_WIDTH_RATIO))
    base_size, minimum_size = _subtitle_font_range(height, preferred_font_size)
    stroke_width = _subtitle_stroke_width(height)
    font_key = str(font_path)
    full_width = _text_width_for_size(clean, base_size, stroke_width, font_key)
    if full_width <= max_width:
        return {
            "lines": [clean],
            "font_size": base_size,
            "max_line_width": full_width,
            "max_width": max_width,
        }
    words, separator = _subtitle_units(clean, break_mode)
    if len(words) < 2:
        low, high = minimum_size, base_size - 1
        best_size = 0
        best_width = 0
        while low <= high:
            font_size = (low + high) // 2
            measured_width = _text_width_for_size(clean, font_size, stroke_width, font_key)
            if measured_width <= max_width:
                best_size, best_width = font_size, measured_width
                low = font_size + 1
            else:
                high = font_size - 1
        if best_size:
            return {
                "lines": [clean],
                "font_size": best_size,
                "max_line_width": best_width,
                "max_width": max_width,
            }
        return None

    best_font_size = 0
    for index in range(1, len(words)):
        first = separator.join(words[:index]).strip()
        second = separator.join(words[index:]).strip()
        low, high = minimum_size, base_size
        candidate_size = 0
        while low <= high:
            font_size = (low + high) // 2
            widest = max(
                _text_width_for_size(first, font_size, stroke_width, font_key),
                _text_width_for_size(second, font_size, stroke_width, font_key),
            )
            if widest <= max_width:
                candidate_size = font_size
                low = font_size + 1
            else:
                high = font_size - 1
        best_font_size = max(best_font_size, candidate_size)
    if best_font_size:
        candidates: list[tuple[float, list[str], int]] = []
        for index in range(1, len(words)):
            first = separator.join(words[:index]).strip()
            second = separator.join(words[index:]).strip()
            first_width = _text_width_for_size(first, best_font_size, stroke_width, font_key)
            second_width = _text_width_for_size(second, best_font_size, stroke_width, font_key)
            if first_width > max_width or second_width > max_width:
                continue
            candidates.append((
                _break_score(words, index, first_width, second_width, max_width),
                [first, second],
                max(first_width, second_width),
            ))
        if candidates:
            _, lines, widest = min(candidates, key=lambda item: item[0])
            return {
                "lines": lines,
                "font_size": best_font_size,
                "max_line_width": widest,
                "max_width": max_width,
            }
    return None


def _subtitle_boundary_penalty(words: list[str], index: int) -> int:
    if index >= len(words):
        return 0
    previous = words[index - 1].rstrip("\"')]}")
    following = words[index].lstrip("\"'([{").strip(".,?!;:").lower()
    if previous.endswith((".", "?", "!")):
        return 0
    if previous.endswith((";", ":")):
        return 1
    if previous.endswith(","):
        return 2
    if following in NATURAL_BREAK_WORDS:
        return 3
    return 8


def _subtitle_word_timings(segment: dict[str, Any], words: list[str]) -> list[dict[str, Any]] | None:
    raw = (
        segment.get("word_level_timings")
        or segment.get("word_timestamps")
        or segment.get("words")
    )
    if not isinstance(raw, list) or len(raw) != len(words):
        return None
    parent_start = int(segment["start_ms"])
    parent_end = int(segment["end_ms"])
    normalized: list[dict[str, Any]] = []
    for expected, item in zip(words, raw):
        if not isinstance(item, dict):
            return None
        actual = str(item.get("text") or item.get("word") or "").strip()
        if actual and actual.casefold() != expected.casefold():
            return None
        try:
            start_ms = int(item.get("start_ms"))
            end_ms = int(item.get("end_ms"))
        except (TypeError, ValueError):
            return None
        if start_ms < parent_start or end_ms > parent_end or end_ms <= start_ms:
            return None
        if normalized and start_ms < int(normalized[-1]["end_ms"]):
            return None
        normalized.append({"start_ms": start_ms, "end_ms": end_ms})
    return normalized


def _allocate_subtitle_times(
    segment: dict[str, Any],
    words: list[str],
    spans: list[tuple[int, int]],
    separator: str = " ",
) -> tuple[list[tuple[int, int]], str]:
    start_ms = int(segment["start_ms"])
    end_ms = int(segment["end_ms"])
    if end_ms - start_ms < len(spans):
        raise AudioLocalizationError(
            "LOCALIZATION_SUBTITLE_TIMING_INVALID",
            f"Subtitle parent_segment_id={segment.get('id') or segment.get('seq')} has insufficient time "
            f"for {len(spans)} safe cues ({start_ms}-{end_ms} ms).",
        )
    word_timings = _subtitle_word_timings(segment, words)
    if word_timings:
        times = [
            (int(word_timings[first]["start_ms"]), int(word_timings[last - 1]["end_ms"]))
            for first, last in spans
        ]
        return times, "word_timestamps"

    weights = [max(1, len(separator.join(words[first:last]).strip())) for first, last in spans]
    total_weight = sum(weights)
    duration = end_ms - start_ms
    boundaries = [start_ms]
    cumulative = 0
    for index, weight in enumerate(weights[:-1], start=1):
        cumulative += weight
        proposed = start_ms + round(duration * cumulative / total_weight)
        minimum = boundaries[-1] + 1
        maximum = end_ms - (len(weights) - index)
        boundaries.append(max(minimum, min(maximum, proposed)))
    boundaries.append(end_ms)
    return list(zip(boundaries, boundaries[1:])), "proportional_visible_text"


def _subtitle_layout_error(
    segment: dict[str, Any],
    segment_index: int,
    text: str,
    width: int,
    height: int,
    preferred_font_size: int | None = None,
    *,
    font_path: Path,
    break_mode: str,
) -> AudioLocalizationError:
    _base_size, minimum_size = _subtitle_font_range(height, preferred_font_size)
    stroke_width = _subtitle_stroke_width(height)
    units, _separator = _subtitle_units(text, break_mode)
    measured_width = max(
        (_text_width_for_size(word, minimum_size, stroke_width, str(font_path)) for word in units),
        default=0,
    )
    max_width = max(1, int(width * SUBTITLE_MAX_WIDTH_RATIO))
    parent_id = str(segment.get("id") or segment.get("seq") or segment_index)
    return AudioLocalizationError(
        "LOCALIZATION_SUBTITLE_TOO_LONG",
        f"Subtitle cue {segment_index} parent_segment_id={parent_id} "
        f"start_ms={int(segment['start_ms'])} end_ms={int(segment['end_ms'])} "
        f"text_length={len(text)} measured_word_width={measured_width}px "
        f"font={font_path.name} minimum_font_size={minimum_size}px safe_width={max_width}px "
        f"safe_margin={SUBTITLE_SAFE_MARGIN_RATIO:.2f} max_lines=2 cannot be rendered safely.",
    )


def build_renderable_subtitle_cues(
    segments: list[dict[str, Any]],
    width: int,
    height: int,
    preferred_font_size: int | None = None,
    target_language: str = "english",
) -> list[dict[str, Any]]:
    """Derive display-only cues without changing transcript or TTS inputs."""
    capability = language_capabilities.normalize_target_language(target_language)
    sample_text = " ".join(
        localization_text_authority.effective_text(segment)
        for segment in segments
        if segment.get("subtitle_enabled", True)
    )
    font_path, font_family = _subtitle_font_spec(capability.id, sample_text)
    events: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(segments, start=1):
        if not segment.get("subtitle_enabled", True):
            continue
        clean = re.sub(r"\s+", " ", localization_text_authority.effective_text(segment)).strip()
        if not clean:
            continue
        words, separator = _subtitle_units(clean, capability.break_mode)
        if not words:
            continue
        layouts: dict[tuple[int, int], dict[str, Any]] = {}
        for first in range(len(words)):
            for last in range(first + 1, len(words) + 1):
                layout = _fit_subtitle_lines(
                    separator.join(words[first:last]).strip(),
                    width,
                    height,
                    preferred_font_size,
                    font_path=font_path,
                    break_mode=capability.break_mode,
                )
                if not layout:
                    break
                layouts[(first, last)] = layout

        best: dict[int, tuple[int, int, list[tuple[int, int]]]] = {
            len(words): (0, 0, [])
        }
        for first in range(len(words) - 1, -1, -1):
            candidates: list[tuple[int, int, list[tuple[int, int]]]] = []
            for last in range(first + 1, len(words) + 1):
                if (first, last) not in layouts or last not in best:
                    continue
                next_count, next_penalty, next_spans = best[last]
                candidates.append((
                    next_count + 1,
                    next_penalty + _subtitle_boundary_penalty(words, last),
                    [(first, last), *next_spans],
                ))
            if candidates:
                best[first] = min(candidates, key=lambda item: (item[0], item[1]))
        if 0 not in best:
            raise _subtitle_layout_error(
                segment,
                segment_index,
                clean,
                width,
                height,
                preferred_font_size,
                font_path=font_path,
                break_mode=capability.break_mode,
            )

        spans = best[0][2]
        times, timing_source = _allocate_subtitle_times(segment, words, spans, separator)
        parent_id = str(segment.get("id") or segment.get("seq") or segment_index)
        source_seq = int(segment.get("seq") or segment_index)
        for sequence, ((first, last), (cue_start, cue_end)) in enumerate(zip(spans, times), start=1):
            layout = layouts[(first, last)]
            cue_text = separator.join(words[first:last]).strip()
            events.append({
                **layout,
                "font_path": str(font_path),
                "font_family": font_family,
                "target_language": capability.id,
                "cue_id": f"{parent_id}:subtitle:{sequence:03d}",
                "parent_segment_id": parent_id,
                "sequence": sequence,
                "text": cue_text,
                "start_ms": cue_start,
                "end_ms": cue_end,
                "source_seq": source_seq,
                "line_count": len(layout["lines"]),
                "timing_source": timing_source,
                "layout_version": SUBTITLE_CUE_LAYOUT_VERSION,
            })
    for cue_index, event in enumerate(events, start=1):
        event["cue_index"] = cue_index
    return events


def subtitle_cue_manifest(cues: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [
        {
            "cue_id": cue["cue_id"],
            "parent_segment_id": cue["parent_segment_id"],
            "sequence": int(cue["sequence"]),
            "start_ms": int(cue["start_ms"]),
            "end_ms": int(cue["end_ms"]),
            "duration_ms": int(cue["end_ms"]) - int(cue["start_ms"]),
            "text_length": len(str(cue["text"])),
            "word_count": len(str(cue["text"]).split()),
            "text_sha256": hashlib.sha256(str(cue["text"]).encode("utf-8")).hexdigest(),
            "lines": list(cue["lines"]),
            "line_count": int(cue["line_count"]),
            "font_size": int(cue["font_size"]),
            "font_family": str(cue["font_family"]),
            "font_path": str(cue["font_path"]),
            "target_language": str(cue["target_language"]),
            "max_line_width": int(cue["max_line_width"]),
            "max_width": int(cue["max_width"]),
            "timing_source": cue["timing_source"],
        }
        for cue in cues
    ]
    cue_identity = [
        {
            key: entry[key]
            for key in (
                "cue_id",
                "parent_segment_id",
                "sequence",
                "start_ms",
                "end_ms",
                "text_sha256",
            )
        }
        for entry in entries
    ]
    return {
        "layout_version": SUBTITLE_CUE_LAYOUT_VERSION,
        "cue_count": len(entries),
        "manifest_sha256": hashlib.sha256(
            json.dumps(cue_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "layout_audit_sha256": hashlib.sha256(
            json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "cues": entries,
    }


def _layout_subtitle_events(
    segments: list[dict[str, Any]],
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    return build_renderable_subtitle_cues(segments, width, height)


def _subtitle_bottom_y(height: int, subtitle_position: str) -> int:
    margin_ratio = SUBTITLE_POSITIONS.get(subtitle_position, SUBTITLE_POSITIONS["low"])
    return height - max(32, round(height * margin_ratio))


def _write_overseas_ass(
    segments: list[dict[str, Any]],
    path: Path,
    width: int,
    height: int,
    *,
    hide_original_subtitles: bool = True,
    original_subtitle_region_start_percent: float = DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
    subtitle_position: str = "low",
    cues: list[dict[str, Any]] | None = None,
) -> int:
    events_layout = cues if cues is not None else build_renderable_subtitle_cues(segments, width, height)
    margin_v = max(0, height - _subtitle_bottom_y(height, subtitle_position))
    base_size, _minimum_size = _subtitle_font_range(height)
    outline_width = _subtitle_stroke_width(height)
    horizontal_margin = max(16, round(width * 0.05))
    font_family = str(events_layout[0]["font_family"]) if events_layout else ""
    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Overseas,{font_family},{base_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{outline_width},0,2,"
        f"{horizontal_margin},{horizontal_margin},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ])
    ass_events = []
    for event in events_layout:
        escaped = [
            line.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
            for line in event["lines"]
        ]
        text = rf"{{\fs{int(event['font_size'])}}}" + r"\N".join(escaped)
        ass_events.append(
            f"Dialogue: 0,{_format_ass_time(int(event['start_ms']))},{_format_ass_time(int(event['end_ms']))},"
            f"Overseas,,0,0,0,,{text}"
        )
    if not ass_events:
        raise AudioLocalizationError("LOCALIZATION_SUBTITLES_MISSING", "No completed target-language subtitles are available.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(ass_events) + "\n", encoding="utf-8")
    return len(ass_events)


def _write_overseas_overlays(
    segments: list[dict[str, Any]],
    directory: Path,
    width: int,
    height: int,
    *,
    hide_original_subtitles: bool = True,
    original_subtitle_region_start_percent: float = DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
    subtitle_position: str = "low",
    cues: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    events = cues if cues is not None else build_renderable_subtitle_cues(segments, width, height)
    bottom_y = _subtitle_bottom_y(height, subtitle_position)
    overlays = []
    for event in events:
        lines = event["lines"]
        font_size = int(event["font_size"])
        stroke_width = _subtitle_stroke_width(height)
        font = _font_for_size(str(event["font_path"]), font_size)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        measurements = [
            draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            for line in lines
        ]
        heights = [box[3] - box[1] for box in measurements]
        line_gap = max(4, round(font_size * 0.16))
        total_height = sum(heights) + line_gap * (len(heights) - 1)
        y = max(round(height * 0.08), bottom_y - total_height)
        for line, box, line_height in zip(lines, measurements, heights):
            line_width_pixels = box[2] - box[0]
            visible_left = (width - line_width_pixels) / 2
            visible_right = visible_left + line_width_pixels
            x = visible_left - box[0]
            safe_margin = width * SUBTITLE_SAFE_MARGIN_RATIO
            if visible_left < safe_margin or visible_right > width - safe_margin:
                raise AudioLocalizationError(
                    "LOCALIZATION_SUBTITLE_OUTSIDE_SAFE_AREA",
                    f"Subtitle cue {event['cue_index']} parent_segment_id={event['parent_segment_id']} "
                    f"start_ms={event['start_ms']} end_ms={event['end_ms']} "
                    f"text_length={len(event['text'])} measured_width={line_width_pixels}px "
                    f"font={event['font_family']} font_size={font_size}px safe_width={int(width * SUBTITLE_MAX_WIDTH_RATIO)}px "
                    f"safe_margin={SUBTITLE_SAFE_MARGIN_RATIO:.2f} max_lines=2 exceeds the horizontal safe area.",
                )
            draw.text(
                (x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 255),
            )
            y += line_height + line_gap
        overlay_path = directory / f"subtitle-{int(event['cue_index']):03d}.png"
        image.save(overlay_path, "PNG")
        overlays.append({
            "path": overlay_path,
            "start_ms": int(event["start_ms"]),
            "end_ms": int(event["end_ms"]),
            "line_count": len(lines),
            "max_line_width": int(event["max_line_width"]),
            "max_width": int(event["max_width"]),
            "font_size": font_size,
            "source_seq": int(event["source_seq"]),
            "cue_id": str(event["cue_id"]),
            "parent_segment_id": str(event["parent_segment_id"]),
            "sequence": int(event["sequence"]),
            "timing_source": str(event["timing_source"]),
            "layout_version": str(event["layout_version"]),
        })
    if not overlays:
        raise AudioLocalizationError("LOCALIZATION_SUBTITLES_MISSING", "No completed target-language subtitles are available.")
    return overlays


def _approved_xmax_timeline(visual: dict[str, Any]) -> dict[str, Any] | None:
    task_id = str(visual.get("task_id") or "")
    if visual.get("kind") != "xmax_final" or not task_id:
        return None
    with get_connection() as db:
        rows = db.execute(
            "SELECT sequence, duration, output_duration, output_path, execution_status, "
            "quality_status, preserve_original, use_source_for_merge "
            "FROM xmax_thirty_second_segments WHERE task_id=? ORDER BY sequence",
            (task_id,),
        ).fetchall()
    segments = [dict(row) for row in rows]
    if not segments or any(
        item.get("execution_status") != "completed"
        or item.get("quality_status") != "approved"
        or bool(item.get("preserve_original"))
        or bool(item.get("use_source_for_merge"))
        for item in segments
    ):
        return None
    paths = [Path(str(item.get("output_path") or "")) for item in segments]
    if any(not path.is_file() or path.stat().st_size <= 0 for path in paths):
        return None
    return {
        "task_id": task_id,
        "paths": paths,
        "target_durations": [float(item["duration"]) for item in segments],
        "actual_durations": [float(item["output_duration"]) for item in segments],
        "sequences": [int(item["sequence"]) for item in segments],
    }


def _task_payload(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    settings = json_load(item.pop("settings_json", None), {})
    item["settings"] = settings
    duration_fit = settings.get("duration_fit") if isinstance(settings.get("duration_fit"), dict) else {}
    item["duration_fit"] = duration_fit
    item["provider_calls"] = int(duration_fit.get("provider_calls") or 0)
    item["xmax_sessions"] = 0
    item["translation_provider_calls"] = int(duration_fit.get("translation_provider_calls") or 0)
    item["tts_provider_calls"] = int(duration_fit.get("tts_provider_calls") or 0)
    item["credits"] = 0
    item["output_version"] = Path(str(item["output_path"])).name if item.get("output_path") else None
    return item


def get_audio_localization_task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM audio_localization_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise AudioLocalizationError("LOCALIZATION_TASK_NOT_FOUND", "Audio Localization task not found.")
    payload = _task_payload(row)
    assert payload is not None
    return payload


def _latest_audio_localization_task(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        row = db.execute(
            "SELECT * FROM audio_localization_tasks WHERE episode_id=? ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    return _task_payload(row)


def _update_task(task_id: str, **values: Any) -> None:
    allowed = {
        "status",
        "progress",
        "progress_message",
        "source_visual_path",
        "source_audio_path",
        "tts_timeline_path",
        "separation_output_dir",
        "vocals_path",
        "no_vocals_path",
        "mixed_audio_path",
        "output_path",
        "output_url",
        "output_duration",
        "output_width",
        "output_height",
        "output_size",
        "error_code",
        "error_message",
        "worker_return_code",
        "worker_stdout",
        "worker_stderr",
        "settings_json",
        "started_at",
        "completed_at",
        "finished_at",
    }
    invalid = set(values) - allowed
    if invalid:
        raise ValueError(f"Unsupported task fields: {sorted(invalid)}")
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in values)
    with get_connection() as db:
        db.execute(
            f"UPDATE audio_localization_tasks SET {assignments} WHERE id=?",
            (*values.values(), task_id),
        )


def _renderable_subtitle_segments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in transcript["segments"]
        if item.get("subtitle_enabled") and localization_text_authority.effective_text(item)
    ]


def _current_translated_segments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    required_segments = [item for item in transcript["segments"] if item.get("subtitle_enabled")]
    translated_segments = _renderable_subtitle_segments(transcript)
    if not required_segments or len(translated_segments) != len(required_segments):
        return []
    return translated_segments


def current_subtitle_render_context(episode_id: str) -> dict[str, Any]:
    """Return current subtitle text and language independently from TTS manifests."""
    transcript = _transcript_assets(episode_id)
    if not transcript["document"]:
        raise AudioLocalizationError("LOCALIZATION_ASR_REQUIRED", "Run the existing ASR step before localizing this Episode.")
    renderable_segments = _current_translated_segments(transcript)
    if not renderable_segments:
        raise AudioLocalizationError(
            "LOCALIZATION_TRANSLATION_REQUIRED",
            "No current subtitle text is available for this Episode.",
        )
    authority = transcript["text_authority"]
    return {
        "segments": renderable_segments,
        "target_language": authority.language,
        "language_mode": authority.language_mode,
        "text_authority": authority.text_field,
        "transcript_document_id": str(transcript["document"]["id"]),
    }


def current_renderable_subtitle_segments(episode_id: str) -> list[dict[str, Any]]:
    """Return current subtitle cues independently from TTS and mix manifests."""
    return list(current_subtitle_render_context(episode_id)["segments"])


def _completed_assets(episode_id: str) -> dict[str, Any]:
    transcript = _transcript_assets(episode_id)
    tts = _tts_timeline(episode_id)
    if not transcript["document"]:
        raise AudioLocalizationError("LOCALIZATION_ASR_REQUIRED", "Run the existing ASR step before localizing this Episode.")
    renderable_segments = _current_translated_segments(transcript)
    if not renderable_segments:
        raise AudioLocalizationError(
            "LOCALIZATION_TRANSLATION_REQUIRED",
            "No current dubbing text is available for this Episode.",
        )
    return {
        "transcript": transcript,
        "translated_segments": renderable_segments,
        "tts": tts,
    }


def _validate_task_request(
    episode_id: str,
    *,
    source_task_id: str | None,
    target_language: str,
    voice: str,
    hide_original_subtitles: bool,
    original_subtitle_region_start_percent: float,
    original_subtitle_region_height_percent: float,
    subtitle_position: str,
    allow_provider_calls: bool = True,
) -> dict[str, Any]:
    try:
        language_capabilities.normalize_target_language(target_language)
    except ValueError as error:
        raise AudioLocalizationError("LOCALIZATION_LANGUAGE_UNSUPPORTED", str(error)) from error
    if voice.strip().lower() != "default":
        raise AudioLocalizationError("LOCALIZATION_VOICE_UNSUPPORTED", "This MVP uses the existing completed voice assets.")
    normalized_position = subtitle_position.strip().lower()
    if normalized_position not in SUBTITLE_POSITIONS:
        raise AudioLocalizationError(
            "LOCALIZATION_SUBTITLE_POSITION_INVALID",
            "Subtitle position must be low, standard, or high.",
        )
    if (
        original_subtitle_region_start_percent < 0
        or original_subtitle_region_height_percent <= 0
        or original_subtitle_region_start_percent + original_subtitle_region_height_percent > 100
    ):
        raise AudioLocalizationError(
            "LOCALIZATION_SUBTITLE_REGION_INVALID",
            "The burned-in subtitle region must stay within the video frame.",
        )
    episode = _episode(episode_id)
    visual = _visual_source(episode_id, source_task_id)
    visual_metadata = media_service.probe_media_metadata(visual["path"])
    duration = float(visual_metadata.get("duration") or 0)
    if not 0 < duration <= 180:
        raise AudioLocalizationError(
            "LOCALIZATION_DURATION_OUT_OF_RANGE",
            "Audio Localization supports a single Episode up to 180 seconds.",
        )
    assets = _completed_assets(episode_id)
    translation_target = str(assets["transcript"]["text_authority"].language)
    try:
        target_capability = language_capabilities.normalize_target_language(translation_target)
    except ValueError as error:
        raise AudioLocalizationError("LOCALIZATION_LANGUAGE_UNSUPPORTED", str(error)) from error
    return {
        "episode": episode,
        "visual": visual,
        "visual_metadata": visual_metadata,
        "assets": assets,
        "settings": {
            "audio_mode": DEFAULT_AUDIO_MODE,
            "target_language": target_capability.id,
            "voice": "default",
            "subtitles_enabled": True,
            "hide_original_subtitles": bool(hide_original_subtitles),
            "original_subtitle_region_start_percent": float(original_subtitle_region_start_percent),
            "original_subtitle_region_height_percent": float(original_subtitle_region_height_percent),
            "subtitle_position": normalized_position,
            "allow_provider_calls": bool(allow_provider_calls),
        },
    }


def create_audio_localization_task(
    episode_id: str,
    *,
    source_task_id: str | None,
    target_language: str,
    voice: str,
    hide_original_subtitles: bool,
    original_subtitle_region_start_percent: float,
    original_subtitle_region_height_percent: float,
    subtitle_position: str,
    allow_provider_calls: bool = True,
) -> tuple[dict[str, Any], bool]:
    prepared = _validate_task_request(
        episode_id,
        source_task_id=source_task_id,
        target_language=target_language,
        voice=voice,
        hide_original_subtitles=hide_original_subtitles,
        original_subtitle_region_start_percent=original_subtitle_region_start_percent,
        original_subtitle_region_height_percent=original_subtitle_region_height_percent,
        subtitle_position=subtitle_position,
        allow_provider_calls=allow_provider_calls,
    )
    with get_connection() as db:
        existing = db.execute(
            "SELECT * FROM audio_localization_tasks WHERE episode_id=? "
            "AND status IN ('pending','preparing_assets','rendering_subtitles','separating_audio',"
            "'building_tts_timeline','mixing_audio','muxing_final') ORDER BY created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if existing:
            payload = _task_payload(existing)
            assert payload is not None
            return payload, False
        task_id = f"audio-localization-{uuid.uuid4().hex[:16]}"
        timestamp = now_iso()
        db.execute(
            """
            INSERT INTO audio_localization_tasks (
                id, project_id, episode_id, source_task_id, mode, status, progress,
                progress_message, settings_json, source_visual_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                prepared["episode"]["project_id"],
                episode_id,
                prepared["visual"]["task_id"],
                DEFAULT_AUDIO_MODE,
                "Waiting for local processing.",
                json_dump(prepared["settings"]),
                str(prepared["visual"]["path"]),
                timestamp,
                timestamp,
            ),
        )
    with _active_task_lock:
        _active_task_ids.add(task_id)
    return get_audio_localization_task(task_id), True


def resolve_demucs_python_executable() -> Path:
    configured = os.getenv(DEMUCS_PYTHON_EXECUTABLE_ENV, "").strip()
    if not configured:
        return DEMUCS_PYTHON
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _run_demucs_worker(task_id: str, source_audio: Path, separation_dir: Path) -> dict[str, Any]:
    demucs_python = resolve_demucs_python_executable()
    if not demucs_python.is_file():
        raise AudioLocalizationError(
            "DEMUCS_PYTHON_UNAVAILABLE",
            f"The isolated Demucs Python environment was not found at {demucs_python}.",
        )
    if not DEMUCS_WORKER.is_file():
        raise AudioLocalizationError("DEMUCS_WORKER_UNAVAILABLE", "The isolated Demucs worker entry point is missing.")
    command = [
        str(demucs_python),
        str(DEMUCS_WORKER),
        "--source-audio",
        str(source_audio),
        "--work-dir",
        str(separation_dir),
        "--task-id",
        task_id,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AudioLocalizationError(
            "DEMUCS_TIMEOUT",
            "Dialogue separation did not finish within 30 minutes.",
        ) from error
    manifest_path = separation_dir / "separation_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    _update_task(
        task_id,
        worker_return_code=result.returncode,
        worker_stdout=str(manifest.get("stdout") or result.stdout)[-12000:],
        worker_stderr=str(manifest.get("stderr") or result.stderr)[-12000:],
    )
    if result.returncode != 0:
        message = str(manifest.get("error") or result.stderr.strip() or "Demucs dialogue separation failed.")
        raise AudioLocalizationError("DEMUCS_SEPARATION_FAILED", message)
    vocals_path = separation_dir / "vocals.wav"
    no_vocals_path = separation_dir / "no_vocals.wav"
    if not vocals_path.is_file() or not no_vocals_path.is_file():
        raise AudioLocalizationError("DEMUCS_STEMS_MISSING", "Demucs did not produce both required audio Stems.")
    return {
        "command": command,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "vocals_path": vocals_path,
        "no_vocals_path": no_vocals_path,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_stem_entry(path: Path, duration: float) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise AudioLocalizationError("DEMUCS_STEMS_MISSING", "A required Demucs Stem is missing.")
    try:
        metadata = media_service.probe_audio_metadata(path)
    except media_service.MediaToolError as error:
        raise AudioLocalizationError("DEMUCS_STEM_INVALID", str(error)) from error
    if abs(float(metadata["duration"]) - duration) > 0.1:
        raise AudioLocalizationError(
            "DEMUCS_STEM_DURATION_MISMATCH",
            "A Demucs Stem differs from the Episode duration by more than 100 ms.",
        )
    return {
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "sha256": _file_sha256(path),
        "duration": float(metadata["duration"]),
        "sample_rate": int(metadata["sample_rate"]),
        "channels": int(metadata["channels"]),
        "codec": metadata.get("codec"),
    }


def _matching_dubbing_preview_stems(
    episode_id: str,
    *,
    source_media_sha256: str,
    duration: float,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT id, status, usage_json, completed_at, updated_at FROM voice_generation_tasks "
            "WHERE episode_id=? AND task_type='dubbing_preview' "
            "ORDER BY COALESCE(completed_at, updated_at) DESC, updated_at DESC, id DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        usage = json_load(row["usage_json"], {})
        demucs = usage.get("demucs_stems") if isinstance(usage.get("demucs_stems"), dict) else None
        if not demucs or demucs.get("status") != "completed":
            continue
        if (
            demucs.get("episode_id") != episode_id
            or demucs.get("source_media_sha256") != source_media_sha256
        ):
            continue
        try:
            vocals = _validated_stem_entry(Path(str(demucs["vocals"]["path"])), duration)
            no_vocals = _validated_stem_entry(Path(str(demucs["no_vocals"]["path"])), duration)
        except (AudioLocalizationError, KeyError, TypeError):
            continue
        return {
            **demucs,
            "task_id": row["id"],
            "task_status": row["status"],
            "vocals": vocals,
            "no_vocals": no_vocals,
            "cache_hit": True,
        }
    return None


def prepare_dubbing_preview_stems(
    episode_id: str,
    *,
    task_id: str,
    source_video: Path,
    duration: float,
    audio_revision: str,
    mix_revision: str,
) -> dict[str, Any]:
    """Create or reuse source-bound Demucs stems for a dubbing preview."""
    if not source_video.is_file() or source_video.stat().st_size <= 0:
        raise AudioLocalizationError(
            "LOCALIZATION_ORIGINAL_VIDEO_MISSING",
            "The original Episode video is not available for dialogue separation.",
        )
    source_media_sha256 = _file_sha256(source_video)
    episode = _episode(episode_id)
    run_directory = (
        OUTPUTS_DIR
        / str(episode["project_id"])
        / episode_id
        / "dubbing"
        / "demucs"
        / task_id
    )
    source_audio = run_directory / "source-audio.wav"
    separation_dir = run_directory / "separation"
    task_vocals = separation_dir / "vocals.wav"
    task_no_vocals = separation_dir / "no_vocals.wav"
    if task_vocals.is_file() and task_no_vocals.is_file() and source_audio.is_file():
        source_audio_metadata = _validated_stem_entry(source_audio, duration)
        return {
            "status": "completed",
            "mode": DUBBING_PREVIEW_DEMUCS_MODE,
            "task_id": task_id,
            "task_status": "mixing",
            "episode_id": episode_id,
            "source_media_path": str(source_video.resolve()),
            "source_media_sha256": source_media_sha256,
            "source_duration": float(duration),
            "source_audio_path": str(source_audio.resolve()),
            "source_audio_duration": float(source_audio_metadata["duration"]),
            "separation_output_dir": str(separation_dir.resolve()),
            "audio_revision": audio_revision,
            "mix_revision": mix_revision,
            "vocals": _validated_stem_entry(task_vocals, duration),
            "no_vocals": _validated_stem_entry(task_no_vocals, duration),
            "worker_manifest_path": str((separation_dir / "separation_manifest.json").resolve()),
            "worker_return_code": 0,
            "cache_hit": True,
            "cache_source": "current_task_directory",
        }
    reusable = _matching_dubbing_preview_stems(
        episode_id,
        source_media_sha256=source_media_sha256,
        duration=duration,
    )
    if reusable:
        return {
            **reusable,
            "audio_revision": audio_revision,
            "mix_revision": mix_revision,
            "cache_source_task_id": reusable["task_id"],
        }
    reusable_audio = _reusable_audio_localization_stems(
        episode_id,
        duration,
        task_id,
        source_media_sha256,
    )
    if reusable_audio:
        return {
            **reusable_audio,
            "audio_revision": audio_revision,
            "mix_revision": mix_revision,
            "cache_source_task_id": reusable_audio["task_id"],
        }
    extracted = media_service.extract_dialogue_separation_audio(source_video, source_audio, duration)
    separated = _run_demucs_worker(task_id, source_audio, separation_dir)
    vocals = _validated_stem_entry(Path(separated["vocals_path"]), duration)
    no_vocals = _validated_stem_entry(Path(separated["no_vocals_path"]), duration)
    return {
        "status": "completed",
        "mode": DUBBING_PREVIEW_DEMUCS_MODE,
        "task_id": task_id,
        "task_status": "mixing",
        "episode_id": episode_id,
        "source_media_path": str(source_video.resolve()),
        "source_media_sha256": source_media_sha256,
        "source_duration": float(duration),
        "source_audio_path": str(source_audio.resolve()),
        "source_audio_duration": float(extracted["duration"]),
        "separation_output_dir": str(separation_dir.resolve()),
        "audio_revision": audio_revision,
        "mix_revision": mix_revision,
        "vocals": vocals,
        "no_vocals": no_vocals,
        "worker_manifest_path": str(Path(separated["manifest_path"]).resolve()),
        "worker_return_code": 0,
        "cache_hit": False,
    }


def _current_dubbing_preview_stems(
    episode_id: str,
    duration: float,
    audio_revision: str,
) -> dict[str, Any] | None:
    episode = _episode(episode_id)
    source_video = Path(str(episode.get("video_path") or ""))
    if not source_video.is_file() or source_video.stat().st_size <= 0:
        return None
    source_media_sha256 = _file_sha256(source_video)
    with get_connection() as db:
        rows = db.execute(
            "SELECT v.id AS task_id, v.usage_json, v.completed_at, v.updated_at, "
            "d.id AS dubbed_output_id, d.audio_path, d.preview_video_path, d.mix_revision, "
            "d.mix_input_manifest_json, d.mix_mode, d.updated_at AS dubbed_output_updated_at "
            "FROM voice_generation_tasks v "
            "JOIN dubbed_outputs d ON d.task_id=v.id AND d.episode_id=v.episode_id "
            "WHERE v.episode_id=? AND v.task_type='dubbing_preview' AND v.status='completed' "
            "AND d.status='completed' AND d.mix_mode='demucs_dialogue_replacement' "
            "ORDER BY COALESCE(v.completed_at, v.updated_at) DESC, v.updated_at DESC, v.id DESC, d.id DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        usage = json_load(row["usage_json"], {})
        demucs = usage.get("demucs_stems") if isinstance(usage.get("demucs_stems"), dict) else None
        mix_manifest = json_load(row["mix_input_manifest_json"], {})
        manifest_settings = mix_manifest.get("mix_settings") if isinstance(mix_manifest, dict) else None
        if not demucs or demucs.get("status") != "completed":
            continue
        if (
            demucs.get("episode_id") != episode_id
            or demucs.get("source_media_sha256") != source_media_sha256
            or demucs.get("audio_revision") != audio_revision
            or demucs.get("mix_revision") != row["mix_revision"]
            or usage.get("mix_revision") != row["mix_revision"]
            or usage.get("dubbed_output_id") != row["dubbed_output_id"]
            or not isinstance(manifest_settings, dict)
            or _mix_settings_revision(manifest_settings) != _current_mix_settings_revision(episode_id)
        ):
            continue
        try:
            vocals = _validated_stem_entry(Path(str(demucs["vocals"]["path"])), duration)
            no_vocals = _validated_stem_entry(Path(str(demucs["no_vocals"]["path"])), duration)
            audio_path = Path(str(row["audio_path"] or ""))
            audio_metadata = media_service.probe_audio_metadata(audio_path)
            if abs(float(audio_metadata["duration"]) - duration) > 0.1:
                continue
        except (AudioLocalizationError, KeyError, TypeError):
            continue
        except (OSError, media_service.MediaToolError):
            continue
        return {
            "task_id": row["task_id"],
            "dubbed_output_id": row["dubbed_output_id"],
            "mix_revision": row["mix_revision"],
            "audio_path": str(audio_path),
            "preview_video_path": str(row["preview_video_path"] or ""),
            "updated_at": row["dubbed_output_updated_at"],
            "source_media_sha256": source_media_sha256,
            "vocals": vocals,
            "no_vocals": no_vocals,
            "asset_source": "dubbing_preview",
        }
    return None


def _current_audio_localization_formal_mix(
    episode_id: str,
    duration: float,
    *,
    source_media_sha256: str,
    audio_revision: str,
    subtitle_revision: str,
    mix_settings_revision: str,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT * FROM audio_localization_tasks WHERE episode_id=? AND status='completed' "
            "ORDER BY completed_at DESC, updated_at DESC, id DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        settings = json_load(row["settings_json"], {})
        if (
            settings.get("source_media_sha256") != source_media_sha256
            or settings.get("audio_revision") != audio_revision
            or settings.get("subtitle_revision") != subtitle_revision
            or settings.get("mix_settings_revision") != mix_settings_revision
        ):
            continue
        try:
            vocals = _validated_stem_entry(Path(str(row["vocals_path"] or "")), duration)
            no_vocals = _validated_stem_entry(Path(str(row["no_vocals_path"] or "")), duration)
            audio_path = Path(str(row["mixed_audio_path"] or ""))
            audio_metadata = media_service.probe_audio_metadata(audio_path)
            if abs(float(audio_metadata["duration"]) - duration) > 0.1:
                continue
        except (AudioLocalizationError, OSError, media_service.MediaToolError):
            continue
        return {
            "task_id": row["id"],
            "dubbed_output_id": None,
            "mix_revision": mix_settings_revision,
            "audio_path": str(audio_path),
            "preview_video_path": str(row["output_path"] or ""),
            "updated_at": row["updated_at"],
            "source_media_sha256": source_media_sha256,
            "vocals": vocals,
            "no_vocals": no_vocals,
            "asset_source": "audio_localization",
        }
    return None


def _latest_quick_preview(episode_id: str) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT id, task_id, audio_path, preview_video_path, mix_mode, mix_revision, updated_at "
            "FROM dubbed_outputs WHERE episode_id=? AND status='completed' "
            "AND mix_mode<>'demucs_dialogue_replacement' ORDER BY updated_at DESC, id DESC",
            (episode_id,),
        ).fetchall()
    for row in rows:
        audio_path = Path(str(row["audio_path"] or ""))
        preview_path = Path(str(row["preview_video_path"] or ""))
        if (
            audio_path.is_file()
            and audio_path.stat().st_size > 0
            and preview_path.is_file()
            and preview_path.stat().st_size > 0
        ):
            return {
                "dubbed_output_id": row["id"],
                "task_id": row["task_id"],
                "mix_mode": row["mix_mode"],
                "mix_revision": row["mix_revision"],
                "audio_path": str(audio_path),
                "preview_video_path": str(preview_path),
                "updated_at": row["updated_at"],
            }
    return None


def _reusable_audio_localization_stems(
    episode_id: str,
    duration: float,
    current_task_id: str,
    source_media_sha256: str,
) -> dict[str, Any] | None:
    with get_connection() as db:
        rows = db.execute(
            "SELECT id, settings_json, vocals_path, no_vocals_path FROM audio_localization_tasks "
            "WHERE episode_id=? AND id<>? AND vocals_path IS NOT NULL "
            "AND no_vocals_path IS NOT NULL ORDER BY completed_at DESC, created_at DESC",
            (episode_id, current_task_id),
        ).fetchall()
    for row in rows:
        settings = json_load(row["settings_json"], {})
        if settings.get("source_media_sha256") != source_media_sha256:
            continue
        vocals_path = Path(str(row["vocals_path"] or ""))
        no_vocals_path = Path(str(row["no_vocals_path"] or ""))
        if not vocals_path.is_file() or not no_vocals_path.is_file():
            continue
        try:
            vocals = media_service.probe_audio_metadata(vocals_path)
            no_vocals = media_service.probe_audio_metadata(no_vocals_path)
        except media_service.MediaToolError:
            continue
        if (
            abs(float(vocals["duration"]) - duration) <= 0.1
            and abs(float(no_vocals["duration"]) - duration) <= 0.1
        ):
            return {
                "status": "completed",
                "task_id": row["id"],
                "source_media_sha256": source_media_sha256,
                "vocals": _validated_stem_entry(vocals_path, duration),
                "no_vocals": _validated_stem_entry(no_vocals_path, duration),
                "cache_hit": True,
                "cache_source": "audio_localization_task",
            }
    return None


def _current_episode_stems(episode_id: str, duration: float) -> dict[str, Any] | None:
    episode = _episode(episode_id)
    source_video = Path(str(episode.get("video_path") or ""))
    if not source_video.is_file() or source_video.stat().st_size <= 0:
        return None
    source_media_sha256 = _file_sha256(source_video)
    preview_stems = _matching_dubbing_preview_stems(
        episode_id,
        source_media_sha256=source_media_sha256,
        duration=duration,
    )
    if preview_stems:
        return preview_stems
    return _reusable_audio_localization_stems(
        episode_id,
        duration,
        "",
        source_media_sha256,
    )


def _mix_settings_revision(manifest: dict[str, Any]) -> str:
    normalized = {
        "dialogue_original_volume": float(manifest["dialogue_original_volume"]),
        "non_dialogue_original_volume": float(manifest["non_dialogue_original_volume"]),
        "dub_voice_volume": float(manifest["dub_voice_volume"]),
        "fade_ms": int(manifest["fade_ms"]),
        "include_target_subtitles": bool(manifest["include_target_subtitles"]),
    }
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _current_mix_settings_revision(episode_id: str) -> str:
    with get_connection() as db:
        row = db.execute(
            "SELECT dialogue_original_volume, non_dialogue_original_volume, dub_voice_volume, "
            "fade_ms, include_target_subtitles FROM audio_mix_settings WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
    manifest = (
        {
            "dialogue_original_volume": float(row["dialogue_original_volume"]),
            "non_dialogue_original_volume": float(row["non_dialogue_original_volume"]),
            "dub_voice_volume": float(row["dub_voice_volume"]),
            "fade_ms": int(row["fade_ms"]),
            "include_target_subtitles": bool(row["include_target_subtitles"]),
        }
        if row
        else {
            "dialogue_original_volume": 0.05,
            "non_dialogue_original_volume": 0.95,
            "dub_voice_volume": 1.9,
            "fade_ms": 10,
            "include_target_subtitles": True,
        }
    )
    return _mix_settings_revision(manifest)


def _current_mix_settings(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute(
            "SELECT dialogue_original_volume, non_dialogue_original_volume, dub_voice_volume, "
            "fade_ms, include_target_subtitles FROM audio_mix_settings WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
    if row:
        return {
            "dialogue_original_volume": float(row["dialogue_original_volume"]),
            "non_dialogue_original_volume": float(row["non_dialogue_original_volume"]),
            "dub_voice_volume": float(row["dub_voice_volume"]),
            "fade_ms": int(row["fade_ms"]),
            "include_target_subtitles": bool(row["include_target_subtitles"]),
        }
    return {
        "dialogue_original_volume": 0.05,
        "non_dialogue_original_volume": 0.95,
        "dub_voice_volume": 1.9,
        "fade_ms": 10,
        "include_target_subtitles": True,
    }


def audio_assets_readiness(episode_id: str, duration: float) -> dict[str, Any]:
    tts = duration_constrained_tts_service.provider_free_episode_readiness(episode_id)
    stems = _current_episode_stems(episode_id, duration)
    source_media_sha256 = _file_sha256(Path(str(_episode(episode_id).get("video_path") or "")))
    mix_settings_revision = _current_mix_settings_revision(episode_id)
    formal_mix = _current_dubbing_preview_stems(episode_id, duration, str(tts["audio_revision"]))
    if formal_mix is None:
        formal_mix = _current_audio_localization_formal_mix(
            episode_id,
            duration,
            source_media_sha256=source_media_sha256,
            audio_revision=str(tts["audio_revision"]),
            subtitle_revision=str(tts["subtitle_revision"]),
            mix_settings_revision=mix_settings_revision,
        )
    quick_preview = _latest_quick_preview(episode_id)
    hard_blockers = list(tts["blockers"])
    auto_prepare_blockers: list[dict[str, Any]] = []
    if stems is None:
        auto_prepare_blockers.append(
            {
                "asset": "demucs_stems",
                "segment_id": None,
                "error_code": "DEMUCS_STEMS_REQUIRED",
            }
        )
    if formal_mix is None:
        auto_prepare_blockers.append(
            {
                "asset": "formal_audio_mix",
                "segment_id": None,
                "error_code": "FORMAL_AUDIO_MIX_REQUIRED",
            }
        )
    stem_manifest = {
        "task_id": stems["task_id"],
        "source_media_sha256": stems["source_media_sha256"],
        "vocals": stems["vocals"],
        "no_vocals": stems["no_vocals"],
    } if stems else {}
    background_audio_revision = hashlib.sha256(
        json.dumps(stem_manifest, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "ready": bool(tts["ready"] and stems and formal_mix),
        "can_auto_prepare": bool(tts["ready"]),
        "tts_ready": bool(tts["ready"]),
        "stems_ready": stems is not None,
        "formal_mix_ready": formal_mix is not None,
        "dubbed_output_ready": formal_mix is not None,
        "quick_preview_ready": quick_preview is not None,
        "audio_asset_state": (
            "ready"
            if tts["ready"] and stems and formal_mix
            else "blocked"
            if not tts["ready"]
            else "preparing_background"
            if stems is None
            else "mix_required"
        ),
        "audio_revision": tts["audio_revision"],
        "subtitle_revision": tts["subtitle_revision"],
        "background_audio_revision": background_audio_revision,
        "mix_settings_revision": mix_settings_revision,
        "tts_manifest": tts["manifest"],
        "subtitle_manifest": tts["subtitle_manifest"],
        "stem_manifest": stem_manifest,
        "formal_mix": formal_mix or {},
        "quick_preview": quick_preview or {},
        "blockers": hard_blockers,
        "auto_prepare_blockers": auto_prepare_blockers,
    }


def provider_free_finalization_readiness(episode_id: str, duration: float) -> dict[str, Any]:
    return audio_assets_readiness(episode_id, duration)


def _save_duration_fit_diagnostics(task_id: str, settings: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = {
        "all_fit": bool(result.get("all_fit")),
        "segment_count": int(result.get("segment_count") or 0),
        "fit_count": int(result.get("fit_count") or 0),
        "blocked_segment_ids": list(result.get("blocked_segment_ids") or []),
        "translation_provider_calls": int(result.get("translation_provider_calls") or 0),
        "tts_provider_calls": int(result.get("tts_provider_calls") or 0),
        "provider_calls": int(result.get("provider_calls") or 0),
        "segments": list(result.get("segments") or []),
    }
    updated_settings = {**settings, "duration_fit": diagnostics}
    _update_task(task_id, settings_json=json_dump(updated_settings))
    return updated_settings


def run_audio_localization_task(task_id: str) -> None:
    try:
        task = get_audio_localization_task(task_id)
        with get_connection() as db:
            claimed = db.execute(
                "UPDATE audio_localization_tasks SET status='preparing_assets', progress=5, "
                "progress_message='Preparing completed Episode assets.', started_at=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (now_iso(), now_iso(), task_id),
            ).rowcount
        if not claimed:
            return

        settings = task["settings"]
        prepared = _validate_task_request(
            task["episode_id"],
            source_task_id=task.get("source_task_id"),
            target_language=str(settings.get("target_language") or "english"),
            voice=str(settings.get("voice") or "default"),
            hide_original_subtitles=bool(settings.get("hide_original_subtitles", True)),
            original_subtitle_region_start_percent=float(
                settings.get("original_subtitle_region_start_percent", DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT)
            ),
            original_subtitle_region_height_percent=float(
                settings.get("original_subtitle_region_height_percent", DEFAULT_BURNED_SUBTITLE_REGION_HEIGHT_PERCENT)
            ),
            subtitle_position=str(settings.get("subtitle_position") or "low"),
            allow_provider_calls=bool(settings.get("allow_provider_calls", True)),
        )
        episode = prepared["episode"]
        visual = prepared["visual"]
        visual_metadata = prepared["visual_metadata"]
        approved_timeline = _approved_xmax_timeline(visual)
        render_metadata = (
            media_service.common_video_resolution(approved_timeline["paths"])
            if approved_timeline
            else visual_metadata
        )
        assets = prepared["assets"]
        duration = float(visual_metadata["duration"])
        run_directory = OUTPUTS_DIR / episode["project_id"] / task["episode_id"] / "audio-localization" / task_id
        separation_dir = run_directory / "separation"
        source_audio = run_directory / "source-audio.wav"
        aligned_tts = run_directory / "english-tts-timeline.wav"
        mixed_audio = run_directory / "final-localized-mix.wav"
        subtitle_path = run_directory / "localized-en.ass"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        output_directory = run_directory.parent
        output_path = output_directory / f"localized-demucs-{task_id}-{timestamp}.mp4"
        run_directory.mkdir(parents=True, exist_ok=True)

        _update_task(
            task_id,
            status="building_tts_timeline",
            progress=10,
            progress_message="Fitting target-language voice lines to their original dialogue slots.",
            source_visual_path=str(visual["path"]),
        )
        try:
            duration_fit = duration_constrained_tts_service.ensure_episode_duration_fit(
                task["episode_id"],
                allow_provider_calls=bool(settings.get("allow_provider_calls", True)),
            )
        except duration_constrained_tts_service.DurationConstrainedTTSError as error:
            raise AudioLocalizationError(error.code, str(error)) from error
        settings = _save_duration_fit_diagnostics(task_id, settings, duration_fit)
        try:
            timeline = duration_constrained_tts_service.render_episode_tts_timeline(
                task["episode_id"],
                aligned_tts,
                duration,
                allow_partial=True,
            )
        except duration_constrained_tts_service.DurationConstrainedTTSError as error:
            raise AudioLocalizationError(error.code, str(error)) from error

        assets["translated_segments"] = current_renderable_subtitle_segments(task["episode_id"])
        _update_task(
            task_id,
            status="rendering_subtitles",
            progress=28,
            progress_message="Preparing the cropped visual master and two-line target-language subtitles.",
            tts_timeline_path=str(aligned_tts),
        )
        subtitle_cues = build_renderable_subtitle_cues(
            assets["translated_segments"],
            int(render_metadata["width"]),
            int(render_metadata["height"]),
            target_language=str(settings.get("target_language") or "english"),
        )
        settings = {**settings, "subtitle_cue_manifest": subtitle_cue_manifest(subtitle_cues)}
        _update_task(task_id, settings_json=json_dump(settings))
        subtitle_count = _write_overseas_ass(
            assets["translated_segments"],
            subtitle_path,
            int(render_metadata["width"]),
            int(render_metadata["height"]),
            hide_original_subtitles=bool(settings.get("hide_original_subtitles", True)),
            original_subtitle_region_start_percent=float(
                settings.get("original_subtitle_region_start_percent", DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT)
            ),
            subtitle_position=str(settings.get("subtitle_position") or "low"),
            cues=subtitle_cues,
        )
        subtitle_overlays = _write_overseas_overlays(
            assets["translated_segments"],
            run_directory / "subtitle-overlays",
            int(render_metadata["width"]),
            int(render_metadata["height"]),
            hide_original_subtitles=bool(settings.get("hide_original_subtitles", True)),
            original_subtitle_region_start_percent=float(
                settings.get("original_subtitle_region_start_percent", DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT)
            ),
            subtitle_position=str(settings.get("subtitle_position") or "low"),
            cues=subtitle_cues,
        )

        original_video = Path(str(episode.get("video_path") or ""))
        if not original_video.is_file() or _is_localized_visual_path(original_video):
            raise AudioLocalizationError(
                "LOCALIZATION_ORIGINAL_VIDEO_MISSING",
                "The original Episode video is not available for dialogue separation.",
            )
        source_media_sha256 = _file_sha256(original_video)
        tts_readiness = duration_constrained_tts_service.provider_free_episode_readiness(task["episode_id"])
        settings = {
            **settings,
            "source_media_sha256": source_media_sha256,
            "audio_revision": tts_readiness.get("audio_revision"),
            "subtitle_revision": tts_readiness.get("subtitle_revision"),
            "mix_settings_revision": _current_mix_settings_revision(task["episode_id"]),
        }
        _update_task(task_id, settings_json=json_dump(settings))

        current_formal_mix = _current_dubbing_preview_stems(
            task["episode_id"],
            duration,
            str(tts_readiness["audio_revision"]),
        )
        if current_formal_mix:
            separated = {
                "vocals_path": Path(str(current_formal_mix["vocals"]["path"])),
                "no_vocals_path": Path(str(current_formal_mix["no_vocals"]["path"])),
            }
            mixed_audio = Path(str(current_formal_mix["audio_path"]))
            _update_task(
                task_id,
                status="mixing_audio",
                progress=74,
                progress_message="Reusing the current verified Demucs-backed formal mix.",
                vocals_path=str(separated["vocals_path"]),
                no_vocals_path=str(separated["no_vocals_path"]),
                mixed_audio_path=str(mixed_audio),
            )
        else:
            _update_task(
                task_id,
                status="separating_audio",
                progress=42,
                progress_message="Removing original-language dialogue while preserving music and ambient sound.",
                separation_output_dir=str(separation_dir),
            )
            stem_manifest = prepare_dubbing_preview_stems(
                task["episode_id"],
                task_id=task_id,
                source_video=original_video,
                duration=duration,
                audio_revision=str(tts_readiness["audio_revision"]),
                mix_revision=f"audio-localization:{tts_readiness['audio_revision']}",
            )
            separated = {
                "vocals_path": Path(str(stem_manifest["vocals"]["path"])),
                "no_vocals_path": Path(str(stem_manifest["no_vocals"]["path"])),
            }
            _update_task(
                task_id,
                vocals_path=str(separated["vocals_path"]),
                no_vocals_path=str(separated["no_vocals_path"]),
            )
            _update_task(
                task_id,
                status="mixing_audio",
                progress=74,
                progress_message=f"Mixing preserved background audio with {timeline['mixed_count']} fitted target-language lines.",
                tts_timeline_path=str(aligned_tts),
            )
            media_service.mix_demucs_localization_audio(
                separated["no_vocals_path"],
                aligned_tts,
                mixed_audio,
                duration,
                vocals_path=separated["vocals_path"],
                dialogue_segments=assets["translated_segments"],
                settings=_current_mix_settings(task["episode_id"]),
            )
        _update_task(
            task_id,
            status="muxing_final",
            progress=88,
            progress_message="Rendering the final localized MP4.",
            mixed_audio_path=str(mixed_audio),
        )
        if approved_timeline:
            rendered = media_service.render_localized_xmax_timeline(
                approved_timeline["paths"],
                approved_timeline["target_durations"],
                approved_timeline["actual_durations"],
                mixed_audio,
                output_path,
                subtitle_overlays,
                hide_original_subtitles=bool(settings.get("hide_original_subtitles", True)),
                original_subtitle_region_start_percent=float(
                    settings.get(
                        "original_subtitle_region_start_percent",
                        DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
                    )
                ),
            )
        else:
            rendered = media_service.render_localized_video(
                visual["path"],
                mixed_audio,
                output_path,
                None,
                subtitle_overlays,
                hide_original_subtitles=bool(settings.get("hide_original_subtitles", True)),
                original_subtitle_region_start_percent=float(
                    settings.get(
                        "original_subtitle_region_start_percent",
                        DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
                    )
                ),
                original_subtitle_region_height_percent=float(
                    settings.get(
                        "original_subtitle_region_height_percent",
                        DEFAULT_BURNED_SUBTITLE_REGION_HEIGHT_PERCENT,
                    )
                ),
            )
        if str(rendered.get("video_codec") or "").lower() != "h264" or str(rendered.get("audio_codec") or "").lower() != "aac":
            raise AudioLocalizationError(
                "LOCALIZATION_OUTPUT_CODEC_INVALID",
                "The final localized MP4 is not H.264/AAC.",
            )
        if abs(float(rendered["duration"]) - duration) > 0.1:
            raise AudioLocalizationError(
                "LOCALIZATION_OUTPUT_DURATION_MISMATCH",
                "The final localized MP4 differs from the visual timeline by more than 100 ms.",
            )
        completed_at = now_iso()
        _update_task(
            task_id,
            status="completed",
            progress=100,
            progress_message=f"Localized version completed with {subtitle_count} subtitle cues.",
            output_path=str(output_path),
            output_url=_public_output_path(output_path),
            output_duration=float(rendered["duration"]),
            output_width=int(rendered["width"]),
            output_height=int(rendered["height"]),
            output_size=int(rendered["size_bytes"]),
            completed_at=completed_at,
            finished_at=completed_at,
            error_code=None,
            error_message=None,
        )
    except (AudioLocalizationError, media_service.MediaToolError, provider_service.ProviderError) as error:
        code = error.code
        message = str(error)
        logger.exception("Audio Localization task %s failed: %s", task_id, message)
        _update_task(
            task_id,
            status="failed",
            progress_message="Audio Localization failed.",
            error_code=code,
            error_message=message,
            finished_at=now_iso(),
        )
    except Exception as error:
        logger.exception("Unexpected Audio Localization task failure: %s", task_id)
        _update_task(
            task_id,
            status="failed",
            progress_message="Audio Localization failed.",
            error_code="LOCALIZATION_INTERNAL_ERROR",
            error_message=str(error),
            finished_at=now_iso(),
        )
    finally:
        with _active_task_lock:
            _active_task_ids.discard(task_id)


def get_audio_localization_state(episode_id: str, source_task_id: str | None = None) -> dict[str, Any]:
    episode = _episode(episode_id)
    visual = _visual_source(episode_id, source_task_id)
    transcript = _transcript_assets(episode_id)
    tts = _tts_timeline(episode_id)
    translated_segments = [
        item for item in transcript["segments"]
        if item.get("subtitle_enabled") and localization_text_authority.effective_text(item)
    ]
    text_authority = transcript.get("text_authority")
    try:
        duration_fit = duration_constrained_tts_service.episode_duration_fit_audit(episode_id)
    except (duration_constrained_tts_service.DurationConstrainedTTSError, media_service.MediaToolError):
        duration_fit = {
            "all_fit": False,
            "segment_count": len(translated_segments),
            "fit_count": 0,
            "blocked_segment_ids": [item["id"] for item in translated_segments],
            "segments": [],
        }
    output_directory = OUTPUTS_DIR / episode["project_id"] / episode_id / "audio-localization"
    latest_task = _latest_audio_localization_task(episode_id)
    latest_completed_task = None
    with get_connection() as db:
        completed_row = db.execute(
            "SELECT * FROM audio_localization_tasks WHERE episode_id=? AND status='completed' "
            "AND output_path IS NOT NULL ORDER BY completed_at DESC, created_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    if completed_row:
        latest_completed_task = _task_payload(completed_row)
    latest = (
        Path(str(latest_completed_task["output_path"]))
        if latest_completed_task and latest_completed_task.get("output_path")
        else _latest_localized_output(output_directory)
    )
    if latest and (not latest.is_file() or latest.stat().st_size <= 0):
        latest = _latest_localized_output(output_directory)
    return {
        "episode_id": episode_id,
        "project_id": episode["project_id"],
        "source": {
            "kind": visual["kind"],
            "task_id": visual["task_id"],
            "video_url": visual["url"],
            "duration": media_service.media_duration_seconds(visual["path"]),
        },
        "settings": {
            "target_language": text_authority.language if text_authority else "english",
            "language_mode": text_authority.language_mode if text_authority else None,
            "voice": "default",
            "audio_mode": DEFAULT_AUDIO_MODE,
            "original_audio_volume": 0,
            "voice_over_volume": 100,
            "keep_original_audio": False,
            "subtitles_enabled": True,
            "hide_original_subtitles": True,
            "original_subtitle_region_start_percent": DEFAULT_BURNED_SUBTITLE_REGION_START_PERCENT,
            "original_subtitle_region_height_percent": DEFAULT_BURNED_SUBTITLE_REGION_HEIGHT_PERCENT,
            "subtitle_position": "low",
        },
        "assets": {
            "asr_ready": bool(transcript["document"] and transcript["segments"]),
            "translation_ready": bool(translated_segments),
            "tts_ready": bool(duration_fit["all_fit"]),
            "transcript_count": len(transcript["segments"]),
            "translated_count": len(translated_segments),
            "tts_count": int(duration_fit["fit_count"]),
            "tts_timeline_url": tts["url"] if tts else None,
            "duration_fit": duration_fit,
        },
        "interfaces": {
            "asr": "provider_service.get_transcription_provider().transcribe_audio",
            "translation": "provider_service.get_translation_provider().translate_segments",
            "tts": "provider_service.generate_voice",
            "execution_mode": "duration_constrained_translation_tts_then_local_assembly",
            "audio_mode": DEFAULT_AUDIO_MODE,
        },
        "ready": bool(transcript["document"] and translated_segments),
        "latest_output": {
            "output_url": (
                latest_completed_task.get("output_url")
                if latest_completed_task and latest_completed_task.get("output_path") == str(latest)
                else _public_output_path(latest)
            ),
            "output_path": str(latest) if latest else None,
            "size_bytes": latest.stat().st_size if latest else None,
            "created_at": datetime.fromtimestamp(latest.stat().st_mtime).isoformat(timespec="seconds") if latest else None,
            "file_name": latest.name if latest else None,
            "version": latest.name if latest else None,
        } if latest else None,
        "latest_task": latest_task,
        "provider_calls": int((latest_task or {}).get("provider_calls") or 0),
        "credits": 0,
    }


def generate_localized_version(
    episode_id: str,
    *,
    source_task_id: str | None,
    target_language: str,
    voice: str,
    original_audio_volume: float,
    voice_over_volume: float,
    keep_original_audio: bool,
    subtitles_enabled: bool,
    hide_original_subtitles: bool,
    original_subtitle_region_start_percent: float,
    original_subtitle_region_height_percent: float,
    subtitle_position: str,
) -> dict[str, Any]:
    try:
        language_capabilities.normalize_target_language(target_language)
    except ValueError as error:
        raise AudioLocalizationError("LOCALIZATION_LANGUAGE_UNSUPPORTED", str(error)) from error
    if voice.strip().lower() != "default":
        raise AudioLocalizationError("LOCALIZATION_VOICE_UNSUPPORTED", "This MVP currently uses the completed default voice bindings.")
    normalized_subtitle_position = subtitle_position.strip().lower()
    if normalized_subtitle_position not in SUBTITLE_POSITIONS:
        raise AudioLocalizationError(
            "LOCALIZATION_SUBTITLE_POSITION_INVALID",
            "Subtitle position must be low, standard, or high.",
        )
    if (
        original_subtitle_region_start_percent < 0
        or original_subtitle_region_height_percent <= 0
        or original_subtitle_region_start_percent + original_subtitle_region_height_percent > 100
    ):
        raise AudioLocalizationError(
            "LOCALIZATION_SUBTITLE_REGION_INVALID",
            "The burned-in subtitle region must stay within the video frame.",
        )
    episode = _episode(episode_id)
    visual = _visual_source(episode_id, source_task_id)
    transcript = _transcript_assets(episode_id)
    translated_segments = [
        item for item in transcript["segments"]
        if item.get("subtitle_enabled") and localization_text_authority.effective_text(item)
    ]
    tts = _tts_timeline(episode_id)
    if not transcript["document"]:
        raise AudioLocalizationError("LOCALIZATION_ASR_REQUIRED", "Run the existing ASR step before localizing this Episode.")
    if not translated_segments:
        raise AudioLocalizationError("LOCALIZATION_TRANSLATION_REQUIRED", "No current dubbing text is available for this Episode.")
    translation_target = str(transcript["text_authority"].language)
    try:
        target_capability = language_capabilities.normalize_target_language(translation_target)
    except ValueError as error:
        raise AudioLocalizationError("LOCALIZATION_LANGUAGE_UNSUPPORTED", str(error)) from error
    if not tts or tts["mixed_count"] != len(translated_segments):
        raise AudioLocalizationError(
            "LOCALIZATION_TTS_REQUIRED",
            f"Completed TTS is required for all {len(translated_segments)} translated segments.",
        )
    source_metadata = media_service.probe_media_metadata(visual["path"])
    approved_timeline = _approved_xmax_timeline(visual)
    render_metadata = (
        media_service.common_video_resolution(approved_timeline["paths"])
        if approved_timeline
        else source_metadata
    )
    version = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    task_slug = str(visual["task_id"] or "episode")
    output_directory = OUTPUTS_DIR / episode["project_id"] / episode_id / "audio-localization"
    run_directory = output_directory / version
    original_path = run_directory / "original-audio.wav"
    mixed_path = run_directory / "localized-audio.m4a"
    subtitle_path = run_directory / "localized-en.ass"
    output_path = output_directory / f"localized-{task_slug}-{version}.mp4"
    extracted = media_service.extract_localization_audio(visual["path"], original_path, source_metadata["duration"])
    mixed = media_service.mix_localization_audio(
        original_path,
        tts["path"],
        mixed_path,
        source_metadata["duration"],
        keep_original_audio=keep_original_audio,
        original_volume=original_audio_volume / 100,
        voice_volume=voice_over_volume / 100,
    )
    subtitle_count = 0
    rendered_subtitle_path: Path | None = None
    subtitle_overlays: list[dict[str, Any]] = []
    if subtitles_enabled:
        subtitle_cues = build_renderable_subtitle_cues(
            translated_segments,
            int(render_metadata["width"]),
            int(render_metadata["height"]),
            target_language=target_capability.id,
        )
        subtitle_count = _write_overseas_ass(
            translated_segments, subtitle_path, int(render_metadata["width"]), int(render_metadata["height"]),
            hide_original_subtitles=hide_original_subtitles,
            original_subtitle_region_start_percent=original_subtitle_region_start_percent,
            subtitle_position=normalized_subtitle_position,
            cues=subtitle_cues,
        )
        subtitle_overlays = _write_overseas_overlays(
            translated_segments,
            run_directory / "subtitle-overlays",
            int(render_metadata["width"]),
            int(render_metadata["height"]),
            hide_original_subtitles=hide_original_subtitles,
            original_subtitle_region_start_percent=original_subtitle_region_start_percent,
            subtitle_position=normalized_subtitle_position,
            cues=subtitle_cues,
        )
    if approved_timeline:
        rendered = media_service.render_localized_xmax_timeline(
            approved_timeline["paths"],
            approved_timeline["target_durations"],
            approved_timeline["actual_durations"],
            mixed_path,
            output_path,
            subtitle_overlays,
            hide_original_subtitles=hide_original_subtitles,
            original_subtitle_region_start_percent=original_subtitle_region_start_percent,
        )
    else:
        rendered = media_service.render_localized_video(
            visual["path"],
            mixed_path,
            output_path,
            rendered_subtitle_path,
            subtitle_overlays,
            hide_original_subtitles=hide_original_subtitles,
            original_subtitle_region_start_percent=original_subtitle_region_start_percent,
            original_subtitle_region_height_percent=original_subtitle_region_height_percent,
        )
    return {
        "episode_id": episode_id,
        "project_id": episode["project_id"],
        "status": "completed",
        "source_task_id": visual["task_id"],
        "source_video_url": visual["url"],
        "target_language": target_capability.id,
        "voice": "default",
        "settings": {
            "target_language": target_capability.id,
            "voice": "default",
            "original_audio_volume": original_audio_volume,
            "voice_over_volume": voice_over_volume,
            "keep_original_audio": keep_original_audio,
            "subtitles_enabled": subtitles_enabled,
            "hide_original_subtitles": hide_original_subtitles,
            "original_subtitle_region_start_percent": original_subtitle_region_start_percent,
            "original_subtitle_region_height_percent": original_subtitle_region_height_percent,
            "subtitle_position": normalized_subtitle_position,
        },
        "asset_counts": {
            "transcript_segments": len(transcript["segments"]),
            "translated_segments": len(translated_segments),
            "tts_segments": tts["mixed_count"],
            "subtitle_cues": subtitle_count,
        },
        "audio": {
            "source_had_audio": extracted["source_had_audio"],
            "original_audio_path": str(original_path),
            "tts_timeline_path": str(tts["path"]),
            "mixed_audio_path": str(mixed_path),
            "amix_inputs": mixed["amix_inputs"],
            "normalize": mixed["normalize"],
        },
        "output_url": _public_output_path(output_path),
        "output_path": str(output_path),
        "output_version": output_path.name,
        "duration": rendered["duration"],
        "width": rendered["width"],
        "height": rendered["height"],
        "has_audio": rendered["has_audio"],
        "size_bytes": rendered["size_bytes"],
        "subtitles_rendered": rendered["subtitles_rendered"],
        "original_subtitles_hidden": rendered["original_subtitles_hidden"],
        "subtitle_mask": rendered["subtitle_mask"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "provider_calls": 0,
        "credits": 0,
    }
