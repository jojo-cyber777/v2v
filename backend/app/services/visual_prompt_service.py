"""Centralized prompt construction for the B1 visual transformation request."""

from __future__ import annotations

from typing import Any


STYLE_PROMPTS = {
    "western-fantasy": "western fantasy live action, grounded cinematic production design",
    "western-live-action": "western live action, cinematic natural lighting and realistic production design",
    "japanese-anime": "Japanese anime, clean expressive animation and coherent character design",
    "korean-webtoon": "Korean webtoon, polished illustrated drama style and controlled linework",
    "american-comic": "American comic book, bold ink, controlled color blocking and cinematic panels",
    "3d-animation": "high quality 3D animation, coherent materials, lighting and character silhouettes",
    "cinematic-realism": "cinematic realism with refined production design",
}


def build_visual_prompt(style: str, aspect_ratio: float, locked_characters: list[dict[str, Any]], extra_description: str = "") -> dict[str, str]:
    style_text = STYLE_PROMPTS.get(style, STYLE_PROMPTS["cinematic-realism"])
    refs = ", ".join(str(item.get("name") or item.get("character_code") or "locked character") for item in locked_characters)
    keep_original = "Preserve the original motion, camera movement, timing, framing, positions, expressions, action, and scene composition. Do not add people, animals, props, text, logos, or cuts."
    identity = f"Keep the locked project characters consistent: {refs}. Preserve their faces, hair, body proportions and voice-independent identity while changing the visual style." if refs else "Preserve every person's original identity and placement; do not introduce new roles."
    extra = extra_description.strip()
    full = f"Transform the full scene into {style_text}. {keep_original} {identity} Keep the source aspect ratio near {aspect_ratio:.3f}. {extra}".strip()
    summary = f"Full-scene transformation to {style_text}; preserve motion, composition and locked character identity." + (f" Extra direction: {extra}" if extra else "")
    return {"summary": summary, "full_prompt": full}
