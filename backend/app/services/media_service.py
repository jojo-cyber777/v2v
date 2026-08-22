"""Real FFmpeg/ffprobe integration for Stage 3 A1."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WINGET_LINKS = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
KNOWN_FFMPEG_DIRS = (
    Path(os.getenv("ProgramFiles(x86)", "")) / "iMobie" / "FocuSee" / "FFmpeg",
)
logger = logging.getLogger(__name__)

TTS_SILENCE_WINDOW_MS = 10
TTS_SILENCE_THRESHOLD_DBFS = -45.0
TTS_SILENCE_GUARD_MS = 50


class MediaToolError(RuntimeError):
    def __init__(self, code: str, message: str, stderr: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr
        self.details = details or {}


def _tool_candidates(name: str) -> list[tuple[str, str]]:
    env_name = "FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH"
    candidates: list[tuple[str, str]] = []
    configured = os.getenv(env_name, "").strip()
    if configured:
        candidates.append((configured, "env"))
    sibling_env_name = "FFPROBE_PATH" if name == "ffmpeg" else "FFMPEG_PATH"
    sibling_configured = os.getenv(sibling_env_name, "").strip()
    if sibling_configured:
        candidates.append((str(Path(sibling_configured).with_name(f"{name}.exe")), "sibling"))
    for command_name in (name, f"{name}.exe"):
        path = shutil.which(command_name)
        if path:
            candidates.append((path, "PATH"))
    sibling_path = shutil.which("ffprobe.exe" if name == "ffmpeg" else "ffmpeg.exe") or shutil.which("ffprobe" if name == "ffmpeg" else "ffmpeg")
    if sibling_path:
        candidates.append((str(Path(sibling_path).with_name(f"{name}.exe")), "sibling"))
    if WINGET_LINKS.is_dir():
        candidates.append((str(WINGET_LINKS / f"{name}.exe"), "PATH"))
    for directory in KNOWN_FFMPEG_DIRS:
        candidates.append((str(directory / f"{name}.exe"), "sibling"))
    project_config = PROJECT_ROOT / "tools" / f"{name}.exe"
    candidates.append((str(project_config), "project"))
    return candidates


def resolve_media_binary(name: str) -> tuple[str | None, str | None]:
    seen: set[str] = set()
    for candidate, source in _tool_candidates(name):
        path = Path(candidate)
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        executable_paths = [path]
        try:
            resolved_path = path.resolve(strict=True)
            if resolved_path != path:
                executable_paths.insert(0, resolved_path)
        except OSError:
            pass
        for executable_path in executable_paths:
            try:
                result = subprocess.run(
                    [str(executable_path), "-version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                return str(executable_path), source
    return None, None


def resolve_ffmpeg_binary() -> str | None:
    return resolve_media_binary("ffmpeg")[0]


def resolve_ffprobe_binary() -> str | None:
    return resolve_media_binary("ffprobe")[0]


def _tool_info(name: str) -> dict[str, Any]:
    path, source = resolve_media_binary(name)
    if not path:
        return {"available": False, "path": None, "version": None, "source": None}
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=5, check=False
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
        return {"available": result.returncode == 0, "path": path, "version": first_line, "source": source}
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "path": path, "version": str(error), "source": source}


def media_tool_status() -> dict[str, Any]:
    ffmpeg = _tool_info("ffmpeg")
    ffprobe = _tool_info("ffprobe")
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    return {
        "available": ffmpeg["available"] and ffprobe["available"],
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "resolved_ffmpeg_path": ffmpeg.get("path"),
        "resolved_ffprobe_path": ffprobe.get("path"),
        "backend_working_directory": str(Path.cwd()),
        "process_path_contains_tool_dir": {
            "ffmpeg": bool(ffmpeg.get("path") and any(Path(entry).resolve() == Path(ffmpeg["path"]).parent.resolve() for entry in path_entries if Path(entry).exists())),
            "ffprobe": bool(ffprobe.get("path") and any(Path(entry).resolve() == Path(ffprobe["path"]).parent.resolve() for entry in path_entries if Path(entry).exists())),
        },
    }


def _require_tool(name: str) -> str:
    path, _source = resolve_media_binary(name)
    if not path:
        searched_paths = [candidate for candidate, _source in _tool_candidates(name)]
        raise MediaToolError(
            f"{name.upper()}_UNAVAILABLE",
            f"{name} is not installed or is not available on PATH.",
            details={
                "searched_paths": searched_paths,
                "current_working_directory": str(Path.cwd()),
                "resolved_path": None,
                "stderr": "",
            },
        )
    return path


def _ffprobe_stream_side_data_unsupported(stderr: str) -> bool:
    normalized = stderr.lower()
    return "no match for section" in normalized and "stream_side_data" in normalized


def probe_video_metadata(video_path: Path) -> dict[str, Any]:
    """Read stable video metadata for clip creation and provider input validation."""
    ffprobe = _require_tool("ffprobe")
    full_show_entries = (
        "stream=width,height,r_frame_rate,avg_frame_rate,duration,codec_name,pix_fmt:"
        "stream_tags=rotate:stream_side_data=rotation:format=duration"
    )

    def run_probe(show_entries: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                show_entries,
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        )

    result = run_probe(full_show_entries)
    if result.returncode != 0 and _ffprobe_stream_side_data_unsupported(result.stderr):
        result = run_probe(full_show_entries.replace(":stream_side_data=rotation", ""))
    if result.returncode != 0:
        raise MediaToolError(
            "FFPROBE_FAILED",
            "Unable to inspect video metadata.",
            result.stderr,
            details={"resolved_path": ffprobe, "stderr": result.stderr, "current_working_directory": str(Path.cwd())},
        )
    try:
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        try:
            video_duration = float(stream.get("duration") or duration)
        except (TypeError, ValueError):
            video_duration = duration
        if not video_duration > 0:
            video_duration = duration
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
        numerator, denominator = (int(part) for part in str(rate).split("/", 1))
        fps = numerator / denominator if denominator else 0.0
        raw_rotation = (
            (stream.get("tags") or {}).get("rotate")
            or next(
                (
                    item.get("rotation")
                    for item in (stream.get("side_data_list") or [])
                    if item.get("rotation") is not None
                ),
                0,
            )
            or 0
        )
        try:
            rotation = float(raw_rotation)
        except (TypeError, ValueError):
            rotation = 0.0
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaToolError(
            "FFPROBE_INVALID_OUTPUT",
            "ffprobe did not return valid video metadata.",
            result.stderr,
            details={"resolved_path": ffprobe, "stderr": result.stderr, "stdout": result.stdout, "current_working_directory": str(Path.cwd())},
        ) from error
    if width <= 0 or height <= 0 or duration <= 0:
        raise MediaToolError("VIDEO_METADATA_INVALID", "The video metadata is incomplete or invalid.")
    aspect_ratio = width / height
    orientation = "portrait" if height > width else "landscape" if width > height else "square"
    return {
        "duration": duration,
        "video_duration": video_duration,
        "width": width,
        "height": height,
        "fps": fps,
        "aspect_ratio": aspect_ratio,
        "orientation": orientation,
        "video_codec": stream.get("codec_name"),
        "pixel_format": stream.get("pix_fmt"),
        "rotation": rotation,
    }


def clamp_video_frame_timestamp(timestamp: float, video_duration: float, fps: float = 0.0) -> float:
    """Keep a thumbnail seek before the final decodable video-frame boundary."""
    duration = float(video_duration)
    if duration <= 0:
        raise MediaToolError("VIDEO_METADATA_INVALID", "The video stream duration is invalid.")
    frame_interval = 1.0 / float(fps) if fps > 0 else 0.04
    safety_margin = min(max(frame_interval, 0.01), duration / 2)
    return round(max(0.0, min(float(timestamp), duration - safety_margin)), 6)


def probe_media_metadata(media_path: Path) -> dict[str, Any]:
    """Read video and audio stream metadata needed by the local Xmax merge."""
    ffprobe = _require_tool("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    if result.returncode != 0:
        raise MediaToolError("FFPROBE_FAILED", "Unable to inspect media streams.", result.stderr, details={"stderr": result.stderr})
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams") or []
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if not video:
            raise ValueError("No video stream found.")
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        numerator, denominator = (int(part) for part in str(rate).split("/", 1))
        fps = numerator / denominator if denominator else 0.0
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaToolError("FFPROBE_INVALID_OUTPUT", "ffprobe did not return valid media metadata.", result.stderr, details={"stdout": result.stdout[:2000]}) from error
    if duration <= 0 or width <= 0 or height <= 0:
        raise MediaToolError("VIDEO_METADATA_INVALID", "The media metadata is incomplete or invalid.")
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_audio": audio is not None,
        "audio_channels": int(audio.get("channels") or 0) if audio else 0,
    }


def decode_media_to_null(media_path: Path) -> dict[str, Any]:
    """Decode every stream to EOF without producing an output file."""
    ffmpeg = _require_tool("ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(media_path), "-map", "0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    return {
        "passed": result.returncode == 0,
        "exit_code": result.returncode,
        "stderr": result.stderr[-4000:],
        "ffmpeg_path": ffmpeg,
    }


def merge_xmax_shots_with_source_audio(
    shot_paths: list[Path],
    target_durations: list[float],
    actual_durations: list[float],
    source_path: Path,
    output_path: Path,
    source_metadata: dict[str, Any],
    pre_rolls: list[float] | None = None,
    post_rolls: list[float] | None = None,
) -> dict[str, Any]:
    """Align content-only Shot inputs, concatenate them, and restore source audio.

    Proxy buffers are normalized before this function is called. Keeping the
    arguments for compatibility, but requiring zero buffers here prevents a
    second pre/post-roll subtraction during the actual FFmpeg merge.
    """
    if len(shot_paths) != len(target_durations) or len(shot_paths) != len(actual_durations) or not shot_paths:
        raise MediaToolError("MERGE_INPUT_INVALID", "Shot metadata does not match the input files.")
    pre_rolls = pre_rolls or [0.0] * len(shot_paths)
    post_rolls = post_rolls or [0.0] * len(shot_paths)
    if len(pre_rolls) != len(shot_paths) or len(post_rolls) != len(shot_paths):
        raise MediaToolError("MERGE_INPUT_INVALID", "Shot proxy metadata does not match the input files.")
    if any(abs(float(value)) > 1e-6 for value in [*pre_rolls, *post_rolls]):
        raise MediaToolError("MERGE_INPUT_INVALID", "Shot inputs must be content-only; proxy buffers must be removed before FFmpeg merge.")
    ratios = []
    content_actual_durations = []
    for target, actual in zip(target_durations, actual_durations):
        content_actual = float(actual)
        if target <= 0 or actual <= 0 or content_actual <= 0:
            raise MediaToolError("SHOT_DURATION_INVALID", "Every Shot duration must be greater than zero.")
        content_actual_durations.append(content_actual)
        ratio = target / content_actual
        if ratio < 0.5 or ratio > 2.0:
            raise MediaToolError("SHOT_DURATION_RATIO_INVALID", f"Shot duration alignment ratio {ratio:.4f} is outside the allowed 0.5-2.0 range.")
        ratios.append(ratio)

    ffmpeg = _require_tool("ffmpeg")
    width = int(source_metadata["width"])
    height = int(source_metadata["height"])
    fps = float(source_metadata.get("fps") or 30.0)
    source_duration = float(source_metadata["duration"])
    filters = []
    for index, (ratio, target, actual) in enumerate(zip(ratios, target_durations, content_actual_durations)):
        filters.append(
            f"[{index}:v]trim=start=0:end={actual:.6f},setpts={ratio:.10f}*PTS,trim=duration={target:.6f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.6f}[shot{index}]"
        )
    labels = "".join(f"[shot{index}]" for index in range(len(shot_paths)))
    filters.append(f"{labels}concat=n={len(shot_paths)}:v=1:a=0,trim=duration={source_duration:.6f},setpts=PTS-STARTPTS[vout]")
    command = ["-y"]
    for shot_path in shot_paths:
        command.extend(["-i", str(shot_path)])
    command.extend(["-i", str(source_path), "-filter_complex", ";".join(filters), "-map", "[vout]"])
    source_index = len(shot_paths)
    if source_metadata.get("has_audio"):
        command.extend(["-map", f"{source_index}:a:0", "-af", f"atrim=duration={source_duration:.6f},asetpts=PTS-STARTPTS", "-c:a", "aac", "-b:a", "192k"])
    else:
        command.append("-an")
    command.extend([
        "-t", f"{source_duration:.6f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output_path),
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([ffmpeg, *command], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False)
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError("MERGE_FFMPEG_FAILED", "FFmpeg could not create the final MP4.", result.stderr, details={"stderr": result.stderr[-4000:]})
    return {"ratios": ratios, "content_actual_durations": content_actual_durations, "ffmpeg_command": [ffmpeg, *command], "file_size": output_path.stat().st_size}


def common_video_resolution(video_paths: list[Path]) -> dict[str, Any]:
    """Choose the largest shared canvas that never upscales an input video."""
    if not video_paths:
        raise MediaToolError("VIDEO_RESOLUTION_INPUT_EMPTY", "No visual inputs are available.")
    metadata = [probe_media_metadata(path) for path in video_paths]
    minimum_width = min(int(item["width"]) for item in metadata)
    minimum_height = min(int(item["height"]) for item in metadata)
    reference = min(metadata, key=lambda item: int(item["width"]) * int(item["height"]))
    aspect_ratio = float(reference["width"]) / float(reference["height"])

    width = minimum_width - (minimum_width % 2)
    height = round(width / aspect_ratio)
    height -= height % 2
    if height > minimum_height:
        height = minimum_height - (minimum_height % 2)
        width = round(height * aspect_ratio)
        width -= width % 2
    if width <= 0 or height <= 0:
        raise MediaToolError("VIDEO_RESOLUTION_INVALID", "The visual inputs do not have a valid common resolution.")

    return {
        "width": width,
        "height": height,
        "fps": min(float(item.get("fps") or 30.0) for item in metadata),
        "inputs": metadata,
        "all_inputs_match": all(
            int(item["width"]) == width and int(item["height"]) == height
            for item in metadata
        ),
        "upscaled": False,
    }


def render_localized_xmax_timeline(
    shot_paths: list[Path],
    target_durations: list[float],
    actual_durations: list[float],
    audio_path: Path,
    output_path: Path,
    subtitle_overlays: list[dict[str, Any]] | None = None,
    *,
    hide_original_subtitles: bool = False,
    original_subtitle_region_start_percent: float = 85.5,
) -> dict[str, Any]:
    """Render approved Xmax units, localized audio, and subtitles in one H.264 encode."""
    if (
        not shot_paths
        or len(shot_paths) != len(target_durations)
        or len(shot_paths) != len(actual_durations)
    ):
        raise MediaToolError("MERGE_INPUT_INVALID", "Shot metadata does not match the visual inputs.")
    if any(not path.is_file() or path.stat().st_size <= 0 for path in shot_paths):
        raise MediaToolError("XMAX_OUTPUT_MISSING", "One or more approved Xmax outputs are missing.")
    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        raise MediaToolError("LOCALIZATION_AUDIO_ASSET_MISSING", "The localized audio track is missing.")

    common = common_video_resolution(shot_paths)
    width = int(common["width"])
    height = int(common["height"])
    fps = max(1.0, float(common["fps"] or 30.0))
    duration = sum(float(value) for value in target_durations)
    ratios: list[float] = []
    filters: list[str] = []
    for index, (target, actual) in enumerate(zip(target_durations, actual_durations)):
        if target <= 0 or actual <= 0:
            raise MediaToolError("SHOT_DURATION_INVALID", "Every Shot duration must be greater than zero.")
        ratio = float(target) / float(actual)
        if ratio < 0.5 or ratio > 2.0:
            raise MediaToolError(
                "SHOT_DURATION_RATIO_INVALID",
                f"Shot duration alignment ratio {ratio:.4f} is outside the allowed 0.5-2.0 range.",
            )
        ratios.append(ratio)
        filters.append(
            f"[{index}:v:0]trim=start=0:end={float(actual):.6f},"
            f"setpts={ratio:.10f}*PTS,trim=duration={float(target):.6f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps:.6f}[shot{index}]"
        )
    labels = "".join(f"[shot{index}]" for index in range(len(shot_paths)))
    filters.append(
        f"{labels}concat=n={len(shot_paths)}:v=1:a=0,"
        f"trim=duration={duration:.6f},setpts=PTS-STARTPTS[timeline]"
    )

    if hide_original_subtitles:
        kept_height = max(
            2,
            min(
                height - (height % 2),
                (int(height * original_subtitle_region_start_percent / 100) // 2) * 2,
            ),
        )
        filters.append(
            f"[timeline]crop=w={width}:h={kept_height}:x=0:y=0,"
            f"scale=w=-2:h={height}:flags=lanczos,"
            f"crop=w={width}:h={height}:x=(iw-{width})/2:y=0,"
            "setsar=1,format=yuv420p[localized_visual]"
        )
    else:
        kept_height = height
        filters.append("[timeline]format=yuv420p[localized_visual]")

    audio_index = len(shot_paths)
    filters.append(
        f"[{audio_index}:a:0]aresample=48000,apad,atrim=0:{duration:.6f},"
        "asetpts=PTS-STARTPTS[localized_audio]"
    )
    overlays = subtitle_overlays or []
    current = "localized_visual"
    for index, overlay in enumerate(overlays):
        next_label = f"localized_v{index + 1}"
        overlay_input = audio_index + index + 1
        start = max(0.0, float(overlay["start_ms"]) / 1000)
        end = max(start, float(overlay["end_ms"]) / 1000)
        filters.append(
            f"[{current}][{overlay_input}:v]overlay=0:0:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_label}]"
        )
        current = next_label

    ffmpeg = _require_tool("ffmpeg")
    command = [ffmpeg, "-y"]
    for path in shot_paths:
        command.extend(["-i", str(path)])
    command.extend(["-i", str(audio_path)])
    for overlay in overlays:
        command.extend(["-loop", "1", "-i", str(overlay["path"])])
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{current}]", "-map", "[localized_audio]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.6f}", "-movflags", "+faststart",
        str(output_path),
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError(
            "LOCALIZATION_RENDER_FAILED",
            "Unable to render the high-definition localized MP4.",
            result.stderr,
        )
    rendered = probe_media_metadata(output_path)
    if (
        int(rendered["width"]) != width
        or int(rendered["height"]) != height
        or abs(float(rendered["duration"]) - duration) > 0.1
    ):
        raise MediaToolError(
            "LOCALIZATION_OUTPUT_INVALID",
            "The high-definition localized MP4 does not match the approved visual timeline.",
        )
    return {
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "subtitles_rendered": bool(overlays),
        "subtitle_overlay_count": len(overlays),
        "original_subtitles_hidden": hide_original_subtitles,
        "subtitle_mask": {
            "method": "crop_and_scale",
            "start_percent": float(original_subtitle_region_start_percent),
            "kept_height": kept_height,
            "removed_height": height - kept_height,
            "output_width": width,
            "output_height": height,
            "preserves_aspect_ratio": True,
        } if hide_original_subtitles else None,
        "common_resolution": common,
        "duration_ratios": ratios,
        "video_encode_count": 1,
        "command": command,
        "stderr": result.stderr,
        **rendered,
    }


def trim_video(input_path: Path, output_path: Path, start_time: float, end_time: float) -> dict[str, Any]:
    if not input_path.is_file():
        raise MediaToolError("VIDEO_FILE_NOT_FOUND", "The source video file does not exist.")
    if start_time < 0 or end_time <= start_time:
        raise MediaToolError("VIDEO_CLIP_RANGE_INVALID", "The clip start and end times are invalid.")
    metadata = probe_video_metadata(input_path)
    if end_time > metadata["duration"] + 0.25:
        raise MediaToolError("VIDEO_CLIP_RANGE_INVALID", "The clip end time exceeds the source video duration.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = min(end_time, metadata["duration"]) - start_time
    command = [ffmpeg, "-y", "-ss", f"{start_time:.3f}", "-i", str(input_path), "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", str(output_path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False)
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("VIDEO_CLIP_FAILED", "Unable to create the visual transformation clip.", result.stderr)
    clip_metadata = probe_video_metadata(output_path)
    return {"path": str(output_path), "command": command, "stderr": result.stderr, **clip_metadata}


def crop_and_scale_video(
    input_path: Path,
    output_path: Path,
    *,
    crop: dict[str, int],
    scale: dict[str, int],
    start_time: float,
    duration: float,
) -> dict[str, Any]:
    """Create a content-only fixed-crop Xmax input without changing playback speed."""
    if not input_path.is_file():
        raise MediaToolError("VIDEO_FILE_NOT_FOUND", "The source video file does not exist.")
    metadata = probe_media_metadata(input_path)
    try:
        x, y = int(crop["x"]), int(crop["y"])
        width, height = int(crop["width"]), int(crop["height"])
        output_width, output_height = int(scale["width"]), int(scale["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise MediaToolError("VIDEO_CROP_INVALID", "The crop or scale parameters are invalid.") from error
    if min(x, y) < 0 or min(width, height, output_width, output_height) <= 0:
        raise MediaToolError("VIDEO_CROP_INVALID", "The crop or scale parameters are invalid.")
    if x + width > metadata["width"] or y + height > metadata["height"]:
        raise MediaToolError("VIDEO_CROP_OUT_OF_BOUNDS", "The crop rectangle exceeds the source frame.")
    if width % 2 or height % 2 or output_width % 2 or output_height % 2:
        raise MediaToolError("VIDEO_CROP_ALIGNMENT_INVALID", "Crop and output dimensions must be even.")
    if abs((width / height) - (output_width / output_height)) > 0.01:
        raise MediaToolError("VIDEO_CROP_ASPECT_INVALID", "The crop and output must use the same aspect ratio.")
    if start_time < 0 or duration <= 0 or start_time + duration > metadata["duration"] + 0.25:
        raise MediaToolError("VIDEO_CLIP_RANGE_INVALID", "The requested content range is invalid.")

    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-ss", f"{start_time:.6f}", "-i", str(input_path),
        "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a?",
        "-vf", f"crop={width}:{height}:{x}:{y},scale={output_width}:{output_height}:flags=lanczos,setpts=PTS-STARTPTS",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
    ]
    if metadata.get("has_audio"):
        command.extend(["-af", "asetpts=PTS-STARTPTS", "-c:a", "aac"])
    command.extend(["-movflags", "+faststart", str(output_path)])
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False,
    )
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError("VIDEO_CROP_FAILED", "Unable to create the Level 2 crop input.", result.stderr)
    output_metadata = probe_media_metadata(output_path)
    if output_metadata["width"] != output_width or output_metadata["height"] != output_height:
        raise MediaToolError("VIDEO_CROP_OUTPUT_INVALID", "The Level 2 crop output resolution is invalid.")
    tolerance = max(0.1, duration * 0.05)
    if abs(float(output_metadata["duration"]) - duration) > tolerance:
        raise MediaToolError("VIDEO_CROP_DURATION_INVALID", "The Level 2 crop output duration is invalid.")
    decode = decode_media_to_null(output_path)
    if not decode["passed"]:
        raise MediaToolError("VIDEO_CROP_DECODE_FAILED", "The Level 2 crop output cannot be decoded completely.", decode["stderr"])
    return {
        "path": str(output_path), "command": command, "stderr": result.stderr,
        "crop": {"x": x, "y": y, "width": width, "height": height},
        "scale": {"width": output_width, "height": output_height},
        "decode": decode, **output_metadata,
    }


def detect_scene_cuts(video_path: Path, threshold: float = 0.35) -> list[float]:
    """Return FFmpeg scene-change timestamps; callers can fall back to fixed cuts."""
    ffmpeg = _require_tool("ffmpeg")
    if not video_path.is_file():
        raise MediaToolError("VIDEO_FILE_NOT_FOUND", "The source video file does not exist.")
    command = [
        ffmpeg, "-hide_banner", "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold:.3f})',showinfo",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False)
    if result.returncode != 0:
        raise MediaToolError("SCENE_DETECTION_FAILED", "Unable to detect scene boundaries.", result.stderr)
    timestamps = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr or "")]
    return sorted({round(value, 3) for value in timestamps if value > 0})


def detect_scene_candidates(video_path: Path, threshold: float = 0.35) -> list[dict[str, float]]:
    """Return local FFmpeg scene candidates with the lavfi scene score."""
    ffmpeg = _require_tool("ffmpeg")
    if not video_path.is_file():
        raise MediaToolError("VIDEO_FILE_NOT_FOUND", "The source video file does not exist.")
    command = [
        ffmpeg, "-hide_banner", "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold:.3f})',metadata=print:file=-,showinfo",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False)
    if result.returncode != 0:
        raise MediaToolError("SCENE_DETECTION_FAILED", "Unable to detect scene boundaries.", result.stderr)
    text = f"{result.stderr}\n{result.stdout}"
    matches = re.findall(
        r"frame:\s*\d+\s+pts:\s*\d+\s+pts_time:([0-9]+(?:\.[0-9]+)?).*?lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.DOTALL,
    )
    candidates = [{"timestamp": round(float(timestamp), 3), "score": round(float(score), 6)} for timestamp, score in matches]
    if candidates:
        return sorted({item["timestamp"]: item for item in candidates}.values(), key=lambda item: item["timestamp"])
    return [{"timestamp": timestamp, "score": threshold} for timestamp in detect_scene_cuts(video_path, threshold)]


def extract_video_frame(
    video_path: Path,
    output_path: Path,
    timestamp: float,
    *,
    width: int = 480,
    height: int = 270,
    video_duration: float | None = None,
    fps: float = 0.0,
) -> dict[str, Any]:
    """Extract one local review thumbnail without invoking a Provider."""
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_timestamp = (
        clamp_video_frame_timestamp(timestamp, video_duration, fps)
        if video_duration is not None
        else max(0.0, timestamp)
    )
    command = [
        ffmpeg, "-y", "-ss", f"{safe_timestamp:.6f}", "-i", str(video_path),
        "-frames:v", "1", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-q:v", "3", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        details = {
            "input_path": str(video_path.resolve()),
            "output_path": str(output_path.resolve()),
            "requested_timestamp": round(float(timestamp), 6),
            "safe_timestamp": safe_timestamp,
            "video_duration": video_duration,
            "fps": fps,
            "ffmpeg_path": ffmpeg,
            "command": command,
            "exit_code": result.returncode,
            "stderr": result.stderr,
        }
        logger.error("Scene thumbnail extraction failed: %s", json.dumps(details, ensure_ascii=True))
        raise MediaToolError(
            "SCENE_THUMBNAIL_FAILED",
            "Unable to create a scene review thumbnail.",
            result.stderr,
            details=details,
        )
    return {
        "path": str(output_path),
        "timestamp": round(safe_timestamp, 6),
        "requested_timestamp": round(float(timestamp), 6),
        "file_size": output_path.stat().st_size,
    }


def inspect_video_decode_health(video_path: Path, duration: float) -> dict[str, Any]:
    """Decode the full video stream and measure whether it is almost entirely black."""
    ffmpeg = _require_tool("ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-vf",
        "blackdetect=d=0.10:pix_th=0.10",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    if result.returncode != 0:
        raise MediaToolError("VIDEO_DECODE_FAILED", "FFmpeg could not decode the complete video stream.", result.stderr)
    black_duration = sum(float(value) for value in re.findall(r"black_duration:([0-9]+(?:\.[0-9]+)?)", result.stderr or ""))
    black_ratio = min(1.0, black_duration / duration) if duration > 0 else 1.0
    return {
        "decode_succeeded": True,
        "black_duration": round(black_duration, 6),
        "black_ratio": round(black_ratio, 6),
    }


def create_xmax_input_proxy(input_path: Path, output_path: Path, start_time: float, end_time: float) -> dict[str, Any]:
    """Create an independent, video-only Xmax input proxy with a local timeline."""
    if not input_path.is_file() or end_time <= start_time:
        raise MediaToolError("VIDEO_PROXY_RANGE_INVALID", "The Xmax proxy range is invalid.")
    metadata = probe_video_metadata(input_path)
    start = max(0.0, float(start_time))
    end = min(float(end_time), float(metadata["duration"]))
    duration = end - start
    if duration <= 0:
        raise MediaToolError("VIDEO_PROXY_RANGE_INVALID", "The Xmax proxy duration is invalid.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(input_path), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-movflags", "+faststart", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False)
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError("VIDEO_PROXY_FAILED", "Unable to create the Xmax input proxy.", result.stderr)
    proxy_metadata = probe_video_metadata(output_path)
    if proxy_metadata.get("has_audio"):
        raise MediaToolError("VIDEO_PROXY_HAS_AUDIO", "The Xmax input proxy unexpectedly contains audio.")
    return {"path": str(output_path), "command": command, **proxy_metadata}


def build_shot_ranges(duration: float, cuts: list[float] | None = None, *, target_seconds: float = 10.0, max_seconds: float = 15.0) -> list[tuple[float, float]]:
    """Build bounded shot ranges while avoiding tiny isolated clips."""
    if duration <= 0:
        raise MediaToolError("VIDEO_DURATION_INVALID", "The source video duration is invalid.")
    normalized_cuts = sorted({max(0.0, min(duration, float(cut))) for cut in (cuts or []) if 0 < float(cut) < duration})
    ranges: list[tuple[float, float]] = []
    start = 0.0
    for cut in normalized_cuts:
        gap = cut - start
        if gap < 5.0:
            continue
        while gap > max_seconds:
            boundary = min(start + target_seconds, duration)
            ranges.append((start, boundary))
            start = boundary
            gap = cut - start
        if gap >= 5.0:
            ranges.append((start, cut))
            start = cut
    while duration - start > max_seconds:
        boundary = min(start + target_seconds, duration)
        ranges.append((start, boundary))
        start = boundary
    if duration - start > 0.001:
        ranges.append((start, duration))
    if len(ranges) > 1 and ranges[-1][1] - ranges[-1][0] < 2.0:
        previous_start, _previous_end = ranges[-2]
        ranges[-2] = (previous_start, ranges[-1][1])
        ranges.pop()
    return [(round(start_time, 3), round(end_time, 3)) for start_time, end_time in ranges]


def concat_videos(input_paths: list[Path], output_path: Path) -> dict[str, Any]:
    """Join provider outputs with the same codec/container into one local result."""
    if not input_paths:
        raise MediaToolError("VIDEO_CONCAT_EMPTY", "No transformed segments are available to join.")
    if len(input_paths) == 1:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_paths[0], output_path)
        return {"path": str(output_path), **probe_video_metadata(output_path)}
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = output_path.with_suffix(".concat.txt")
    list_path.write_text("\n".join(f"file '{path.resolve().as_posix().replace(chr(39), chr(39) + chr(39))}'" for path in input_paths), encoding="utf-8")
    result = subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(output_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, check=False)
    try:
        list_path.unlink(missing_ok=True)
    except OSError:
        pass
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("VIDEO_CONCAT_FAILED", "Unable to join transformed video segments.", result.stderr)
    return {"path": str(output_path), **probe_video_metadata(output_path)}


def merge_video_with_original_audio(source_path: Path, transformed_paths: list[Path], output_path: Path) -> dict[str, Any]:
    """Normalize browser-recorded clips, concatenate them, and restore source audio."""
    if not source_path.is_file():
        raise MediaToolError("VIDEO_FILE_NOT_FOUND", "The source video file does not exist.")
    if not transformed_paths or any(not path.is_file() or path.stat().st_size <= 0 for path in transformed_paths):
        raise MediaToolError("XMAX_OUTPUT_MISSING", "One or more transformed shot files are missing or empty.")
    source_metadata = probe_video_metadata(source_path)
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = int(source_metadata["width"])
    height = int(source_metadata["height"])
    fps = max(1, round(float(source_metadata.get("fps") or 30)))
    command = [ffmpeg, "-y"]
    for path in transformed_paths:
        command.extend(["-i", str(path)])
    source_index = len(transformed_paths)
    command.extend(["-i", str(source_path)])
    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(transformed_paths)):
        label = f"v{index}"
        filters.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[video]")
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[video]", "-map", f"{source_index}:a:0?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", f"{float(source_metadata['duration']):.3f}",
        "-movflags", "+faststart", str(output_path),
    ])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, check=False)
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError("XMAX_MERGE_FAILED", "Unable to merge transformed shots with the original audio.", result.stderr)
    metadata = probe_video_metadata(output_path)
    if abs(float(metadata["duration"]) - float(source_metadata["duration"])) > 0.5:
        raise MediaToolError("XMAX_DURATION_MISMATCH", "The merged video duration differs from the source by more than 0.5 seconds.")
    return {"path": str(output_path), "command": command, "stderr": result.stderr, **metadata}


def probe_media(video_path: Path) -> dict[str, Any]:
    ffprobe = _require_tool("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError(
            "FFPROBE_FAILED",
            "Unable to inspect the uploaded video.",
            result.stderr,
            details={"resolved_path": ffprobe, "stderr": result.stderr, "current_working_directory": str(Path.cwd())},
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MediaToolError(
            "FFPROBE_INVALID_OUTPUT",
            "ffprobe returned invalid JSON.",
            result.stderr,
            details={"resolved_path": ffprobe, "stderr": result.stderr, "stdout": result.stdout, "current_working_directory": str(Path.cwd())},
        ) from error
    streams = payload.get("streams", [])
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    normalized = []
    for stream in subtitle_streams:
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        normalized.append({
            "index": int(stream.get("index", 0)),
            "codec_name": stream.get("codec_name"),
            "language": tags.get("language"),
            "title": tags.get("title"),
            "default": bool(disposition.get("default")),
            "metadata": stream,
        })
    return {
        "subtitle_source_type": "embedded_soft_subtitle" if normalized else "unknown",
        "subtitle_stream_count": len(normalized),
        "subtitle_streams": normalized,
        "audio_stream_count": len(audio_streams),
        "message": None if normalized else "No independent subtitle track was found. The video may have no subtitles or may contain burned-in subtitles.",
        "probe": payload,
    }


def extract_audio(video_path: Path, output_path: Path) -> dict[str, Any]:
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(output_path),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False
    )
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("AUDIO_EXTRACTION_FAILED", "Unable to extract source audio.", result.stderr)
    return {"path": str(output_path), "stderr": result.stderr, "command": command}


def wav_duration_seconds(audio_path: Path) -> float:
    if not audio_path.is_file():
        raise MediaToolError("AUDIO_FILE_NOT_FOUND", "The extracted audio file does not exist.", str(audio_path))
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_rate = audio.getframerate()
            if frame_rate <= 0:
                raise MediaToolError("AUDIO_FORMAT_INVALID", "The WAV audio has an invalid sample rate.")
            return audio.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error) as error:
        raise MediaToolError("AUDIO_FORMAT_INVALID", "The extracted WAV audio is invalid.", str(error)) from error


def media_duration_seconds(media_path: Path) -> float:
    ffprobe = _require_tool("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(media_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    if result.returncode != 0:
        raise MediaToolError("FFPROBE_FAILED", "Unable to read media duration.", result.stderr)
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise MediaToolError("FFPROBE_INVALID_OUTPUT", "ffprobe did not return a valid media duration.", result.stdout) from error
    if duration <= 0:
        raise MediaToolError("AUDIO_DURATION_INVALID", "The generated audio duration is invalid.")
    return duration


def align_tts_audio(
    input_path: Path,
    output_path: Path,
    target_duration: float,
    *,
    minimum_ratio: float = 0.85,
    maximum_ratio: float = 1.35,
    silence_trim_candidate_path: Path | None = None,
) -> dict[str, Any]:
    if target_duration <= 0:
        raise MediaToolError("TTS_TARGET_DURATION_INVALID", "The subtitle segment duration is invalid.")
    alignment_input_path = input_path
    silence_trim = None
    if silence_trim_candidate_path is not None:
        silence_trim = create_tts_silence_trim_candidate(input_path, silence_trim_candidate_path)
        alignment_input_path = silence_trim_candidate_path
    generated_duration = media_duration_seconds(alignment_input_path)
    duration_ratio = generated_duration / target_duration
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if generated_duration <= target_duration:
        shutil.copy2(alignment_input_path, output_path)
        return {
            "generated_duration": generated_duration,
            "target_duration": target_duration,
            "aligned_duration": generated_duration,
            "duration_ratio": duration_ratio,
            "speed_adjustment": 1.0,
            "duration_warning": False,
            "needs_regeneration": False,
            "direct_use": True,
            "path": str(output_path),
            "alignment_input_path": str(alignment_input_path),
            "silence_trim": silence_trim,
            "stderr": "",
            "command": [],
        }
    capped_speed_warning = duration_ratio > maximum_ratio
    applied_ratio = maximum_ratio if capped_speed_warning else duration_ratio
    atempo_parts: list[str] = []
    remaining = applied_ratio
    while remaining > 2.0:
        atempo_parts.append("atempo=2.000000")
        remaining /= 2.0
    while remaining < 0.5:
        atempo_parts.append("atempo=0.500000")
        remaining /= 0.5
    atempo_parts.append(f"atempo={remaining:.6f}")
    atempo_chain = ",".join(atempo_parts)
    ffmpeg = _require_tool("ffmpeg")
    audio_filter = atempo_chain
    if not capped_speed_warning:
        audio_filter += f",apad=pad_dur={target_duration:.6f},atrim=0:{target_duration:.6f}"
    speed_adjustment = applied_ratio
    command = [
        ffmpeg, "-y", "-i", str(alignment_input_path), "-vn", "-af", audio_filter,
        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(output_path),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("TTS_ALIGNMENT_FAILED", "Unable to align the generated voice segment.", result.stderr)
    aligned_duration = media_duration_seconds(output_path)
    return {
        "generated_duration": generated_duration,
        "target_duration": target_duration,
        "aligned_duration": aligned_duration,
        "duration_ratio": duration_ratio,
        "speed_adjustment": speed_adjustment,
        "duration_warning": capped_speed_warning,
        "needs_regeneration": False,
        "capped_speed_warning": capped_speed_warning,
        "slot_overrun_seconds": max(0.0, aligned_duration - target_duration),
        "atempo_chain": atempo_chain,
        "path": str(output_path),
        "alignment_input_path": str(alignment_input_path),
        "silence_trim": silence_trim,
        "stderr": result.stderr,
        "command": command,
    }


def create_tts_silence_trim_candidate(
    input_path: Path,
    output_path: Path,
    *,
    window_ms: int = TTS_SILENCE_WINDOW_MS,
    threshold_dbfs: float = TTS_SILENCE_THRESHOLD_DBFS,
    guard_ms: int = TTS_SILENCE_GUARD_MS,
) -> dict[str, Any]:
    """Trim only quiet WAV boundaries using the audited PCM peak-window rule."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise MediaToolError("TTS_AUDIO_MISSING", "The original TTS WAV is missing.")
    if window_ms <= 0 or guard_ms < 0 or not math.isfinite(threshold_dbfs):
        raise MediaToolError("TTS_SILENCE_TRIM_CONFIG_INVALID", "The silence trim settings are invalid.")

    try:
        with wave.open(str(input_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            compression = source.getcomptype()
            total_frames = source.getnframes()
            pcm_bytes = source.readframes(total_frames)
    except (wave.Error, OSError) as error:
        raise MediaToolError("TTS_AUDIO_FORMAT_INVALID", "The original TTS WAV is unreadable.", str(error)) from error

    if channels <= 0 or sample_width != 2 or sample_rate <= 0 or compression != "NONE" or total_frames <= 0:
        raise MediaToolError(
            "TTS_AUDIO_FORMAT_INVALID",
            "Silence trimming requires a non-empty 16-bit PCM WAV.",
            details={
                "channels": channels,
                "sample_width": sample_width,
                "sample_rate": sample_rate,
                "compression": compression,
                "total_frames": total_frames,
            },
        )

    samples = array("h")
    samples.frombytes(pcm_bytes)
    if len(samples) != total_frames * channels:
        raise MediaToolError("TTS_AUDIO_FORMAT_INVALID", "The original TTS WAV frame data is incomplete.")

    window_frames = max(1, round(sample_rate * window_ms / 1000))
    threshold_amplitude = 32768.0 * (10.0 ** (threshold_dbfs / 20.0))
    first_active_frame: int | None = None
    last_active_frame: int | None = None
    for window_start in range(0, total_frames, window_frames):
        window_end = min(total_frames, window_start + window_frames)
        sample_start = window_start * channels
        sample_end = window_end * channels
        peak = max(abs(value) for value in samples[sample_start:sample_end])
        if peak >= threshold_amplitude:
            if first_active_frame is None:
                first_active_frame = window_start
            last_active_frame = window_end

    if first_active_frame is None or last_active_frame is None:
        raise MediaToolError("TTS_AUDIO_SILENT", "The original TTS WAV contains no detected speech samples.")

    guard_frames = round(sample_rate * guard_ms / 1000)
    trim_start_frame = max(0, first_active_frame - guard_frames)
    trim_end_frame = min(total_frames, last_active_frame + guard_frames)
    if trim_start_frame >= trim_end_frame:
        raise MediaToolError("TTS_SILENCE_TRIM_INVALID", "The silence trim candidate has an invalid frame range.")

    frame_size = channels * sample_width
    candidate_bytes = pcm_bytes[trim_start_frame * frame_size:trim_end_frame * frame_size]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reused = output_path.is_file()
    if not reused:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output_path.stem}-",
                suffix=".tmp.wav",
                dir=str(output_path.parent),
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
            with wave.open(temporary_name, "wb") as candidate:
                candidate.setnchannels(channels)
                candidate.setsampwidth(sample_width)
                candidate.setframerate(sample_rate)
                candidate.writeframes(candidate_bytes)
            os.replace(temporary_name, output_path)
            temporary_name = None
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    try:
        with wave.open(str(output_path), "rb") as candidate:
            candidate_channels = candidate.getnchannels()
            candidate_width = candidate.getsampwidth()
            candidate_rate = candidate.getframerate()
            candidate_frames = candidate.getnframes()
            candidate_compression = candidate.getcomptype()
    except (wave.Error, OSError) as error:
        raise MediaToolError("TTS_SILENCE_TRIM_INVALID", "The silence trim candidate is unreadable.", str(error)) from error

    expected_frames = trim_end_frame - trim_start_frame
    if (
        candidate_channels != channels
        or candidate_width != sample_width
        or candidate_rate != sample_rate
        or candidate_frames != expected_frames
        or candidate_compression != "NONE"
    ):
        raise MediaToolError("TTS_SILENCE_TRIM_INVALID", "The silence trim candidate does not match the audited frame range.")

    return {
        "source_path": str(input_path),
        "candidate_path": str(output_path),
        "source_duration": total_frames / sample_rate,
        "candidate_duration": candidate_frames / sample_rate,
        "active_start": first_active_frame / sample_rate,
        "active_end": last_active_frame / sample_rate,
        "active_duration": (last_active_frame - first_active_frame) / sample_rate,
        "trim_start": trim_start_frame / sample_rate,
        "trim_end": trim_end_frame / sample_rate,
        "leading_trimmed": trim_start_frame / sample_rate,
        "trailing_trimmed": (total_frames - trim_end_frame) / sample_rate,
        "window_ms": window_ms,
        "threshold_dbfs": threshold_dbfs,
        "threshold_amplitude": threshold_amplitude,
        "guard_ms": guard_ms,
        "guard_frames": guard_frames,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "source_frames": total_frames,
        "candidate_frames": candidate_frames,
        "first_active_frame": first_active_frame,
        "last_active_frame": last_active_frame,
        "trim_start_frame": trim_start_frame,
        "trim_end_frame": trim_end_frame,
        "reused": reused,
    }


def fit_tts_audio_to_slot(
    input_path: Path,
    output_path: Path,
    usable_duration: float,
    *,
    maximum_speed: float = 1.10,
) -> dict[str, Any]:
    """Decode TTS to WAV and apply only the speed needed to fit its own slot."""
    if usable_duration <= 0:
        raise MediaToolError("TTS_TARGET_DURATION_INVALID", "The dialogue slot duration is invalid.")
    if maximum_speed < 1 or maximum_speed > 2:
        raise MediaToolError("TTS_SPEED_LIMIT_INVALID", "The TTS speed limit is invalid.")
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise MediaToolError("DUBBING_AUDIO_MISSING", "The generated voice segment file is missing.", str(input_path))

    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    original_duration = media_duration_seconds(input_path)

    def render(speed: float) -> tuple[list[str], subprocess.CompletedProcess[str]]:
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-af",
            f"aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,atempo={speed:.6f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
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
            raise MediaToolError("TTS_ALIGNMENT_FAILED", "Unable to prepare the voice segment for its timeline slot.", result.stderr)
        return command, result

    command, result = render(1.0)
    decoded_duration = media_duration_seconds(output_path)
    if decoded_duration <= usable_duration + 0.001:
        return {
            "path": str(output_path),
            "original_duration": original_duration,
            "decoded_duration": decoded_duration,
            "final_duration": decoded_duration,
            "usable_duration": usable_duration,
            "speed_adjustment": 1.0,
            "duration_ratio": decoded_duration / usable_duration,
            "fit": True,
            "command": command,
            "stderr": result.stderr,
        }

    duration_ratio = decoded_duration / usable_duration
    capped_speed_warning = duration_ratio > maximum_speed + 0.000001
    required_speed = (
        maximum_speed
        if capped_speed_warning
        else min(maximum_speed, duration_ratio * 1.002)
    )
    command, result = render(required_speed)
    final_duration = media_duration_seconds(output_path)
    fit = capped_speed_warning or final_duration <= usable_duration + 0.005
    if not fit:
        output_path.unlink(missing_ok=True)
    return {
        "path": str(output_path) if fit else None,
        "original_duration": original_duration,
        "decoded_duration": decoded_duration,
        "final_duration": final_duration,
        "usable_duration": usable_duration,
        "speed_adjustment": required_speed,
        "duration_ratio": decoded_duration / usable_duration,
        "required_speed": duration_ratio if capped_speed_warning else required_speed,
        "capped_speed_warning": capped_speed_warning,
        "slot_overrun_seconds": max(0.0, final_duration - usable_duration),
        "fit": fit,
        "command": command,
        "stderr": result.stderr,
    }


def _validate_audio_output(output_path: Path, error_code: str, error_message: str, stderr: str = "") -> dict[str, Any]:
    output_size = output_path.stat().st_size if output_path.is_file() else 0
    if output_size <= 0:
        raise MediaToolError(error_code, error_message, stderr)
    output_duration = media_duration_seconds(output_path)
    if output_duration <= 0:
        raise MediaToolError(error_code, "The generated audio has no readable duration.", stderr)
    return {"path": str(output_path), "duration": output_duration, "size_bytes": output_size}


def render_original_ducked_audio(
    video_path: Path,
    segments: list[dict[str, Any]],
    output_path: Path,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not segments:
        raise MediaToolError("DUBBING_SEGMENTS_REQUIRED", "No dialogue intervals are available for ducking.")
    ffmpeg = _require_tool("ffmpeg")
    video_duration = media_duration_seconds(video_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dialogue_volume = max(0.0, min(1.0, float(settings.get("dialogue_original_volume", 0.05))))
    non_dialogue_volume = max(0.0, min(1.0, float(settings.get("non_dialogue_original_volume", 0.95))))
    ordered_segments = sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    intervals = [
        f"between(t,{max(0.0, float(item['start_ms']) / 1000):.3f},{max(0.0, float(item['end_ms']) / 1000):.3f})"
        for item in ordered_segments
    ]
    forced_silence_intervals = [
        f"between(t,{max(0.0, float(item['start_ms']) / 1000):.3f},{max(0.0, float(item['end_ms']) / 1000):.3f})"
        for item in ordered_segments
        if bool(item.get("force_silence"))
    ]
    active_expression = "+".join(intervals) or "0"
    forced_silence_expression = "+".join(forced_silence_intervals) or "0"
    filter_graph = (
        "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS,"
        f"volume='if(gt({forced_silence_expression},0),0,"
        f"if(gt({active_expression},0),{dialogue_volume:.4f},{non_dialogue_volume:.4f}))':eval=frame,"
        f"atrim=0:{video_duration:.6f}[ducked]"
    )
    command = [
        ffmpeg, "-y", "-i", str(video_path), "-filter_complex", filter_graph, "-map", "[ducked]",
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(output_path),
    ]
    logger.info(
        "A2 original ducked intervals=%s forced_silence=%s dialogue_volume=%.3f non_dialogue_volume=%.3f output=%s",
        len(intervals), len(forced_silence_intervals), dialogue_volume, non_dialogue_volume, output_path,
    )
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False)
    logger.info("A2 original ducked ffmpeg exit_code=%s output=%s", result.returncode, output_path)
    if result.returncode != 0:
        raise MediaToolError("DUBBING_DUCKED_AUDIO_FAILED", "Unable to create the ducked original audio.", result.stderr)
    validated = _validate_audio_output(output_path, "DUBBING_DUCKED_AUDIO_FAILED", "The ducked original audio is invalid.", result.stderr)
    return {
        **validated,
        "interval_count": len(intervals),
        "forced_silence_count": len(forced_silence_intervals),
        "command": command,
    }


def mix_dubbing_audio(
    ducked_audio_path: Path,
    tts_only_path: Path,
    output_path: Path,
    duration: float,
    settings: dict[str, Any],
) -> dict[str, Any]:
    if not ducked_audio_path.is_file() or ducked_audio_path.stat().st_size <= 0:
        raise MediaToolError("DUBBING_DUCKED_AUDIO_MISSING", "The ducked original audio is missing.", str(ducked_audio_path))
    if not tts_only_path.is_file() or tts_only_path.stat().st_size <= 0:
        raise MediaToolError("DUBBING_TTS_ONLY_MISSING", "The complete TTS timeline is missing.", str(tts_only_path))
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    dub_volume = max(0.0, min(2.0, float(settings.get("dub_voice_volume", 1.9))))
    filter_graph = (
        "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"asetpts=PTS-STARTPTS,atrim=0:{duration:.6f}[ducked];"
        "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"asetpts=PTS-STARTPTS,volume={dub_volume:.4f},atrim=0:{duration:.6f}[tts];"
        f"[ducked][tts]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95,atrim=0:{duration:.6f}[mix]"
    )
    audio_codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if output_path.suffix.lower() == ".mp3" else ["-c:a", "pcm_s16le"]
    command = [
        ffmpeg, "-y", "-i", str(ducked_audio_path), "-i", str(tts_only_path),
        "-filter_complex", filter_graph, "-map", "[mix]", *audio_codec, "-ar", "44100", "-ac", "2", str(output_path),
    ]
    logger.info(
        "A2 two-track mix amix_inputs=2 normalize=0 ducked=%s tts_only=%s output=%s",
        ducked_audio_path, tts_only_path, output_path,
    )
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False)
    logger.info("A2 two-track mix ffmpeg exit_code=%s output=%s", result.returncode, output_path)
    if result.returncode != 0:
        raise MediaToolError("DUBBING_MIX_FAILED", "Unable to create the two-track dubbing mix.", result.stderr)
    validated = _validate_audio_output(output_path, "DUBBING_MIX_FAILED", "The two-track mix is invalid.", result.stderr)
    return {**validated, "amix_inputs": 2, "normalize": 0, "command": command}


def extract_localization_audio(video_path: Path, output_path: Path, duration: float) -> dict[str, Any]:
    """Extract a delivery-quality source track, or create silence when the video has no audio."""
    if duration <= 0:
        raise MediaToolError("LOCALIZATION_DURATION_INVALID", "The localization source duration is invalid.")
    metadata = probe_media_metadata(video_path)
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if metadata["has_audio"]:
        command = [
            ffmpeg, "-y", "-i", str(video_path), "-map", "0:a:0", "-vn",
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(output_path),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1200, check=False,
        )
        if result.returncode != 0:
            raise MediaToolError("LOCALIZATION_AUDIO_EXTRACTION_FAILED", "Unable to prepare the original audio track.", result.stderr)
        stderr = result.stderr
    else:
        command = ["python-wave-silence", "44100", "stereo", f"{duration:.6f}", str(output_path)]
        frame_count = max(1, round(duration * 44100))
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            remaining = frame_count
            silent_frame = b"\x00\x00\x00\x00"
            while remaining:
                count = min(remaining, 44100)
                output.writeframesraw(silent_frame * count)
                remaining -= count
        stderr = ""
    validated = _validate_audio_output(
        output_path, "LOCALIZATION_AUDIO_EXTRACTION_FAILED", "The prepared original audio track is invalid.", stderr,
    )
    return {**validated, "source_had_audio": bool(metadata["has_audio"]), "command": command}


def probe_audio_metadata(audio_path: Path) -> dict[str, Any]:
    """Read the duration and stream format of an audio-only asset."""
    ffprobe = _require_tool("ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(audio_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError("AUDIO_PROBE_FAILED", "Unable to inspect the audio asset.", result.stderr)
    try:
        payload = json.loads(result.stdout)
        stream = next(item for item in payload.get("streams", []) if item.get("codec_type") == "audio")
        duration = float((payload.get("format") or {}).get("duration") or stream.get("duration") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MediaToolError("AUDIO_METADATA_INVALID", "The audio metadata is incomplete or invalid.", result.stdout) from error
    if duration <= 0 or sample_rate <= 0 or channels <= 0:
        raise MediaToolError("AUDIO_METADATA_INVALID", "The audio metadata is incomplete or invalid.", result.stdout)
    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": stream.get("codec_name"),
        "size_bytes": audio_path.stat().st_size if audio_path.is_file() else 0,
    }


def extract_dialogue_separation_audio(video_path: Path, output_path: Path, duration: float) -> dict[str, Any]:
    """Extract the Episode source audio for the isolated Demucs worker."""
    if duration <= 0:
        raise MediaToolError("LOCALIZATION_DURATION_INVALID", "The localization source duration is invalid.")
    metadata = probe_media_metadata(video_path)
    if not metadata["has_audio"]:
        raise MediaToolError(
            "LOCALIZATION_SOURCE_AUDIO_MISSING",
            "The Episode source has no audio track to separate.",
        )
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target_samples = max(1, round(duration * 48000))
    filter_graph = (
        f"aresample=48000,asetpts=N/SR/TB,apad=pad_len={target_samples},"
        f"atrim=end_sample={target_samples},asetpts=N/SR/TB"
    )
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        filter_graph,
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError(
            "LOCALIZATION_AUDIO_EXTRACTION_FAILED",
            "Unable to extract the Episode source audio for dialogue separation.",
            result.stderr,
        )
    audio = probe_audio_metadata(output_path)
    if abs(float(audio["duration"]) - duration) > 0.1:
        raise MediaToolError(
            "LOCALIZATION_AUDIO_DURATION_MISMATCH",
            "The extracted Episode audio does not match the visual duration.",
            details={"expected_duration": duration, "actual_duration": audio["duration"]},
        )
    return {**audio, "source_had_audio": True, "command": command}


def align_localization_audio(input_path: Path, output_path: Path, duration: float) -> dict[str, Any]:
    """Resample and pad a completed local audio asset to the final timeline."""
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise MediaToolError("LOCALIZATION_AUDIO_ASSET_MISSING", "A required localization audio asset is missing.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-af",
        f"aresample=48000,apad,atrim=0:{duration:.6f},asetpts=PTS-STARTPTS",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError("LOCALIZATION_AUDIO_ALIGNMENT_FAILED", "Unable to align the localization audio asset.", result.stderr)
    metadata = probe_audio_metadata(output_path)
    if abs(float(metadata["duration"]) - duration) > 0.1:
        raise MediaToolError(
            "LOCALIZATION_AUDIO_DURATION_MISMATCH",
            "The aligned localization audio does not match the final timeline.",
        )
    return {**metadata, "command": command}


def measure_audio_peak_db(audio_path: Path) -> float:
    ffmpeg = _require_tool("ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-i", str(audio_path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?|inf)) dB", result.stderr)
    if not match:
        raise MediaToolError("LOCALIZATION_PEAK_MEASUREMENT_FAILED", "Unable to measure the final audio peak.", result.stderr)
    return float(match.group(1))


def mix_demucs_localization_audio(
    no_vocals_path: Path,
    tts_timeline_path: Path,
    output_path: Path,
    duration: float,
    *,
    vocals_path: Path | None = None,
    dialogue_segments: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mix Demucs stems with the existing target-language TTS timeline."""
    for path, code in (
        (no_vocals_path, "LOCALIZATION_ACCOMPANIMENT_MISSING"),
        (tts_timeline_path, "LOCALIZATION_TTS_MISSING"),
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            raise MediaToolError(code, "A required localization audio asset is missing.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    use_product_mix = bool(settings is not None and vocals_path is not None and vocals_path.is_file())
    if use_product_mix:
        dialogue_volume = max(0.0, min(1.0, float(settings.get("dialogue_original_volume", 0.05))))
        non_dialogue_volume = max(0.0, min(1.0, float(settings.get("non_dialogue_original_volume", 0.95))))
        dub_volume = max(0.0, min(2.0, float(settings.get("dub_voice_volume", 1.9))))
        intervals = [
            f"between(t,{max(0.0, float(item['start_ms']) / 1000):.3f},{max(0.0, float(item['end_ms']) / 1000):.3f})"
            for item in sorted(dialogue_segments or [], key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
        ]
        active_expression = "+".join(intervals) or "0"
        original_volume = f"if(gt({active_expression},0),{dialogue_volume:.4f},{non_dialogue_volume:.4f})"
        filter_graph = (
            "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,volume='{original_volume}':eval=frame,atrim=0:{duration:.6f}[accompaniment];"
            "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,volume='{original_volume}':eval=frame,atrim=0:{duration:.6f}[original_vocals];"
            "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,volume={dub_volume:.4f},atrim=0:{duration:.6f}[voice];"
            "[accompaniment][original_vocals][voice]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.93:level=disabled,atrim=0:{duration:.6f}[localized]"
        )
        inputs = ["-i", str(no_vocals_path), "-i", str(vocals_path), "-i", str(tts_timeline_path)]
        amix_inputs = 3
    else:
        filter_graph = (
            "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,atrim=0:{duration:.6f}[accompaniment];"
            "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,apad,atrim=0:{duration:.6f}[voice];"
            "[accompaniment][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.93:level=disabled,atrim=0:{duration:.6f}[localized]"
        )
        inputs = ["-i", str(no_vocals_path), "-i", str(tts_timeline_path)]
        amix_inputs = 2
    command = [
        ffmpeg,
        "-y",
        *inputs,
        "-filter_complex",
        filter_graph,
        "-map",
        "[localized]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    if result.returncode != 0:
        raise MediaToolError(
            "LOCALIZATION_AUDIO_MIX_FAILED",
            "Unable to mix the separated accompaniment with the target-language TTS timeline.",
            result.stderr,
        )
    metadata = probe_audio_metadata(output_path)
    peak_db = measure_audio_peak_db(output_path)
    if abs(float(metadata["duration"]) - duration) > 0.1:
        raise MediaToolError("LOCALIZATION_AUDIO_DURATION_MISMATCH", "The final localized mix has the wrong duration.")
    if peak_db > -0.5:
        raise MediaToolError(
            "LOCALIZATION_AUDIO_PEAK_INVALID",
            "The final localized mix exceeds the -0.5 dB peak limit.",
            details={"peak_db": peak_db},
        )
    return {
        **metadata,
        "peak_db": peak_db,
        "amix_inputs": amix_inputs,
        "normalize": 0,
        "limiter": 0.93,
        "command": command,
        "mix_settings": dict(settings) if use_product_mix else None,
        "dialogue_interval_count": len(dialogue_segments or []) if use_product_mix else 0,
    }


def mix_localization_audio(
    original_audio_path: Path,
    tts_only_path: Path,
    output_path: Path,
    duration: float,
    *,
    keep_original_audio: bool,
    original_volume: float,
    voice_volume: float,
) -> dict[str, Any]:
    """Mix an existing TTS timeline over the source track without issuing Provider requests."""
    if not original_audio_path.is_file() or original_audio_path.stat().st_size <= 0:
        raise MediaToolError("LOCALIZATION_ORIGINAL_AUDIO_MISSING", "The prepared original audio track is missing.")
    if not tts_only_path.is_file() or tts_only_path.stat().st_size <= 0:
        raise MediaToolError("LOCALIZATION_TTS_MISSING", "The completed target-language TTS timeline is missing.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_original_volume = max(0.0, min(1.0, float(original_volume)))
    normalized_voice_volume = max(0.0, min(2.0, float(voice_volume)))
    command = [ffmpeg, "-y", "-i", str(original_audio_path), "-i", str(tts_only_path)]
    if keep_original_audio:
        filter_graph = (
            "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,volume={normalized_original_volume:.4f},apad,atrim=0:{duration:.6f}[original];"
            "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,volume={normalized_voice_volume:.4f},apad,atrim=0:{duration:.6f}[voice];"
            "[original][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95,atrim=0:{duration:.6f}[localized]"
        )
        mix_inputs = 2
    else:
        filter_graph = (
            "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,volume={normalized_voice_volume:.4f},apad,"
            f"atrim=0:{duration:.6f},alimiter=limit=0.95[localized]"
        )
        mix_inputs = 1
    command.extend([
        "-filter_complex", filter_graph, "-map", "[localized]",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(output_path),
    ])
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1200, check=False,
    )
    if result.returncode != 0:
        raise MediaToolError("LOCALIZATION_AUDIO_MIX_FAILED", "Unable to mix the localized audio track.", result.stderr)
    validated = _validate_audio_output(
        output_path, "LOCALIZATION_AUDIO_MIX_FAILED", "The localized audio track is invalid.", result.stderr,
    )
    return {
        **validated,
        "keep_original_audio": keep_original_audio,
        "original_volume": normalized_original_volume,
        "voice_volume": normalized_voice_volume,
        "amix_inputs": mix_inputs,
        "normalize": 0,
        "command": command,
    }


def render_localized_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    subtitle_path: Path | None,
    subtitle_overlays: list[dict[str, Any]] | None = None,
    *,
    hide_original_subtitles: bool = False,
    original_subtitle_region_start_percent: float = 85.5,
    original_subtitle_region_height_percent: float = 14.5,
) -> dict[str, Any]:
    """Mux localized audio and optional timed raster subtitles."""
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_metadata = probe_media_metadata(video_path)
    width = int(source_metadata["width"])
    height = int(source_metadata["height"])
    command = [ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path)]
    cwd = None
    overlays = subtitle_overlays or []
    needs_video_filter = bool(overlays or hide_original_subtitles)
    mask_details: dict[str, Any] | None = None
    if needs_video_filter:
        for overlay in overlays:
            command.extend(["-loop", "1", "-i", str(overlay["path"])])
        filters: list[str] = []
        if hide_original_subtitles:
            kept_height = max(
                2,
                min(
                    height - (height % 2),
                    (int(height * original_subtitle_region_start_percent / 100) // 2) * 2,
                ),
            )
            scaled_width = max(width, round((width * height / kept_height) / 2) * 2)
            center_crop_x = max(0, (scaled_width - width) // 2)
            filters.append(
                f"[0:v]setpts=PTS-STARTPTS,crop=w={width}:h={kept_height}:x=0:y=0,"
                f"scale=w=-2:h={height}:flags=lanczos,"
                f"crop=w={width}:h={height}:x=(iw-{width})/2:y=0,"
                "setsar=1,format=yuv420p[localized_visual]"
            )
            current = "localized_visual"
            mask_details = {
                "method": "crop_and_scale",
                "start_percent": float(original_subtitle_region_start_percent),
                "height_percent": float(original_subtitle_region_height_percent),
                "kept_height": kept_height,
                "removed_height": height - kept_height,
                "removed_bottom_percent": round((height - kept_height) * 100 / height, 4),
                "scale_width": scaled_width,
                "scale_height": height,
                "center_crop_x": center_crop_x,
                "output_width": width,
                "output_height": height,
                "preserves_aspect_ratio": True,
            }
        else:
            filters.append("[0:v]setpts=PTS-STARTPTS[localized_visual]")
            current = "localized_visual"
        for index, overlay in enumerate(overlays):
            next_label = f"v{index + 1}"
            start = max(0.0, float(overlay["start_ms"]) / 1000)
            end = max(start, float(overlay["end_ms"]) / 1000)
            filters.append(
                f"[{current}][{index + 2}:v]overlay=0:0:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{next_label}]"
            )
            current = next_label
        command.extend([
            "-filter_complex", ";".join(filters), "-map", f"[{current}]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        ])
    elif subtitle_path:
        cwd = subtitle_path.parent
        command.extend([
            "-vf", f"ass={subtitle_path.name}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-map", "0:v:0", "-map", "1:a:0",
        ])
    else:
        command.extend(["-c:v", "copy", "-map", "0:v:0", "-map", "1:a:0"])
    command.extend([
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{media_duration_seconds(video_path):.6f}", "-movflags", "+faststart", str(output_path),
    ])
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800, check=False,
    )
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        raise MediaToolError("LOCALIZATION_RENDER_FAILED", "Unable to render the localized MP4.", result.stderr)
    metadata = probe_media_metadata(output_path)
    return {
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "subtitles_rendered": bool(subtitle_path or overlays),
        "subtitle_overlay_count": len(overlays),
        "original_subtitles_hidden": hide_original_subtitles,
        "subtitle_mask": mask_details,
        "command": command,
        **metadata,
    }


def render_tts_only_audio(
    segments: list[dict[str, Any]],
    output_path: Path,
    duration: float,
    *,
    fade_ms: int = 10,
) -> dict[str, Any]:
    if not segments:
        raise MediaToolError("DUBBING_SEGMENTS_REQUIRED", "No TTS segments are available for the diagnostic mix.")
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ordered_segments = sorted(segments, key=lambda item: (int(item["start_ms"]), int(item.get("seq") or 0)))
    inputs = [ffmpeg, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    fade_seconds = max(0.0, min(0.03, float(fade_ms) / 1000))
    for index, item in enumerate(ordered_segments):
        audio_path = Path(str(item["audio_path"]))
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise MediaToolError("DUBBING_AUDIO_MISSING", "A generated voice segment file is missing.", str(audio_path))
        audio_duration = media_duration_seconds(audio_path)
        slot_duration = max(0.0, (int(item["end_ms"]) - int(item["start_ms"])) / 1000)
        if audio_duration > slot_duration + 0.005 and not bool(item.get("allow_slot_overrun")):
            raise MediaToolError(
                "TTS_DURATION_EXCEEDED",
                f"Voice segment {item.get('segment_id') or item.get('seq')} exceeds its original dialogue slot.",
            )
        delay = max(0, int(item["start_ms"]))
        inputs.extend(["-i", str(audio_path)])
        label = f"tts{index + 1}"
        fade_filter = ""
        if fade_seconds > 0 and audio_duration > fade_seconds * 2:
            fade_filter = (
                f"afade=t=in:st=0:d={fade_seconds:.3f},"
                f"afade=t=out:st={max(0.0, audio_duration - fade_seconds):.6f}:d={fade_seconds:.3f},"
            )
        filters.append(
            f"[{index}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,{fade_filter}volume=1.0000,adelay={delay}|{delay}[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95,atrim=0:{max(0.001, duration):.6f}[tts_mix]"
    )
    command = inputs + [
        "-filter_complex", ";".join(filters), "-map", "[tts_mix]", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(output_path),
    ]
    logger.info("A2 tts-only diagnostic inputs=%s output=%s", len(ordered_segments), output_path)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, check=False)
    output_size = output_path.stat().st_size if output_path.is_file() else 0
    logger.info("A2 tts-only ffmpeg exit_code=%s output=%s size_bytes=%s", result.returncode, output_path, output_size)
    if result.returncode != 0 or output_size <= 0:
        raise MediaToolError("DUBBING_TTS_ONLY_FAILED", "Unable to create the TTS-only diagnostic audio.", result.stderr)
    return {"path": str(output_path), "duration": media_duration_seconds(output_path), "command": command}


def _subtitle_filter(subtitle_path: Path | None, style: dict[str, Any] | None = None) -> str | None:
    style = style or {}
    render_mode = str(style.get("render_mode") or "")
    subtitle_filter = None
    if subtitle_path:
        if subtitle_path.suffix.lower() == ".ass" and style.get("use_embedded_ass_style") is True:
            subtitle_filter = f"subtitles=filename={subtitle_path.name}"
        else:
            alignment = {"top": 8, "middle": 5, "bottom": 2}.get(str(style.get("position")), 2)
            force_style = (
                f"FontName={style.get('font_family', 'Arial')},FontSize={int(style.get('font_size', 42))},"
                f"Outline={int(style.get('outline_width', 2))},Alignment={alignment},"
                f"MarginV={max(10, int(style.get('safe_margin_percent', 8)) * 4)},WrapStyle=2"
            )
            subtitle_filter = (
                f"subtitles=filename={subtitle_path.name}:force_style='{force_style}'"
            )
    if render_mode in {"target_only", "mask_burned_subtitle_and_add_target"} and style.get("mask_enabled", True) is not False:
        mask_height = int(style.get("mask_height_percent", 22) or 22)
        if mask_height == 18:
            mask_height = 22
        mask_fraction = max(10, min(35, mask_height)) / 100
        mask = f"drawbox=x=0:y=ih*(1-{mask_fraction}):w=iw:h=ih*{mask_fraction}:color=black@1.0:t=fill"
        return f"{mask},{subtitle_filter}" if subtitle_filter else mask
    return subtitle_filter


def _require_ffmpeg_filter(ffmpeg: str, filter_name: str) -> None:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaToolError(
            "FFMPEG_FILTER_CHECK_FAILED",
            "Unable to inspect the configured FFmpeg filters.",
            str(error),
        ) from error
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    available = any(
        len(parts := line.split()) >= 2 and parts[1] == filter_name
        for line in output.splitlines()
    )
    if result.returncode != 0 or not available:
        raise MediaToolError(
            "FFMPEG_SUBTITLES_FILTER_UNAVAILABLE",
            "The configured FFmpeg build does not provide the subtitles filter.",
            output,
            details={"filter_name": filter_name, "ffmpeg_path": ffmpeg, "stderr": output},
        )


def render_dubbed_preview(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    subtitle_path: Path | None = None,
    style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path)]
    cwd = None
    video_filter = _subtitle_filter(subtitle_path, style)
    if video_filter:
        _require_ffmpeg_filter(ffmpeg, "subtitles")
        cwd = subtitle_path.parent if subtitle_path else video_path.parent
        command.extend([
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        ])
    else:
        command.extend(["-c:v", "copy"])
    command.extend(["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest", str(output_path)])
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1200, check=False,
    )
    output_size = output_path.stat().st_size if output_path.is_file() else 0
    logger.info("A2 preview ffmpeg exit_code=%s output=%s size_bytes=%s", result.returncode, output_path, output_size)
    if result.returncode != 0 or output_size <= 0:
        raise MediaToolError("DUBBING_PREVIEW_FAILED", "Unable to render the dubbed preview video.", result.stderr)
    preview_duration = media_duration_seconds(output_path)
    if preview_duration <= 0:
        raise MediaToolError("DUBBING_PREVIEW_FAILED", "The generated preview has no readable duration.", result.stderr)
    return {
        "path": str(output_path),
        "duration": preview_duration,
        "stderr": result.stderr,
        "command": command,
    }


def extract_subtitle_stream(video_path: Path, stream_index: int, output_path: Path) -> dict[str, Any]:
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-i", str(video_path), "-map", f"0:{stream_index}", str(output_path)
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False
    )
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("SUBTITLE_EXTRACTION_FAILED", "Unable to extract the embedded subtitle stream.", result.stderr)
    return {"path": str(output_path), "stderr": result.stderr, "command": command}


def render_subtitle_preview(
    video_path: Path, subtitle_path: Path | None, output_path: Path, style: dict[str, Any] | None = None
) -> dict[str, Any]:
    ffmpeg = _require_tool("ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    style = style or {}
    subtitle_filter = _subtitle_filter(subtitle_path, style)
    video_filter = subtitle_filter
    command = [
        ffmpeg, "-y", "-i", str(video_path),
    ]
    if video_filter:
        _require_ffmpeg_filter(ffmpeg, "subtitles")
        command.extend(["-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22"])
    else:
        command.extend(["-c:v", "copy"])
    command.extend(["-c:a", "copy", str(output_path)])
    result = subprocess.run(
        command,
        cwd=(subtitle_path.parent if subtitle_path else video_path.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    if result.returncode != 0 or not output_path.is_file():
        raise MediaToolError("SUBTITLE_PREVIEW_FAILED", "Unable to render subtitle preview.", result.stderr)
    return {"path": str(output_path), "stderr": result.stderr, "command": command}
