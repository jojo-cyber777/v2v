"""Backend-only Xmax Offline execution for prerecorded Transform Units."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ..database import ROOT
from ..environment import get_xmax_permanent_api_key, get_xmax_setting
from . import xmax_browser_attempt_service as attempts


OFFLINE_QUALITY = "sd"
DEFAULT_POLL_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_PAGE_SCAN = 100


class XmaxOfflineError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        charge_points: int | None = None,
        submission_unknown: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.charge_points = charge_points
        self.submission_unknown = submission_unknown
        self.diagnostics = diagnostics or {}


def _cos_exception_value(error: Exception, *names: str) -> Any:
    for name in names:
        value = getattr(error, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value not in (None, ""):
            return value
    return None


def _cos_upload_diagnostics(
    error: Exception,
    *,
    sts: dict[str, str],
    key: str,
    media_kind: str,
) -> dict[str, Any]:
    endpoint = sts.get("endpoint", "").split("?", 1)[0]
    return {
        "phase": f"cos_{media_kind}_upload",
        "media_kind": media_kind,
        "cos_endpoint": endpoint or None,
        "cos_bucket": sts.get("bucket") or None,
        "cos_region": sts.get("region") or None,
        "cos_object_key": key,
        "provider_http_status": _cos_exception_value(error, "get_status_code", "status_code"),
        "provider_error_code": _cos_exception_value(error, "get_error_code", "error_code", "code"),
        "provider_error_body": _cos_exception_value(error, "get_error_msg", "error_message", "message"),
        "provider_request_id": _cos_exception_value(error, "get_request_id", "request_id"),
        "provider_trace_id": _cos_exception_value(error, "get_trace_id", "trace_id"),
        "exception_type": type(error).__name__,
    }


@dataclass(frozen=True)
class OfflineCompletedResult:
    attempt_id: str
    task_id: str
    unit_id: str
    user_id: str
    staged_path: Path
    content_type: str
    charge_points: int
    provider_task: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise XmaxOfflineError("XMAX_OFFLINE_RESPONSE_INVALID", f"Xmax returned an invalid {field}.")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise XmaxOfflineError("XMAX_OFFLINE_RESPONSE_INVALID", f"Xmax returned an invalid {field}.") from error
    if result < 0:
        raise XmaxOfflineError("XMAX_OFFLINE_RESPONSE_INVALID", f"Xmax returned an invalid {field}.")
    return result


def _response_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise XmaxOfflineError("XMAX_OFFLINE_PROVIDER_ERROR", "Xmax returned an unsuccessful response.")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise XmaxOfflineError("XMAX_OFFLINE_RESPONSE_INVALID", "Xmax returned an invalid response.")
    return data


def _task_value(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def normalize_offline_task(payload: Any) -> dict[str, Any]:
    data = _response_data(payload)
    nested = data.get("task")
    if isinstance(nested, dict):
        data = nested
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    uid = str(_task_value(data, "uid", "taskUid", "task_uid") or "").strip()
    status = str(data.get("status") or "").strip().lower()
    charge_raw = _task_value(data, "chargePoints", "charge_points")
    return {
        "uid": uid,
        "status": status,
        "prompt": str(data.get("prompt") or ""),
        "ref_video_path": str(_task_value(data, "refVideoPath", "ref_video_path") or ""),
        "ref_image_path": str(_task_value(data, "refImagePath", "ref_image_path") or ""),
        "quality": str(data.get("quality") or ""),
        "charge_points": _strict_nonnegative_int(charge_raw, "chargePoints") if charge_raw is not None else None,
        "processor_id": _task_value(data, "processorId", "processor_id"),
        "resolved_fps": _task_value(data, "resolvedFps", "resolved_fps"),
        "retry_count": _task_value(data, "retryCount", "retry_count"),
        "result_url": str(_task_value(result, "result_url", "resultUrl", "url") or ""),
        "error_code": _task_value(data, "errorCode", "error_code", "code"),
        "error_message": str(_task_value(data, "errorMessage", "error_message", "message") or ""),
        "raw": data,
    }


class XmaxOfflineProviderClient:
    def __init__(self) -> None:
        self._api_key = get_xmax_permanent_api_key()
        self._base_url = get_xmax_setting("XMAX_API_BASE_URL", "https://api.xmaxai.com").rstrip("/")
        if not self._api_key:
            raise XmaxOfflineError("XMAX_PERMANENT_KEY_MISSING", "Xmax permanent API key is not configured.")

    def _request(self, method: str, endpoint: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{self._base_url}{endpoint}",
                headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
                json=json_body,
                timeout=httpx.Timeout(30.0),
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise XmaxOfflineError(
                "XMAX_OFFLINE_TRANSPORT_UNKNOWN",
                "The Xmax Offline request outcome is unknown.",
                submission_unknown=method.upper() == "POST" and endpoint.endswith("/offline-task"),
            ) from error
        if response.status_code >= 500 and method.upper() == "POST" and endpoint.endswith("/offline-task"):
            raise XmaxOfflineError(
                "XMAX_OFFLINE_SUBMIT_UNKNOWN",
                "The Xmax Offline submission outcome is unknown.",
                submission_unknown=True,
            )
        if response.status_code >= 400:
            raise XmaxOfflineError(
                "XMAX_OFFLINE_PROVIDER_ERROR",
                f"Xmax Offline request failed with HTTP {response.status_code}.",
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise XmaxOfflineError("XMAX_OFFLINE_RESPONSE_INVALID", "Xmax returned non-JSON data.") from error
        return _response_data(payload)

    def _sts(self) -> dict[str, str]:
        data = self._request("GET", "/open/api/v1/cos/sts")
        credentials = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
        result = {
            "bucket": str(data.get("bucket") or ""),
            "region": str(data.get("region") or ""),
            "endpoint": str(data.get("endpoint") or ""),
            "prefix": str(data.get("prefix") or ""),
            "secret_id": str(credentials.get("accessKeyId") or credentials.get("tmpSecretId") or ""),
            "secret_key": str(credentials.get("secretAccessKey") or credentials.get("tmpSecretKey") or ""),
            "token": str(credentials.get("sessionToken") or credentials.get("token") or ""),
        }
        if not all(result[name] for name in ("bucket", "region", "secret_id", "secret_key", "token")):
            raise XmaxOfflineError("XMAX_COS_STS_INVALID", "Xmax returned incomplete COS credentials.")
        return result

    @staticmethod
    def _provider_url(sts: dict[str, str], key: str) -> str:
        endpoint = sts["endpoint"].strip()
        if endpoint:
            endpoint = endpoint if endpoint.startswith(("http://", "https://")) else f"https://{endpoint}"
            parsed = httpx.URL(endpoint)
            host = parsed.host or ""
            if host.startswith("cos.") and not host.startswith(f"{sts['bucket']}."):
                host = f"{sts['bucket']}.{host}"
            base_path = parsed.path.decode("utf-8").rstrip("/") if isinstance(parsed.path, bytes) else str(parsed.path).rstrip("/")
            return str(parsed.copy_with(host=host, path=f"{base_path}/{quote(key)}"))
        return f"https://{sts['bucket']}.cos.{sts['region']}.myqcloud.com/{quote(key)}"

    def upload_file(self, path: Path, *, object_key: str, content_type: str) -> str:
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as error:
            raise XmaxOfflineError(
                "XMAX_COS_SDK_MISSING",
                "The Tencent COS Python SDK is not installed.",
            ) from error
        sts = self._sts()
        config = CosConfig(
            Region=sts["region"],
            SecretId=sts["secret_id"],
            SecretKey=sts["secret_key"],
            Token=sts["token"],
        )
        client = CosS3Client(config)
        key = f"{sts['prefix']}{Path(object_key).name}"
        media_kind = "reference_image" if "/reference-" in object_key else "source_video"
        try:
            with path.open("rb") as body:
                client.put_object(
                    Bucket=sts["bucket"],
                    Body=body,
                    Key=key,
                    ContentLength=str(path.stat().st_size),
                    ContentType=content_type,
                )
        except Exception as error:
            raise XmaxOfflineError(
                "XMAX_COS_UPLOAD_FAILED",
                "The Xmax media upload failed.",
                diagnostics=_cos_upload_diagnostics(
                    error,
                    sts=sts,
                    key=key,
                    media_kind=media_kind,
                ),
            ) from error
        return self._provider_url(sts, key)

    def check_image(self, url: str) -> str:
        data = self._request("POST", "/open/api/v1/cos/image/check", json_body={"url": url})
        checked = str(data.get("url") or "")
        if data.get("safe") is False or not checked.startswith("https://"):
            raise XmaxOfflineError("XMAX_REFERENCE_REJECTED", "The Xmax reference image check failed.")
        return checked

    def submit(self, *, prompt: str, video_url: str, reference_url: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/open/api/v1/offline-task",
            json_body={
                "prompt": prompt,
                "refImagePath": reference_url,
                "refVideoPath": video_url,
                "quality": OFFLINE_QUALITY,
            },
        )
        return normalize_offline_task(data)

    def get_task(self, uid: str) -> dict[str, Any]:
        data = self._request("GET", f"/open/api/v1/offline-task/{quote(uid, safe='')}")
        return normalize_offline_task(data)

    def list_page(self, page_number: int, page_size: int = 100) -> tuple[list[dict[str, Any]], bool]:
        data = self._request(
            "GET",
            f"/open/api/v1/offline-task/page?pageNumber={page_number}&pageSize={page_size}",
        )
        raw_items = data.get("records") or data.get("items") or data.get("list") or []
        items = [normalize_offline_task(item) for item in raw_items if isinstance(item, dict)]
        total = int(data.get("total") or data.get("totalCount") or 0)
        has_more = bool(data.get("hasNext") or (total and page_number * page_size < total) or len(items) == page_size)
        return items, has_more

    def download(self, url: str, destination: Path) -> str:
        if not url.startswith("https://"):
            raise XmaxOfflineError("XMAX_OFFLINE_RESULT_URL_INVALID", "Xmax returned an invalid result URL.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.stream("GET", url, timeout=httpx.Timeout(120.0), follow_redirects=True) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("content-type") or "video/mp4").split(";", 1)[0].strip().lower()
                with destination.open("wb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        target.write(chunk)
        except Exception as error:
            destination.unlink(missing_ok=True)
            raise XmaxOfflineError("XMAX_OFFLINE_RESULT_DOWNLOAD_FAILED", "The Xmax result could not be downloaded.") from error
        if not destination.is_file() or destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            raise XmaxOfflineError("XMAX_OFFLINE_RESULT_EMPTY", "The Xmax result is empty.")
        return content_type


client_factory: Callable[[], XmaxOfflineProviderClient] = XmaxOfflineProviderClient


def _matching_provider_task(
    client: XmaxOfflineProviderClient,
    *,
    prompt: str,
    video_url: str,
    reference_url: str,
) -> dict[str, Any] | None:
    try:
        for page_number in range(1, MAX_PAGE_SCAN + 1):
            items, has_more = client.list_page(page_number)
            for item in items:
                if (
                    item["prompt"] == prompt
                    and item["ref_video_path"] == video_url
                    and item["ref_image_path"] == reference_url
                    and item["quality"] == OFFLINE_QUALITY
                ):
                    return item
            if not has_more:
                return None
    except XmaxOfflineError as error:
        raise XmaxOfflineError(
            "XMAX_OFFLINE_RECONCILE_UNKNOWN",
            "The Xmax Offline submission could not be reconciled yet.",
            submission_unknown=True,
        ) from error
    raise XmaxOfflineError(
        "XMAX_OFFLINE_RECONCILE_INCOMPLETE",
        "Xmax task reconciliation exceeded the page limit.",
        submission_unknown=True,
    )


def _get_bound_provider_task(client: XmaxOfflineProviderClient, uid: str) -> dict[str, Any]:
    try:
        return client.get_task(uid)
    except XmaxOfflineError as error:
        raise XmaxOfflineError(
            "XMAX_OFFLINE_STATUS_UNKNOWN",
            "The current Xmax Offline task status could not be confirmed yet.",
            submission_unknown=True,
        ) from error


def execute_attempt(
    record_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> OfflineCompletedResult:
    client = client_factory()
    runtime = attempts.get_offline_attempt_runtime(record_id)
    offline = dict(runtime["offline"])
    source = Path(runtime["selected_source_path"])
    reference = Path(runtime["reference_path"])
    if not source.is_file() or _sha256_file(source) != runtime["selected_source_sha256"]:
        raise XmaxOfflineError("XMAX_OFFLINE_SOURCE_CHANGED", "The prepared Unit source is missing or changed.")
    if not reference.is_file() or _sha256_file(reference) != runtime["reference_sha256"]:
        raise XmaxOfflineError("XMAX_OFFLINE_REFERENCE_CHANGED", "The approved reference is missing or changed.")

    video_url = str(offline.get("provider_video_url") or "")
    reference_url = str(offline.get("provider_reference_url") or "")
    if not video_url or not reference_url:
        attempts.update_offline_metadata(record_id, status="uploading_media")
        if not video_url:
            video_url = client.upload_file(
                source,
                object_key=f"drama-ai/{record_id}/source-{runtime['selected_source_sha256']}.mp4",
                content_type="video/mp4",
            )
        if not reference_url:
            guessed = mimetypes.guess_type(reference.name)[0] or "image/png"
            raw_reference_url = client.upload_file(
                reference,
                object_key=f"drama-ai/{record_id}/reference-{runtime['reference_sha256']}{reference.suffix.lower()}",
                content_type=guessed,
            )
            reference_url = client.check_image(raw_reference_url)
        attempts.update_offline_metadata(
            record_id,
            status="media_ready",
            provider_video_url=video_url,
            provider_reference_url=reference_url,
        )

    provider_task = None
    if runtime["offline_task_uid"]:
        provider_task = _get_bound_provider_task(client, runtime["offline_task_uid"])
    else:
        submit_count = int(offline.get("submit_attempt_count") or 0)
        if offline.get("status") == "submit_unknown":
            provider_task = _matching_provider_task(
                client,
                prompt=runtime["prompt"],
                video_url=video_url,
                reference_url=reference_url,
            )
            if provider_task is None and submit_count >= 2:
                raise XmaxOfflineError(
                    "XMAX_OFFLINE_SUBMIT_UNRESOLVED",
                    "The Xmax Offline submission could not be reconciled safely.",
                    submission_unknown=True,
                )
        if provider_task is None:
            try:
                attempts.update_offline_metadata(record_id, status="submitting", submit_attempt_count=submit_count + 1)
                provider_task = client.submit(
                    prompt=runtime["prompt"],
                    video_url=video_url,
                    reference_url=reference_url,
                )
            except XmaxOfflineError as error:
                if not error.submission_unknown:
                    raise
                current_submit_count = submit_count + 1
                attempts.update_offline_metadata(record_id, status="submit_unknown")
                provider_task = _matching_provider_task(
                    client,
                    prompt=runtime["prompt"],
                    video_url=video_url,
                    reference_url=reference_url,
                )
                if provider_task is None:
                    if current_submit_count >= 2:
                        raise XmaxOfflineError(
                            "XMAX_OFFLINE_SUBMIT_UNRESOLVED",
                            "The Xmax Offline submission could not be reconciled safely.",
                            submission_unknown=True,
                        ) from error
                    attempts.update_offline_metadata(record_id, status="resubmitting_after_reconcile", submit_attempt_count=2)
                    try:
                        provider_task = client.submit(
                            prompt=runtime["prompt"],
                            video_url=video_url,
                            reference_url=reference_url,
                        )
                    except XmaxOfflineError as retry_error:
                        if retry_error.submission_unknown:
                            attempts.update_offline_metadata(record_id, status="submit_unknown")
                        raise
        if not provider_task or not provider_task["uid"]:
            raise XmaxOfflineError("XMAX_OFFLINE_TASK_UID_MISSING", "Xmax did not return an Offline task UID.")
        attempts.bind_offline_task_uid(
            record_id,
            provider_task["uid"],
            status=provider_task["status"] or "submitted",
            submitted_at=attempts.now_iso(),
            provider_video_url=video_url,
            provider_reference_url=reference_url,
            charge_points=provider_task["charge_points"],
            processor_id=provider_task["processor_id"],
            retry_count=provider_task["retry_count"],
        )

    timeout_seconds = int(get_xmax_setting("XMAX_OFFLINE_JOB_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    poll_seconds = float(get_xmax_setting("XMAX_OFFLINE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))
    started = monotonic()
    while True:
        if provider_task is None or provider_task["status"] not in {"completed", "error", "failed"}:
            provider_task = _get_bound_provider_task(
                client,
                str(runtime["offline_task_uid"] or provider_task["uid"]),
            )
        attempts.update_offline_metadata(
            record_id,
            status=provider_task["status"] or "processing",
            charge_points=provider_task["charge_points"],
            processor_id=provider_task["processor_id"],
            retry_count=provider_task["retry_count"],
            result_url=provider_task["result_url"] or None,
        )
        if provider_task["status"] == "completed":
            if not provider_task["result_url"]:
                raise XmaxOfflineError("XMAX_OFFLINE_RESULT_URL_MISSING", "The completed Xmax task has no result URL.")
            if provider_task["charge_points"] is None:
                raise XmaxOfflineError("XMAX_OFFLINE_CHARGE_MISSING", "The completed Xmax task has no chargePoints.")
            staging = (
                ROOT / "outputs" / str(runtime["project_id"]) / str(runtime["episode_id"])
                / "xmax-offline" / record_id / "provider-result.mp4"
            )
            content_type = client.download(provider_task["result_url"], staging)
            attempts.update_offline_metadata(record_id, status="result_downloaded", result_url=provider_task["result_url"])
            return OfflineCompletedResult(
                attempt_id=record_id,
                task_id=runtime["task_id"],
                unit_id=runtime["unit_id"],
                user_id=runtime["user_id"],
                staged_path=staging,
                content_type=content_type,
                charge_points=provider_task["charge_points"],
                provider_task=provider_task,
            )
        if provider_task["status"] in {"error", "failed"}:
            raise XmaxOfflineError(
                str(provider_task["error_code"] or "XMAX_OFFLINE_TASK_FAILED"),
                provider_task["error_message"] or "The Xmax Offline task failed.",
                charge_points=provider_task["charge_points"],
            )
        if monotonic() - started >= timeout_seconds:
            raise XmaxOfflineError(
                "XMAX_OFFLINE_TASK_TIMEOUT",
                "The Xmax Offline task exceeded its configured execution timeout.",
                submission_unknown=True,
            )
        sleep(max(0.1, poll_seconds))
