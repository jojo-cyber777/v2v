"""Edge neural TTS adapter with project-standard WAV normalization."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Coroutine

from . import media_service
from .provider_service import ProviderError, TTSConfiguration, TTSProvider


logger = logging.getLogger(__name__)

PROJECT_AUDIO_CODEC = "pcm_s16le"
PROJECT_AUDIO_SAMPLE_RATE = 44100
PROJECT_AUDIO_CHANNELS = 2
PROJECT_AUDIO_SAMPLE_FORMAT = "s16"


def _run_async(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """Run an Edge coroutine from the existing synchronous provider contract."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    failure: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as error:  # pragma: no cover - forwarded to caller
            failure.append(error)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


class EdgeTTSProvider(TTSProvider):
    """No-key Edge neural voice adapter. It never falls back to another provider."""

    def __init__(self, configuration: TTSConfiguration | None = None) -> None:
        super().__init__(configuration)
        self._voice_cache: list[dict[str, Any]] | None = None
        self.last_error: str | None = None
        self.synthesis_request_count = 0
        self.voice_catalog_request_count = 0

    @staticmethod
    def _edge_module() -> Any:
        try:
            import edge_tts
        except ImportError as error:
            raise ProviderError(
                "EDGE_TTS_DEPENDENCY_MISSING",
                "edge-tts is not installed in the backend Python environment.",
            ) from error
        return edge_tts

    def validate_configuration(self) -> None:
        self._edge_module()
        if not self.configuration.configured:
            missing = ", ".join(self.configuration.public_status()["missing"])
            raise ProviderError(
                "TTS_PROVIDER_DISABLED",
                f"Edge TTS is not configured. Missing: {missing}.",
            )

    @staticmethod
    def _normalize_voice(row: dict[str, Any]) -> dict[str, Any]:
        voice_id = str(row.get("ShortName") or "").strip()
        locale = str(row.get("Locale") or "").strip()
        if not voice_id or not locale:
            raise ValueError("Edge voice row is missing ShortName or Locale.")
        display_name = str(
            row.get("FriendlyName") or row.get("LocalName") or voice_id
        ).strip()
        return {
            "provider": "edge",
            "model": "edge-neural",
            "voice_id": voice_id,
            "name": display_name,
            "display_name": display_name,
            "locale": locale,
            "language": locale.split("-", 1)[0].lower(),
            "gender": str(row.get("Gender") or "Unknown"),
            "content_categories": list(row.get("VoiceTag", {}).get("ContentCategories") or []),
            "voice_personalities": list(row.get("VoiceTag", {}).get("VoicePersonalities") or []),
        }

    def list_voices(
        self,
        *,
        language: str | None = None,
        gender: str | None = None,
    ) -> list[dict[str, Any]]:
        self.validate_configuration()
        if self._voice_cache is None:
            edge_tts = self._edge_module()
            self.voice_catalog_request_count += 1
            try:
                rows = _run_async(edge_tts.list_voices())
                self._voice_cache = [self._normalize_voice(row) for row in rows]
            except ProviderError:
                raise
            except Exception as error:
                self.last_error = "EDGE_TTS_VOICE_LIST_FAILED"
                raise ProviderError(
                    "EDGE_TTS_VOICE_LIST_FAILED",
                    "Unable to load the Edge voice catalog.",
                    diagnostics={"exception_type": type(error).__name__},
                ) from error
        normalized_language = str(language or "").strip().lower()
        normalized_gender = str(gender or "").strip().lower()
        return [
            dict(voice)
            for voice in self._voice_cache
            if (
                not normalized_language
                or voice["language"] == normalized_language
                or voice["locale"].lower() == normalized_language
            )
            and (
                not normalized_gender
                or str(voice["gender"]).lower() == normalized_gender
            )
        ]

    def _resolve_voice(self, voice_id: str) -> dict[str, Any]:
        normalized = str(voice_id or "").strip()
        if not normalized or normalized.startswith("tencent:"):
            raise ProviderError(
                "EDGE_TTS_VOICE_INVALID",
                "Select a valid Edge ShortName voice.",
            )
        voice = next(
            (item for item in self.list_voices() if item["voice_id"] == normalized),
            None,
        )
        if not voice:
            raise ProviderError(
                "EDGE_TTS_VOICE_INVALID",
                f"Edge voice '{normalized}' is not available.",
            )
        return voice

    @staticmethod
    def _probe_audio(path: Path) -> dict[str, Any]:
        ffprobe = media_service.resolve_ffprobe_binary()
        if not ffprobe:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "FFprobe is required to validate Edge TTS audio.",
            )
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "Edge TTS audio could not be read.",
                diagnostics={"ffprobe_exit_code": result.returncode},
            )
        try:
            payload = json.loads(result.stdout)
            stream = next(
                row for row in payload.get("streams", []) if row.get("codec_type") == "audio"
            )
            duration = float(
                (payload.get("format") or {}).get("duration")
                or stream.get("duration")
                or 0
            )
        except (StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "Edge TTS audio metadata is invalid.",
            ) from error
        if duration <= 0 or path.stat().st_size <= 0:
            raise ProviderError(
                "EDGE_TTS_NO_AUDIO",
                "Edge TTS returned no usable audio.",
            )
        return {"payload": payload, "stream": stream, "duration": duration}

    @staticmethod
    def _normalize_wav(source_mp3: Path, output_wav: Path) -> dict[str, Any]:
        ffmpeg = media_service.resolve_ffmpeg_binary()
        if not ffmpeg:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "FFmpeg is required to normalize Edge TTS audio.",
            )
        command = [
            str(ffmpeg),
            "-y",
            "-i",
            str(source_mp3),
            "-vn",
            "-ar",
            str(PROJECT_AUDIO_SAMPLE_RATE),
            "-ac",
            str(PROJECT_AUDIO_CHANNELS),
            "-c:a",
            PROJECT_AUDIO_CODEC,
            str(output_wav),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or not output_wav.is_file():
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "FFmpeg could not normalize the Edge TTS audio.",
                diagnostics={"ffmpeg_exit_code": result.returncode},
            )
        return {"command": command, "stderr": result.stderr}

    @staticmethod
    def _decode_wav(path: Path) -> None:
        ffmpeg = media_service.resolve_ffmpeg_binary()
        if not ffmpeg:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "FFmpeg is required to decode-check Edge TTS audio.",
            )
        result = subprocess.run(
            [str(ffmpeg), "-v", "error", "-i", str(path), "-f", "null", os.devnull],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise ProviderError(
                "EDGE_TTS_OUTPUT_INVALID",
                "The normalized Edge TTS WAV failed complete decoding.",
                diagnostics={"ffmpeg_exit_code": result.returncode},
            )

    @staticmethod
    def _mapped_error(error: BaseException) -> ProviderError:
        if isinstance(error, ProviderError):
            return error
        name = type(error).__name__
        marker = str(error).lower()
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or "timeout" in marker:
            code = "EDGE_TTS_TIMEOUT"
            message = "Edge TTS timed out."
        elif name == "NoAudioReceived":
            code = "EDGE_TTS_NO_AUDIO"
            message = "Edge TTS returned no audio."
        elif "429" in marker or "rate limit" in marker or "too many requests" in marker:
            code = "EDGE_TTS_RATE_LIMITED"
            message = "Edge TTS rate-limited the request."
        elif name in {"ClientConnectionError", "ClientConnectorError", "WebSocketError"} or isinstance(error, OSError):
            code = "EDGE_TTS_CONNECTION_FAILED"
            message = "Unable to connect to Edge TTS."
        else:
            code = "EDGE_TTS_INTERNAL_ERROR"
            message = "Edge TTS could not synthesize the requested audio."
        return ProviderError(
            code,
            message,
            attempt_count=1,
            diagnostics={"exception_type": name},
        )

    def generate_speech_to_files(
        self,
        text: str,
        voice_id: str,
        raw_mp3_path: Path,
        normalized_wav_path: Path,
        *,
        rate: str | None = None,
        volume: str | None = None,
        pitch: str | None = None,
    ) -> dict[str, Any]:
        self.validate_configuration()
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ProviderError("TTS_TEXT_EMPTY", "The TTS text is empty.")
        voice = self._resolve_voice(voice_id)
        effective_rate = str(rate or self.configuration.rate)
        effective_volume = str(volume or self.configuration.volume)
        effective_pitch = str(pitch or self.configuration.pitch)
        raw_mp3_path = Path(raw_mp3_path)
        normalized_wav_path = Path(normalized_wav_path)
        raw_mp3_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_wav_path.parent.mkdir(parents=True, exist_ok=True)

        edge_tts = self._edge_module()
        try:
            with tempfile.TemporaryDirectory(
                prefix=".edge-tts-", dir=str(normalized_wav_path.parent)
            ) as directory:
                temporary_directory = Path(directory)
                temporary_mp3 = temporary_directory / "provider-output.mp3"
                temporary_wav = temporary_directory / "normalized-output.wav"
                communicator = edge_tts.Communicate(
                    clean_text,
                    voice["voice_id"],
                    rate=effective_rate,
                    volume=effective_volume,
                    pitch=effective_pitch,
                    connect_timeout=self.configuration.connect_timeout,
                    receive_timeout=self.configuration.receive_timeout,
                )
                self.synthesis_request_count += 1
                _run_async(communicator.save(str(temporary_mp3)))
                if not temporary_mp3.is_file() or temporary_mp3.stat().st_size <= 0:
                    raise ProviderError(
                        "EDGE_TTS_NO_AUDIO",
                        "Edge TTS returned no audio.",
                    )
                raw_probe = self._probe_audio(temporary_mp3)
                if raw_probe["stream"].get("codec_name") not in {"mp3", "mp3float"}:
                    raise ProviderError(
                        "EDGE_TTS_OUTPUT_INVALID",
                        "Edge TTS did not return an MP3 audio stream.",
                    )
                normalization = self._normalize_wav(temporary_mp3, temporary_wav)
                wav_probe = self._probe_audio(temporary_wav)
                stream = wav_probe["stream"]
                if (
                    stream.get("codec_name") != PROJECT_AUDIO_CODEC
                    or int(stream.get("sample_rate") or 0) != PROJECT_AUDIO_SAMPLE_RATE
                    or int(stream.get("channels") or 0) != PROJECT_AUDIO_CHANNELS
                    or stream.get("sample_fmt") != PROJECT_AUDIO_SAMPLE_FORMAT
                ):
                    raise ProviderError(
                        "EDGE_TTS_OUTPUT_INVALID",
                        "The normalized Edge TTS WAV does not match the project audio standard.",
                    )
                self._decode_wav(temporary_wav)
                os.replace(temporary_mp3, raw_mp3_path)
                os.replace(temporary_wav, normalized_wav_path)
        except BaseException as error:
            mapped = self._mapped_error(error)
            self.last_error = mapped.code
            logger.warning(
                "Edge TTS synthesis failed code=%s exception_type=%s",
                mapped.code,
                type(error).__name__,
            )
            raise mapped from error

        self.last_error = None
        return {
            "provider": "edge",
            "provider_name": "edge",
            "model": "edge-neural",
            "voice_id": voice["voice_id"],
            "audio_path": str(normalized_wav_path),
            "raw_audio_path": str(raw_mp3_path),
            "duration_ms": round(float(wav_probe["duration"]) * 1000),
            "provider_request_id": None,
            "character_cost": len(clean_text),
            "output_format": "wav",
            "attempt_count": 1,
            "usage": {
                "character_cost": len(clean_text),
                "rate": effective_rate,
                "volume": effective_volume,
                "pitch": effective_pitch,
                "retry_count": 0,
            },
            "voice": voice,
            "raw_probe": raw_probe["payload"],
            "wav_probe": wav_probe["payload"],
            "normalization_command": normalization["command"],
            "decode_succeeded": True,
        }

    def generate_speech(
        self,
        text: str,
        voice_id: str,
        *,
        session_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> dict[str, Any]:
        del session_id, speed_ratio
        with tempfile.TemporaryDirectory(prefix="edge-tts-result-") as directory:
            root = Path(directory)
            result = self.generate_speech_to_files(
                text,
                voice_id,
                root / "provider.mp3",
                root / "normalized.wav",
            )
            return {
                **result,
                "audio": Path(result["audio_path"]).read_bytes(),
                "provider_audio": Path(result["raw_audio_path"]).read_bytes(),
            }

    def health_check(self, *, check_catalog: bool = True) -> dict[str, Any]:
        try:
            edge_tts = self._edge_module()
            package_installed = True
            package_version = importlib.metadata.version("edge-tts")
        except (ProviderError, importlib.metadata.PackageNotFoundError) as error:
            return {
                "package_installed": False,
                "package_version": None,
                "voice_catalog_available": False,
                "default_voice_available": False,
                "synthesis_available": False,
                "last_error": getattr(error, "code", "EDGE_TTS_DEPENDENCY_MISSING"),
            }
        del edge_tts
        voices: list[dict[str, Any]] = []
        catalog_available = False
        if check_catalog:
            try:
                voices = self.list_voices()
                catalog_available = True
            except ProviderError as error:
                self.last_error = error.code
        default_available = any(
            voice["voice_id"] == self.configuration.default_voice_id for voice in voices
        )
        return {
            "package_installed": package_installed,
            "package_version": package_version,
            "voice_catalog_available": catalog_available,
            "default_voice_available": default_available,
            "synthesis_available": bool(
                package_installed and catalog_available and default_available
            ),
            "last_error": self.last_error,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
