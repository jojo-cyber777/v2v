"""Loopback-only client for the Python 3.11 local audio sidecar."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..database import ROOT


DEFAULT_LOCAL_STT_JOB_TIMEOUT_SECONDS = 1800.0


def configured_local_stt_job_timeout_seconds() -> float:
    try:
        value = float(os.getenv("LOCAL_STT_JOB_TIMEOUT_SECONDS", str(DEFAULT_LOCAL_STT_JOB_TIMEOUT_SECONDS)))
    except ValueError:
        value = DEFAULT_LOCAL_STT_JOB_TIMEOUT_SECONDS
    if not math.isfinite(value):
        value = DEFAULT_LOCAL_STT_JOB_TIMEOUT_SECONDS
    return max(1.0, value)


class LocalAudioSidecarError(RuntimeError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail[:4000]


class LocalAudioSidecarClient:
    """Small synchronous API client with strict local-path and loopback guards."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("LOCAL_AUDIO_SIDECAR_URL") or "http://127.0.0.1:8765").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise LocalAudioSidecarError(
                "LOCAL_STT_SIDECAR_UNAVAILABLE",
                "The local STT sidecar URL must use http://127.0.0.1.",
            )
        configured_timeout = timeout_seconds
        if configured_timeout is None:
            configured_timeout = configured_local_stt_job_timeout_seconds()
        self.timeout_seconds = max(1.0, float(configured_timeout))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=15.0,
            transport=transport,
            trust_env=False,
        )

    @staticmethod
    def _allowed_path(path: Path, *, output: bool = False) -> Path:
        resolved = path.resolve()
        roots = [ROOT / "uploads", ROOT / "outputs", ROOT / "test_assets"]
        if output:
            roots = [ROOT / "outputs"]
        if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
            raise LocalAudioSidecarError(
                "LOCAL_STT_OUTPUT_INVALID" if output else "LOCAL_STT_AUDIO_INVALID",
                "The local STT path is outside the allowed project media directories.",
                str(resolved),
            )
        return resolved

    @staticmethod
    def _response_error(response: httpx.Response, fallback_code: str) -> LocalAudioSidecarError:
        try:
            payload = response.json()
            detail = payload.get("detail", payload)
            if isinstance(detail, dict):
                code = str(detail.get("error_code") or fallback_code)
                message = str(detail.get("error_message") or f"Local sidecar returned HTTP {response.status_code}.")
            else:
                code, message = fallback_code, str(detail)
        except (ValueError, TypeError):
            code, message = fallback_code, f"Local sidecar returned HTTP {response.status_code}."
        return LocalAudioSidecarError(code, message, response.text)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.NetworkError) as error:
            raise LocalAudioSidecarError(
                "LOCAL_STT_SIDECAR_UNAVAILABLE", "The local STT sidecar is unavailable.", repr(error)
            ) from error
        except httpx.TimeoutException as error:
            raise LocalAudioSidecarError(
                "LOCAL_STT_SIDECAR_UNAVAILABLE", "The local STT sidecar request timed out.", repr(error)
            ) from error
        if response.is_error:
            raise self._response_error(response, "LOCAL_STT_JOB_FAILED")
        try:
            payload = response.json()
        except ValueError as error:
            raise LocalAudioSidecarError(
                "LOCAL_STT_OUTPUT_INVALID", "The local STT sidecar returned invalid JSON.", response.text
            ) from error
        if not isinstance(payload, dict):
            raise LocalAudioSidecarError("LOCAL_STT_OUTPUT_INVALID", "The local STT sidecar response is not an object.")
        return payload

    def health(self) -> dict[str, Any]:
        payload = self._request("GET", "/health")
        if payload.get("service_status") != "ready" or not payload.get("models_available") or not payload.get("ffmpeg_available"):
            raise LocalAudioSidecarError(
                "LOCAL_STT_HEALTH_FAILED", "The local STT sidecar is not ready.", json.dumps(payload, ensure_ascii=True)
            )
        return payload

    def create_job(
        self,
        input_path: Path,
        output_dir: Path,
        *,
        language: str = "auto",
        known_speaker_count: int | None = None,
    ) -> dict[str, Any]:
        source = self._allowed_path(input_path)
        destination = self._allowed_path(output_dir, output=True)
        if not source.is_file():
            raise LocalAudioSidecarError("LOCAL_STT_AUDIO_INVALID", "The local STT input audio does not exist.", str(source))
        destination.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "input_path": str(source),
            "output_dir": str(destination),
            "language": language if language in {"auto", "zh", "en"} else "auto",
            "audit_only": True,
        }
        if known_speaker_count is not None:
            payload["known_speaker_count"] = int(known_speaker_count)
        return self._request("POST", "/jobs", json=payload)

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not job_id.strip():
            raise LocalAudioSidecarError("LOCAL_STT_OUTPUT_INVALID", "A local STT job id is required.")
        return self._request("GET", f"/jobs/{job_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/cancel")

    def wait_for_job(
        self,
        job_id: str,
        *,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            state = self.get_job(job_id)
            status = state.get("status")
            if status in {"queued", "running"} and status_callback is not None:
                status_callback(state)
            if status == "completed":
                return state
            if status == "cancelled":
                raise LocalAudioSidecarError("LOCAL_STT_JOB_CANCELLED", "The local STT job was cancelled.")
            if status == "failed":
                raise LocalAudioSidecarError(
                    "LOCAL_STT_JOB_FAILED",
                    str(state.get("error_message") or "The local STT job failed."),
                    str(state.get("error_code") or ""),
                )
            time.sleep(self.poll_interval_seconds)
        try:
            self.cancel_job(job_id)
        except LocalAudioSidecarError:
            pass
        raise LocalAudioSidecarError("LOCAL_STT_JOB_TIMEOUT", "The local STT job exceeded its configured timeout.")

    def close(self) -> None:
        self._client.close()
