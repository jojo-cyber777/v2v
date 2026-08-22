"""Shared validity rules for persisted Xmax visual results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


def _value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _diagnostics(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    value = _value(row, "diagnostics_json")
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def default_valid_file(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    return path.is_file() and path.stat().st_size > 0


def segment_preserved_by_user(row: Mapping[str, Any] | Any) -> bool:
    return bool(_value(row, "use_source_for_merge")) and _diagnostics(row).get("preserveSourceByUser") is True


def evaluate_visual_result(
    task: Mapping[str, Any] | Any,
    segments: Sequence[Mapping[str, Any] | Any],
    *,
    valid_file: Callable[[str | None], bool] = default_valid_file,
) -> dict[str, Any]:
    real_output_count = 0
    explicit_source_count = 0
    automatic_fallback_count = 0
    invalid_segment_ids: list[str] = []

    for row in segments:
        segment_id = str(_value(row, "id") or _value(row, "segment_id") or "")
        source_mapping = bool(_value(row, "preserve_original")) or _value(row, "mapping_status") == "preserve_original"
        explicit_source = source_mapping or segment_preserved_by_user(row)
        if explicit_source:
            if valid_file(_value(row, "source_path")):
                explicit_source_count += 1
            else:
                invalid_segment_ids.append(segment_id)
            continue

        if bool(_value(row, "use_source_for_merge")):
            automatic_fallback_count += 1

        execution = _value(row, "execution_status") or _value(row, "status")
        output_valid = (
            execution == "completed"
            and _value(row, "quality_status") == "approved"
            and valid_file(_value(row, "output_path"))
        )
        if output_valid:
            real_output_count += 1
        else:
            invalid_segment_ids.append(segment_id)

    units_valid = bool(segments) and not invalid_segment_ids and automatic_fallback_count == 0
    final_file_valid = (
        _value(task, "status") == "completed"
        and bool(_value(task, "final_output_url"))
        and valid_file(_value(task, "final_output_path"))
    )
    result_valid = units_valid and final_file_valid
    source_only = bool(segments) and real_output_count == 0
    if result_valid:
        classification = "valid_explicit_source_only" if source_only else "valid"
    elif automatic_fallback_count:
        classification = "stale_source_only"
    elif not units_valid:
        classification = "incomplete"
    else:
        classification = "missing_final_output"

    return {
        "result_valid": result_valid,
        "classification": classification,
        "source_only": source_only,
        "real_output_count": real_output_count,
        "explicit_source_count": explicit_source_count,
        "automatic_fallback_count": automatic_fallback_count,
        "invalid_segment_ids": invalid_segment_ids,
        "total_units": len(segments),
    }
