"""Runway-only adapter for the Stage 3 B1 visual transformation PoC."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .provider_service import ProviderError, redact_sensitive_text


RUNWAY_API_BASE = "https://api.dev.runwayml.com"
DEFAULT_MODEL_ORDER = ("gemini_omni_flash", "seedance2_mini", "aleph2", "seedance2_fast")
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "gemini_omni_flash": {"label": "Gemini Omni Flash", "min_seconds": 4, "max_seconds": 10, "credits_per_second": 11, "video_to_video": True, "ratio_parameter": False},
    "seedance2_mini": {"label": "Seedance 2 Mini", "min_seconds": 4, "max_seconds": 15, "credits_per_second": 16, "minimum_credits": 64, "video_to_video": True, "ratio_parameter": True},
    "aleph2": {"label": "Aleph 2", "min_seconds": 2, "max_seconds": 30, "credits_per_second": 28, "minimum_credits": 56, "video_to_video": True, "ratio_parameter": True},
    "seedance2_fast": {"label": "Seedance 2 Fast", "min_seconds": 4, "max_seconds": 15, "credits_per_second": 29, "video_to_video": True, "ratio_parameter": True},
}


@dataclass(frozen=True)
class RunwayConfiguration:
    provider: str
    api_key: str
    model: str
    api_version: str
    max_test_seconds: float
    base_url: str

    @property
    def configured(self) -> bool:
        return self.provider == "runway" and bool(self.api_key and self.model and self.api_version)

    def public_status(self) -> dict[str, Any]:
        missing = []
        if self.provider != "runway":
            missing.append("provider")
        if not self.api_key:
            missing.append("api_key")
        if not self.model:
            missing.append("model")
        if not self.api_version:
            missing.append("api_version")
        return {
            "provider": self.provider or None,
            "model": self.model or None,
            "configured": self.configured,
            "missing": missing,
            "max_test_seconds": self.max_test_seconds,
        }


def runway_configuration() -> RunwayConfiguration:
    return RunwayConfiguration(
        provider=os.getenv("VIDEO_TRANSFORMATION_PROVIDER", "runway").strip().lower(),
        api_key=os.getenv("RUNWAY_API_KEY", "").strip(),
        model=os.getenv("RUNWAY_VIDEO_MODEL", DEFAULT_MODEL_ORDER[0]).strip() or DEFAULT_MODEL_ORDER[0],
        api_version=os.getenv("RUNWAY_API_VERSION", "2024-11-06").strip() or "2024-11-06",
        max_test_seconds=max(1.0, float(os.getenv("RUNWAY_MAX_TEST_SECONDS", "10") or 10)),
        base_url=os.getenv("RUNWAY_BASE_URL", RUNWAY_API_BASE).strip().rstrip("/"),
    )


def available_visual_models(configuration: RunwayConfiguration | None = None, duration: float | None = None) -> list[dict[str, Any]]:
    """Return the backend-owned allowlist; never discover or switch models implicitly."""
    config = configuration or runway_configuration()
    raw = os.getenv("RUNWAY_VIDEO_MODELS", "").strip()
    model_ids = list(DEFAULT_MODEL_ORDER)
    model_ids.extend(item.strip() for item in raw.split(",") if item.strip() and item.strip() not in model_ids)
    if config.model not in model_ids:
        model_ids.append(config.model)
    cost_per_credit = float(os.getenv("RUNWAY_COST_PER_CREDIT_USD", "0.01") or 0.01)
    output: list[dict[str, Any]] = []
    for model_id in dict.fromkeys(model_ids):
        spec = MODEL_SPECS.get(model_id, {"label": model_id, "min_seconds": 4, "max_seconds": config.max_test_seconds, "credits_per_second": None, "video_to_video": True, "ratio_parameter": True})
        credits = None
        if duration is not None and spec.get("credits_per_second"):
            credits = max(float(spec.get("minimum_credits") or 0), round(float(spec["credits_per_second"]) * duration, 2))
        output.append({"id": model_id, "label": spec["label"], "min_seconds": spec["min_seconds"], "max_seconds": spec["max_seconds"], "estimated_credits": credits, "estimated_cost_usd": round(credits * cost_per_credit, 2) if credits is not None else None, "video_to_video": bool(spec.get("video_to_video")), "available_for_duration": duration is None or spec["min_seconds"] <= duration <= spec["max_seconds"]})
    return output


def model_spec(model: str) -> dict[str, Any]:
    return MODEL_SPECS.get(model, {"min_seconds": 4, "max_seconds": runway_configuration().max_test_seconds, "credits_per_second": None, "video_to_video": True, "ratio_parameter": True})


def build_video_to_video_payload(
    model: str,
    video_uri: str,
    prompt: str,
    ratio: str,
    references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the model-specific request body without making a network call."""
    clean_references = [{"uri": str(item["uri"])} for item in (references or []) if item.get("uri")]
    if model == "gemini_omni_flash":
        payload: dict[str, Any] = {"model": model, "videoUri": video_uri, "promptText": prompt}
        if clean_references:
            payload["references"] = clean_references
        return payload
    if model in {"seedance2_mini", "seedance2_fast"}:
        payload = {"model": model, "promptVideo": video_uri, "promptText": prompt, "ratio": ratio}
        if clean_references:
            payload["references"] = clean_references
        return payload
    if model == "aleph2":
        return {"model": model, "videoUri": video_uri, "promptText": prompt}
    raise ValueError(f"Unsupported Runway video-to-video model: {model}")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw": response.text[:4000]}
    return payload if isinstance(payload, dict) else {"data": payload}


