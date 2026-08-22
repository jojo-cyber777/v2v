"""Validated local Tencent Cloud TTS voice catalog access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "tencent_tts_voices.json"
TEXT_TO_VOICE_ENDPOINT = "basic_synthesis"
KNOWN_ENDPOINTS = {
    "realtime_synthesis",
    TEXT_TO_VOICE_ENDPOINT,
    "long_text_synthesis",
}


@dataclass(frozen=True)
class TencentVoiceCatalogError(Exception):
    code: str
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return self.message


def _invalid(detail: str) -> TencentVoiceCatalogError:
    return TencentVoiceCatalogError(
        "VOICE_CATALOG_INVALID",
        "The local Tencent TTS voice catalog is invalid.",
        detail,
    )


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string.")
    return value.strip()


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise _invalid(f"{field} must be a{' non-empty' if not allow_empty else ''} list.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _invalid(f"{field} contains an invalid value.")
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise _invalid(f"{field} contains duplicate values.")
    return result


def _validate_profile(profile: Any, field: str) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise _invalid(f"{field} must be an object.")
    languages = _string_list(profile.get("languages"), f"{field}.languages")
    sample_rates = profile.get("sample_rates_hz")
    if not isinstance(sample_rates, list) or not sample_rates:
        raise _invalid(f"{field}.sample_rates_hz must be a non-empty list.")
    normalized_rates: list[int] = []
    for value in sample_rates:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _invalid(f"{field}.sample_rates_hz contains an invalid value.")
        normalized_rates.append(value)
    if len(normalized_rates) != len(set(normalized_rates)):
        raise _invalid(f"{field}.sample_rates_hz contains duplicate values.")
    return {
        **profile,
        "language_label": _required_string(profile.get("language_label"), f"{field}.language_label"),
        "languages": languages,
        "sample_rates_hz": normalized_rates,
        "emotions": _string_list(profile.get("emotions"), f"{field}.emotions", allow_empty=True),
    }


def _validate_voice(raw: Any, index: int) -> dict[str, Any]:
    field = f"voices[{index}]"
    if not isinstance(raw, dict):
        raise _invalid(f"{field} must be an object.")
    voice_type = raw.get("voice_type")
    if isinstance(voice_type, bool) or not isinstance(voice_type, int) or voice_type < 0:
        raise _invalid(f"{field}.voice_type must be a non-negative integer.")
    supported_endpoints = _string_list(
        raw.get("supported_endpoints"),
        f"{field}.supported_endpoints",
    )
    unknown_endpoints = sorted(set(supported_endpoints) - KNOWN_ENDPOINTS)
    if unknown_endpoints:
        raise _invalid(f"{field}.supported_endpoints contains unknown values: {', '.join(unknown_endpoints)}.")
    profiles = raw.get("endpoint_profiles")
    if not isinstance(profiles, dict):
        raise _invalid(f"{field}.endpoint_profiles must be an object.")
    normalized_profiles: dict[str, Any] = {}
    for endpoint in supported_endpoints:
        if endpoint not in profiles:
            raise _invalid(f"{field}.endpoint_profiles is missing {endpoint}.")
        normalized_profiles[endpoint] = _validate_profile(
            profiles[endpoint],
            f"{field}.endpoint_profiles.{endpoint}",
        )
    extra_profiles = sorted(set(profiles) - set(supported_endpoints))
    if extra_profiles:
        raise _invalid(f"{field}.endpoint_profiles has unsupported entries: {', '.join(extra_profiles)}.")
    compatible = TEXT_TO_VOICE_ENDPOINT in supported_endpoints
    if "text_to_voice_compatible" in raw and bool(raw["text_to_voice_compatible"]) != compatible:
        raise _invalid(f"{field}.text_to_voice_compatible conflicts with supported_endpoints.")
    return {
        **raw,
        "voice_type": voice_type,
        "name": _required_string(raw.get("name"), f"{field}.name"),
        "recommended_scene": _required_string(
            raw.get("recommended_scene"),
            f"{field}.recommended_scene",
        ),
        "voice_tier": _required_string(raw.get("voice_tier"), f"{field}.voice_tier"),
        "gender_or_role": _required_string(
            raw.get("gender_or_role"),
            f"{field}.gender_or_role",
        ),
        "supported_endpoints": supported_endpoints,
        "text_to_voice_compatible": compatible,
        "endpoint_profiles": normalized_profiles,
    }


def load_tencent_voice_catalog(path: Path | None = None) -> dict[str, Any]:
    source_path = path or CATALOG_PATH
    if not source_path.is_file():
        raise TencentVoiceCatalogError(
            "VOICE_CATALOG_SOURCE_MISSING",
            "The local Tencent TTS voice catalog is missing.",
            str(source_path),
        )
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _invalid(f"Unable to read {source_path}: {error}") from error
    if not isinstance(payload, dict):
        raise _invalid("The catalog root must be an object.")
    schema_version = _required_string(payload.get("schema_version"), "schema_version")
    catalog_version = _required_string(payload.get("catalog_version"), "catalog_version")
    voices = payload.get("voices")
    if not isinstance(voices, list) or not voices:
        raise _invalid("voices must be a non-empty list.")
    normalized = [_validate_voice(item, index) for index, item in enumerate(voices)]
    voice_types = [item["voice_type"] for item in normalized]
    if len(voice_types) != len(set(voice_types)):
        raise _invalid("voice_type values must be unique.")
    return {
        **payload,
        "schema_version": schema_version,
        "catalog_version": catalog_version,
        "voices": normalized,
    }


def provider_voice_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    catalog_version = str(catalog["catalog_version"])
    rows: list[dict[str, Any]] = []
    for voice in catalog["voices"]:
        basic_profile = voice["endpoint_profiles"].get(TEXT_TO_VOICE_ENDPOINT, {})
        rows.append({
            "voice_id": f"tencent:{voice['voice_type']}",
            "provider": "tencent",
            "name": voice["name"],
            "category": voice["voice_tier"],
            "labels": {
                "gender": voice["gender_or_role"],
                "language": str(basic_profile.get("language_label") or ""),
                "recommended_scene": voice["recommended_scene"],
                "voice_tier": voice["voice_tier"],
            },
            "description": voice["recommended_scene"],
            "preview_url": None,
            "metadata": {
                "voice_type": str(voice["voice_type"]),
                "catalog_version": catalog_version,
                "recommended_scene": voice["recommended_scene"],
                "voice_tier": voice["voice_tier"],
                "gender_or_role": voice["gender_or_role"],
                "supported_endpoints": voice["supported_endpoints"],
                "endpoint_profiles": voice["endpoint_profiles"],
                "text_to_voice_compatible": voice["text_to_voice_compatible"],
                "requires_billing_notice": voice["voice_tier"] in {
                    "大模型音色",
                    "超自然大模型音色",
                },
            },
            "verified_languages": [],
            "high_quality_base_model_ids": [],
        })
    return rows


def find_voice(catalog: dict[str, Any], voice_id: str | int) -> dict[str, Any] | None:
    normalized = str(voice_id or "").strip()
    if normalized.startswith("tencent:"):
        normalized = normalized.split(":", 1)[1]
    if not normalized.isdigit():
        return None
    voice_type = int(normalized)
    return next(
        (voice for voice in catalog["voices"] if voice["voice_type"] == voice_type),
        None,
    )
