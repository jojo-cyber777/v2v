"""Capture one structured translation response before any downstream commit."""

from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class TranslationCaptureError(RuntimeError):
    pass


def _optional_token_count(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def extract_translation_capture(
    provider_response: dict[str, Any],
    *,
    expected_segment_id: str,
    input_hash: str,
    required_term: str,
) -> dict[str, Any]:
    """Return an immutable JSON-safe capture with stable, non-overlapping names."""
    if not isinstance(provider_response, dict):
        raise TranslationCaptureError("Provider response must be an object.")
    response_copy = deepcopy(provider_response)
    translation_rows = response_copy.get("translations")
    if not isinstance(translation_rows, list):
        raise TranslationCaptureError("Provider response does not contain translations.")
    matching_rows = [
        translation_row
        for translation_row in translation_rows
        if isinstance(translation_row, dict)
        and str(translation_row.get("id") or "") == expected_segment_id
    ]
    if len(matching_rows) != 1:
        raise TranslationCaptureError("Provider response must contain exactly one matching segment.")
    translation_row = matching_rows[0]
    translated_text = str(translation_row.get("final") or "").strip()
    if not translated_text:
        raise TranslationCaptureError("Final translated text is empty.")
    if "???" in translated_text or "\ufffd" in translated_text:
        raise TranslationCaptureError("Final translated text contains invalid replacement characters.")
    if required_term.casefold() not in translated_text.casefold():
        raise TranslationCaptureError(f"Final translated text does not preserve {required_term}.")

    usage = response_copy.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    capture = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "provider_response": response_copy,
        "translated_text": translated_text,
        "request_id": str(response_copy.get("provider_request_id") or "") or None,
        "input_tokens": _optional_token_count(usage, "prompt_tokens"),
        "output_tokens": _optional_token_count(usage, "completion_tokens"),
        "total_tokens": _optional_token_count(usage, "total_tokens"),
        "provider": str(response_copy.get("provider") or "") or None,
        "model": str(response_copy.get("model") or "") or None,
        "input_hash": str(input_hash),
        "segment_id": expected_segment_id,
    }
    try:
        return json.loads(json.dumps(capture, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise TranslationCaptureError("Translation capture is not JSON serializable.") from error


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def save_translation_capture(
    capture: dict[str, Any],
    *,
    json_path: Path,
    text_path: Path,
    commit_after_reports: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Persist and reread both reports before an optional database commit callback."""
    json_path = Path(json_path)
    text_path = Path(text_path)
    json_content = json.dumps(capture, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    text_content = "\n".join(
        [
            f"provider: {capture.get('provider')}",
            f"model: {capture.get('model')}",
            f"request_id: {capture.get('request_id')}",
            f"input_hash: {capture.get('input_hash')}",
            f"input_tokens: {capture.get('input_tokens')}",
            f"output_tokens: {capture.get('output_tokens')}",
            f"total_tokens: {capture.get('total_tokens')}",
            f"translated_text: {capture.get('translated_text')}",
            "provider_response:",
            json.dumps(capture.get("provider_response"), ensure_ascii=False, indent=2, sort_keys=True),
            "",
        ]
    )
    _atomic_write_text(json_path, json_content)
    _atomic_write_text(text_path, text_content)

    reloaded_json = json.loads(json_path.read_text(encoding="utf-8"))
    reloaded_text = text_path.read_text(encoding="utf-8")
    if reloaded_json != capture or str(capture.get("translated_text")) not in reloaded_text:
        raise TranslationCaptureError("Translation reports failed read-back validation.")
    if commit_after_reports is not None:
        commit_after_reports(reloaded_json)
    return reloaded_json


def capture_provider_call(
    provider_call: Callable[[], dict[str, Any]],
    *,
    expected_segment_id: str,
    input_hash: str,
    required_term: str,
    json_path: Path,
    text_path: Path,
    commit_after_reports: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Call a provider once, capture its response, and persist before committing."""
    provider_response = provider_call()
    capture = extract_translation_capture(
        provider_response,
        expected_segment_id=expected_segment_id,
        input_hash=input_hash,
        required_term=required_term,
    )
    return save_translation_capture(
        capture,
        json_path=json_path,
        text_path=text_path,
        commit_after_reports=commit_after_reports,
    )
