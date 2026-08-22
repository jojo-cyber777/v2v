"""Resolve the persisted text and language used by subtitles and TTS."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import language_capabilities


TRANSLATE_DUB = "translate-dub"
DUB_ONLY = "dub-only"
SUPPORTED_LANGUAGE_MODES = {TRANSLATE_DUB, DUB_ONLY}
UNKNOWN_SOURCE_LANGUAGES = {"", "auto", "unknown", "und", "unsupported", "none", "null"}


class LocalizationTextAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LocalizationTextAuthority:
    language_mode: str
    text_field: str
    language: str
    transcript_document_id: str
    translation_document_id: str | None = None

    @property
    def translation_required(self) -> bool:
        return self.text_field == "final_translation"

    def text_for_segment(self, segment: Mapping[str, Any]) -> str:
        return str(segment.get(self.text_field) or "").strip()

    def apply_to_segment(self, segment: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(segment)
        item.update(
            {
                "localization_mode": self.language_mode,
                "localization_text_field": self.text_field,
                "localization_text": self.text_for_segment(segment),
                "localization_language": self.language,
                "target_language": self.language,
            }
        )
        return item

    def apply_to_segments(self, segments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self.apply_to_segment(segment) for segment in segments]


def _settings_override(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalized_mode(value: Any) -> str:
    mode = str(value or TRANSLATE_DUB).strip().lower()
    return mode if mode in SUPPORTED_LANGUAGE_MODES else TRANSLATE_DUB


def _normalized_language_id(value: str) -> str:
    try:
        return language_capabilities.normalize_target_language(value).id
    except ValueError as error:
        primary_language = value.split("-", 1)[0]
        if primary_language != value:
            try:
                return language_capabilities.normalize_target_language(primary_language).id
            except ValueError:
                pass
        raise error


def _source_language(candidates: Iterable[Any]) -> str:
    unsupported: str | None = None
    for candidate in candidates:
        value = str(candidate or "").strip().lower().replace("_", "-")
        if value in UNKNOWN_SOURCE_LANGUAGES:
            continue
        try:
            return _normalized_language_id(value)
        except ValueError:
            unsupported = value
    if unsupported:
        raise LocalizationTextAuthorityError(
            "LOCALIZATION_SOURCE_LANGUAGE_UNSUPPORTED",
            f"The detected source language '{unsupported}' is not supported for dubbing.",
        )
    raise LocalizationTextAuthorityError(
        "LOCALIZATION_SOURCE_LANGUAGE_REQUIRED",
        "Confirm the source language before generating original-language dubbing.",
    )


def _target_language(candidates: Iterable[Any]) -> str:
    for candidate in candidates:
        value = str(candidate or "").strip().lower().replace("_", "-")
        if value in UNKNOWN_SOURCE_LANGUAGES:
            continue
        try:
            return _normalized_language_id(value)
        except ValueError as error:
            raise LocalizationTextAuthorityError(
                "LOCALIZATION_LANGUAGE_UNSUPPORTED",
                str(error),
            ) from error
    return language_capabilities.normalize_target_language("english").id


def resolve_episode_text_authority(episode_id: str) -> LocalizationTextAuthority:
    # Keep this import lazy because database migrations import the TTS revision helper.
    from ..database import get_connection

    with get_connection() as db:
        episode = db.execute(
            "SELECT e.*, p.source_language AS project_source_language, "
            "p.target_language AS project_target_language, "
            "p.default_source_language, p.default_target_language, p.default_language_mode, "
            "s.source_language AS settings_source_language, "
            "s.target_language AS settings_target_language, "
            "s.language_mode AS settings_language_mode "
            "FROM episodes e JOIN projects p ON p.id=e.project_id "
            "LEFT JOIN settings s ON s.project_id=p.id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
        if not episode:
            raise LocalizationTextAuthorityError("EPISODE_NOT_FOUND", "Episode not found.")
        document = db.execute(
            "SELECT * FROM transcript_documents WHERE episode_id=? AND status='completed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        if not document:
            raise LocalizationTextAuthorityError(
                "LOCALIZATION_ASR_REQUIRED",
                "Run the existing ASR step before localizing this Episode.",
            )
        translation = db.execute(
            "SELECT * FROM translation_documents WHERE transcript_document_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (document["id"],),
        ).fetchone()

    episode_item = dict(episode)
    document_item = dict(document)
    translation_item = dict(translation) if translation else None
    override = _settings_override(episode_item.get("settings_override"))
    mode = _normalized_mode(
        override.get("language_mode")
        or episode_item.get("settings_language_mode")
        or episode_item.get("default_language_mode")
    )

    source_language_candidates = (
        document_item.get("detected_language"),
        document_item.get("source_language"),
        override.get("source_language"),
        episode_item.get("source_language"),
        episode_item.get("settings_source_language"),
        episode_item.get("default_source_language"),
        episode_item.get("project_source_language"),
    )

    if mode == DUB_ONLY:
        language = _source_language(source_language_candidates)
        return LocalizationTextAuthority(
            language_mode=mode,
            text_field="source_text",
            language=language,
            transcript_document_id=str(document_item["id"]),
        )

    language = _target_language(
        (
            override.get("target_language"),
            episode_item.get("settings_target_language"),
            episode_item.get("default_target_language"),
            episode_item.get("project_target_language"),
            (translation_item or {}).get("target_language"),
        )
    )
    try:
        source_language = _source_language(source_language_candidates)
    except LocalizationTextAuthorityError:
        source_language = None
    if source_language == language:
        return LocalizationTextAuthority(
            language_mode=DUB_ONLY,
            text_field="source_text",
            language=source_language,
            transcript_document_id=str(document_item["id"]),
        )
    return LocalizationTextAuthority(
        language_mode=mode,
        text_field="final_translation",
        language=language,
        transcript_document_id=str(document_item["id"]),
        translation_document_id=(
            str(translation_item["id"]) if translation_item and translation_item.get("id") else None
        ),
    )


def effective_text(segment: Mapping[str, Any]) -> str:
    if str(segment.get("localization_mode") or "") == DUB_ONLY:
        return str(segment.get("localization_text") or segment.get("source_text") or "").strip()
    return str(segment.get("localization_text") or segment.get("final_translation") or "").strip()
