"""Provider adapters for real transcription and translation calls."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import shutil
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..database import ROOT
from .local_audio_sidecar_client import LocalAudioSidecarClient, LocalAudioSidecarError
from .tencent_tts_voice_catalog import (
    TEXT_TO_VOICE_ENDPOINT,
    TencentVoiceCatalogError,
    find_voice,
    load_tencent_voice_catalog,
    provider_voice_rows,
)


ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_API_BASE_URL = "https://api.elevenlabs.io"
logger = logging.getLogger(__name__)


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    for variable in (
        "ELEVENLABS_API_KEY",
        "DEEPSEEK_API_KEY",
        "RUNWAY_API_KEY",
        "TRANSCRIPTION_API_KEY",
        "TRANSLATION_API_KEY",
        "TENCENTCLOUD_SECRET_ID",
        "TENCENTCLOUD_SECRET_KEY",
    ):
        secret = os.getenv(variable, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _safe_diagnostic_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)[:4000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact_sensitive_text(value)[:1000]


def _safe_request_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _network_exception_diagnostics(
    error: BaseException,
    *,
    method: str,
    url: str,
    attempt_count: int,
) -> dict[str, Any]:
    chain = _exception_chain(error)
    messages = [redact_sensitive_text(item)[:1000] for item in chain]
    combined = " | ".join(messages)
    winerror = next((getattr(item, "winerror", None) for item in chain if getattr(item, "winerror", None) is not None), None)
    errno = next((getattr(item, "errno", None) for item in chain if getattr(item, "errno", None) is not None), None)
    if winerror is None:
        match = re.search(r"\bWinError\s+(\d+)\b", combined, flags=re.IGNORECASE)
        winerror = int(match.group(1)) if match else None
    proxy_type = getattr(httpx, "ProxyError", None)
    is_proxy = bool(
        (proxy_type and any(isinstance(item, proxy_type) for item in chain))
        or re.search(r"\bproxy\b", combined, flags=re.IGNORECASE)
    )
    is_dns = any(isinstance(item, socket.gaierror) for item in chain) or bool(
        re.search(r"(getaddrinfo|name resolution|nodename nor servname|dns)", combined, flags=re.IGNORECASE)
    )
    is_tls = any(isinstance(item, ssl.SSLError) for item in chain) or bool(
        re.search(r"(certificate verify|ssl|tls)", combined, flags=re.IGNORECASE)
    )
    is_timeout = any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in chain)
    category = "timeout" if is_timeout else "dns" if is_dns else "tls" if is_tls else "proxy" if is_proxy else "network"
    return {
        "request_method": method,
        "request_url": _safe_request_url(url),
        "exception_type": type(error).__name__,
        "exception_chain": [
            {"type": type(item).__name__, "message": message}
            for item, message in zip(chain, messages)
        ],
        "category": category,
        "winerror": winerror,
        "errno": errno,
        "timeout": is_timeout,
        "dns_error": is_dns,
        "tls_error": is_tls,
        "proxy_error": is_proxy,
        "proxy_configured": {
            "http": bool(os.getenv("HTTP_PROXY") or os.getenv("http_proxy")),
            "https": bool(os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")),
            "all": bool(os.getenv("ALL_PROXY") or os.getenv("all_proxy")),
        },
        "attempt_count": attempt_count,
    }


def _response_diagnostics(
    response: httpx.Response,
    *,
    method: str,
    url: str,
    attempt_count: int,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    safe_body = redact_sensitive_text(response.text)[:4000]
    for secret in sensitive_values:
        if secret:
            safe_body = safe_body.replace(secret, "[REDACTED]")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return {
        "request_method": method,
        "request_url": _safe_request_url(url),
        "http_status": response.status_code,
        "response_body": safe_body,
        "response_field_names": sorted(str(key) for key in payload) if isinstance(payload, dict) else [],
        "request_id": response.headers.get("request-id") or response.headers.get("x-request-id"),
        "attempt_count": attempt_count,
    }


def provider_error_diagnostics(error: Exception) -> dict[str, Any]:
    if not isinstance(error, ProviderError):
        return {}
    return {
        "code": error.code,
        "message": str(error),
        "detail": error.detail,
        "retryable": error.retryable,
        "request_id": error.request_id,
        "attempt_count": error.attempt_count,
        **error.diagnostics,
    }


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        detail: str = "",
        *,
        retryable: bool = False,
        request_id: str | None = None,
        attempt_count: int = 0,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = _safe_diagnostic_value(diagnostics or {})
        self.detail = redact_sensitive_text(
            detail or (json.dumps(self.diagnostics, ensure_ascii=True) if self.diagnostics else "")
        )
        self.retryable = retryable
        self.request_id = request_id
        self.attempt_count = attempt_count


@dataclass(frozen=True)
class ProviderConfiguration:
    provider: str
    model: str
    api_key: str
    base_url: str
    secret_id: str = ""
    secret_key: str = ""
    region: str = ""
    endpoint: str = ""

    @property
    def api_key_has_valid_transport_format(self) -> bool:
        return bool(self.api_key and self.api_key.isascii() and not any(character.isspace() for character in self.api_key))

    @property
    def configured(self) -> bool:
        if self.provider == "local_whisper_pyannote":
            return bool(self.model and self.base_url)
        if self.provider == "tencent":
            return bool(
                self.model
                and self.secret_id
                and self.secret_key
                and self.region
                and self.endpoint
            )
        return bool(self.provider and self.model and self.api_key_has_valid_transport_format)

    def public_status(self) -> dict[str, Any]:
        missing = []
        if not self.provider:
            missing.append("provider")
        if not self.model:
            missing.append("model")
        if self.provider == "local_whisper_pyannote":
            if not self.base_url:
                missing.append("base_url")
        elif self.provider == "tencent":
            if not self.secret_id:
                missing.append("secret_id")
            if not self.secret_key:
                missing.append("secret_key")
            if not self.region:
                missing.append("region")
            if not self.endpoint:
                missing.append("endpoint")
        else:
            if not self.api_key:
                missing.append("api_key")
            elif not self.api_key_has_valid_transport_format:
                missing.append("api_key_format")
        return {
            "provider": self.provider or None,
            "model": self.model or None,
            "configured": self.configured,
            "missing": missing,
        }


@dataclass(frozen=True)
class TTSConfiguration:
    provider: str
    model: str
    api_key: str
    base_url: str
    default_voice_id: str
    output_format: str
    secret_id: str = ""
    secret_key: str = ""
    voice_type: str = ""
    primary_language: int = 2
    sample_rate: int = 16000
    codec: str = "wav"
    endpoint: str = "tts.tencentcloudapi.com"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    connect_timeout: int = 15
    receive_timeout: int = 90

    @property
    def api_key_has_valid_transport_format(self) -> bool:
        return bool(self.api_key and self.api_key.isascii() and not any(character.isspace() for character in self.api_key))

    @property
    def configured(self) -> bool:
        if self.provider == "elevenlabs":
            return bool(self.model and self.api_key_has_valid_transport_format and self.output_format)
        if self.provider == "tencent":
            return bool(
                self.secret_id
                and self.secret_key
                and self.voice_type.isdigit()
                and self.primary_language in {1, 2}
                and self.sample_rate in {8000, 16000, 24000}
                and self.codec == "wav"
            )
        if self.provider == "edge":
            return bool(
                self.model == "edge-neural"
                and self.default_voice_id
                and self.output_format == "wav"
                and self.connect_timeout > 0
                and self.receive_timeout > 0
            )
        return False

    def public_status(self) -> dict[str, Any]:
        missing = []
        if self.provider not in {"elevenlabs", "tencent", "edge"}:
            missing.append("provider")
        if self.provider == "elevenlabs":
            if not self.model:
                missing.append("model")
            if not self.api_key:
                missing.append("api_key")
            elif not self.api_key_has_valid_transport_format:
                missing.append("api_key_format")
            if not self.output_format:
                missing.append("output_format")
        elif self.provider == "tencent":
            if not self.secret_id:
                missing.append("secret_id")
            if not self.secret_key:
                missing.append("secret_key")
            if not self.voice_type or not self.voice_type.isdigit():
                missing.append("voice_type")
            if self.primary_language not in {1, 2}:
                missing.append("primary_language")
            if self.sample_rate not in {8000, 16000, 24000}:
                missing.append("sample_rate")
            if self.codec != "wav":
                missing.append("codec")
        elif self.provider == "edge":
            if self.model != "edge-neural":
                missing.append("model")
            if not self.default_voice_id:
                missing.append("default_voice")
            if self.output_format != "wav":
                missing.append("output_format")
            if self.connect_timeout <= 0:
                missing.append("connect_timeout")
            if self.receive_timeout <= 0:
                missing.append("receive_timeout")
        return {
            "provider": self.provider or None,
            "model": self.model or None,
            "configured": self.configured,
            "missing": missing,
            "output_format": self.output_format or None,
            "default_voice_configured": bool(self.default_voice_id),
        }


def transcription_configuration(provider_override: str | None = None) -> ProviderConfiguration:
    provider = (provider_override or (
        os.getenv("STT_PROVIDER")
        or os.getenv("TRANSCRIPTION_PROVIDER")
        or ""
    )).strip().lower()
    if provider == "local_whisper_pyannote":
        return ProviderConfiguration(
            provider=provider,
            model="faster-whisper-medium",
            api_key="",
            base_url=(os.getenv("LOCAL_AUDIO_SIDECAR_URL") or "http://127.0.0.1:8765").rstrip("/"),
        )
    if provider == "tencent":
        return ProviderConfiguration(
            provider=provider,
            model=(
                os.getenv("TENCENT_ASR_ENGINE_MODEL_TYPE", "16k_zh").strip()
                or "16k_zh"
            ),
            api_key="",
            base_url="https://asr.tencentcloudapi.com",
            secret_id=os.getenv("TENCENTCLOUD_SECRET_ID", "").strip(),
            secret_key=os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip(),
            region=os.getenv("TENCENT_ASR_REGION", "ap-shanghai").strip() or "ap-shanghai",
            endpoint=(
                os.getenv("TENCENT_ASR_ENDPOINT", "asr.tencentcloudapi.com").strip()
                or "asr.tencentcloudapi.com"
            ),
        )
    if provider == "elevenlabs":
        return ProviderConfiguration(
            provider=provider,
            model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2").strip() or "scribe_v2",
            api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
            base_url=os.getenv("ELEVENLABS_STT_BASE_URL", ELEVENLABS_STT_URL).rstrip("/"),
        )
    return ProviderConfiguration(
        provider=provider,
        model=os.getenv("TRANSCRIPTION_MODEL", "").strip(),
        api_key=os.getenv("TRANSCRIPTION_API_KEY", "").strip(),
        base_url=os.getenv("TRANSCRIPTION_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
    )


def translation_configuration() -> ProviderConfiguration:
    provider = os.getenv("TRANSLATION_PROVIDER", "").strip().lower()
    if provider == "deepseek":
        return ProviderConfiguration(
            provider=provider,
            model=(os.getenv("DEEPSEEK_MODEL") or os.getenv("TRANSLATION_MODEL") or "deepseek-v4-flash").strip(),
            api_key=(os.getenv("DEEPSEEK_API_KEY") or os.getenv("TRANSLATION_API_KEY") or "").strip(),
            base_url=(os.getenv("DEEPSEEK_BASE_URL") or os.getenv("TRANSLATION_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
        )
    return ProviderConfiguration(
        provider=provider,
        model=os.getenv("TRANSLATION_MODEL", "").strip(),
        api_key=os.getenv("TRANSLATION_API_KEY", "").strip(),
        base_url=os.getenv("TRANSLATION_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
    )


def tts_configuration(provider_override: str | None = None) -> TTSConfiguration:
    provider = (provider_override or os.getenv("TTS_PROVIDER", "")).strip().lower()
    if provider == "edge":
        try:
            connect_timeout = int(os.getenv("EDGE_TTS_CONNECT_TIMEOUT", "15").strip())
        except ValueError:
            connect_timeout = 0
        try:
            receive_timeout = int(os.getenv("EDGE_TTS_RECEIVE_TIMEOUT", "90").strip())
        except ValueError:
            receive_timeout = 0
        return TTSConfiguration(
            provider=provider,
            model="edge-neural",
            api_key="",
            base_url="",
            default_voice_id=os.getenv(
                "EDGE_TTS_DEFAULT_VOICE", "en-US-GuyNeural"
            ).strip(),
            output_format="wav",
            rate=os.getenv("EDGE_TTS_DEFAULT_RATE", "+0%").strip() or "+0%",
            volume=os.getenv("EDGE_TTS_DEFAULT_VOLUME", "+0%").strip() or "+0%",
            pitch=os.getenv("EDGE_TTS_DEFAULT_PITCH", "+0Hz").strip() or "+0Hz",
            connect_timeout=connect_timeout,
            receive_timeout=receive_timeout,
        )
    if provider == "tencent":
        voice_type = (
            os.getenv("TENCENT_TTS_VOICE_TYPE")
            or os.getenv("TENCENT_TTS_DEFAULT_VOICE_TYPE")
            or ""
        ).strip()
        try:
            primary_language = int(os.getenv("TENCENT_TTS_PRIMARY_LANGUAGE", "2").strip())
        except ValueError:
            primary_language = 0
        try:
            sample_rate = int(os.getenv("TENCENT_TTS_SAMPLE_RATE", "16000").strip())
        except ValueError:
            sample_rate = 0
        codec = os.getenv("TENCENT_TTS_CODEC", "wav").strip().lower()
        return TTSConfiguration(
            provider=provider,
            model="TextToVoice-2019-08-23",
            api_key="",
            base_url="https://tts.tencentcloudapi.com",
            default_voice_id=f"tencent:{voice_type}" if voice_type.isdigit() else "",
            output_format=codec,
            secret_id=os.getenv("TENCENTCLOUD_SECRET_ID", "").strip(),
            secret_key=os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip(),
            voice_type=voice_type,
            primary_language=primary_language,
            sample_rate=sample_rate,
            codec=codec,
            endpoint=os.getenv("TENCENT_TTS_ENDPOINT", "tts.tencentcloudapi.com").strip()
            or "tts.tencentcloudapi.com",
        )
    return TTSConfiguration(
        provider=provider,
        model=os.getenv("ELEVENLABS_TTS_MODEL", "").strip(),
        api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
        base_url=os.getenv("ELEVENLABS_API_BASE_URL", ELEVENLABS_API_BASE_URL).rstrip("/"),
        default_voice_id=os.getenv("ELEVENLABS_DEFAULT_VOICE_ID", "").strip(),
        output_format=os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip(),
    )


def validate_configuration(config: ProviderConfiguration, capability: str) -> None:
    if not config.configured:
        missing = ", ".join(config.public_status()["missing"])
        raise ProviderError(
            f"{capability.upper()}_PROVIDER_DISABLED",
            f"{capability.title()} provider is not configured. Missing: {missing}.",
        )
    supported = {"openai", "openai-compatible"}
    if capability == "transcription":
        supported.update({"elevenlabs", "tencent", "local_whisper_pyannote"})
    if capability == "translation":
        supported.add("deepseek")
    if config.provider not in supported:
        raise ProviderError(
            "UNSUPPORTED_PROVIDER",
            f"Provider '{config.provider}' is not supported by this A1 adapter.",
        )


class TranscriptionProvider:
    """OpenAI-compatible transcription adapter used by the A1 pipeline."""

    def __init__(self, configuration: ProviderConfiguration | None = None) -> None:
        self.configuration = configuration or transcription_configuration()

    def validate_configuration(self) -> None:
        if self.configuration.api_key and not self.configuration.api_key_has_valid_transport_format:
            raise ProviderError("API_KEY_INVALID", "The ElevenLabs API key format is invalid.")
        validate_configuration(self.configuration, "transcription")
        if self.configuration.provider not in {"openai", "openai-compatible"}:
            raise ProviderError("UNSUPPORTED_PROVIDER", "This adapter only supports OpenAI-compatible transcription.")

    def supported_languages(self) -> list[str]:
        return ["auto", "zh", "en", "es", "fr", "pt", "ja", "ko", "vi", "th", "id", "ar", "de", "it", "ru"]

    def normalize_response(self, payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        raw_segments = payload.get("segments") or []
        rows = [
            {
                "start_ms": max(0, round(float(item.get("start", 0)) * 1000)),
                "end_ms": max(0, round(float(item.get("end", item.get("start", 0))) * 1000)),
                "text": str(item.get("text", "")).strip(),
                "speaker": "Speaker 01",
                "segment_type": "dialogue",
                "confidence": None,
                "timestamp_source": "provider",
            }
            for item in raw_segments
            if str(item.get("text", "")).strip()
        ]
        if not rows:
            raise ProviderError("TIMESTAMPS_MISSING", "The provider response did not include usable segment timestamps.")
        return {
            "provider": self.configuration.provider,
            "model": self.configuration.model,
            "detected_language": payload.get("language") or payload.get("language_code") or "unknown",
            "transcript": str(payload.get("text", "")).strip(),
            "segments": rows,
            "speaker_count": 1,
            "provider_request_id": request_id,
            "timestamp_source": "provider",
            "attempt_count": 1,
            "metadata": {},
        }

    def transcribe_audio(self, audio_path: Path, language: str = "auto") -> dict[str, Any]:
        config = self.configuration
        self.validate_configuration()
        if not audio_path.is_file():
            raise ProviderError("AUDIO_FILE_NOT_FOUND", "The extracted audio file does not exist.", str(audio_path))
        data: list[tuple[str, str]] = [
            ("model", config.model), ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "segment"),
        ]
        if language and language != "auto":
            data.append(("language", language))
        try:
            with audio_path.open("rb") as audio_file:
                response = httpx.post(
                    f"{config.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {config.api_key}"}, data=data,
                    files={"file": (audio_path.name, audio_file, "audio/wav")}, timeout=600,
                )
        except OSError as error:
            raise ProviderError("AUDIO_FILE_NOT_FOUND", "The extracted audio file could not be opened.", str(error)) from error
        except httpx.TimeoutException as error:
            raise ProviderError("NETWORK_TIMEOUT", "The transcription provider request timed out.", str(error), retryable=True) from error
        except httpx.HTTPError as error:
            raise ProviderError("TRANSCRIPTION_REQUEST_FAILED", "The transcription request could not be completed.", str(error), retryable=True) from error
        if response.is_error:
            raise ProviderError("TRANSCRIPTION_PROVIDER_ERROR", f"Transcription provider returned HTTP {response.status_code}.", response.text[:4000])
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError("PROVIDER_RESPONSE_INVALID", "The provider returned an invalid JSON response.", response.text[:4000]) from error
        return self.normalize_response(payload, response.headers.get("request-id") or response.headers.get("x-request-id"))


class ElevenLabsTranscriptionProvider(TranscriptionProvider):
    """ElevenLabs Scribe file-transcription adapter with provider timestamps and diarization."""

    LANGUAGE_CODES = {
        "zh": "zho", "chinese": "zho", "mandarin": "zho", "zho": "zho",
        "en": "eng", "english": "eng", "es": "spa", "spanish": "spa",
        "fr": "fra", "french": "fra", "pt": "por", "portuguese": "por",
        "ja": "jpn", "japanese": "jpn", "ko": "kor", "korean": "kor",
        "vi": "vie", "vietnamese": "vie", "th": "tha", "thai": "tha",
        "id": "ind", "indonesian": "ind", "ar": "ara", "arabic": "ara",
        "de": "deu", "german": "deu", "it": "ita", "italian": "ita",
        "ru": "rus", "russian": "rus",
    }

    def __init__(
        self,
        configuration: ProviderConfiguration | None = None,
        *,
        max_retries: int = 1,
    ) -> None:
        super().__init__(configuration)
        self.max_retries = max(0, min(max_retries, 1))

    def validate_configuration(self) -> None:
        if self.configuration.api_key and not self.configuration.api_key_has_valid_transport_format:
            raise ProviderError("API_KEY_INVALID", "The ElevenLabs API key format is invalid.")
        validate_configuration(self.configuration, "transcription")
        if self.configuration.provider != "elevenlabs":
            raise ProviderError("UNSUPPORTED_PROVIDER", "ElevenLabsTranscriptionProvider requires TRANSCRIPTION_PROVIDER=elevenlabs.")
        if self.configuration.model != "scribe_v2":
            raise ProviderError("UNSUPPORTED_MODEL", "A1-1 requires ELEVENLABS_STT_MODEL=scribe_v2.")

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        return response.headers.get("request-id") or response.headers.get("x-request-id")

    def map_provider_error(self, response: httpx.Response) -> ProviderError:
        request_id = self._request_id(response)
        body = response.text[:4000]
        lowered = body.lower()
        provider_status = ""
        try:
            payload = response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
            if isinstance(detail, dict):
                provider_status = str(detail.get("status") or detail.get("code") or "").lower()
        except ValueError:
            pass
        if provider_status == "missing_permissions" or "missing the permission speech_to_text" in lowered:
            return ProviderError(
                "API_KEY_MISSING_PERMISSION",
                "The ElevenLabs API key does not have speech_to_text permission.",
                body,
                request_id=request_id,
            )
        if response.status_code == 401:
            return ProviderError("API_KEY_INVALID", "The ElevenLabs API key is invalid.", body, request_id=request_id)
        if response.status_code == 402 or any(term in lowered for term in ("quota", "credit", "balance", "payment")):
            return ProviderError("QUOTA_EXCEEDED", "The ElevenLabs account has insufficient quota or credits.", body, request_id=request_id)
        if response.status_code == 429:
            return ProviderError("RATE_LIMITED", "ElevenLabs rate-limited the transcription request.", body, retryable=True, request_id=request_id)
        if response.status_code == 403 and any(term in lowered for term in ("moderation", "content", "policy", "blocked")):
            return ProviderError("CONTENT_MODERATION_REJECTED", "ElevenLabs rejected the audio under its content policy.", body, request_id=request_id)
        if response.status_code in {400, 415, 422}:
            return ProviderError("AUDIO_FORMAT_INVALID", "ElevenLabs rejected the audio file or request format.", body, request_id=request_id)
        if response.status_code >= 500:
            return ProviderError("PROVIDER_UNAVAILABLE", "ElevenLabs is temporarily unavailable.", body, retryable=True, request_id=request_id)
        return ProviderError(
            "TRANSCRIPTION_PROVIDER_ERROR",
            f"ElevenLabs returned HTTP {response.status_code}.",
            body,
            request_id=request_id,
        )

    @staticmethod
    def _speaker_label(raw: str | None, labels: dict[str, str]) -> str:
        key = raw or "speaker_unknown"
        if key not in labels:
            labels[key] = f"Speaker {len(labels) + 1:02d}"
        return labels[key]

    @staticmethod
    def _join_text(parts: list[str], value: str) -> None:
        token = value.strip()
        if not token:
            return
        if parts and re.match(r"[A-Za-z0-9]", token) and re.search(r"[A-Za-z0-9]$", parts[-1]):
            parts.append(" ")
        parts.append(token)

    def normalize_response(self, payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        words = payload.get("words")
        if not isinstance(words, list):
            raise ProviderError("PROVIDER_RESPONSE_INVALID", "ElevenLabs returned an unexpected response format.")

        speaker_labels: dict[str, str] = {}
        timed_words: list[dict[str, Any]] = []
        pending_prefix: list[str] = []
        for item in words:
            if not isinstance(item, dict) or item.get("type") == "spacing":
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise ProviderError("TIMESTAMPS_MISSING", "ElevenLabs did not return timestamps for one or more words.")
            if not math.isfinite(float(start)) or not math.isfinite(float(end)) or float(start) < 0 or float(start) > float(end):
                raise ProviderError("TIMESTAMPS_INVALID", "ElevenLabs returned an invalid word timestamp.")
            if float(start) == float(end):
                if timed_words:
                    timed_words[-1]["text"] += text
                else:
                    pending_prefix.append(text)
                continue
            logprob = item.get("logprob")
            confidence = math.exp(float(logprob)) if isinstance(logprob, (int, float)) else None
            timed_words.append({
                "text": "".join(pending_prefix) + text,
                "start_ms": round(float(start) * 1000),
                "end_ms": round(float(end) * 1000),
                "speaker": self._speaker_label(item.get("speaker_id"), speaker_labels),
                "confidence": max(0.0, min(1.0, confidence)) if confidence is not None else None,
            })
            pending_prefix = []
        if not timed_words:
            raise ProviderError("TIMESTAMPS_MISSING", "ElevenLabs returned no words with usable timestamps.")

        segments: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []

        def flush() -> None:
            nonlocal current
            if not current:
                return
            parts: list[str] = []
            for word in current:
                self._join_text(parts, word["text"])
            confidences = [word["confidence"] for word in current if word["confidence"] is not None]
            start_ms = current[0]["start_ms"]
            end_ms = current[-1]["end_ms"]
            if segments and start_ms < segments[-1]["end_ms"]:
                overlap = segments[-1]["end_ms"] - start_ms
                if overlap > 250 or segments[-1]["end_ms"] >= end_ms:
                    raise ProviderError("TIMESTAMP_OVERLAP", "ElevenLabs returned overlapping segment timestamps.")
                start_ms = segments[-1]["end_ms"]
            segments.append({
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": "".join(parts).strip(),
                "speaker": current[0]["speaker"],
                "segment_type": "dialogue",
                "confidence": round(sum(confidences) / len(confidences), 6) if confidences else None,
                "timestamp_source": "provider",
            })
            current = []

        terminal = re.compile(r"[。！？!?；;…]$")
        for word in timed_words:
            if current:
                pause_ms = word["start_ms"] - current[-1]["end_ms"]
                duration_ms = current[-1]["end_ms"] - current[0]["start_ms"]
                char_count = sum(len(item["text"]) for item in current)
                if word["speaker"] != current[0]["speaker"] or pause_ms >= 800 or duration_ms >= 6000 or char_count >= 36:
                    flush()
            current.append(word)
            if terminal.search(word["text"]):
                flush()
        flush()

        transcript = str(payload.get("text", "")).strip()
        if not transcript:
            transcript = "".join(segment["text"] for segment in segments)
        provider_id = payload.get("transcription_id") or payload.get("transcript_id") or request_id
        return {
            "provider": "elevenlabs",
            "model": self.configuration.model,
            "detected_language": str(payload.get("language_code") or "unknown"),
            "transcript": transcript,
            "segments": segments,
            "speaker_count": len({segment["speaker"] for segment in segments}),
            "provider_request_id": str(provider_id) if provider_id else None,
            "timestamp_source": "provider",
            "attempt_count": 1,
            "metadata": {
                "language_probability": payload.get("language_probability"),
                "word_count": len(timed_words),
                "confidence_source": "provider_logprob",
            },
        }

    def transcribe_audio(self, audio_path: Path, language: str = "auto") -> dict[str, Any]:
        self.validate_configuration()
        if not audio_path.is_file():
            raise ProviderError("AUDIO_FILE_NOT_FOUND", "The extracted audio file does not exist.", str(audio_path))
        if audio_path.suffix.lower() != ".wav":
            raise ProviderError("AUDIO_FORMAT_INVALID", "A1-1 expects a 16 kHz mono WAV audio file.", str(audio_path))

        data = {
            "model_id": self.configuration.model,
            "tag_audio_events": "false",
            "timestamps_granularity": "word",
            "diarize": "true",
        }
        language_code = self.LANGUAGE_CODES.get(language.strip().lower()) if language else None
        if language_code:
            data["language_code"] = language_code

        attempts = self.max_retries + 1
        last_error: ProviderError | None = None
        for attempt in range(1, attempts + 1):
            try:
                with audio_path.open("rb") as audio_file:
                    response = httpx.post(
                        self.configuration.base_url,
                        headers={"xi-api-key": self.configuration.api_key},
                        data=data,
                        files={"file": (audio_path.name, audio_file, "audio/wav")},
                        timeout=httpx.Timeout(600.0, connect=30.0),
                    )
            except OSError as error:
                raise ProviderError("AUDIO_FILE_NOT_FOUND", "The extracted audio file could not be opened.", str(error), attempt_count=attempt) from error
            except (UnicodeEncodeError, httpx.LocalProtocolError) as error:
                raise ProviderError(
                    "API_KEY_INVALID", "The ElevenLabs API key format is invalid.",
                    "The provider authentication header could not be encoded.", attempt_count=0,
                ) from error
            except httpx.TimeoutException as error:
                last_error = ProviderError("NETWORK_TIMEOUT", "The ElevenLabs request timed out.", str(error), retryable=True, attempt_count=attempt)
            except httpx.HTTPError as error:
                last_error = ProviderError("NETWORK_REQUEST_FAILED", "The ElevenLabs request could not be completed.", str(error), retryable=True, attempt_count=attempt)
            else:
                if response.is_error:
                    last_error = self.map_provider_error(response)
                    last_error.attempt_count = attempt
                else:
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise ProviderError(
                            "PROVIDER_RESPONSE_INVALID", "ElevenLabs returned an invalid JSON response.",
                            response.text[:4000], request_id=self._request_id(response), attempt_count=attempt,
                        ) from error
                    request_id = self._request_id(response)
                    try:
                        result = self.normalize_response(payload, request_id)
                    except ProviderError as error:
                        error.request_id = error.request_id or request_id
                        error.attempt_count = attempt
                        raise
                    result["attempt_count"] = attempt
                    return result
            if not last_error.retryable or attempt >= attempts:
                raise last_error
            time.sleep(1)
        raise last_error or ProviderError("TRANSCRIPTION_REQUEST_FAILED", "The transcription request failed.")


class LocalWhisperDiarizationProvider(TranscriptionProvider):
    """Adapter for the loopback-only faster-whisper + pyannote sidecar."""

    DIARIZATION_MODEL = "pyannote-speaker-diarization-community-1"

    def __init__(
        self,
        configuration: ProviderConfiguration | None = None,
        *,
        client: LocalAudioSidecarClient | None = None,
    ) -> None:
        super().__init__(configuration)
        self.client = client or LocalAudioSidecarClient(self.configuration.base_url)

    def validate_configuration(self) -> None:
        validate_configuration(self.configuration, "transcription")
        if self.configuration.provider != "local_whisper_pyannote":
            raise ProviderError(
                "UNSUPPORTED_PROVIDER",
                "LocalWhisperDiarizationProvider requires an explicit local_whisper_pyannote provider selection.",
            )

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ProviderError(
                "LOCAL_STT_OUTPUT_INVALID", "A local STT result file is missing or invalid.", str(path)
            ) from error

    @staticmethod
    def _output_file(state: dict[str, Any], filename: str) -> Path:
        for value in state.get("output_files") or []:
            path = Path(str(value)).resolve()
            if path.name == filename and path.is_file():
                return path
        raise ProviderError("LOCAL_STT_OUTPUT_INVALID", f"The local STT output is missing {filename}.")

    @staticmethod
    def _confidence(words: list[dict[str, Any]]) -> float | None:
        values = [float(word["probability"]) for word in words if isinstance(word.get("probability"), (int, float))]
        return sum(values) / len(values) if values else None

    def transcribe_audio(
        self,
        audio_path: Path,
        language: str = "auto",
        *,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.validate_configuration()
        if not audio_path.is_file():
            raise ProviderError("AUDIO_FILE_NOT_FOUND", "The extracted audio file does not exist.", str(audio_path))
        job_root = ROOT / "outputs" / "audit" / "local-stt-speaker" / "provider-jobs" / uuid.uuid4().hex
        output_dir = job_root / "result"
        staged_audio = job_root / "input.wav"
        job_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_path, staged_audio)
        try:
            health = self.client.health()
            created = self.client.create_job(staged_audio, output_dir, language=language)
            job_id = str(created.get("job_id") or "").strip()
            if not job_id:
                raise LocalAudioSidecarError("LOCAL_STT_OUTPUT_INVALID", "The local STT sidecar did not return a job id.")
            state = self.client.wait_for_job(job_id, status_callback=status_callback)
        except LocalAudioSidecarError as error:
            raise ProviderError(error.code, str(error), error.detail) from error

        raw_segments = self._read_json(self._output_file(state, "transcript_segments.json"))
        metrics = self._read_json(self._output_file(state, "run_metrics.json"))
        alignment = self._read_json(self._output_file(state, "word_speaker_alignment.json"))
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ProviderError("LOCAL_STT_OUTPUT_INVALID", "The local STT result contains no transcript segments.")
        rows: list[dict[str, Any]] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                raise ProviderError("LOCAL_STT_OUTPUT_INVALID", "A local STT segment is not an object.")
            words = item.get("words") or []
            speaker = str(item.get("speaker_id") or "SPEAKER_UNKNOWN")
            rows.append(
                {
                    "start_ms": round(float(item.get("start_time", 0)) * 1000),
                    "end_ms": round(float(item.get("end_time", 0)) * 1000),
                    "text": str(item.get("text") or "").strip(),
                    "speaker": speaker,
                    "segment_type": "dialogue",
                    "confidence": self._confidence(words),
                    "timestamp_source": "local_word_alignment",
                    "words": words,
                    "overlap": any(bool(word.get("overlap")) for word in words if isinstance(word, dict)),
                    "detected_language": item.get("detected_language"),
                }
            )
        if any(not row["text"] for row in rows):
            raise ProviderError("LOCAL_STT_OUTPUT_INVALID", "A local STT segment has empty text.")
        speakers = {row["speaker"] for row in rows if row["speaker"] != "SPEAKER_UNKNOWN"}
        return {
            "provider": self.configuration.provider,
            "model": self.configuration.model,
            "detected_language": str(metrics.get("detected_language") or raw_segments[0].get("detected_language") or "unknown"),
            "transcript": "".join(row["text"] for row in rows),
            "segments": rows,
            "speaker_count": len(speakers),
            "provider_request_id": job_id,
            "timestamp_source": "local_word_alignment",
            "attempt_count": 1,
            "metadata": {
                "sidecar_job_id": job_id,
                "transcription_model": self.configuration.model,
                "diarization_model": self.DIARIZATION_MODEL,
                "device": health.get("device"),
                "compute_type": health.get("compute_type"),
                "metrics": metrics,
                "word_alignment": alignment,
                "segments": [
                    {
                        "speaker_id": row["speaker"],
                        "words": row["words"],
                        "overlap": row["overlap"],
                    }
                    for row in rows
                ],
            },
        }


class TencentAsrProvider(TranscriptionProvider):
    """Tencent Cloud recording-file recognition via the official Python SDK."""

    MAX_LOCAL_AUDIO_BYTES = 5 * 1024 * 1024
    LANGUAGE_LABELS = {
        "chinese": "zh",
        "english": "en",
        "japanese": "ja",
        "korean": "ko",
        "arabic": "ar",
        "filipino": "fil",
        "french": "fr",
        "hindi": "hi",
        "indonesian": "id",
        "malay": "ms",
        "portugal": "pt",
        "spanish": "es",
        "thai": "th",
        "turkish": "tr",
        "vietnam": "vi",
        "german": "de",
    }

    def __init__(
        self,
        configuration: ProviderConfiguration | None = None,
        *,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 600.0,
    ) -> None:
        super().__init__(configuration)
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.poll_timeout_seconds = max(1.0, float(poll_timeout_seconds))

    def validate_configuration(self) -> None:
        validate_configuration(self.configuration, "transcription")
        if self.configuration.provider != "tencent":
            raise ProviderError(
                "UNSUPPORTED_PROVIDER",
                "TencentAsrProvider requires STT_PROVIDER=tencent.",
            )

    @staticmethod
    def _sdk() -> tuple[Any, Any, Any, Any, Any, Any]:
        try:
            from tencentcloud.asr.v20190614 import asr_client, models
            from tencentcloud.common import credential
            from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
        except ImportError as error:
            raise ProviderError(
                "TRANSCRIPTION_SDK_MISSING",
                "Tencent Cloud ASR SDK is not installed.",
            ) from error
        return credential, TencentCloudSDKException, ClientProfile, HttpProfile, asr_client, models

    def _client(self) -> tuple[Any, Any, Any]:
        credential, sdk_error, client_profile_type, http_profile_type, asr_client, models = self._sdk()
        http_profile = http_profile_type()
        http_profile.endpoint = self.configuration.endpoint
        client_profile = client_profile_type(httpProfile=http_profile)
        client = asr_client.AsrClient(
            credential.Credential(
                self.configuration.secret_id,
                self.configuration.secret_key,
            ),
            self.configuration.region,
            client_profile,
        )
        return client, models, sdk_error

    @staticmethod
    def _detected_language(details: list[Any], transcript: str, requested_language: str) -> str:
        labels = [
            TencentAsrProvider.LANGUAGE_LABELS.get(
                str(getattr(item, "LangType", "") or "").strip().lower()
            )
            for item in details
        ]
        labels = [label for label in labels if label]
        if labels:
            return max(set(labels), key=labels.count)
        normalized_requested = str(requested_language or "").strip().lower()
        if normalized_requested not in {"", "auto"}:
            return normalized_requested
        chinese_count = len(re.findall(r"[\u3400-\u9fff]", transcript))
        latin_count = len(re.findall(r"[A-Za-z]", transcript))
        if chinese_count or latin_count:
            return "zh" if chinese_count >= latin_count else "en"
        return "unknown"

    def normalize_task_status(
        self,
        status: Any,
        *,
        requested_language: str,
        create_request_id: str | None,
        describe_request_id: str | None,
        poll_count: int,
    ) -> dict[str, Any]:
        details = list(getattr(status, "ResultDetail", None) or [])
        speaker_keys = sorted({
            str(getattr(item, "SpeakerRoleName", None) or getattr(item, "SpeakerId", 0))
            for item in details
        })
        speaker_labels = {
            key: f"Speaker {index + 1:02d}"
            for index, key in enumerate(speaker_keys)
        }
        segments: list[dict[str, Any]] = []
        for item in sorted(
            details,
            key=lambda detail: (
                int(getattr(detail, "StartMs", -1) or -1),
                int(getattr(detail, "EndMs", -1) or -1),
            ),
        ):
            text = str(
                getattr(item, "FinalSentence", None)
                or getattr(item, "WrittenText", None)
                or ""
            ).strip()
            start_ms = getattr(item, "StartMs", None)
            end_ms = getattr(item, "EndMs", None)
            if not text:
                continue
            if not isinstance(start_ms, int) or not isinstance(end_ms, int):
                raise ProviderError(
                    "TIMESTAMPS_MISSING",
                    "Tencent Cloud ASR omitted sentence timestamps.",
                )
            if start_ms < 0 or start_ms >= end_ms:
                raise ProviderError(
                    "TIMESTAMPS_INVALID",
                    "Tencent Cloud ASR returned an invalid sentence time range.",
                )
            if segments and start_ms < segments[-1]["end_ms"]:
                overlap_ms = segments[-1]["end_ms"] - start_ms
                if overlap_ms > 250 or segments[-1]["end_ms"] >= end_ms:
                    raise ProviderError(
                        "TIMESTAMP_OVERLAP",
                        "Tencent Cloud ASR returned overlapping sentence timestamps.",
                    )
                start_ms = segments[-1]["end_ms"]
            speaker_key = str(
                getattr(item, "SpeakerRoleName", None)
                or getattr(item, "SpeakerId", 0)
            )
            segments.append({
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "speaker": speaker_labels.get(speaker_key, "Speaker 01"),
                "segment_type": "dialogue",
                "confidence": None,
                "timestamp_source": "provider",
            })
        if not segments:
            raise ProviderError(
                "TRANSCRIPTION_EMPTY",
                "Tencent Cloud ASR returned no sentence-level transcript.",
                request_id=create_request_id,
                attempt_count=1,
            )
        transcript = "\n".join(segment["text"] for segment in segments)
        return {
            "provider": "tencent",
            "model": self.configuration.model,
            "detected_language": self._detected_language(
                details, transcript, requested_language
            ),
            "transcript": transcript,
            "segments": segments,
            "speaker_count": len({segment["speaker"] for segment in segments}),
            "provider_request_id": create_request_id,
            "timestamp_source": "provider",
            "attempt_count": 1,
            "metadata": {
                "tencent_task_id": getattr(status, "TaskId", None),
                "describe_request_id": describe_request_id,
                "poll_count": poll_count,
                "audio_duration": getattr(status, "AudioDuration", None),
                "result_detail_count": len(details),
                "source_type": "local_audio_data",
            },
        }

    @staticmethod
    def _provider_error(error: Exception, sdk_error: type[Exception]) -> ProviderError:
        if isinstance(error, sdk_error):
            error_code = str(getattr(error, "code", "") or "")
            request_id = getattr(error, "requestId", None) or getattr(error, "request_id", None)
            safe_message = redact_sensitive_text(getattr(error, "message", None) or str(error))
            mapped_code = (
                "API_KEY_INVALID"
                if error_code.startswith("AuthFailure")
                else "ASR_SERVICE_NOT_OPEN"
                if error_code == "FailedOperation.UserNotRegistered"
                else "QUOTA_EXCEEDED"
                if error_code in {
                    "FailedOperation.ServiceIsolate",
                    "FailedOperation.UserHasNoAmount",
                    "FailedOperation.UserHasNoFreeAmount",
                }
                else "RATE_LIMITED"
                if "Limit" in error_code or "RequestLimitExceeded" in error_code
                else "TRANSCRIPTION_REQUEST_INVALID"
                if error_code.startswith(("InvalidParameter", "MissingParameter"))
                else "TRANSCRIPTION_PROVIDER_ERROR"
            )
            safe_provider_message = (
                "Tencent Cloud ASR service or the selected recognition model is not enabled."
                if mapped_code == "ASR_SERVICE_NOT_OPEN"
                else "Tencent Cloud ASR quota is unavailable."
                if mapped_code == "QUOTA_EXCEEDED"
                else safe_message or "Tencent Cloud ASR request failed."
            )
            return ProviderError(
                mapped_code,
                safe_provider_message,
                request_id=request_id,
                attempt_count=1,
                diagnostics={
                    "provider": "tencent",
                    "sdk_error_code": error_code or None,
                    "request_id": request_id,
                },
            )
        return ProviderError(
            "NETWORK_REQUEST_FAILED",
            "Tencent Cloud ASR request could not be completed.",
            redact_sensitive_text(error),
            attempt_count=1,
        )

    def transcribe_audio(self, audio_path: Path, language: str = "auto") -> dict[str, Any]:
        self.validate_configuration()
        if not audio_path.is_file():
            raise ProviderError(
                "AUDIO_FILE_NOT_FOUND",
                "The extracted audio file does not exist.",
                str(audio_path),
            )
        if audio_path.suffix.lower() != ".wav":
            raise ProviderError(
                "AUDIO_FORMAT_INVALID",
                "Tencent Cloud ASR expects the extracted 16 kHz mono WAV file.",
                str(audio_path),
            )
        audio = audio_path.read_bytes()
        if not audio:
            raise ProviderError("AUDIO_FORMAT_INVALID", "The extracted WAV audio is empty.")
        if len(audio) > self.MAX_LOCAL_AUDIO_BYTES:
            raise ProviderError(
                "AUDIO_FILE_TOO_LARGE",
                "Tencent Cloud local-audio recognition accepts files up to 5 MB.",
                f"audio_bytes={len(audio)}",
            )

        client, models, sdk_error = self._client()
        create_request = models.CreateRecTaskRequest()
        create_request.EngineModelType = self.configuration.model
        create_request.ChannelNum = 1
        create_request.ResTextFormat = 3
        create_request.SourceType = 1
        create_request.Data = base64.b64encode(audio).decode("ascii")
        create_request.DataLen = len(audio)
        create_request.SpeakerDiarization = 1
        create_request.SpeakerNumber = 0
        create_request.FilterDirty = 0
        create_request.FilterModal = 0
        create_request.FilterPunc = 0
        create_request.ConvertNumMode = 1
        try:
            create_response = client.CreateRecTask(create_request)
        except Exception as error:
            raise self._provider_error(error, sdk_error) from error
        task_id = getattr(getattr(create_response, "Data", None), "TaskId", None)
        create_request_id = getattr(create_response, "RequestId", None)
        if not isinstance(task_id, int):
            raise ProviderError(
                "PROVIDER_RESPONSE_INVALID",
                "Tencent Cloud ASR did not return a TaskId.",
                request_id=create_request_id,
                attempt_count=1,
            )

        deadline = time.monotonic() + self.poll_timeout_seconds
        poll_count = 0
        describe_request_id: str | None = None
        while time.monotonic() < deadline:
            describe_request = models.DescribeTaskStatusRequest()
            describe_request.TaskId = task_id
            try:
                describe_response = client.DescribeTaskStatus(describe_request)
            except Exception as error:
                raise self._provider_error(error, sdk_error) from error
            poll_count += 1
            describe_request_id = getattr(describe_response, "RequestId", None)
            status = getattr(describe_response, "Data", None)
            status_code = getattr(status, "Status", None)
            if status_code == 2:
                return self.normalize_task_status(
                    status,
                    requested_language=language,
                    create_request_id=create_request_id,
                    describe_request_id=describe_request_id,
                    poll_count=poll_count,
                )
            if status_code == 3:
                raise ProviderError(
                    "TRANSCRIPTION_PROVIDER_ERROR",
                    "Tencent Cloud ASR could not recognize the audio.",
                    redact_sensitive_text(getattr(status, "ErrorMsg", None) or ""),
                    request_id=describe_request_id or create_request_id,
                    attempt_count=1,
                    diagnostics={
                        "provider": "tencent",
                        "tencent_task_id": task_id,
                        "status": getattr(status, "StatusStr", None),
                    },
                )
            if status_code not in {0, 1}:
                raise ProviderError(
                    "PROVIDER_RESPONSE_INVALID",
                    "Tencent Cloud ASR returned an unknown task status.",
                    request_id=describe_request_id or create_request_id,
                    attempt_count=1,
                    diagnostics={
                        "provider": "tencent",
                        "tencent_task_id": task_id,
                        "status": status_code,
                    },
                )
            time.sleep(self.poll_interval_seconds)
        raise ProviderError(
            "NETWORK_TIMEOUT",
            "Tencent Cloud ASR did not finish before the polling deadline.",
            request_id=describe_request_id or create_request_id,
            attempt_count=1,
            diagnostics={
                "provider": "tencent",
                "tencent_task_id": task_id,
                "poll_count": poll_count,
            },
        )


class TranslationProvider:
    """OpenAI-compatible structured translation adapter used by A1."""

    def __init__(self, configuration: ProviderConfiguration | None = None) -> None:
        self.configuration = configuration or translation_configuration()

    def validate_configuration(self) -> None:
        validate_configuration(self.configuration, "translation")

    def supported_languages(self) -> list[str]:
        return TranscriptionProvider().supported_languages()[1:]

    @staticmethod
    def language_display_name(value: str) -> str:
        normalized = re.sub(r"[-_]+", " ", str(value or "").strip())
        return normalized.title()

    def normalize(self, payload: Any, source_segments: list[dict[str, Any]]) -> list[dict[str, str]]:
        translated = payload.get("segments", payload if isinstance(payload, list) else [])
        by_id = {str(item.get("id")): item for item in translated}
        if any(item["id"] not in by_id for item in source_segments):
            raise ProviderError("TRANSLATION_INCOMPLETE_OUTPUT", "Translation provider omitted one or more segments.")
        result = [
            {
                "id": item["id"],
                "literal": str(by_id[item["id"]].get("literal", "")).strip(),
                "localized": str(by_id[item["id"]].get("localized", "")).strip(),
                "final": str(by_id[item["id"]].get("final") or by_id[item["id"]].get("localized") or by_id[item["id"]].get("literal") or "").strip(),
            }
            for item in source_segments
        ]
        if any(not item["literal"] or not item["localized"] or not item["final"] for item in result):
            raise ProviderError(
                "TRANSLATION_INCOMPLETE_OUTPUT",
                "Translation provider returned an empty literal, localized, or final translation.",
            )
        return result


class DeepSeekTranslationProvider(TranslationProvider):
    """DeepSeek JSON-output adapter for one-call structured subtitle translation."""

    def validate_configuration(self) -> None:
        if self.configuration.api_key and not self.configuration.api_key_has_valid_transport_format:
            raise ProviderError("API_KEY_INVALID", "The DeepSeek API key format is invalid.")
        validate_configuration(self.configuration, "translation")
        if self.configuration.provider != "deepseek":
            raise ProviderError("UNSUPPORTED_PROVIDER", "DeepSeekTranslationProvider requires TRANSLATION_PROVIDER=deepseek.")

    @staticmethod
    def _request_id(response: httpx.Response, payload: dict[str, Any] | None = None) -> str | None:
        return (
            (str(payload.get("id")) if payload and payload.get("id") else None)
            or response.headers.get("x-request-id")
            or response.headers.get("request-id")
        )

    def map_network_error(
        self,
        error: BaseException,
        *,
        method: str,
        url: str,
        operation: str,
    ) -> ProviderError:
        diagnostics = _network_exception_diagnostics(
            error,
            method=method,
            url=url,
            attempt_count=1,
        )
        diagnostics["provider"] = "deepseek"
        diagnostics["operation"] = operation
        if diagnostics.get("winerror") is None and diagnostics.get("errno") == 10013:
            diagnostics["winerror"] = 10013
        if diagnostics.get("winerror") == 10013 or diagnostics.get("errno") == 10013:
            code = "DEEPSEEK_NETWORK_DENIED"
            message = "Windows denied the DeepSeek network connection."
        elif diagnostics.get("proxy_error"):
            code = "DEEPSEEK_PROXY_FAILED"
            message = "The configured proxy could not connect to DeepSeek."
        elif diagnostics.get("dns_error"):
            code = "DEEPSEEK_DNS_FAILED"
            message = "DeepSeek hostname resolution failed."
        elif diagnostics.get("tls_error"):
            code = "DEEPSEEK_TLS_FAILED"
            message = "The TLS connection to DeepSeek failed."
        elif diagnostics.get("timeout"):
            code = "DEEPSEEK_TIMEOUT"
            message = "The DeepSeek request timed out."
        else:
            code = "DEEPSEEK_INTERNAL_ERROR"
            message = "The DeepSeek request failed before a response was received."
        return ProviderError(
            code,
            message,
            retryable=False,
            attempt_count=1,
            diagnostics=diagnostics,
        )

    def map_provider_error(self, response: httpx.Response) -> ProviderError:
        request_id = self._request_id(response)
        body = redact_sensitive_text(response.text)[:4000]
        lowered = body.lower()
        diagnostics = _response_diagnostics(
            response,
            method=response.request.method,
            url=str(response.request.url),
            attempt_count=1,
            sensitive_values=(self.configuration.api_key,),
        )
        diagnostics["provider"] = "deepseek"
        if response.status_code == 401:
            return ProviderError(
                "DEEPSEEK_AUTH_FAILED",
                "DeepSeek rejected the configured credentials.",
                body,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        if response.status_code == 402 or any(term in lowered for term in ("balance", "quota", "insufficient", "payment")):
            return ProviderError(
                "QUOTA_EXCEEDED",
                "The DeepSeek account has insufficient balance or quota.",
                body,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        if response.status_code == 429:
            return ProviderError(
                "DEEPSEEK_RATE_LIMITED",
                "DeepSeek rate-limited the translation request.",
                body,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        if response.status_code == 403 or "content_filter" in lowered:
            return ProviderError(
                "CONTENT_MODERATION_REJECTED",
                "DeepSeek rejected the translation request under its content policy.",
                body,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        if response.status_code in {400, 404, 422}:
            return ProviderError(
                "DEEPSEEK_RESPONSE_INVALID",
                "DeepSeek rejected the request format or configured model.",
                body,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        if response.status_code >= 500:
            return ProviderError(
                "DEEPSEEK_INTERNAL_ERROR",
                "DeepSeek is temporarily unavailable.",
                body,
                retryable=False,
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            )
        return ProviderError(
            "DEEPSEEK_INTERNAL_ERROR",
            f"DeepSeek returned HTTP {response.status_code}.",
            body,
            request_id=request_id,
            attempt_count=1,
            diagnostics=diagnostics,
        )

    def probe_model_availability(self) -> dict[str, Any]:
        """Verify credentials and configured model without sending subtitle text."""
        self.validate_configuration()
        url = f"{self.configuration.base_url}/models"
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {self.configuration.api_key}"},
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
        except (UnicodeEncodeError, httpx.LocalProtocolError) as error:
            raise ProviderError(
                "DEEPSEEK_AUTH_FAILED",
                "The DeepSeek authentication header is invalid.",
                attempt_count=0,
            ) from error
        except httpx.HTTPError as error:
            raise self.map_network_error(
                error,
                method="GET",
                url=url,
                operation="model_probe",
            ) from error
        if response.is_error:
            raise self.map_provider_error(response)
        try:
            payload = response.json()
            model_ids = {
                str(item["id"])
                for item in payload["data"]
                if isinstance(item, dict) and item.get("id")
            }
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID",
                "DeepSeek returned an invalid model catalog.",
                request_id=self._request_id(response),
                attempt_count=1,
            ) from error
        configured_model_available = self.configuration.model in model_ids
        if not configured_model_available:
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID",
                "The configured DeepSeek model is not available to this account.",
                request_id=self._request_id(response, payload),
                attempt_count=1,
                diagnostics={
                    "provider": "deepseek",
                    "configured_model": self.configuration.model,
                    "available_model_count": len(model_ids),
                },
            )
        return {
            "provider": "deepseek",
            "model": self.configuration.model,
            "configured_model_available": True,
            "available_model_count": len(model_ids),
            "provider_request_id": self._request_id(response, payload),
            "attempt_count": 1,
        }

    def normalize_response(
        self,
        payload: dict[str, Any],
        source_segments: list[dict[str, Any]],
        response: httpx.Response,
    ) -> dict[str, Any]:
        request_id = self._request_id(response, payload)
        try:
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned an unexpected response format.",
                response.text[:4000], request_id=request_id, attempt_count=1,
            ) from error
        if finish_reason == "content_filter":
            raise ProviderError(
                "CONTENT_MODERATION_REJECTED", "DeepSeek filtered the translation response.",
                request_id=request_id, attempt_count=1,
            )
        if finish_reason == "length":
            raise ProviderError(
                "DEEPSEEK_TRANSLATION_INCOMPLETE", "DeepSeek truncated the structured translation output.",
                request_id=request_id, attempt_count=1,
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned empty translation content.",
                request_id=request_id, attempt_count=1,
            )
        cleaned = content.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            structured = json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned invalid JSON translation output.",
                "The JSON response could not be parsed.", request_id=request_id, attempt_count=1,
            ) from error
        try:
            translations = self.normalize(structured, source_segments)
        except ProviderError as error:
            if error.code != "TRANSLATION_INCOMPLETE_OUTPUT":
                raise
            raise ProviderError(
                "DEEPSEEK_TRANSLATION_INCOMPLETE",
                str(error),
                request_id=request_id,
                attempt_count=1,
            ) from error
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "provider": "deepseek",
            "model": self.configuration.model,
            "translations": translations,
            "provider_request_id": request_id,
            "attempt_count": 1,
            "usage": usage,
        }

    def translate_segments(
        self,
        segments: list[dict[str, Any]],
        source_language: str,
        target_language: str,
        style: str,
    ) -> dict[str, Any]:
        self.validate_configuration()
        source_language_display = self.language_display_name(source_language)
        target_language_display = self.language_display_name(target_language)
        compact = []
        for index, item in enumerate(segments):
            start_ms = int(item.get("start_ms") or 0)
            end_ms = int(item.get("end_ms") or start_ms)
            available_seconds = max(0.1, (end_ms - start_ms) / 1000)
            compact.append({
                "id": item["id"],
                "source_text": item["source_text"],
                "speaker": item.get("speaker_label") or item.get("speaker") or "Speaker 01",
                "dialogue_role": item.get("segment_type") or "dialogue",
                "available_seconds": round(available_seconds, 3),
                "estimated_max_words": max(1, math.floor(available_seconds * 2.6)),
                "previous_source_text": segments[index - 1]["source_text"] if index > 0 else None,
                "next_source_text": segments[index + 1]["source_text"] if index + 1 < len(segments) else None,
            })
        style_instruction = {
            "literal": f"Keep the final {target_language_display} subtitle close to the literal meaning.",
            "natural": f"Make the final subtitle natural, concise spoken {target_language_display}.",
            "short_drama_localization": f"Localize the final subtitle as concise {target_language_display} short-drama dialogue.",
            "commercial_voiceover": f"Make the final subtitle suitable for concise {target_language_display} commercial voiceover.",
        }.get(style, f"Make the final {target_language_display} subtitle natural and concise.")
        system_prompt = (
            f"You are a professional subtitle translator. Translate from {source_language_display} to "
            f"{target_language_display}. Return one valid JSON object only. "
            "Use exactly this JSON schema: {\"segments\":[{\"id\":\"unchanged id\","
            f"\"literal\":\"faithful {target_language_display} translation\","
            f"\"localized\":\"idiomatic {target_language_display} translation\","
            f"\"final\":\"concise final {target_language_display} subtitle\"}}]}}. "
            "Return every input id exactly once and never merge, omit, reorder, or invent segments. "
            f"All literal, localized, and final fields must be non-empty strings written in {target_language_display}. "
            "The final field is also the spoken TTS script. Keep it within estimated_max_words for its own "
            "available_seconds. Preserve names, numbers, money, time, negation, plot facts, and relationships. "
            "Remove repetition, filler, and decorative phrasing first. Use adjacent source lines only as context; "
            "never move information or timing between segments."
        )
        user_prompt = (
            f"Source language: {source_language_display}. Target language: {target_language_display}. "
            f"{style_instruction} Translate this JSON input: "
            f"{json.dumps({'segments': compact}, ensure_ascii=False)}"
        )
        request_body: dict[str, Any] = {
            "model": self.configuration.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 4096,
            "stream": False,
        }
        if self.configuration.model.startswith("deepseek-v4"):
            request_body["thinking"] = {"type": "disabled"}
        try:
            response = httpx.post(
                f"{self.configuration.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.configuration.api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=httpx.Timeout(600.0, connect=30.0),
            )
        except (UnicodeEncodeError, httpx.LocalProtocolError) as error:
            raise ProviderError(
                "DEEPSEEK_AUTH_FAILED", "The DeepSeek API key format is invalid.",
                "The provider authentication header could not be encoded.", attempt_count=0,
            ) from error
        except httpx.HTTPError as error:
            raise self.map_network_error(
                error,
                method="POST",
                url=f"{self.configuration.base_url}/chat/completions",
                operation="translate_segments",
            ) from error
        if response.is_error:
            error = self.map_provider_error(response)
            error.attempt_count = 1
            raise error
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError(
                "DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned invalid JSON at the HTTP response level.",
                "The HTTP response body could not be parsed as JSON.",
                request_id=self._request_id(response), attempt_count=1,
            ) from error
        return self.normalize_response(payload, segments, response)

    def suggest_shorter_text(
        self,
        source_text: str,
        current_text: str,
        speaker: str,
        max_available_duration: float,
        previous_text: str | None,
        next_text: str | None,
        target_language: str,
        *,
        actual_tts_duration: float | None = None,
        retry_index: int = 1,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Return one meaning-preserving spoken line for a measured TTS slot."""
        self.validate_configuration()
        estimated_max_words = max(1, math.floor(float(max_available_duration) * 2.6))
        system_prompt = (
            "You rewrite translated short-drama dialogue to fit a strict spoken-audio duration. Return one "
            "valid JSON object only with exactly shorter_text and meaning_preserved. Preserve plot facts, "
            "character relationships, names, money amounts, times, and all negation. Add no information. "
            "Remove repetition, rhetoric, and filler before removing meaningful facts. The result must be "
            "grammatical, natural spoken English, materially shorter than current_english, and no longer "
            "than estimated_max_words. Treat source_text, current_english, and max_available_duration as "
            "required inputs rather than optional context."
        )
        request_context = {
            "source_text": source_text,
            "current_english": current_text,
            "speaker": speaker,
            "max_available_duration": round(float(max_available_duration), 3),
            "estimated_max_words": estimated_max_words,
            "actual_tts_duration": round(float(actual_tts_duration), 3) if actual_tts_duration else None,
            "shortening_attempt": int(retry_index),
            "maximum_attempts": int(max_retries),
            "previous_context": previous_text or "",
            "next_context": next_text or "",
            "target_language": target_language,
            "hard_constraints": [
                "The spoken line must finish within max_available_duration.",
                "shorter_text must contain no more than estimated_max_words English words.",
                "Do not change names, amounts, times, or negation.",
                "Do not merge this line with adjacent context.",
            ],
        }
        request_body: dict[str, Any] = {
            "model": self.configuration.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(request_context, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 256,
            "stream": False,
        }
        if self.configuration.model.startswith("deepseek-v4"):
            request_body["thinking"] = {"type": "disabled"}
        try:
            response = httpx.post(
                f"{self.configuration.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.configuration.api_key}", "Content-Type": "application/json"},
                json=request_body,
                timeout=httpx.Timeout(120.0, connect=30.0),
            )
        except httpx.HTTPError as error:
            raise self.map_network_error(
                error,
                method="POST",
                url=f"{self.configuration.base_url}/chat/completions",
                operation="suggest_shorter_text",
            ) from error
        if response.is_error:
            error = self.map_provider_error(response)
            error.attempt_count = 1
            raise error
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.strip().startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
            result = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ProviderError("DEEPSEEK_RESPONSE_INVALID", "DeepSeek returned invalid shortening JSON.", response.text[:4000], request_id=self._request_id(response), attempt_count=1) from error
        shorter_text = str(result.get("shorter_text") or "").strip()
        meaning_preserved = bool(result.get("meaning_preserved"))
        if not shorter_text or not meaning_preserved or len(shorter_text) >= len(current_text.strip()):
            raise ProviderError("TRANSLATION_INVALID_OUTPUT", "DeepSeek did not return a shorter meaning-preserving line.", request_id=self._request_id(response), attempt_count=1)
        return {
            "shorter_text": shorter_text,
            "meaning_preserved": True,
            "provider": "deepseek",
            "model": self.configuration.model,
            "provider_request_id": self._request_id(response, payload),
        }