class VideoTransformationProvider:
    def validate_configuration(self) -> None:
        raise NotImplementedError

    def validate_input(self, clip_path: Path, duration: float, aspect_ratio: float) -> None:
        raise NotImplementedError

    def estimate_usage(self, duration: float, width: int, height: int) -> dict[str, Any]:
        raise NotImplementedError

    def submit_video_to_video(self, clip_path: Path, prompt: str, ratio: str, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def get_task_status(self, provider_task_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def cancel_task(self, provider_task_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def download_output(self, output_url: str, destination: Path) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def map_provider_error(self, response: httpx.Response, payload: dict[str, Any] | None = None) -> ProviderError:
        raise NotImplementedError


class RunwayVideoTransformationProvider(VideoTransformationProvider):
    def __init__(self, configuration: RunwayConfiguration | None = None) -> None:
        self.configuration = configuration or runway_configuration()

    def validate_configuration(self) -> None:
        if self.configuration.provider != "runway":
            raise ProviderError("UNSUPPORTED_PROVIDER", "Only the Runway visual transformation provider is supported.")
        if not self.configuration.api_key:
            raise ProviderError("RUNWAY_PROVIDER_DISABLED", "Runway is not configured. Add RUNWAY_API_KEY to the project-root .env file.")
        if any(character.isspace() for character in self.configuration.api_key):
            raise ProviderError("API_KEY_INVALID", "The Runway API key format is invalid.")

    def validate_input(self, clip_path: Path, duration: float, aspect_ratio: float) -> None:
        self.validate_configuration()
        if not clip_path.is_file():
            raise ProviderError("VIDEO_FILE_NOT_FOUND", "The visual transformation clip does not exist.")
        spec = model_spec(self.configuration.model)
        if duration < float(spec.get("min_seconds", 4)):
            raise ProviderError("VIDEO_DURATION_TOO_SHORT", f"{self.configuration.model} requires at least {spec.get('min_seconds', 4):g} seconds for video-to-video.")
        if duration > float(spec.get("max_seconds", self.configuration.max_test_seconds)) + 0.05:
            raise ProviderError("VIDEO_DURATION_LIMIT", f"{self.configuration.model} supports {spec.get('max_seconds', self.configuration.max_test_seconds):g} seconds or less.")
        if aspect_ratio < 0.4 or aspect_ratio > 4:
            raise ProviderError("VIDEO_ASPECT_RATIO_UNSUPPORTED", "The clip aspect ratio is outside the configured Runway model range.")

    def estimate_usage(self, duration: float, width: int, height: int) -> dict[str, Any]:
        spec = model_spec(self.configuration.model)
        configured_rate = os.getenv("RUNWAY_CREDITS_PER_SECOND", "").strip()
        rate = float(configured_rate) if configured_rate else spec.get("credits_per_second")
        credits = max(float(spec.get("minimum_credits") or 0), round(float(rate) * duration, 2)) if rate else None
        cost = round(credits * float(os.getenv("RUNWAY_COST_PER_CREDIT_USD", "0.01") or 0.01), 2) if credits is not None else None
        return {
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "estimated_credits": credits,
            "estimated_cost_usd": cost,
            "note": "Estimate only; the Provider invoice is authoritative." if credits is not None else "Provider pricing is not configured; the Provider invoice is authoritative.",
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.configuration.api_key}", "X-Runway-Version": self.configuration.api_version, "Content-Type": "application/json"}

    def _create_ephemeral_upload(self, path: Path) -> str:
        if not path.is_file():
            raise ProviderError("VIDEO_FILE_NOT_FOUND", "The visual transformation clip does not exist.", str(path))
        if path.stat().st_size > 200 * 1024 * 1024:
            raise ProviderError("VIDEO_INPUT_TOO_LARGE", "The Runway ephemeral upload limit is 200 MB.")
        try:
            response = httpx.post(
                f"{self.configuration.base_url}/v1/uploads",
                headers=self._headers(),
                json={"filename": path.name, "type": "ephemeral"},
                timeout=60,
            )
        except httpx.TimeoutException as error:
            raise ProviderError("NETWORK_TIMEOUT", "Runway upload initialization timed out.", str(error), retryable=False) from error
        except httpx.HTTPError as error:
            raise ProviderError("NETWORK_REQUEST_FAILED", "Runway upload initialization could not be completed.", str(error), retryable=False) from error
        if response.is_error:
            raise self.map_provider_error(response)
        upload = _json(response)
        upload_url = upload.get("uploadUrl")
        fields = upload.get("fields") or {}
        runway_uri = upload.get("runwayUri")
        if not upload_url or not runway_uri:
            raise ProviderError("PROVIDER_UPLOAD_RESPONSE_INVALID", "Runway did not return a usable ephemeral upload.", json.dumps(upload)[:2000])
        try:
            with path.open("rb") as video_file:
                uploaded = httpx.post(
                    upload_url,
                    data={str(key): str(value) for key, value in fields.items()},
                    files={"file": (path.name, video_file, "video/mp4")},
                    timeout=180,
                )
        except httpx.TimeoutException as error:
            raise ProviderError("NETWORK_TIMEOUT", "Runway video upload timed out.", str(error), retryable=False) from error
        except (httpx.HTTPError, OSError) as error:
            raise ProviderError("NETWORK_REQUEST_FAILED", "Runway video upload could not be completed.", str(error), retryable=False) from error
        if uploaded.is_error:
            raise ProviderError("VIDEO_UPLOAD_FAILED", "Runway rejected the video upload.", f"HTTP {uploaded.status_code}; {uploaded.text[:2000]}")
        return str(runway_uri)

    def submit_video_to_video(self, clip_path: Path, prompt: str, ratio: str, references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.validate_configuration()
        video_uri = self._create_ephemeral_upload(clip_path)
        payload = build_video_to_video_payload(self.configuration.model, video_uri, prompt, ratio, references)
        try:
            response = httpx.post(f"{self.configuration.base_url}/v1/video_to_video", headers=self._headers(), json=payload, timeout=120)
        except httpx.TimeoutException as error:
            raise ProviderError("NETWORK_TIMEOUT", "Runway submission timed out.", str(error), retryable=False) from error
        except httpx.HTTPError as error:
            raise ProviderError("NETWORK_REQUEST_FAILED", "Runway submission could not be completed.", str(error), retryable=False) from error
        if response.is_error:
            raise self.map_provider_error(response)
        return self.normalize_response(_json(response))

    def get_task_status(self, provider_task_id: str) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.configuration.base_url}/v1/tasks/{provider_task_id}", headers={k: v for k, v in self._headers().items() if k != "Content-Type"}, timeout=60)
        except httpx.TimeoutException as error:
            raise ProviderError("NETWORK_TIMEOUT", "Runway task status polling timed out.", str(error), retryable=True) from error
        except httpx.HTTPError as error:
            raise ProviderError("NETWORK_REQUEST_FAILED", "Runway task status could not be read.", str(error), retryable=True) from error
        if response.is_error:
            raise self.map_provider_error(response)
        return self.normalize_response(_json(response))

    def cancel_task(self, provider_task_id: str) -> dict[str, Any]:
        try:
            response = httpx.delete(f"{self.configuration.base_url}/v1/tasks/{provider_task_id}", headers={k: v for k, v in self._headers().items() if k != "Content-Type"}, timeout=60)
        except httpx.HTTPError as error:
            raise ProviderError("NETWORK_REQUEST_FAILED", "Runway task cancellation could not be completed.", str(error), retryable=False) from error
        if response.is_error:
            raise self.map_provider_error(response)
        return self.normalize_response(_json(response))

    def download_output(self, output_url: str, destination: Path) -> dict[str, Any]:
        try:
            response = httpx.get(output_url, timeout=180)
        except httpx.HTTPError as error:
            raise ProviderError("OUTPUT_DOWNLOAD_FAILED", "The Runway output could not be downloaded.", str(error), retryable=True) from error
        if response.is_error or not response.content:
            raise ProviderError("OUTPUT_DOWNLOAD_FAILED", "The Runway output could not be downloaded.", response.text[:1000])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return {"path": str(destination), "file_size": len(response.content)}

    def normalize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_status = str(payload.get("status") or payload.get("state") or "PENDING").upper()
        mapping = {"PENDING": "queued", "RUNNING": "processing", "THROTTLED": "processing", "SUCCEEDED": "completed", "FAILED": "failed", "CANCELED": "cancelled", "CANCELLED": "cancelled"}
        output = payload.get("output") or payload.get("outputs") or []
        if isinstance(output, str):
            output = [output]
        return {"provider_task_id": str(payload.get("id") or payload.get("taskId") or ""), "status": mapping.get(raw_status, "processing"), "provider_status": raw_status, "output_url": output[0] if output else None, "failure_code": payload.get("failureCode") or payload.get("errorCode"), "failure_message": redact_sensitive_text(payload.get("failureMessage") or payload.get("error") or ""), "usage": payload.get("usage") or {}}

    def map_provider_error(self, response: httpx.Response, payload: dict[str, Any] | None = None) -> ProviderError:
        body = payload or _json(response)
        safe_body = redact_sensitive_text(json.dumps(body, ensure_ascii=True, separators=(",", ":"))[:2000])
        detail = f"HTTP {response.status_code}; body={safe_body}"
        if response.status_code in {401, 403}:
            code, message = ("API_KEY_INVALID", "Runway API key is invalid or unauthorized.") if response.status_code == 401 else ("CONTENT_MODERATION_REJECTED", "Runway rejected this request under its content policy.")
        elif response.status_code == 402:
            code, message = "INSUFFICIENT_CREDITS", "Runway credits or account balance are insufficient."
        elif response.status_code == 429:
            code, message = "RATE_LIMITED", "Runway rate-limited this request. No automatic paid retry was made."
        elif response.status_code >= 500:
            code, message = "PROVIDER_UNAVAILABLE", "Runway is temporarily unavailable."
        else:
            code, message = "RUNWAY_REQUEST_INVALID", "Runway rejected the visual transformation request."
        return ProviderError(code, message, detail, retryable=False, request_id=response.headers.get("x-request-id"))


def get_visual_transformation_provider(model: str | None = None) -> RunwayVideoTransformationProvider:
    configuration = runway_configuration()
    if model and model != configuration.model:
        spec = model_spec(model)
        configuration = replace(configuration, model=model, max_test_seconds=float(spec.get("max_seconds", configuration.max_test_seconds)))
    return RunwayVideoTransformationProvider(configuration)
