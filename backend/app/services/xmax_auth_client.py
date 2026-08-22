"""Server-only Xmax authentication and usage API client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..environment import get_xmax_permanent_api_key, get_xmax_setting


class XmaxAuthClientError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class XmaxAuthConfig:
    base_url: str
    permanent_api_key: str
    connect_timeout: float
    read_timeout: float


def configuration() -> XmaxAuthConfig:
    return XmaxAuthConfig(
        base_url=get_xmax_setting("XMAX_API_BASE_URL", "https://api.xmaxai.com").rstrip("/"),
        permanent_api_key=get_xmax_permanent_api_key(),
        connect_timeout=float(get_xmax_setting("XMAX_REQUEST_CONNECT_TIMEOUT", "10")),
        read_timeout=float(get_xmax_setting("XMAX_REQUEST_READ_TIMEOUT", "30")),
    )


def _public_error_message(code: str) -> str:
    return {
        "XMAX_AUTH_NOT_CONFIGURED": "Xmax server authentication is not configured.",
        "XMAX_AUTH_FAILED": "Xmax server authentication failed.",
        "XMAX_RATE_LIMITED": "Xmax temporarily rate limited the request.",
        "XMAX_NETWORK_FAILED": "Unable to reach the Xmax authentication service.",
        "XMAX_TIMEOUT": "The Xmax authentication request timed out.",
        "XMAX_RESPONSE_INVALID": "Xmax returned an invalid response.",
    }.get(code, "The Xmax request failed.")


class XmaxAuthClient:
    def __init__(self, config: XmaxAuthConfig | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config or configuration()
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation_error: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.permanent_api_key:
            raise XmaxAuthClientError("XMAX_AUTH_NOT_CONFIGURED", _public_error_message("XMAX_AUTH_NOT_CONFIGURED"))
        timeout = httpx.Timeout(
            connect=self.config.connect_timeout,
            read=self.config.read_timeout,
            write=self.config.read_timeout,
            pool=self.config.connect_timeout,
        )
        try:
            with httpx.Client(timeout=timeout, transport=self.transport, trust_env=True) as client:
                response = client.request(
                    method,
                    f"{self.config.base_url}{path}",
                    headers={"X-Api-Key": self.config.permanent_api_key, "Accept": "application/json"},
                    json=json_body,
                )
        except httpx.TimeoutException as error:
            raise XmaxAuthClientError("XMAX_TIMEOUT", _public_error_message("XMAX_TIMEOUT")) from None
        except httpx.RequestError as error:
            raise XmaxAuthClientError("XMAX_NETWORK_FAILED", _public_error_message("XMAX_NETWORK_FAILED")) from None

        if response.status_code in {401, 403}:
            raise XmaxAuthClientError("XMAX_AUTH_FAILED", _public_error_message("XMAX_AUTH_FAILED"), status_code=response.status_code)
        if response.status_code == 429:
            raise XmaxAuthClientError("XMAX_RATE_LIMITED", _public_error_message("XMAX_RATE_LIMITED"), status_code=429)
        if response.status_code >= 400:
            raise XmaxAuthClientError(operation_error, "The Xmax operation failed.", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise XmaxAuthClientError("XMAX_RESPONSE_INVALID", _public_error_message("XMAX_RESPONSE_INVALID")) from error
        if not isinstance(payload, dict):
            raise XmaxAuthClientError("XMAX_RESPONSE_INVALID", _public_error_message("XMAX_RESPONSE_INVALID"))
        if payload.get("success") is False:
            provider_code = str(payload.get("code") or "")
            if provider_code in {"46001", "46002"}:
                code = "XMAX_AUTH_FAILED"
            elif provider_code in {"429", "46029"}:
                code = "XMAX_RATE_LIMITED"
            else:
                code = operation_error
            raise XmaxAuthClientError(code, _public_error_message(code))
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise XmaxAuthClientError("XMAX_RESPONSE_INVALID", _public_error_message("XMAX_RESPONSE_INVALID"))
        return data

    def create_temporary_key(self, *, expire_seconds: int, points_limit: int) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/open/api/v1/temporary-api-key",
            operation_error="XMAX_TEMP_KEY_CREATE_FAILED",
            json_body={"expireSeconds": expire_seconds, "pointsLimit": points_limit},
        )
        required = {"uid", "temporaryApiKey", "pointsLimit", "expireTimestamp"}
        if not required.issubset(data) or not str(data.get("temporaryApiKey", "")).startswith("tk-"):
            raise XmaxAuthClientError("XMAX_RESPONSE_INVALID", _public_error_message("XMAX_RESPONSE_INVALID"))
        return data

    def get_temporary_key_status(self, temporary_key: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/open/api/v1/temporary-api-key/{quote(temporary_key, safe='')}/status",
            operation_error="XMAX_TEMP_KEY_STATUS_FAILED",
        )

    def get_session_status(self, session_uid: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/open/api/v1/session/{quote(session_uid, safe='')}/status",
            operation_error="XMAX_SESSION_STATUS_FAILED",
        )

    def inactivate_temporary_key(self, temporary_key: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/open/api/v1/temporary-api-key/{quote(temporary_key, safe='')}/inactive",
            operation_error="XMAX_TEMP_KEY_INACTIVATE_FAILED",
        )