class TTSProvider:
    """Base interface for real text-to-speech providers."""

    def __init__(self, configuration: TTSConfiguration | None = None) -> None:
        self.configuration = configuration or tts_configuration()

    def validate_configuration(self) -> None:
        if self.configuration.api_key and not self.configuration.api_key_has_valid_transport_format:
            raise ProviderError("API_KEY_INVALID", "The ElevenLabs API key format is invalid.")
        if not self.configuration.configured:
            missing = ", ".join(self.configuration.public_status()["missing"])
            raise ProviderError(
                "TTS_PROVIDER_DISABLED",
                f"Text-to-speech provider is not configured. Missing: {missing}.",
            )

    def list_voices(self) -> list[dict[str, Any]]:
        raise ProviderError("UNSUPPORTED_PROVIDER", "The configured provider does not implement voice listing.")

    def generate_speech(
        self,
        text: str,
        voice_id: str,
        *,
        session_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> dict[str, Any]:
        raise ProviderError("UNSUPPORTED_PROVIDER", "The configured provider does not implement text to speech.")


class ElevenLabsTTSProvider(TTSProvider):
    """ElevenLabs voice catalog and non-streaming text-to-speech adapter."""

    MAX_NETWORK_ATTEMPTS = 2

    def __init__(self, configuration: TTSConfiguration | None = None) -> None:
        super().__init__(configuration)
        self.last_attempt_count = 0

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        return response.headers.get("request-id") or response.headers.get("x-request-id")

    @staticmethod
    def _error_markers(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        return json.dumps(payload, ensure_ascii=True).lower()[:4000]

    def map_provider_error(
        self,
        response: httpx.Response,
        *,
        voice_id: str | None = None,
        method: str = "POST",
        url: str | None = None,
        attempt_count: int = 1,
    ) -> ProviderError:
        request_id = self._request_id(response)
        markers = self._error_markers(response)
        request_url = url or str(response.request.url)
        diagnostics = _response_diagnostics(
            response,
            method=method,
            url=request_url,
            attempt_count=attempt_count,
            sensitive_values=(self.configuration.api_key,),
        )
        safe_detail = json.dumps(diagnostics, ensure_ascii=True)
        if response.status_code == 401 and "voices_read" in markers:
            return ProviderError(
                "VOICE_LIST_PERMISSION_MISSING",
                "The current ElevenLabs API key does not have voices_read permission.",
                safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics,
            )
        if response.status_code == 401 and "text_to_speech" in markers:
            return ProviderError(
                "TTS_PERMISSION_MISSING",
                "The current ElevenLabs API key does not have Text to Speech permission.",
                safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics,
            )
        if response.status_code == 401 and "missing_permissions" in markers:
            return ProviderError(
                "ELEVENLABS_PERMISSION_MISSING",
                "The current ElevenLabs API key is missing a required permission.",
                safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics,
            )
        if response.status_code == 401:
            return ProviderError("API_KEY_INVALID", "The ElevenLabs API key is invalid.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code == 402 or any(term in markers for term in ("quota", "credit", "balance", "payment")):
            return ProviderError("QUOTA_EXCEEDED", "The ElevenLabs account has insufficient credits.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code == 429:
            return ProviderError("RATE_LIMITED", "ElevenLabs rate-limited the text-to-speech request.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code == 403:
            return ProviderError("VOICE_PERMISSION_DENIED", "The selected ElevenLabs voice is not available to this account.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code == 404:
            message = "The selected ElevenLabs voice does not exist." if voice_id else "The ElevenLabs resource was not found."
            return ProviderError("VOICE_NOT_FOUND", message, safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code in {400, 422}:
            code = "MODEL_UNAVAILABLE" if "model" in markers else "TTS_REQUEST_INVALID"
            message = "The configured ElevenLabs TTS model is unavailable." if code == "MODEL_UNAVAILABLE" else "ElevenLabs rejected the text-to-speech request."
            return ProviderError(code, message, safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        if response.status_code >= 500:
            return ProviderError("PROVIDER_UNAVAILABLE", "ElevenLabs Text to Speech is temporarily unavailable.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)
        return ProviderError("TTS_PROVIDER_ERROR", f"ElevenLabs returned HTTP {response.status_code}.", safe_detail, request_id=request_id, attempt_count=attempt_count, diagnostics=diagnostics)

    @staticmethod
    def _is_transient_network_error(error: httpx.HTTPError, diagnostics: dict[str, Any]) -> bool:
        if diagnostics.get("dns_error") or diagnostics.get("tls_error") or diagnostics.get("proxy_error"):
            return False
        return isinstance(
            error,
            (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.RemoteProtocolError,
            ),
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout_message: str,
        network_message: str,
        voice_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response, int]:
        request = httpx.get if method == "GET" else httpx.post
        for attempt in range(1, self.MAX_NETWORK_ATTEMPTS + 1):
            self.last_attempt_count = attempt
            try:
                response = request(url, **kwargs)
            except (UnicodeEncodeError, httpx.LocalProtocolError) as error:
                diagnostics = _network_exception_diagnostics(
                    error, method=method, url=url, attempt_count=0
                )
                raise ProviderError(
                    "API_KEY_INVALID",
                    "The ElevenLabs API key format is invalid.",
                    attempt_count=0,
                    diagnostics=diagnostics,
                ) from error
            except httpx.HTTPError as error:
                diagnostics = _network_exception_diagnostics(
                    error, method=method, url=url, attempt_count=attempt
                )
                retryable = self._is_transient_network_error(error, diagnostics)
                code = "NETWORK_TIMEOUT" if diagnostics["timeout"] else "NETWORK_REQUEST_FAILED"
                message = timeout_message if diagnostics["timeout"] else network_message
                provider_error = ProviderError(
                    code,
                    message,
                    retryable=retryable,
                    attempt_count=attempt,
                    diagnostics=diagnostics,
                )
                logger.warning(
                    "ElevenLabs request failed code=%s attempt=%s diagnostics=%s",
                    code,
                    attempt,
                    json.dumps(diagnostics, ensure_ascii=True),
                )
                if not retryable or attempt >= self.MAX_NETWORK_ATTEMPTS:
                    raise provider_error from error
                time.sleep(0.25)
                continue
            if response.is_error:
                raise self.map_provider_error(
                    response,
                    voice_id=voice_id,
                    method=method,
                    url=url,
                    attempt_count=attempt,
                )
            return response, attempt
        raise ProviderError("NETWORK_REQUEST_FAILED", network_message)

    @staticmethod
    def normalize_voice(payload: dict[str, Any]) -> dict[str, Any]:
        labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}
        verified = payload.get("verified_languages") if isinstance(payload.get("verified_languages"), list) else []
        return {
            "voice_id": str(payload.get("voice_id") or ""),
            "name": str(payload.get("name") or "Unnamed voice"),
            "category": payload.get("category"),
            "labels": {str(key): str(value) for key, value in labels.items()},
            "description": payload.get("description"),
            "preview_url": payload.get("preview_url"),
            "verified_languages": [
                {
                    "language": item.get("language"),
                    "locale": item.get("locale"),
                    "accent": item.get("accent"),
                    "model_id": item.get("model_id"),
                }
                for item in verified if isinstance(item, dict)
            ],
            "high_quality_base_model_ids": payload.get("high_quality_base_model_ids") or [],
        }

    def list_voices(self) -> list[dict[str, Any]]:
        self.validate_configuration()
        url = f"{self.configuration.base_url}/v2/voices"
        response, _attempt = self._request(
            "GET",
            url,
            timeout_message="The ElevenLabs voice list request timed out.",
            network_message="The ElevenLabs voice list request failed.",
            headers={"xi-api-key": self.configuration.api_key},
            params={"page_size": 100, "include_total_count": "false", "sort": "name", "sort_direction": "asc"},
            timeout=httpx.Timeout(60.0, connect=20.0),
        )
        try:
            payload = response.json()
            raw_voices = payload["voices"]
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError("PROVIDER_RESPONSE_INVALID", "ElevenLabs returned an invalid voice catalog response.") from error
        voices = [self.normalize_voice(item) for item in raw_voices if isinstance(item, dict)]
        return [voice for voice in voices if voice["voice_id"]]

    def normalize_response(self, response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").lower()
        if not response.content or (content_type and not content_type.startswith("audio/")):
            raise ProviderError(
                "TTS_AUDIO_INVALID",
                "ElevenLabs returned an invalid audio response.",
                f"Unexpected content type: {content_type or 'missing'}.",
                request_id=self._request_id(response),
            )
        character_cost = response.headers.get("character-cost")
        usage: dict[str, Any] = {}
        if character_cost:
            try:
                usage["character_cost"] = int(character_cost)
            except ValueError:
                usage["character_cost"] = character_cost
        return {
            "provider": "elevenlabs",
            "model": self.configuration.model,
            "output_format": self.configuration.output_format,
            "audio": response.content,
            "provider_request_id": self._request_id(response),
            "usage": usage,
        }

    def generate_speech(
        self,
        text: str,
        voice_id: str,
        *,
        session_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> dict[str, Any]:
        self.validate_configuration()
        clean_text = text.strip()
        if not clean_text:
            raise ProviderError("TTS_TEXT_EMPTY", "The final translation is empty.")
        if not voice_id.strip():
            raise ProviderError("VOICE_REQUIRED", "Select an ElevenLabs voice before generating speech.")
        url = f"{self.configuration.base_url}/v1/text-to-speech/{voice_id}"
        response, attempt = self._request(
            "POST",
            url,
            timeout_message="The ElevenLabs text-to-speech request timed out.",
            network_message="The ElevenLabs text-to-speech request failed.",
            voice_id=voice_id,
            headers={"xi-api-key": self.configuration.api_key, "Content-Type": "application/json"},
            params={"output_format": self.configuration.output_format},
            json={"text": clean_text, "model_id": self.configuration.model},
            timeout=httpx.Timeout(180.0, connect=30.0),
        )
        result = self.normalize_response(response)
        result["attempt_count"] = attempt
        return result


def tencent_speed_for_ratio(speed_ratio: float) -> float:
    """Map a playback multiplier to Tencent's documented -2..6 Speed scale."""
    ratio = min(2.5, max(0.6, float(speed_ratio or 1.0)))
    points = ((0.6, -2.0), (0.8, -1.0), (1.0, 0.0), (1.2, 1.0), (1.5, 2.0), (2.5, 6.0))
    for index in range(1, len(points)):
        left_ratio, left_speed = points[index - 1]
        right_ratio, right_speed = points[index]
        if ratio <= right_ratio:
            progress = (ratio - left_ratio) / (right_ratio - left_ratio)
            return round(left_speed + progress * (right_speed - left_speed), 2)
    return 6.0


class TencentTTSProvider(TTSProvider):
    """Tencent Cloud TextToVoice adapter using the official server-side Python SDK."""

    @staticmethod
    def _voice_type(voice_id: str) -> int:
        normalized = str(voice_id or "").strip()
        if normalized.startswith("tencent:"):
            normalized = normalized.split(":", 1)[1]
        if not normalized.isdigit():
            raise ProviderError(
                "VOICE_REQUIRED",
                "请选择腾讯音色。ElevenLabs voice_id 不能用于腾讯云 TTS。",
            )
        return int(normalized)

    def validate_configuration(self) -> None:
        if not self.configuration.configured:
            missing = ", ".join(self.configuration.public_status()["missing"])
            raise ProviderError(
                "TTS_PROVIDER_DISABLED",
                f"Tencent Cloud Text-to-Speech is not configured. Missing: {missing}.",
            )

    def list_voices(self) -> list[dict[str, Any]]:
        self.last_attempt_count = 0
        try:
            return provider_voice_rows(load_tencent_voice_catalog())
        except TencentVoiceCatalogError as error:
            raise ProviderError(error.code, error.message, detail=error.detail) from error

    def generate_speech(
        self,
        text: str,
        voice_id: str,
        *,
        session_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> dict[str, Any]:
        self.validate_configuration()
        clean_text = text.strip()
        if not clean_text:
            raise ProviderError("TTS_TEXT_EMPTY", "The final translation is empty.")
        voice_type = self._voice_type(voice_id)
        try:
            catalog = load_tencent_voice_catalog()
        except TencentVoiceCatalogError as error:
            raise ProviderError(error.code, error.message, detail=error.detail) from error
        catalog_voice = find_voice(catalog, voice_type)
        if not catalog_voice:
            raise ProviderError("VOICE_NOT_FOUND", f"未知腾讯音色 ID {voice_type}")
        if TEXT_TO_VOICE_ENDPOINT not in catalog_voice["supported_endpoints"]:
            raise ProviderError(
                "VOICE_NOT_TEXT_TO_VOICE_COMPATIBLE",
                "The selected Tencent voice does not support TextToVoice.",
            )
        if not session_id:
            raise ProviderError(
                "TTS_SESSION_ID_REQUIRED",
                "Tencent TTS requires transcript_segment_id + tts_revision as SessionId.",
            )
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.tts.v20190823 import models, tts_client
        except ImportError as error:
            raise ProviderError(
                "TTS_SDK_MISSING",
                "Tencent Cloud TTS SDK is not installed.",
            ) from error

        http_profile = HttpProfile()
        http_profile.endpoint = self.configuration.endpoint
        client_profile = ClientProfile(httpProfile=http_profile)
        client = tts_client.TtsClient(
            credential.Credential(self.configuration.secret_id, self.configuration.secret_key),
            "",
            client_profile,
        )
        request = models.TextToVoiceRequest()
        request.Text = clean_text
        request.SessionId = session_id
        request.Volume = 0.0
        request.Speed = tencent_speed_for_ratio(speed_ratio)
        request.ProjectId = 0
        request.ModelType = 1
        request.VoiceType = voice_type
        request.PrimaryLanguage = self.configuration.primary_language
        request.SampleRate = self.configuration.sample_rate
        request.Codec = self.configuration.codec
        request.EnableSubtitle = False
        try:
            response = client.TextToVoice(request)
        except TencentCloudSDKException as error:
            request_id = getattr(error, "requestId", None) or getattr(error, "request_id", None)
            error_code = str(getattr(error, "code", "") or "")
            safe_message = redact_sensitive_text(getattr(error, "message", None) or str(error))
            diagnostics = {
                "provider": "tencent",
                "sdk_error_code": error_code or None,
                "request_id": request_id,
            }
            mapped_code = (
                "API_KEY_INVALID"
                if error_code.startswith("AuthFailure")
                else "RATE_LIMITED"
                if "Limit" in error_code or "RequestLimitExceeded" in error_code
                else "TTS_REQUEST_INVALID"
                if error_code.startswith("InvalidParameter")
                else "TTS_PROVIDER_ERROR"
            )
            raise ProviderError(
                mapped_code,
                safe_message or "Tencent Cloud Text-to-Speech request failed.",
                request_id=request_id,
                attempt_count=1,
                diagnostics=diagnostics,
            ) from error
        try:
            audio = base64.b64decode(str(response.Audio or ""), validate=True)
        except (ValueError, TypeError) as error:
            raise ProviderError(
                "TTS_AUDIO_INVALID",
                "Tencent Cloud returned invalid Base64 audio.",
                request_id=getattr(response, "RequestId", None),
                attempt_count=1,
            ) from error
        if not audio:
            raise ProviderError(
                "TTS_AUDIO_INVALID",
                "Tencent Cloud returned empty audio.",
                request_id=getattr(response, "RequestId", None),
                attempt_count=1,
            )
        return {
            "provider": "tencent",
            "model": self.configuration.model,
            "output_format": self.configuration.codec,
            "audio": audio,
            "provider_request_id": getattr(response, "RequestId", None),
            "attempt_count": 1,
            "usage": {
                "character_cost": len(clean_text),
                "session_id": session_id,
                "requested_speed_ratio": float(speed_ratio),
                "provider_speed": request.Speed,
            },
        }


def get_transcription_provider(configuration: ProviderConfiguration | None = None) -> TranscriptionProvider:
    config = configuration or transcription_configuration()
    if config.provider == "local_whisper_pyannote":
        return LocalWhisperDiarizationProvider(config)
    if config.provider == "elevenlabs":
        return ElevenLabsTranscriptionProvider(config)
    if config.provider == "tencent":
        return TencentAsrProvider(config)
    return TranscriptionProvider(config)


def get_translation_provider(configuration: ProviderConfiguration | None = None) -> TranslationProvider:
    config = configuration or translation_configuration()
    if config.provider == "deepseek":
        return DeepSeekTranslationProvider(config)
    return TranslationProvider(config)


def get_tts_provider(configuration: TTSConfiguration | None = None) -> TTSProvider:
    config = configuration or tts_configuration()
    if config.provider == "edge":
        from .edge_tts_provider import EdgeTTSProvider

        return EdgeTTSProvider(config)
    if config.provider == "elevenlabs":
        return ElevenLabsTTSProvider(config)
    if config.provider == "tencent":
        return TencentTTSProvider(config)
    return TTSProvider(config)


def generate_voice(text: str, language: str, voice_config: dict[str, Any]) -> dict[str, Any]:
    """Unified TTS entrypoint; completed local audio may bypass it entirely."""
    normalized_text = text.strip()
    if not normalized_text:
        raise ProviderError("TTS_TEXT_EMPTY", "The voice-over text is empty.")
    voice_id = str(voice_config.get("voice_id") or "").strip()
    if not voice_id:
        raise ProviderError("TTS_VOICE_REQUIRED", "A voice_id is required for voice generation.")
    if not str(language or "").strip():
        raise ProviderError("TTS_LANGUAGE_REQUIRED", "A target language is required for voice generation.")
    segment_id = str(voice_config.get("transcript_segment_id") or "").strip()
    revision = str(voice_config.get("tts_revision") or voice_config.get("tts_input_hash") or "").strip()
    session_id = f"{segment_id}-{revision[:16]}" if segment_id and revision else None
    try:
        speed_ratio = float(voice_config.get("speed_ratio") or 1.0)
    except (TypeError, ValueError):
        speed_ratio = 1.0
    return get_tts_provider().generate_speech(
        normalized_text,
        voice_id,
        session_id=session_id,
        speed_ratio=speed_ratio,
    )


def transcription_provider_status(configuration: ProviderConfiguration | None = None) -> dict[str, Any]:
    config = configuration or transcription_configuration()
    status = config.public_status()
    if config.provider != "local_whisper_pyannote":
        return status
    try:
        health = LocalAudioSidecarClient(config.base_url, timeout_seconds=3).health()
        status.update(
            {
                "reachable": True,
                "device": health.get("device"),
                "compute_type": health.get("compute_type"),
                "diarization_model": health.get("diarization_model"),
            }
        )
    except LocalAudioSidecarError as error:
        status.update({"reachable": False, "health_error_code": error.code})
    return status


def transcribe_audio(
    audio_path: Path,
    language: str = "auto",
    configuration: ProviderConfiguration | None = None,
    *,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    adapter = get_transcription_provider(configuration)
    if isinstance(adapter, LocalWhisperDiarizationProvider):
        return adapter.transcribe_audio(audio_path, language, status_callback=status_callback)
    return adapter.transcribe_audio(audio_path, language)


def translate_segments(
    segments: list[dict[str, Any]], source_language: str, target_language: str, style: str
) -> dict[str, Any]:
    adapter = get_translation_provider()
    if isinstance(adapter, DeepSeekTranslationProvider):
        return adapter.translate_segments(segments, source_language, target_language, style)
    raise ProviderError(
        "UNSUPPORTED_PROVIDER",
        "The configured translation provider does not implement A1-2 structured translation.",
    )
