"""Shared target-language capabilities for subtitles and TTS voice selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class LanguageCapability:
    id: str
    code: str
    locales: tuple[str, ...]
    break_mode: str
    font_families: tuple[str, ...]
    default_edge_voices: tuple[str, ...]
    male_edge_voices: tuple[str, ...] = ()
    female_edge_voices: tuple[str, ...] = ()


LANGUAGE_CAPABILITIES: dict[str, LanguageCapability] = {
    "mandarin": LanguageCapability(
        "mandarin", "zh", ("zh-cn", "zh-sg"), "grapheme",
        ("Noto Sans CJK SC", "Noto Sans SC", "Microsoft YaHei", "SimSun"),
        ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"),
        ("zh-CN-YunxiNeural",), ("zh-CN-XiaoxiaoNeural",),
    ),
    "cantonese": LanguageCapability(
        "cantonese", "yue", ("zh-hk", "yue-hk"), "grapheme",
        ("Noto Sans CJK TC", "Noto Sans TC", "Microsoft JhengHei", "Microsoft YaHei"),
        ("zh-HK-HiuMaanNeural", "zh-HK-WanLungNeural"),
        ("zh-HK-WanLungNeural",), ("zh-HK-HiuMaanNeural",),
    ),
    "english": LanguageCapability(
        "english", "en", ("en",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("en-US-JennyNeural", "en-US-GuyNeural"),
        ("en-US-GuyNeural",), ("en-US-JennyNeural",),
    ),
    "spanish": LanguageCapability(
        "spanish", "es", ("es",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("es-ES-ElviraNeural", "es-ES-AlvaroNeural"),
        ("es-ES-AlvaroNeural",), ("es-ES-ElviraNeural",),
    ),
    "french": LanguageCapability(
        "french", "fr", ("fr",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("fr-FR-DeniseNeural", "fr-FR-HenriNeural"),
        ("fr-FR-HenriNeural",), ("fr-FR-DeniseNeural",),
    ),
    "portuguese": LanguageCapability(
        "portuguese", "pt", ("pt",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"),
        ("pt-BR-AntonioNeural",), ("pt-BR-FranciscaNeural",),
    ),
    "japanese": LanguageCapability(
        "japanese", "ja", ("ja",), "grapheme",
        ("Noto Sans CJK JP", "Noto Sans JP", "Yu Gothic", "Meiryo", "MS Gothic"),
        ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
        ("ja-JP-KeitaNeural",), ("ja-JP-NanamiNeural",),
    ),
    "korean": LanguageCapability(
        "korean", "ko", ("ko",), "grapheme",
        ("Noto Sans CJK KR", "Noto Sans KR", "Malgun Gothic"),
        ("ko-KR-SunHiNeural", "ko-KR-InJoonNeural"),
        ("ko-KR-InJoonNeural",), ("ko-KR-SunHiNeural",),
    ),
    "vietnamese": LanguageCapability(
        "vietnamese", "vi", ("vi",), "word",
        ("Arial", "Noto Sans", "DejaVu Sans"),
        ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"),
        ("vi-VN-NamMinhNeural",), ("vi-VN-HoaiMyNeural",),
    ),
    "thai": LanguageCapability(
        "thai", "th", ("th",), "grapheme",
        ("Noto Sans Thai", "Leelawadee UI", "Tahoma"),
        ("th-TH-PremwadeeNeural", "th-TH-NiwatNeural"),
        ("th-TH-NiwatNeural",), ("th-TH-PremwadeeNeural",),
    ),
    "indonesian": LanguageCapability(
        "indonesian", "id", ("id",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("id-ID-GadisNeural", "id-ID-ArdiNeural"),
        ("id-ID-ArdiNeural",), ("id-ID-GadisNeural",),
    ),
    "arabic": LanguageCapability(
        "arabic", "ar", ("ar",), "word",
        ("Noto Sans Arabic", "DejaVu Sans", "Arial"),
        ("ar-SA-ZariyahNeural", "ar-SA-HamedNeural"),
        ("ar-SA-HamedNeural",), ("ar-SA-ZariyahNeural",),
    ),
    "german": LanguageCapability(
        "german", "de", ("de",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("de-DE-KatjaNeural", "de-DE-ConradNeural"),
        ("de-DE-ConradNeural",), ("de-DE-KatjaNeural",),
    ),
    "italian": LanguageCapability(
        "italian", "it", ("it",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("it-IT-ElsaNeural", "it-IT-DiegoNeural"),
        ("it-IT-DiegoNeural",), ("it-IT-ElsaNeural",),
    ),
    "russian": LanguageCapability(
        "russian", "ru", ("ru",), "word",
        ("Arial", "DejaVu Sans", "Liberation Sans"),
        ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"),
        ("ru-RU-DmitryNeural",), ("ru-RU-SvetlanaNeural",),
    ),
}


LANGUAGE_ALIASES = {
    capability.id: capability.id
    for capability in LANGUAGE_CAPABILITIES.values()
}
for _capability in LANGUAGE_CAPABILITIES.values():
    LANGUAGE_ALIASES[_capability.code] = _capability.id
    for _locale in _capability.locales:
        LANGUAGE_ALIASES[_locale] = _capability.id
LANGUAGE_ALIASES.update({
    "chinese": "mandarin",
    "zh": "mandarin",
    "zh-cn": "mandarin",
    "zh-sg": "mandarin",
    "yue": "cantonese",
    "zh-hk": "cantonese",
})


def normalize_target_language(value: str | None) -> LanguageCapability:
    normalized = str(value or "english").strip().lower().replace("_", "-")
    capability_id = LANGUAGE_ALIASES.get(normalized)
    if not capability_id:
        raise ValueError(f"Unsupported target language: {value}")
    return LANGUAGE_CAPABILITIES[capability_id]


def language_code(value: str | None) -> str:
    return normalize_target_language(value).code


def voice_matches_target_language(
    target_language: str | None,
    *,
    locale: str | None = None,
    language: str | None = None,
    supported_languages: Iterable[str] = (),
) -> bool:
    capability = normalize_target_language(target_language)
    values = {
        str(item or "").strip().lower().replace("_", "-")
        for item in (locale, language, *supported_languages)
        if str(item or "").strip()
    }
    for value in values:
        if value in capability.locales:
            return True
        if capability.id == "mandarin" and value in {"zh", "cmn", "zh-cn", "zh-sg"}:
            return True
        if capability.id == "cantonese" and value in {"yue", "zh-hk", "yue-hk"}:
            return True
        if value == capability.code or value.startswith(f"{capability.code}-"):
            return True
        if any(
            locale_prefix == value or value.startswith(f"{locale_prefix}-")
            for locale_prefix in capability.locales
        ):
            return True
    return False


def voice_row_matches_target_language(row: dict[str, Any], target_language: str | None) -> bool:
    labels = dict(row.get("labels") or {})
    metadata = dict(row.get("metadata") or {})
    supported: list[str] = []
    for profile in (metadata.get("endpoint_profiles") or {}).values():
        if isinstance(profile, dict):
            supported.extend(str(item) for item in profile.get("languages") or [])
    supported.extend(str(item) for item in metadata.get("verified_languages") or [])
    return voice_matches_target_language(
        target_language,
        locale=str(metadata.get("locale") or labels.get("locale") or row.get("locale") or ""),
        language=str(metadata.get("language") or labels.get("language") or row.get("language") or ""),
        supported_languages=supported,
    )


def normalized_gender(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"male", "man", "boy", "m", "男", "男性"}:
        return "male"
    if normalized in {"female", "woman", "girl", "f", "女", "女性"}:
        return "female"
    return "unknown"


def recommended_voice_id(
    rows: list[dict[str, Any]],
    target_language: str | None,
    gender: str | None = None,
) -> str | None:
    capability = normalize_target_language(target_language)
    compatible = [row for row in rows if voice_row_matches_target_language(row, capability.id)]
    if not compatible:
        return None
    normalized = normalized_gender(gender)
    preferred = (
        capability.male_edge_voices if normalized == "male"
        else capability.female_edge_voices if normalized == "female"
        else capability.default_edge_voices
    )
    by_id = {str(row.get("voice_id") or ""): row for row in compatible}
    for voice_id in (*preferred, *capability.default_edge_voices):
        if voice_id in by_id:
            return voice_id
    if normalized != "unknown":
        for row in compatible:
            labels = dict(row.get("labels") or {})
            metadata = dict(row.get("metadata") or {})
            if normalized_gender(str(metadata.get("gender_or_role") or labels.get("gender") or "")) == normalized:
                return str(row.get("voice_id") or "") or None
    return str(sorted(compatible, key=lambda row: (str(row.get("name") or ""), str(row.get("voice_id") or "")))[0].get("voice_id") or "") or None
