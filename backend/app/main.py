from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image
from starlette.formparsers import MultiPartException, MultiPartParser

from .database import DB_PATH, ROOT, UPLOADS_DIR, bool_int, get_connection, init_db, json_dump, json_load, now_iso, row_to_dict
from .services import ai_service, media_service, provider_service, subtitle_service, task_state
from .routes.stage3a1 import router as stage3a1_router
from .routes.stage3a2 import router as stage3a2_router
from .routes.stage3b1 import router as stage3b1_router
from .routes.xmax_realtime import router as xmax_realtime_router
from .routes.xmax_single_shot import router as xmax_single_shot_router
from .routes.xmax_thirty_second import router as xmax_thirty_second_router
from .routes.overseas_episode import router as overseas_episode_router
from .routes.auth import router as auth_router
from .routes.admin import router as admin_router
from .routes.admin_billing import router as admin_billing_router
from .routes.credits import router as credits_router
from .routes.xmax_temporary_keys import router as xmax_temporary_keys_router
from .routes.xmax_browser_attempts import router as xmax_browser_attempts_router
from .routes.admin_xmax import router as admin_xmax_router
from .routes.self_service_closure import router as self_service_closure_router
from .access_control import enforce_api_access, require_project_access, require_request_user
from .protected_media import ProtectedMediaFiles
from .secure_response import SecureJSONResponse
from .user_storage import STORAGE_ROOT, project_uploads_dir, resolve_upload_asset_path, storage_asset_url

app = FastAPI(
    title="AI Drama Transformation Studio API",
    version="0.1.0",
    default_response_class=SecureJSONResponse,
    dependencies=[Depends(enforce_api_access)],
)

MAX_VIDEO_UPLOAD_BYTES = 500 * 1024 * 1024


class EpisodeUploadRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def parse_large_upload_form(request: Request):
            if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                parser = MultiPartParser(request.headers, request.stream())
                parser.max_part_size = MAX_VIDEO_UPLOAD_BYTES
                try:
                    request._form = await parser.parse()
                except MultiPartException as error:
                    raise HTTPException(status_code=400, detail=error.message) from error
            return await route_handler(request)

        return parse_large_upload_form


@app.exception_handler(HTTPException)
async def sanitized_http_exception_handler(_request: Request, error: HTTPException) -> SecureJSONResponse:
    return SecureJSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail},
        headers=error.headers,
    )


app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
OUTPUTS_DIR = ROOT / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
app.mount("/mock-assets", StaticFiles(directory=ROOT / "mock-assets"), name="mock-assets")
app.mount("/uploads", ProtectedMediaFiles("uploads"), name="uploads")
app.mount("/outputs", ProtectedMediaFiles("outputs"), name="outputs")
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(credits_router)
app.include_router(admin_billing_router)
app.include_router(xmax_temporary_keys_router)
app.include_router(xmax_browser_attempts_router)
app.include_router(admin_xmax_router)
app.include_router(stage3a1_router)
app.include_router(stage3a2_router)
app.include_router(stage3b1_router)
app.include_router(xmax_realtime_router)
app.include_router(xmax_single_shot_router)
app.include_router(xmax_thirty_second_router)
app.include_router(overseas_episode_router)
app.include_router(self_service_closure_router)

TASK_STEPS = [
    "Upload Validation", "Source Language Detection", "Audio Extraction", "Keyframe Extraction", "Character Detection", "Scene Detection", "Character Asset Loading", "Video Segmentation", "Character Transformation", "Background and Scene Transformation", "Object and Clothing Transformation", "Dialogue Translation", "Character Voice Generation", "Lip Sync", "Subtitle Generation", "Segment Rendering", "Video Merging", "Audio and Video Alignment", "Quality Check", "Final Export",
]
CHARACTER_TYPES = {"Human", "Anime", "Comic", "3D", "Creature", "Pet", "Animal", "Other"}
ENTITY_TYPES = {"Human", "Pet", "Animal", "Other"}
REFERENCE_FIELDS = ("original_reference_image", "front_view_image", "angle_view_image", "side_view_image", "full_body_image")
REFERENCE_ASSET_TYPES = {
    "original_reference_image": "original-frame",
    "front_view_image": "front-view",
    "angle_view_image": "three-quarter-view",
    "side_view_image": "side-view",
    "full_body_image": "full-body",
}
PLACEHOLDER_NAMES = {"name", "姓名", "角色名", "character name", "new character", "新角色", "new character name"}
STYLE_CATALOG = [
    {"id": "western-live-action", "name": "Western Live Action", "description": "Cinematic realism with modern wardrobe, natural light, and grounded actors.", "accent": "#e7b76a", "prompt": ["realistic Western actors", "Western facial features", "cinematic wardrobe", "realistic lighting", "modern live-action drama"]},
    {"id": "japanese-anime", "name": "Japanese Anime", "description": "Clean line art, expressive eyes, and precise cel-shaded character continuity.", "accent": "#e78b9d", "prompt": ["2D anime", "clean line art", "expressive eyes", "cel shading", "consistent anime character design"]},
    {"id": "korean-webtoon", "name": "Korean Webtoon", "description": "Elegant digital illustration with soft shading and romantic drama atmosphere.", "accent": "#9aa6ef", "prompt": ["Korean webtoon", "elegant character design", "soft shading", "romantic drama atmosphere", "clean digital illustration"]},
    {"id": "american-comic", "name": "American Comic", "description": "Graphic ink, confident silhouettes, and bold panels with cinematic contrast.", "accent": "#6ec9bb", "prompt": ["American comic book", "bold ink lines", "graphic shadows", "dynamic composition", "heroic color blocking"]},
    {"id": "3d-animation", "name": "3D Animation", "description": "Polished 3D characters, tactile materials, and soft studio lighting.", "accent": "#c2a4e9", "prompt": ["stylized 3D animation", "soft materials", "cinematic studio lighting", "consistent character model", "detailed environment"]},
    {"id": "cinematic-realism", "name": "Cinematic Realism", "description": "Premium film texture, controlled color, and a natural live-action finish.", "accent": "#d9985f", "prompt": ["cinematic realism", "natural skin texture", "controlled film color", "realistic production design", "premium drama still"]},
]
LANGUAGES = [
    {"id": "auto", "name": "Auto Detect", "type": "automatic"}, {"id": "mandarin", "name": "Mandarin Chinese", "type": "language"}, {"id": "cantonese", "name": "Cantonese", "type": "dialect"}, {"id": "english", "name": "English", "type": "language"}, {"id": "spanish", "name": "Spanish", "type": "language"}, {"id": "french", "name": "French", "type": "language"}, {"id": "portuguese", "name": "Portuguese", "type": "language"}, {"id": "japanese", "name": "Japanese", "type": "language"}, {"id": "korean", "name": "Korean", "type": "language"}, {"id": "vietnamese", "name": "Vietnamese", "type": "language"}, {"id": "thai", "name": "Thai", "type": "language"}, {"id": "indonesian", "name": "Indonesian", "type": "language"}, {"id": "arabic", "name": "Arabic", "type": "language"}, {"id": "german", "name": "German", "type": "language"}, {"id": "italian", "name": "Italian", "type": "language"}, {"id": "russian", "name": "Russian", "type": "language"},
]
SUPPORTED_TARGET_LANGUAGES = {
    "english", "spanish", "portuguese", "french", "german", "japanese", "korean", "thai", "vietnamese", "indonesian",
    "en", "es", "pt", "fr", "de", "ja", "ko", "th", "vi", "id",
}
VOICE_CATALOG = [
    {"id": "auto-match", "name": "Auto Match", "base_voice": "Auto Match", "sample": "Balanced and scene-aware"}, {"id": "male-deep", "name": "Atlas", "base_voice": "Male", "sample": "Low, calm and cinematic"}, {"id": "female-warm", "name": "Mira", "base_voice": "Female", "sample": "Warm, clear and intimate"}, {"id": "neutral-calm", "name": "Sora", "base_voice": "Neutral", "sample": "Neutral and adaptable"},
]
VOICE_PROVIDERS = [{"id": "none", "name": "No provider connected", "models": []}]
VOICE_SOURCE_TYPES = ["system-preset", "external-tts", "upload-sample", "voice-clone"]

MOCK_FRAME_PRESETS: dict[str, list[dict[str, int]]] = {
    # Existing three-person outdoor demo frame: left, center, right.
    "78ca3980edecd52f21fe11f236be14284d805a4f370fe6186bae63a71bbec180": [
        {"x": 13, "y": 17, "width": 25, "height": 81},
        {"x": 38, "y": 13, "width": 23, "height": 85},
        {"x": 61, "y": 12, "width": 23, "height": 86},
        {"x": 0, "y": 18, "width": 14, "height": 77},
        {"x": 84, "y": 18, "width": 16, "height": 77},
    ],
    # Existing supermarket demo frame: the single foreground actor.
    "e7cc952cf6e0c84019ca57b33bb37ef376ce2fc8c7baeeb0f4407867e73297f8": [
        {"x": 37, "y": 1, "width": 61, "height": 97},
    ],
}


class ProjectCreate(BaseModel):
    name: str = Field(default="Untitled Drama", min_length=1, max_length=120)
    processing_mode: str = "quick-transformation"


class ProjectUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    source_language: str | None = None
    target_language: str | None = None
    video_duration: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    orientation: str | None = None
    processing_mode: str | None = None
    use_character_library: bool | None = None
    default_style_id: str | None = None
    default_source_language: str | None = None
    default_target_language: str | None = None
    default_language_mode: str | None = None
    default_subtitle_enabled: bool | None = None
    default_lip_sync_enabled: bool | None = None
    character_appearance_policy: str | None = None
    active_episode_id: str | None = None


class CharacterPayload(BaseModel):
    name: str = "New Character"
    gender: str = "Unknown"
    estimated_age: str = "Unknown"
    role: str = "Supporting"
    appearance_description: str = ""
    hair_description: str = ""
    headwear_description: str = ""
    clothing_description: str = ""
    original_reference_image: str | None = None
    source_crop_path: str | None = None
    target_reference_path: str | None = None
    front_view_image: str | None = None
    angle_view_image: str | None = None
    side_view_image: str | None = None
    full_body_image: str | None = None
    voice_id: str = "auto-match"
    voice_settings: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    character_code: str | None = None
    character_type: str = "Human"
    primary_reference_type: str | None = None
    priority_level: str = "Temporary"
    identity_description: str = ""
    face_description: str = ""
    body_description: str = ""
    default_clothing_description: str = ""
    voice_profile_id: str | None = None
    source: str = "manual"
    created_from_episode_id: str | None = None


class SettingsPayload(BaseModel):
    style_id: str = "cinematic-realism"
    source_language: str = "auto"
    target_language: str = "english"
    language_mode: str = "translate-dub"
    character_consistency: str = "strong"
    transformation_strength: str = "medium"
    scene_processing_mode: str = "all-elements"
    subtitle_enabled: bool = True
    lip_sync_enabled: bool = True
    visual_scope: str = "full-scene"
    character_appearance_policy: str = "follow-original-outfit"


class GeneratePayload(BaseModel):
    simulate_failure: bool = False


class EpisodeCreate(BaseModel):
    title: str | None = None
    episode_number: int | None = None


class EpisodeUpdate(BaseModel):
    title: str | None = None
    episode_number: int | None = None
    sort_order: int | None = None
    status: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    orientation: str | None = None
    file_size: int | None = None
    character_handling: str | None = None
    source_language: str | None = None
    settings_override: dict[str, Any] | None = None
    thumbnail_data: str | None = None


class EpisodeBatchCreate(BaseModel):
    filenames: list[str] = Field(default_factory=list)
    job_type: str = "upload"
    strategy: str = "selected"
    episode_ids: list[str] = Field(default_factory=list)


class EpisodeBatchItemUpdate(BaseModel):
    status: str
    progress: int = 0
    error_message: str | None = None


class CharacterActionPayload(BaseModel):
    priority_level: str | None = None
    confirmed: bool | None = None
    character_code: str | None = None


class StyledAssetPayload(BaseModel):
    scope: str = "current"


class LookPayload(BaseModel):
    name: str = "New Look"
    description: str = ""
    reference_image: str | None = None
    clothing_description: str = ""
    hair_override: str = ""
    headwear_description: str = ""
    source_episode_id: str | None = None
    is_default: bool = False


class VoiceProfilePayload(BaseModel):
    name: str = "Untitled Voice"
    source_type: str = "system-preset"
    provider_id: str | None = None
    model_id: str | None = None
    external_voice_id: str | None = None
    base_voice: str = "Neutral"
    voice_age: str = "Adult"
    voice_style: str = "Calm"
    speech_speed: float = 1
    pitch: float = 0
    emotion_strength: int = 60
    status: str = "Mock Only"


class MatchUpdatePayload(BaseModel):
    character_id: str | None = None
    match_status: str | None = None
    use_project_asset: bool | None = None


class CandidateUpdatePayload(BaseModel):
    status: str
    character_id: str | None = None
    entity_type: str | None = None


def project_or_404(project_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    project = row_to_dict(row)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.setdefault("video_width", None)
    project.setdefault("video_height", None)
    project.setdefault("aspect_ratio", None)
    project.setdefault("orientation", None)
    project["width"] = project["video_width"]
    project["height"] = project["video_height"]
    project["subtitle_enabled"] = bool(project["subtitle_enabled"])
    project["lip_sync_enabled"] = bool(project["lip_sync_enabled"])
    project["use_character_library"] = bool(project.get("use_character_library"))
    project["default_subtitle_enabled"] = bool(project.get("default_subtitle_enabled"))
    project["default_lip_sync_enabled"] = bool(project.get("default_lip_sync_enabled"))
    return project


def characters_for(project_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM characters WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
    result = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["confirmed"] = bool(item["confirmed"])
        item["voice_settings"] = json_load(item.get("voice_settings"), {})
        item["priority_level"] = item.get("priority_level") or ("Locked" if item["confirmed"] else "Temporary")
        item["character_type"] = item.get("character_type") if item.get("character_type") in CHARACTER_TYPES else "Other"
        item["identity_description"] = item.get("identity_description") or item.get("appearance_description") or ""
        item["entity_type"] = item.get("entity_type") or item.get("character_type") or "Other"
        item["optional_reference_images"] = json_load(item.get("optional_reference_images"), [])
        item["styled_reference_assets"] = [
            {"asset_type": asset_type, "image": item.get(field)}
            for asset_type, field in (("front-view", "styled_front_view_image"), ("three-quarter-view", "styled_angle_view_image"), ("side-view", "styled_side_view_image"), ("full-body", "styled_full_body_image"))
            if item.get(field)
        ]
        item["reference_assets"] = reference_assets_for(item["id"])
        result.append(item)
    return result


def upload_url_for_path(value: str | Path | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str) and value.startswith("/uploads/"):
        target = upload_path_for_url(value)
        return value if target.is_file() else None
    target = Path(value)
    try:
        resolved = target.resolve()
        scoped_url = storage_asset_url(resolved)
        if resolved.is_file() and scoped_url:
            return scoped_url
        if resolved.is_file() and resolved.is_relative_to(UPLOADS_DIR.resolve()):
            return f"/uploads/{resolved.relative_to(UPLOADS_DIR.resolve()).as_posix()}"
    except (OSError, ValueError):
        return None
    return None


def episode_with_video_url(episode: dict[str, Any]) -> dict[str, Any]:
    """Attach the protected, owner-scoped URL for the persisted Episode source."""
    episode["video_url"] = upload_url_for_path(episode.get("video_path"))
    return episode


def atomic_publish_episode_video(target: Path, content: bytes) -> dict[str, Any]:
    """Validate a sibling staging file before atomically publishing an Episode source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_path = target.with_name(f".{target.name}.upload-{uuid.uuid4().hex}.tmp")
    try:
        with staging_path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if not staging_path.is_file() or staging_path.stat().st_size <= 0:
            raise media_service.MediaToolError("VIDEO_FILE_EMPTY", "The uploaded video file is empty.")
        metadata = media_service.probe_video_metadata(staging_path)
        try:
            metadata_valid = (
                bool(metadata.get("video_codec"))
                and int(metadata.get("width") or 0) > 0
                and int(metadata.get("height") or 0) > 0
                and float(metadata.get("duration") or 0) > 0
            )
        except (TypeError, ValueError):
            metadata_valid = False
        if not metadata_valid:
            raise media_service.MediaToolError("VIDEO_METADATA_INVALID", "The uploaded video has no readable video stream.")
        if float(metadata["duration"]) > 180:
            raise media_service.MediaToolError("VIDEO_DURATION_UNSUPPORTED", "当前MVP单集支持3分钟以内，请分集上传。")
        file_size = staging_path.stat().st_size
        os.replace(staging_path, target)
        return {
            **metadata,
            "path": str(target.resolve()),
            "file_size": file_size,
        }
    finally:
        staging_path.unlink(missing_ok=True)


def delete_upload_file(value: str | Path | None) -> None:
    if not value:
        return
    target = upload_path_for_url(value) if isinstance(value, str) and value.startswith("/uploads/") else Path(value)
    try:
        resolved = target.resolve()
        if resolved.is_relative_to(UPLOADS_DIR.resolve()) or resolved.is_relative_to(STORAGE_ROOT.resolve()):
            resolved.unlink(missing_ok=True)
    except (OSError, ValueError):
        return


def project_upload_directory(project_id: str) -> Path:
    with get_connection() as db:
        row = db.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["owner_user_id"]:
        raise HTTPException(status_code=409, detail="Project ownership has not been initialized.")
    return project_uploads_dir(row["owner_user_id"], project_id)


def upload_url_for_target(target: Path) -> str:
    scoped_url = storage_asset_url(target)
    if scoped_url:
        return scoped_url
    return f"/uploads/{target.resolve().relative_to(UPLOADS_DIR.resolve()).as_posix()}"


def upload_path_for_url(value: str) -> Path:
    resolved = resolve_upload_asset_path(value)
    if resolved is None:
        raise ValueError("Upload URL cannot be resolved safely.")
    return resolved


def persist_reference_value(character_id: str, field_name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("data:image/"):
        try:
            header, encoded = value.split(",", 1)
            extension = "png" if "png" in header.lower() else "jpg"
            with get_connection() as db:
                row = db.execute("SELECT project_id FROM characters WHERE id=?", (character_id,)).fetchone()
            if not row:
                return None
            target_dir = project_upload_directory(row["project_id"]) / "character-references"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{character_id}-{field_name}-{uuid.uuid4().hex[:8]}.{extension}"
            content = base64.b64decode(encoded)
            if not content:
                return None
            target.write_bytes(content)
            return upload_url_for_target(target)
        except (ValueError, base64.binascii.Error):
            return None
    return upload_url_for_path(value)


def prepare_reference_values(character_id: str, values: dict[str, Any]) -> None:
    for field_name in REFERENCE_FIELDS:
        value = values.get(field_name)
        if isinstance(value, str) and value.startswith("data:image/"):
            values[field_name] = persist_reference_value(character_id, field_name, value)
        elif value:
            values[field_name] = upload_url_for_path(value) or value
    for field_name in ("source_crop_path", "target_reference_path"):
        value = values.get(field_name)
        if isinstance(value, str) and value.startswith("data:image/"):
            values[field_name] = persist_reference_value(character_id, field_name, value)
        elif value:
            values[field_name] = upload_url_for_path(value) or value


def sync_reference_records(
    db: sqlite3.Connection,
    character_id: str,
    values: dict[str, Any],
    *,
    source_type: str = "manual_upload",
    source_episode_id: str | None = None,
    source_timestamp: float | None = None,
) -> None:
    timestamp = now_iso()
    valid: list[tuple[str, str]] = []
    for field_name in REFERENCE_FIELDS:
        image_path = upload_url_for_path(values.get(field_name))
        if image_path:
            valid.append((field_name, image_path))
            db.execute(
                "INSERT INTO character_reference_images (id, character_id, field_name, image_path, source_type, source_episode_id, source_timestamp, is_primary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?) ON CONFLICT(character_id, field_name) DO UPDATE SET source_type=CASE WHEN character_reference_images.image_path=excluded.image_path THEN character_reference_images.source_type ELSE excluded.source_type END, source_episode_id=CASE WHEN character_reference_images.image_path=excluded.image_path THEN character_reference_images.source_episode_id ELSE excluded.source_episode_id END, source_timestamp=CASE WHEN character_reference_images.image_path=excluded.image_path THEN character_reference_images.source_timestamp ELSE excluded.source_timestamp END, image_path=excluded.image_path, updated_at=excluded.updated_at",
                (f"ref-{uuid.uuid4().hex[:8]}", character_id, field_name, image_path, source_type, source_episode_id, source_timestamp, timestamp, timestamp),
            )
        else:
            db.execute("DELETE FROM character_reference_images WHERE character_id=? AND field_name=?", (character_id, field_name))
    preferred = values.get("primary_reference_type")
    primary = next(((field_name, image_path) for field_name, image_path in valid if field_name == preferred), valid[0] if valid else None)
    db.execute("UPDATE character_reference_images SET is_primary=0 WHERE character_id=?", (character_id,))
    if primary:
        db.execute("UPDATE character_reference_images SET is_primary=1, updated_at=? WHERE character_id=? AND field_name=?", (timestamp, character_id, primary[0]))
        source = next((path for field_name, path in valid if field_name == "original_reference_image"), valid[0][1])
        db.execute("UPDATE characters SET primary_reference_type=?, source_reference_image=?, main_reference_image=? WHERE id=?", (primary[0], source, primary[1], character_id))
    else:
        db.execute("UPDATE characters SET primary_reference_type=NULL, source_reference_image=NULL, main_reference_image=NULL WHERE id=?", (character_id,))


def reference_assets_for(character_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM character_reference_images WHERE character_id=? ORDER BY rowid", (character_id,)).fetchall()
    return [
        {
            "id": row["id"], "asset_type": REFERENCE_ASSET_TYPES.get(row["field_name"], row["field_name"]),
            "field": row["field_name"], "image": row["image_path"], "is_primary": bool(row["is_primary"]),
            "source_type": row["source_type"], "source_episode_id": row["source_episode_id"], "source_timestamp": row["source_timestamp"],
        }
        for row in rows
    ]


def sync_target_reference_record(db: sqlite3.Connection, character_id: str, values: dict[str, Any], source_episode_id: str | None = None) -> None:
    target = upload_url_for_path(values.get("target_reference_path"))
    if not target:
        db.execute("DELETE FROM character_reference_images WHERE character_id=? AND field_name='target_reference_path'", (character_id,))
        return
    timestamp = now_iso()
    db.execute("UPDATE character_reference_images SET is_primary=0 WHERE character_id=?", (character_id,))
    db.execute(
        "INSERT INTO character_reference_images (id, character_id, field_name, image_path, source_type, source_episode_id, is_primary, created_at, updated_at) VALUES (?, ?, ?, ?, 'user_target_upload', ?, 1, ?, ?) ON CONFLICT(character_id, field_name) DO UPDATE SET image_path=excluded.image_path, source_episode_id=excluded.source_episode_id, is_primary=1, updated_at=excluded.updated_at",
        (f"ref-{uuid.uuid4().hex[:8]}", character_id, "target_reference_path", target, source_episode_id, timestamp, timestamp),
    )


def confirmation_problems(character: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    name = str(character.get("name") or "").strip()
    if not name or name.lower() in PLACEHOLDER_NAMES or len(name) < 2:
        problems.append("Please enter a real character name")
    if (character.get("entity_type") or character.get("character_type")) not in ENTITY_TYPES:
        problems.append("Entity type must be Human, Pet, Animal, or Other")
    if not upload_url_for_path(character.get("target_reference_path") or character.get("main_reference_image")):
        problems.append("Please upload a target reference image before confirming this character")
    return problems


def batch_job_detail(job_id: str) -> dict[str, Any]:
    with get_connection() as db:
        job = db.execute("SELECT * FROM episode_batch_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Batch job not found")
        items = db.execute("SELECT * FROM episode_batch_items WHERE job_id=? ORDER BY item_order", (job_id,)).fetchall()
    return {**dict(job), "items": [dict(item) for item in items]}


def recalculate_batch_job(db: sqlite3.Connection, job_id: str) -> None:
    counts = db.execute("SELECT COUNT(*) total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN status IN ('queued','processing','uploading') THEN 1 ELSE 0 END) pending FROM episode_batch_items WHERE job_id=?", (job_id,)).fetchone()
    total = counts["total"] or 0
    completed = counts["completed"] or 0
    failed = counts["failed"] or 0
    pending = counts["pending"] or 0
    status = "processing" if pending else ("completed_with_errors" if failed else "completed")
    db.execute("UPDATE episode_batch_jobs SET status=?, total_count=?, completed_count=?, failed_count=?, updated_at=? WHERE id=?", (status, total, completed, failed, now_iso(), job_id))


def candidate_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["occurrence_frames"] = json_load(item.get("occurrence_frames"), [])
    item["occurrence_timestamps"] = json_load(item.get("occurrence_timestamps"), [])
    item["bounding_box"] = json_load(item.get("bounding_box"), {})
    item["candidate_id"] = item["id"]
    item["mock_confidence"] = item["confidence"]
    item["candidate_status"] = item["status"]
    item["matched_character_id"] = item.get("character_id")
    return item


def candidate_counts_for(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: sum(1 for item in candidates if item["status"] == status) for status in ("candidate", "retained", "matched", "temporary", "background", "ignored", "locked")}
    counts["retained_total"] = counts["retained"] + counts["temporary"]
    counts["matched_total"] = counts["matched"] + counts["locked"]
    return counts


def episodes_for(project_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY COALESCE(sort_order, episode_number), episode_number", (project_id,)).fetchall()
        result = []
        for row in rows:
            item = row_to_dict(row) or {}
            item["settings_override"] = json_load(item.get("settings_override"), {})
            item["has_video"] = bool(item.get("video_filename"))
            matches = db.execute("SELECT * FROM episode_character_matches WHERE episode_id=? ORDER BY created_at", (item["id"],)).fetchall()
            item["matches"] = [dict(match) | {"use_project_asset": bool(match["use_project_asset"])} for match in matches]
            item["matched_count"] = len(matches)
            item["new_character_count"] = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level IN ('Temporary', 'Background')", (item["id"],)).fetchone()["count"]
            item["pending_character_count"] = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level='Temporary'", (item["id"],)).fetchone()["count"]
            item["candidates"] = [candidate_to_dict(candidate) for candidate in db.execute("SELECT * FROM episode_character_candidates WHERE episode_id=? ORDER BY source_index", (item["id"],)).fetchall()]
            item["candidate_counts"] = candidate_counts_for(item["candidates"])
            item["candidate_count"] = len(item["candidates"])
            item["conflict_count"] = item.get("conflict_count") or 0
            item["analysis_status"] = item.get("analysis_status") or ("completed" if item["status"] in {"Character Review", "Ready", "Completed"} else "not_started")
            item["upload_status"] = item.get("upload_status") or ("completed" if item.get("video_path") else "not_uploaded")
            result.append(episode_with_video_url(item))
    return result


def voice_profiles_for(project_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM voice_profiles WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        bindings = db.execute("SELECT id, name, voice_profile_id FROM characters WHERE project_id=? AND voice_profile_id IS NOT NULL", (project_id,)).fetchall()
    bound = {row["voice_profile_id"]: {"id": row["id"], "name": row["name"]} for row in bindings}
    result = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["bound_character"] = bound.get(item["id"])
        if item.get("uploaded_audio_path"):
            item["sample_url"] = upload_url_for_path(item["uploaded_audio_path"])
        result.append(item)
    return result


def looks_for(character_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM character_looks WHERE character_id=? ORDER BY is_default DESC, created_at", (character_id,)).fetchall()
    return [{**(row_to_dict(row) or {}), "is_default": bool(row["is_default"])} for row in rows]


def project_defaults_for(project_id: str) -> dict[str, Any]:
    project = project_or_404(project_id)
    return {
        "project_id": project_id,
        "processing_mode": project.get("processing_mode") or "quick-transformation",
        "style_id": project.get("default_style_id") or project.get("selected_style") or "cinematic-realism",
        "source_language": project.get("default_source_language") or project.get("source_language") or "auto",
        "target_language": project.get("default_target_language") or project.get("target_language") or "english",
        "language_mode": project.get("default_language_mode") or "translate-dub",
        "subtitle_enabled": bool(project.get("default_subtitle_enabled", True)),
        "lip_sync_enabled": bool(project.get("default_lip_sync_enabled", True)),
        "character_consistency": project.get("default_character_consistency") or "strong",
        "transformation_strength": project.get("default_transformation_strength") or "medium",
        "scene_processing_mode": project.get("default_scene_processing_mode") or "all-elements",
        "visual_scope": "full-scene" if (project.get("default_visual_scope") or "all-elements") == "all-elements" else project.get("default_visual_scope"),
        "character_appearance_policy": project.get("character_appearance_policy") or "follow-original-outfit",
        "use_character_library": bool(project.get("use_character_library")),
    }


def scenes_for(project_id: str) -> list[dict[str, Any]]:
    with get_connection() as db:
        rows = db.execute("SELECT * FROM scenes WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
    result = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["main_objects"] = json_load(item.get("main_objects"), [])
        result.append(item)
    return result


def settings_for(project_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM settings WHERE project_id = ?", (project_id,)).fetchone()
    item = row_to_dict(row)
    if item:
        item["subtitle_enabled"] = bool(item["subtitle_enabled"])
        item["lip_sync_enabled"] = bool(item["lip_sync_enabled"])
        item["visual_scope"] = item.get("visual_scope") or "full-scene"
        if item["visual_scope"] == "all-elements":
            item["visual_scope"] = "full-scene"
        item["character_appearance_policy"] = item.get("character_appearance_policy") or "follow-original-outfit"
    return item or SettingsPayload().model_dump()


def project_detail(project_id: str) -> dict[str, Any]:
    project = project_or_404(project_id)
    project["characters"] = characters_for(project_id)
    project["scenes"] = scenes_for(project_id)
    project["settings"] = settings_for(project_id)
    project["episodes"] = episodes_for(project_id)
    project["voice_profiles"] = voice_profiles_for(project_id)
    project["character_library"] = {"locked": [item for item in project["characters"] if item.get("priority_level") == "Locked"], "temporary": [item for item in project["characters"] if item.get("priority_level") == "Temporary"], "background": [item for item in project["characters"] if item.get("priority_level") == "Background"]}
    project["defaults"] = project_defaults_for(project_id)
    return project


@app.on_event("startup")
def startup() -> None:
    init_db()
    config = provider_service.transcription_configuration()
    print(f"STT provider: {config.provider or 'not configured'}")
    print(f"STT configured: {config.configured}")
    print(f"STT model: {config.model or 'not configured'}")
    tts = provider_service.tts_configuration()
    print(f"TTS provider: {tts.provider or 'not configured'}")
    print(f"TTS configured: {tts.configured}")
    print(f"TTS model: {tts.model or 'not configured'}")
    print(f"TTS output format: {tts.output_format or 'not configured'}")
    from .services.visual_provider_service import runway_configuration
    runway = runway_configuration()
    print(f"Runway configured: {runway.configured}")
    print(f"Runway model: {runway.model or 'not configured'}")
    print(f"Runway max test seconds: {runway.max_test_seconds:g}")
    tools = media_service.media_tool_status()
    print(f"FFmpeg available: {tools['ffmpeg']['available']} ({tools['ffmpeg'].get('source') or 'unavailable'})")
    print(f"FFprobe available: {tools['ffprobe']['available']} ({tools['ffprobe'].get('source') or 'unavailable'})")
    print(f"Resolved FFmpeg: {tools.get('resolved_ffmpeg_path') or 'unavailable'}")
    print(f"Resolved FFprobe: {tools.get('resolved_ffprobe_path') or 'unavailable'}")
    print(f"Backend working directory: {tools.get('backend_working_directory')}")
    recovered = task_state.mark_interrupted_on_startup()
    print(f"Interrupted stale tasks: {sum(recovered.values())}")


@app.get("/api/health")
def health() -> dict[str, Any]:
    from .services.visual_provider_service import runway_configuration
    tools = media_service.media_tool_status()
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "media_tools": tools,
        "ffmpeg_available": tools["ffmpeg"]["available"],
        "ffprobe_available": tools["ffprobe"]["available"],
        "ffmpeg_source": tools["ffmpeg"].get("source"),
        "ffprobe_source": tools["ffprobe"].get("source"),
        "resolved_ffmpeg_path": tools.get("resolved_ffmpeg_path"),
        "resolved_ffprobe_path": tools.get("resolved_ffprobe_path"),
        "ffmpeg_version": tools["ffmpeg"].get("version"),
        "ffprobe_version": tools["ffprobe"].get("version"),
        "backend_working_directory": tools.get("backend_working_directory"),
        "process_path_contains_tool_dir": tools.get("process_path_contains_tool_dir"),
        "transcription": provider_service.transcription_configuration().public_status(),
        "translation": provider_service.translation_configuration().public_status(),
        "visual_transformation": runway_configuration().public_status(),
    }


@app.post("/api/projects")
def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
    user = require_request_user(request)
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    timestamp = now_iso()
    mode = payload.processing_mode if payload.processing_mode in {"quick-transformation", "character-consistency", "batch-long-episode"} else "quick-transformation"
    use_library = 1 if mode == "character-consistency" else 0
    episode_id = f"ep-{project_id}-01"
    with get_connection() as db:
        db.execute("INSERT INTO projects (id, name, status, source_language, target_language, selected_style, processing_mode, active_episode_id, default_style_id, default_source_language, default_target_language, default_language_mode, default_subtitle_enabled, default_lip_sync_enabled, use_character_library, owner_user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (project_id, payload.name, "Draft", "auto", "english", "cinematic-realism", mode, episode_id, "cinematic-realism", "auto", "english", "translate-dub", 1, 1, use_library, user["id"], timestamp, timestamp))
        db.execute("INSERT INTO settings (project_id, style_id, source_language, target_language, language_mode, character_consistency, transformation_strength, scene_processing_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (project_id, "cinematic-realism", "auto", "english", "translate-dub", "strong", "medium", "all-elements"))
        db.execute("INSERT INTO episodes (id, project_id, episode_number, sort_order, title, source_language, status, upload_status, created_at, updated_at) VALUES (?, ?, 1, 1, ?, ?, ?, 'not_uploaded', ?, ?)", (episode_id, project_id, "Episode 01", "auto", "Draft", timestamp, timestamp))
    return project_detail(project_id)


@app.get("/api/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    user = require_request_user(request)
    with get_connection() as db:
        if user["role"] == "admin":
            rows = db.execute("SELECT id FROM projects ORDER BY updated_at DESC").fetchall()
        else:
            rows = db.execute(
                "SELECT id FROM projects WHERE owner_user_id=? ORDER BY updated_at DESC",
                (user["id"],),
            ).fetchall()
    return [project_or_404(row["id"]) for row in rows]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return project_detail(project_id)


def _delete_restricted_xmax_records(db: sqlite3.Connection, scope_column: str, scope_id: str) -> None:
    if scope_column not in {"episode_id", "project_id"}:
        raise ValueError("Unsupported cleanup scope.")
    # Selection and final-output rows reference attempts/tasks, while attempts
    # reference temporary keys. Delete strictly from leaves to roots.
    for table in (
        "episode_unit_selections",
        "episode_final_outputs",
        "xmax_browser_attempts",
        "xmax_temporary_keys",
    ):
        db.execute(f"DELETE FROM {table} WHERE {scope_column}=?", (scope_id,))


def _delete_episode_database_records(db: sqlite3.Connection, episode_id: str) -> None:
    """Delete one Episode graph inside the caller's existing transaction."""
    _delete_restricted_xmax_records(db, "episode_id", episode_id)
    task_ids = [row["id"] for row in db.execute("SELECT id FROM tasks WHERE episode_id=?", (episode_id,)).fetchall()]
    for task_id in task_ids:
        db.execute("DELETE FROM task_steps WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    db.execute("DELETE FROM episode_character_matches WHERE episode_id=?", (episode_id,))
    db.execute(
        "DELETE FROM characters WHERE created_from_episode_id=? AND priority_level IN ('Temporary', 'Background')",
        (episode_id,),
    )
    db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    # Uploaded videos are named with the project id. Restrict cleanup to this
    # workspace directory so deleting a project cannot remove another file.
    project_files = [path for path in UPLOADS_DIR.glob(f"{project_id}.*") if path.is_file()]
    with get_connection() as db:
        episodes = db.execute(
            "SELECT id, video_path, thumbnail_path FROM episodes WHERE project_id=? ORDER BY episode_number",
            (project_id,),
        ).fetchall()
        episode_files = [value for row in episodes for value in (row["video_path"], row["thumbnail_path"])]
        episode_files.extend(
            str(path)
            for row in episodes
            for path in UPLOADS_DIR.glob(f"{row['id']}*")
        )
        candidate_files = [row["representative_frame"] for row in db.execute("SELECT representative_frame FROM episode_character_candidates WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?)", (project_id,)).fetchall()]
        reference_files = [row["image_path"] for row in db.execute("SELECT image_path FROM character_reference_images WHERE character_id IN (SELECT id FROM characters WHERE project_id=?)", (project_id,)).fetchall()]
        _delete_restricted_xmax_records(db, "project_id", project_id)
        for episode in episodes:
            _delete_episode_database_records(db, episode["id"])
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    for path in project_files:
        path.unlink(missing_ok=True)
    for value in episode_files + candidate_files + reference_files:
        delete_upload_file(value)
    return {"deleted": True, "project_id": project_id}


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate) -> dict[str, Any]:
    project_or_404(project_id)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return project_detail(project_id)
    if "width" in updates:
        updates["video_width"] = updates.pop("width")
    if "height" in updates:
        updates["video_height"] = updates.pop("height")
    if "active_episode_id" in updates:
        with get_connection() as db:
            active = db.execute("SELECT id FROM episodes WHERE id=? AND project_id=?", (updates["active_episode_id"], project_id)).fetchone()
        if not active:
            raise HTTPException(status_code=400, detail="Active episode does not belong to this project")
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    with get_connection() as db:
        db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", (*updates.values(), project_id))
        if "active_episode_id" in updates:
            selected = db.execute("SELECT video_filename, video_path, duration, width, height, aspect_ratio, orientation, status FROM episodes WHERE id=?", (updates["active_episode_id"],)).fetchone()
            if selected:
                db.execute("UPDATE projects SET video_filename=?, video_path=?, video_duration=?, video_width=?, video_height=?, aspect_ratio=?, orientation=?, status=?, updated_at=? WHERE id=?", (selected["video_filename"], selected["video_path"], selected["duration"], selected["width"], selected["height"], selected["aspect_ratio"], selected["orientation"], selected["status"], now_iso(), project_id))
        metadata_columns = {"video_duration": "duration", "video_width": "width", "video_height": "height", "aspect_ratio": "aspect_ratio", "orientation": "orientation"}
        metadata_updates = [(column, updates[project_column]) for project_column, column in metadata_columns.items() if project_column in updates]
        if metadata_updates:
            db.execute(f"UPDATE episodes SET {', '.join(f'{column}=?' for column, _ in metadata_updates)}, updated_at=? WHERE id=(SELECT active_episode_id FROM projects WHERE id=?)", tuple(value for _, value in metadata_updates) + (now_iso(), project_id))
    return project_detail(project_id)


@app.post("/api/projects/{project_id}/upload")
async def upload_video(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    project_or_404(project_id)
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov"}:
        raise HTTPException(status_code=400, detail="Only MP4 and MOV files are supported")
    target = project_upload_directory(project_id) / f"{project_id}{suffix}"
    content = await file.read()
    if len(content) > 500 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Video must be smaller than 500 MB")
    target.write_bytes(content)
    with get_connection() as db:
        db.execute("UPDATE projects SET video_filename=?, video_path=?, video_duration=NULL, video_width=NULL, video_height=NULL, aspect_ratio=NULL, orientation=NULL, status=?, updated_at=? WHERE id=?", (file.filename, str(target), "Uploaded", now_iso(), project_id))
        episode = db.execute("SELECT id FROM episodes WHERE project_id=? ORDER BY episode_number LIMIT 1", (project_id,)).fetchone()
        if episode:
            db.execute("UPDATE episodes SET video_filename=?, video_path=?, duration=NULL, width=NULL, height=NULL, aspect_ratio=NULL, orientation=NULL, status='Uploaded', updated_at=? WHERE id=?", (file.filename, str(target), now_iso(), episode["id"]))
    return project_detail(project_id)


@app.post("/api/projects/{project_id}/analyze")
def analyze_project(project_id: str) -> dict[str, Any]:
    project = project_or_404(project_id)
    if not project.get("video_path"):
        raise HTTPException(status_code=400, detail="Upload a video before analysis")
    try:
        analysis = ai_service.analyze_video(project["video_path"])
        source_characters = ai_service.extract_characters(project["video_path"])
        source_scenes = ai_service.extract_scenes(project["video_path"])
        characters = [{**character, "id": f"{project_id}-{character['id']}"} for character in source_characters]
        scenes = [{**scene, "id": f"{project_id}-{scene['id']}"} for scene in source_scenes]
    except ai_service.MockAIError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        with get_connection() as db:
            db.execute("DELETE FROM characters WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM scenes WHERE project_id=?", (project_id,))
            for character in characters:
                db.execute("INSERT INTO characters (id, project_id, name, gender, estimated_age, role, appearance_description, hair_description, headwear_description, clothing_description, original_reference_image, front_view_image, angle_view_image, side_view_image, full_body_image, voice_id, voice_settings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (character["id"], project_id, character["name"], character["gender"], character["estimated_age"], character["role"], character["appearance_description"], character["hair_description"], character["headwear_description"], character["clothing_description"], character["original_reference_image"], f"{character['id']}-front", f"{character['id']}-angle", f"{character['id']}-side", f"{character['id']}-full-body", character["voice_id"], json_dump(character["voice_settings"])))
                db.execute("UPDATE characters SET character_code=?, character_type='Human', priority_level=?, identity_description=?, face_description=?, body_description='', default_clothing_description=?, source='mock-analysis', created_from_episode_id=?, created_at=?, updated_at=? WHERE id=?", (f"C{characters.index(character) + 1:02d}", "Temporary", character["appearance_description"], character["appearance_description"], character["clothing_description"], project.get("active_episode_id"), now_iso(), now_iso(), character["id"]))
            for scene in scenes:
                db.execute("INSERT INTO scenes (id, project_id, name, location, description, main_objects, reference_image) VALUES (?, ?, ?, ?, ?, ?, ?)", (scene["id"], project_id, scene["name"], scene["location"], scene["description"], json_dump(scene["main_objects"]), scene["reference_image"]))
            db.execute("UPDATE projects SET source_language=?, status=?, updated_at=? WHERE id=?", ("mandarin", "Character Review", now_iso(), project_id))
            if project.get("active_episode_id"):
                db.execute("UPDATE episodes SET source_language=?, status='Character Review', updated_at=? WHERE id=?", ("mandarin", now_iso(), project["active_episode_id"]))
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail=f"Analysis results could not be saved: {error}") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Analysis failed while saving results: {error}") from error
    return {"analysis": analysis, "characters": characters_for(project_id), "scenes": scenes_for(project_id), "project": project_detail(project_id)}


@app.get("/api/projects/{project_id}/characters")
def get_characters(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    return characters_for(project_id)


def insert_character(project_id: str, payload: CharacterPayload, character_id: str | None = None) -> dict[str, Any]:
    character_id = character_id or f"char-{uuid.uuid4().hex[:8]}"
    values = payload.model_dump()
    values["character_type"] = values["character_type"] if values.get("character_type") in CHARACTER_TYPES else "Other"
    prepare_reference_values(character_id, values)
    if values.get("primary_reference_type") not in REFERENCE_FIELDS:
        values["primary_reference_type"] = next((field for field in REFERENCE_FIELDS if values.get(field)), None)
    timestamp = now_iso()
    if not values.get("character_code"):
        with get_connection() as db:
            count = db.execute("SELECT COUNT(*) AS count FROM characters WHERE project_id=?", (project_id,)).fetchone()["count"]
        values["character_code"] = f"C{count + 1:02d}"
    with get_connection() as db:
        db.execute("INSERT INTO characters (id, project_id, name, gender, estimated_age, role, appearance_description, hair_description, headwear_description, clothing_description, original_reference_image, source_crop_path, target_reference_path, front_view_image, angle_view_image, side_view_image, full_body_image, voice_id, voice_settings, confirmed, character_code, character_type, primary_reference_type, priority_level, identity_description, face_description, body_description, default_clothing_description, voice_profile_id, source, created_from_episode_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (character_id, project_id, values["name"], values["gender"], values["estimated_age"], values["role"], values["appearance_description"], values["hair_description"], values["headwear_description"], values["clothing_description"], values["original_reference_image"], values["source_crop_path"], values["target_reference_path"], values["front_view_image"], values["angle_view_image"], values["side_view_image"], values["full_body_image"], values["voice_id"], json_dump(values["voice_settings"]), bool_int(values["confirmed"]), values["character_code"], values["character_type"], values["primary_reference_type"], values["priority_level"], values["identity_description"] or values["appearance_description"], values["face_description"], values["body_description"], values["default_clothing_description"] or values["clothing_description"], values["voice_profile_id"], values["source"], values["created_from_episode_id"], timestamp, timestamp))
        db.execute("UPDATE characters SET entity_type=?, confirmation_status=? WHERE id=?", (values["character_type"] if values["character_type"] in {"Human", "Pet", "Animal", "Other"} else "Other", "confirmed" if values["confirmed"] else "temporary", character_id))
        sync_reference_records(db, character_id, values, source_type=values["source"] or "manual_upload", source_episode_id=values["created_from_episode_id"])
        sync_target_reference_record(db, character_id, values, values.get("created_from_episode_id"))
        db.execute("UPDATE characters SET source_reference_image=COALESCE(?, source_reference_image), main_reference_image=COALESCE(?, main_reference_image) WHERE id=?", (values.get("source_crop_path") or values.get("original_reference_image"), values.get("target_reference_path"), character_id))
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id))
    return next(item for item in characters_for(project_id) if item["id"] == character_id)


@app.post("/api/projects/{project_id}/characters")
def add_character(project_id: str, payload: CharacterPayload) -> dict[str, Any]:
    project_or_404(project_id)
    if payload.created_from_episode_id:
        episode = episode_or_404(payload.created_from_episode_id)
        if episode["project_id"] != project_id:
            raise HTTPException(status_code=422, detail="created_from_episode_id must belong to the current project")
    return insert_character(project_id, payload)


@app.put("/api/projects/{project_id}/characters/{character_id}")
def update_character(project_id: str, character_id: str, payload: CharacterPayload) -> dict[str, Any]:
    project_or_404(project_id)
    values = payload.model_dump()
    values["character_type"] = values["character_type"] if values.get("character_type") in CHARACTER_TYPES else "Other"
    prepare_reference_values(character_id, values)
    if values.get("primary_reference_type") not in REFERENCE_FIELDS:
        values["primary_reference_type"] = next((field for field in REFERENCE_FIELDS if values.get(field)), None)
    with get_connection() as db:
        exists = db.execute("SELECT * FROM characters WHERE id=? AND project_id=?", (character_id, project_id)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Character not found")
        requested_confirmation = bool(values.get("confirmed")) and not bool(exists["confirmed"])
        if requested_confirmation:
            values["confirmed"] = False
            values["priority_level"] = "Temporary"
        elif not values.get("confirmed") and bool(exists["confirmed"]):
            values["priority_level"] = "Temporary"
        db.execute("UPDATE characters SET name=?, gender=?, estimated_age=?, role=?, appearance_description=?, hair_description=?, headwear_description=?, clothing_description=?, original_reference_image=?, source_crop_path=?, target_reference_path=?, front_view_image=?, angle_view_image=?, side_view_image=?, full_body_image=?, voice_id=?, voice_settings=?, confirmed=?, character_code=?, character_type=?, primary_reference_type=?, priority_level=?, identity_description=?, face_description=?, body_description=?, default_clothing_description=?, voice_profile_id=?, updated_at=? WHERE id=? AND project_id=?", (values["name"], values["gender"], values["estimated_age"], values["role"], values["appearance_description"], values["hair_description"], values["headwear_description"], values["clothing_description"], values["original_reference_image"], values["source_crop_path"], values["target_reference_path"], values["front_view_image"], values["angle_view_image"], values["side_view_image"], values["full_body_image"], values["voice_id"], json_dump(values["voice_settings"]), bool_int(values["confirmed"]), values["character_code"], values["character_type"], values["primary_reference_type"], values["priority_level"], values["identity_description"] or values["appearance_description"], values["face_description"], values["body_description"], values["default_clothing_description"] or values["clothing_description"], values["voice_profile_id"], now_iso(), character_id, project_id))
        db.execute("UPDATE characters SET entity_type=?, confirmation_status=? WHERE id=?", (values["character_type"] if values["character_type"] in {"Human", "Pet", "Animal", "Other"} else "Other", "confirmed" if values["confirmed"] else "temporary", character_id))
        sync_reference_records(db, character_id, values, source_type="manual_upload", source_episode_id=exists["created_from_episode_id"])
        sync_target_reference_record(db, character_id, values, exists["created_from_episode_id"])
        db.execute("UPDATE characters SET source_reference_image=COALESCE(?, source_reference_image), main_reference_image=COALESCE(?, main_reference_image) WHERE id=?", (values.get("source_crop_path") or values.get("original_reference_image"), values.get("target_reference_path"), character_id))
        if values["confirmed"]:
            db.execute("UPDATE episode_character_candidates SET status='locked', updated_at=? WHERE character_id=?", (now_iso(), character_id))
        source_episode_id = exists["created_from_episode_id"]
        if values["confirmed"] and source_episode_id:
            pending = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level='Temporary'", (source_episode_id,)).fetchone()["count"]
            if pending == 0:
                db.execute("UPDATE episodes SET status='Ready', updated_at=? WHERE id=? AND analysis_status='completed'", (now_iso(), source_episode_id))
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id))
    if requested_confirmation:
        return confirm_character(character_id)
    return next(item for item in characters_for(project_id) if item["id"] == character_id)


@app.delete("/api/projects/{project_id}/characters/{character_id}")
def delete_character(project_id: str, character_id: str) -> dict[str, bool]:
    project_or_404(project_id)
    with get_connection() as db:
        reference_files = [row["image_path"] for row in db.execute("SELECT image_path FROM character_reference_images WHERE character_id=?", (character_id,)).fetchall()]
        cursor = db.execute("DELETE FROM characters WHERE id=? AND project_id=?", (character_id, project_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Character not found")
        db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id))
    for value in reference_files:
        delete_upload_file(value)
    return {"deleted": True}


def episode_or_404(episode_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    episode = row_to_dict(row)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    episode["settings_override"] = json_load(episode.get("settings_override"), {})
    episode["has_video"] = bool(episode.get("video_filename"))
    return episode_with_video_url(episode)


def character_or_404(character_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    character = row_to_dict(row)
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")
    character["confirmed"] = bool(character["confirmed"])
    character["voice_settings"] = json_load(character.get("voice_settings"), {})
    character["reference_assets"] = reference_assets_for(character_id)
    character["looks"] = looks_for(character_id)
    return character


def episode_detail(episode_id: str) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    project = project_or_404(episode["project_id"])
    with get_connection() as db:
        matches = db.execute("SELECT * FROM episode_character_matches WHERE episode_id=? ORDER BY created_at", (episode_id,)).fetchall()
    episode["matches"] = [dict(row) | {"use_project_asset": bool(row["use_project_asset"])} for row in matches]
    episode["matched_count"] = len(matches)
    with get_connection() as db:
        episode["new_character_count"] = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level IN ('Temporary', 'Background')", (episode_id,)).fetchone()["count"]
        episode["pending_character_count"] = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level='Temporary'", (episode_id,)).fetchone()["count"]
        candidate_rows = db.execute("SELECT * FROM episode_character_candidates WHERE episode_id=? ORDER BY source_index", (episode_id,)).fetchall()
    episode["candidates"] = [candidate_to_dict(row) for row in candidate_rows]
    episode["candidate_counts"] = candidate_counts_for(episode["candidates"])
    episode["candidate_count"] = len(episode["candidates"])
    episode["analysis_status"] = episode.get("analysis_status") or ("completed" if episode.get("status") in {"Character Review", "Ready", "Completed"} else "not_started")
    episode["upload_status"] = episode.get("upload_status") or ("completed" if episode.get("video_path") else "not_uploaded")
    episode["conflict_count"] = episode.get("conflict_count") or 0
    episode["project_mode"] = project.get("processing_mode")
    return episode


@app.post("/api/projects/{project_id}/episodes")
def create_episode(project_id: str, payload: EpisodeCreate) -> dict[str, Any]:
    project = project_or_404(project_id)
    timestamp = now_iso()
    with get_connection() as db:
        next_number = payload.episode_number or ((db.execute("SELECT COALESCE(MAX(episode_number), 0) AS number FROM episodes WHERE project_id=?", (project_id,)).fetchone()["number"]) + 1)
        if db.execute("SELECT id FROM episodes WHERE project_id=? AND episode_number=?", (project_id, next_number)).fetchone():
            raise HTTPException(status_code=409, detail="Episode number already exists")
        episode_id = f"ep-{uuid.uuid4().hex[:8]}"
        title = payload.title or f"Episode {next_number:02d}"
        db.execute("INSERT INTO episodes (id, project_id, episode_number, sort_order, title, source_language, status, upload_status, character_handling, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'Draft', 'not_uploaded', ?, ?, ?)", (episode_id, project_id, next_number, next_number, title, project.get("default_source_language") or project.get("source_language") or "auto", "use-project-library" if project.get("use_character_library") else "reanalyze-all", timestamp, timestamp))
        db.execute("UPDATE projects SET active_episode_id=?, updated_at=? WHERE id=?", (episode_id, timestamp, project_id))
    return episode_detail(episode_id)


@app.post("/api/projects/{project_id}/episode-batches")
def create_episode_batch(project_id: str, payload: EpisodeBatchCreate) -> dict[str, Any]:
    project = project_or_404(project_id)
    if payload.job_type not in {"upload", "analysis"}:
        raise HTTPException(status_code=400, detail="Unsupported batch job type")
    job_id = f"batch-{uuid.uuid4().hex[:8]}"
    timestamp = now_iso()
    items: list[tuple[str, str | None, str | None]] = []
    with get_connection() as db:
        if payload.job_type == "upload":
            filenames = sorted(payload.filenames, key=lambda name: [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)])
            current = db.execute("SELECT id, episode_number, video_path FROM episodes WHERE project_id=? ORDER BY COALESCE(sort_order, episode_number)", (project_id,)).fetchall()
            used = {row["episode_number"] for row in current}
            empty_by_number = {row["episode_number"]: row["id"] for row in current if not row["video_path"]}
            empty_episode_ids = [row["id"] for row in current if not row["video_path"]]
            next_number = max(used, default=0) + 1
            for filename in filenames:
                extracted = re.search(r"(?:episode|ep)?\s*0*(\d+)", Path(filename).stem, re.IGNORECASE)
                requested_number = int(extracted.group(1)) if extracted and int(extracted.group(1)) > 0 else None
                if requested_number in empty_by_number:
                    episode_id = empty_by_number.pop(requested_number)
                    empty_episode_ids.remove(episode_id)
                    db.execute("UPDATE episodes SET upload_status='queued', upload_error=NULL, updated_at=? WHERE id=?", (timestamp, episode_id))
                    items.append((episode_id, filename, None))
                    continue
                if requested_number is None and empty_episode_ids:
                    episode_id = empty_episode_ids.pop(0)
                    db.execute("UPDATE episodes SET upload_status='queued', upload_error=NULL, updated_at=? WHERE id=?", (timestamp, episode_id))
                    items.append((episode_id, filename, None))
                    continue
                number = requested_number if requested_number and requested_number not in used else next_number
                while number in used:
                    number += 1
                used.add(number)
                next_number = max(next_number, number + 1)
                episode_id = f"ep-{uuid.uuid4().hex[:8]}"
                db.execute("INSERT INTO episodes (id, project_id, episode_number, sort_order, title, source_language, status, upload_status, character_handling, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'Draft', 'queued', ?, ?, ?)", (episode_id, project_id, number, number, f"Episode {number:02d}", project.get("default_source_language") or "auto", "use-project-library" if project.get("use_character_library") else "reanalyze-all", timestamp, timestamp))
                items.append((episode_id, filename, None))
        else:
            requested = payload.episode_ids
            if not requested:
                requested = [row["id"] for row in db.execute("SELECT id FROM episodes WHERE project_id=? AND video_path IS NOT NULL AND analysis_status != 'completed' ORDER BY COALESCE(sort_order, episode_number)", (project_id,)).fetchall()]
            for episode_id in requested:
                row = db.execute("SELECT id, video_filename FROM episodes WHERE id=? AND project_id=?", (episode_id, project_id)).fetchone()
                if row:
                    items.append((row["id"], row["video_filename"], None))
        db.execute("INSERT INTO episode_batch_jobs (id, project_id, job_type, strategy, status, total_count, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)", (job_id, project_id, payload.job_type, payload.strategy, len(items), timestamp, timestamp))
        for index, (episode_id, filename, _) in enumerate(items):
            db.execute("INSERT INTO episode_batch_items (id, job_id, episode_id, filename, item_order, status, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)", (f"batch-item-{uuid.uuid4().hex[:8]}", job_id, episode_id, filename, index, timestamp, timestamp))
        if items and not project.get("active_episode_id"):
            db.execute("UPDATE projects SET active_episode_id=?, updated_at=? WHERE id=?", (items[0][0], timestamp, project_id))
    return batch_job_detail(job_id)


@app.get("/api/projects/{project_id}/episode-batches")
def list_episode_batches(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    with get_connection() as db:
        rows = db.execute("SELECT id FROM episode_batch_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 20", (project_id,)).fetchall()
    return [batch_job_detail(row["id"]) for row in rows]


@app.get("/api/episode-batches/{job_id}")
def get_episode_batch(job_id: str) -> dict[str, Any]:
    return batch_job_detail(job_id)


@app.put("/api/episode-batches/{job_id}/items/{item_id}")
def update_episode_batch_item(job_id: str, item_id: str, payload: EpisodeBatchItemUpdate) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT episode_id FROM episode_batch_items WHERE id=? AND job_id=?", (item_id, job_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Batch item not found")
        progress = max(0, min(100, payload.progress))
        db.execute("UPDATE episode_batch_items SET status=?, progress=?, error_message=?, updated_at=? WHERE id=?", (payload.status, progress, payload.error_message, now_iso(), item_id))
        if row["episode_id"] and payload.status == "failed":
            db.execute("UPDATE episodes SET upload_status=CASE WHEN video_path IS NULL THEN 'failed' ELSE upload_status END, upload_error=CASE WHEN video_path IS NULL THEN ? ELSE upload_error END, updated_at=? WHERE id=?", (payload.error_message, now_iso(), row["episode_id"]))
        recalculate_batch_job(db, job_id)
    return batch_job_detail(job_id)


@app.get("/api/projects/{project_id}/episodes")
def get_episodes(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    return episodes_for(project_id)


@app.get("/api/episodes/{episode_id}")
def get_episode(episode_id: str) -> dict[str, Any]:
    return episode_detail(episode_id)


@app.put("/api/episodes/{episode_id}")
def update_episode(episode_id: str, payload: EpisodeUpdate) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    values = payload.model_dump(exclude_none=True)
    thumbnail_data = values.pop("thumbnail_data", None)
    if thumbnail_data:
        try:
            encoded = thumbnail_data.split(",", 1)[1] if "," in thumbnail_data else thumbnail_data
            thumbnail_target = project_upload_directory(episode["project_id"]) / f"{episode_id}-thumbnail.jpg"
            thumbnail_target.write_bytes(base64.b64decode(encoded))
            values["thumbnail_path"] = str(thumbnail_target)
        except (ValueError, base64.binascii.Error) as error:
            raise HTTPException(status_code=400, detail=f"Invalid thumbnail data: {error}") from error
    if values:
        if "settings_override" in values:
            values["settings_override"] = json_dump(values["settings_override"])
        values["updated_at"] = now_iso()
        try:
            with get_connection() as db:
                db.execute(f"UPDATE episodes SET {', '.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), episode_id))
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="Episode number already exists") from error
    return episode_detail(episode_id)


@app.delete("/api/episodes/{episode_id}")
def delete_episode(episode_id: str) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    paths = {episode.get("video_path"), episode.get("thumbnail_path")}
    paths.update(str(path) for path in UPLOADS_DIR.glob(f"{episode_id}*"))
    with get_connection() as db:
        _delete_episode_database_records(db, episode_id)
        remaining = db.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY episode_number LIMIT 1", (episode["project_id"],)).fetchone()
        if remaining:
            db.execute("UPDATE projects SET active_episode_id=?, video_filename=?, video_path=?, video_duration=?, video_width=?, video_height=?, aspect_ratio=?, orientation=?, updated_at=? WHERE id=?", (remaining["id"], remaining["video_filename"], remaining["video_path"], remaining["duration"], remaining["width"], remaining["height"], remaining["aspect_ratio"], remaining["orientation"], now_iso(), episode["project_id"]))
        else:
            db.execute("DELETE FROM task_steps WHERE task_id IN (SELECT id FROM tasks WHERE project_id=? AND episode_id IS NULL)", (episode["project_id"],))
            db.execute("DELETE FROM tasks WHERE project_id=? AND episode_id IS NULL", (episode["project_id"],))
            db.execute("UPDATE projects SET active_episode_id=NULL, video_filename=NULL, video_path=NULL, video_duration=NULL, video_width=NULL, video_height=NULL, aspect_ratio=NULL, orientation=NULL, status='Draft', updated_at=? WHERE id=?", (now_iso(), episode["project_id"]))
    for raw_path in paths:
        if raw_path:
            Path(raw_path).unlink(missing_ok=True)
    return {"deleted": True, "episode_id": episode_id}


async def upload_episode(episode_id: str, file: UploadFile = File(...), duration: float | None = Form(None), width: int | None = Form(None), height: int | None = Form(None), aspect_ratio: float | None = Form(None), orientation: str | None = Form(None), thumbnail_data: str | None = Form(None)) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov"}:
        raise HTTPException(status_code=400, detail="Only MP4 and MOV files are supported")
    if file.size is not None and file.size > MAX_VIDEO_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Video must be smaller than 500 MB")
    if duration is not None and duration > 180:
        message = "当前MVP单集支持3分钟以内，请分集上传。"
        with get_connection() as db:
            db.execute("UPDATE episodes SET upload_status='failed', upload_error=?, updated_at=? WHERE id=?", (message, now_iso(), episode_id))
        raise HTTPException(status_code=400, detail=message)
    upload_dir = project_upload_directory(episode["project_id"])
    target = upload_dir / f"{episode_id}{suffix}"
    thumbnail_content = None
    if thumbnail_data:
        try:
            encoded = thumbnail_data.split(",", 1)[1] if "," in thumbnail_data else thumbnail_data
            thumbnail_content = base64.b64decode(encoded)
        except (ValueError, base64.binascii.Error) as error:
            raise HTTPException(status_code=400, detail=f"Invalid thumbnail data: {error}") from error
    previous_upload_status = episode.get("upload_status") or ("completed" if episode.get("video_path") else "not_uploaded")
    previous_upload_error = episode.get("upload_error")
    previous_updated_at = episode.get("updated_at") or now_iso()
    with get_connection() as db:
        db.execute("UPDATE episodes SET upload_status='uploading', upload_error=NULL, updated_at=? WHERE id=?", (now_iso(), episode_id))
    try:
        content = await file.read()
        if len(content) > MAX_VIDEO_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Video must be smaller than 500 MB")
        published = atomic_publish_episode_video(target, content)
    except HTTPException:
        with get_connection() as db:
            db.execute(
                "UPDATE episodes SET upload_status=?, upload_error=?, updated_at=? WHERE id=?",
                (previous_upload_status, previous_upload_error, previous_updated_at, episode_id),
            )
        raise
    except (media_service.MediaToolError, OSError) as error:
        with get_connection() as db:
            db.execute(
                "UPDATE episodes SET upload_status=?, upload_error=?, updated_at=? WHERE id=?",
                (previous_upload_status, previous_upload_error, previous_updated_at, episode_id),
            )
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": getattr(error, "code", "VIDEO_UPLOAD_FAILED"),
                "error_message": str(error),
                "probe_stderr": getattr(error, "stderr", "").strip()[:2000] or None,
            },
        ) from error
    thumbnail_path = None
    if thumbnail_content is not None:
        thumbnail_target = upload_dir / f"{episode_id}-thumbnail.jpg"
        thumbnail_target.write_bytes(thumbnail_content)
        thumbnail_path = str(thumbnail_target.resolve())
    timestamp = now_iso()
    with get_connection() as db:
        db.execute("DELETE FROM episode_character_matches WHERE episode_id=?", (episode_id,))
        db.execute("UPDATE episodes SET video_filename=?, video_path=?, duration=?, width=?, height=?, aspect_ratio=?, orientation=?, thumbnail_path=COALESCE(?, thumbnail_path), file_size=?, analysis_status='not_started', analysis_error=NULL, upload_status='completed', upload_error=NULL, status='Uploaded', updated_at=? WHERE id=?", (file.filename, published["path"], published["duration"], published["width"], published["height"], published["aspect_ratio"], published["orientation"], thumbnail_path, published["file_size"], timestamp, episode_id))
        db.execute("UPDATE projects SET video_filename=?, video_path=?, video_duration=?, video_width=?, video_height=?, aspect_ratio=?, orientation=?, status='Uploaded', updated_at=? WHERE id=? AND active_episode_id=?", (file.filename, published["path"], published["duration"], published["width"], published["height"], published["aspect_ratio"], published["orientation"], timestamp, episode["project_id"], episode_id))
    return {"episode": episode_detail(episode_id), "project": project_detail(episode["project_id"])}


app.router.add_api_route(
    "/api/episodes/{episode_id}/upload",
    upload_episode,
    methods=["POST"],
    route_class_override=EpisodeUploadRoute,
)


def mock_candidate_boxes(thumbnail_path: str | None) -> list[dict[str, int]]:
    source_url = upload_url_for_path(thumbnail_path)
    if not source_url:
        return []
    source_path = upload_path_for_url(source_url)
    try:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError:
        return []
    return MOCK_FRAME_PRESETS.get(digest, [])


def crop_mock_candidate_frame(episode_id: str, candidate_id: str, thumbnail_path: str | None, bounding_box: dict[str, int]) -> str | None:
    source_url = upload_url_for_path(thumbnail_path)
    if not source_url:
        return None
    source_path = upload_path_for_url(source_url)
    episode = episode_or_404(episode_id)
    target = project_upload_directory(episode["project_id"]) / f"{episode_id}-candidate-{candidate_id}.jpg"
    try:
        with Image.open(source_path) as image:
            width, height = image.size
            left = max(0, round(width * bounding_box["x"] / 100))
            top = max(0, round(height * bounding_box["y"] / 100))
            right = min(width, round(width * (bounding_box["x"] + bounding_box["width"]) / 100))
            bottom = min(height, round(height * (bounding_box["y"] + bounding_box["height"]) / 100))
            if right <= left or bottom <= top:
                return None
            image.crop((left, top, right, bottom)).convert("RGB").save(target, "JPEG", quality=88)
    except (OSError, ValueError, KeyError):
        return None
    return upload_url_for_target(target)


@app.post("/api/episodes/{episode_id}/analyze")
def analyze_episode(episode_id: str) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    if not episode.get("video_path"):
        raise HTTPException(status_code=400, detail="Upload a video before analysis")
    project = project_or_404(episode["project_id"])
    with get_connection() as db:
        db.execute("UPDATE episodes SET analysis_status='processing', analysis_error=NULL, status='Analyzing', updated_at=? WHERE id=?", (now_iso(), episode_id))
    try:
        detected = ai_service.detect_episode_characters(episode["video_path"])
        ai_service.extract_character_reference_set(episode["video_path"])
    except ai_service.MockAIError as error:
        with get_connection() as db:
            db.execute("UPDATE episodes SET analysis_status='failed', analysis_error=?, status='Failed', updated_at=? WHERE id=?", (str(error), now_iso(), episode_id))
        raise HTTPException(status_code=422, detail=str(error)) from error
    can_reuse_library = project.get("processing_mode") == "character-consistency" and bool(project.get("use_character_library"))
    existing = [item for item in characters_for(project["id"]) if item.get("priority_level") == "Locked"] if can_reuse_library else []
    timestamp = now_iso()
    duration = max(float(episode.get("duration") or 8), 0.5)

    def times(values: list[float]) -> list[float]:
        maximum = max(duration - 0.1, 0.1)
        normalized = [round(min(value, maximum), 2) for value in values]
        return list(dict.fromkeys(normalized))

    # The phase-two Mock models the product rule, not exhaustive person detection:
    # first establish a small core cast, then auto-reuse it and surface only one
    # possible new long-term role per later episode.
    boxes = mock_candidate_boxes(episode.get("thumbnail_path"))
    candidate_specs: list[tuple[str, str, list[float], float, dict[str, int], dict[str, Any] | None]] = []
    if can_reuse_library:
        visible_existing = existing[:min(5, len(boxes))]
        for source_index, character in enumerate(visible_existing):
            candidate_specs.append((character["name"], character.get("entity_type") or "Human", times([0.1]), 0.94 - source_index * 0.02, boxes[source_index], character))
        available_new_slots = max(0, len(boxes) - len(visible_existing))
        new_candidate_count = min(3 if not existing else 1, available_new_slots)
        for offset in range(new_candidate_count):
            next_number = len(existing) + offset + 1
            source_index = len(visible_existing) + offset
            candidate_specs.append((f"Character {next_number:02d}", "Human", times([0.1]), 0.88 - offset * 0.03, boxes[source_index], None))
    # Quick mode deliberately has no project-library candidate review.
    with get_connection() as db:
        db.execute("DELETE FROM episode_character_matches WHERE episode_id=?", (episode_id,))
        db.execute("DELETE FROM episode_character_candidates WHERE episode_id=?", (episode_id,))
        db.execute("DELETE FROM characters WHERE created_from_episode_id=? AND priority_level IN ('Temporary', 'Background')", (episode_id,))
        candidate_upload_dir = project_upload_directory(project["id"])
        for stale_crop in candidate_upload_dir.glob(f"{episode_id}-candidate-*.jpg"):
            stale_crop.unlink(missing_ok=True)
        for source_index, (name, entity_type, occurrences, confidence, bounding_box, matched) in enumerate(candidate_specs):
            candidate_status = "matched" if matched else "candidate"
            candidate_id = f"candidate-{uuid.uuid4().hex[:8]}"
            representative_timestamp = occurrences[0]
            representative_frame = crop_mock_candidate_frame(episode_id, candidate_id, episode.get("thumbnail_path"), bounding_box)
            occurrence_frames = [representative_frame for _ in occurrences] if representative_frame else []
            db.execute(
                "INSERT INTO episode_character_candidates (id, episode_id, detected_name, entity_type, status, character_id, confidence, source_index, notes, representative_frame, representative_timestamp, occurrence_frames, occurrence_timestamps, bounding_box, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, episode_id, name, entity_type, candidate_status, matched["id"] if matched else None, confidence, source_index, "Static Mock candidate preset; no real detection or tracking was performed.", representative_frame, representative_timestamp, json_dump(occurrence_frames), json_dump(occurrences), json_dump(bounding_box), timestamp, timestamp),
            )
            if matched:
                db.execute("INSERT INTO episode_character_matches (id, episode_id, character_id, detected_name, match_status, confidence, use_project_asset, created_at, updated_at) VALUES (?, ?, ?, ?, 'mock-match', ?, 1, ?, ?)", (f"match-{uuid.uuid4().hex[:8]}", episode_id, matched["id"], name, confidence, timestamp, timestamp))
        db.execute("UPDATE episodes SET analysis_status='completed', analysis_error=NULL, status='Character Review', conflict_count=0, updated_at=? WHERE id=?", (timestamp, episode_id))
        db.execute("UPDATE projects SET active_episode_id=?, updated_at=? WHERE id=?", (episode_id, timestamp, project["id"]))
    return {"episode": episode_detail(episode_id), "matches": get_episode_matches(episode_id), "new_characters": [], "existing_characters": existing, "project": project_detail(project["id"]), "mock": True}


@app.get("/api/projects/{project_id}/character-library")
def get_character_library(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    all_characters = characters_for(project_id)
    return {"project_id": project_id, "locked": [item for item in all_characters if item.get("priority_level") == "Locked"], "temporary": [item for item in all_characters if item.get("priority_level") == "Temporary"], "background": [item for item in all_characters if item.get("priority_level") == "Background"], "characters": all_characters}


@app.get("/api/characters/{character_id}")
def get_library_character(character_id: str) -> dict[str, Any]:
    return character_or_404(character_id)


@app.put("/api/characters/{character_id}")
def update_library_character(character_id: str, payload: CharacterPayload) -> dict[str, Any]:
    character = character_or_404(character_id)
    return update_character(character["project_id"], character_id, payload)


@app.delete("/api/characters/{character_id}")
def delete_library_character(character_id: str) -> dict[str, bool]:
    character = character_or_404(character_id)
    return delete_character(character["project_id"], character_id)


@app.post("/api/characters/{character_id}/confirm")
def confirm_character(character_id: str) -> dict[str, Any]:
    character = character_or_404(character_id)
    project = project_or_404(character["project_id"])
    if project.get("processing_mode") == "character-consistency" and character.get("priority_level") != "Locked":
        with get_connection() as db:
            locked_count = db.execute("SELECT COUNT(*) AS count FROM characters WHERE project_id=? AND priority_level='Locked'", (character["project_id"],)).fetchone()["count"]
        if locked_count >= 5:
            raise HTTPException(status_code=409, detail="Character Consistency projects support 1-5 locked core characters in this MVP")
    problems = confirmation_problems(character)
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))
    with get_connection() as db:
        timestamp = now_iso()
        target_reference = upload_url_for_path(character.get("target_reference_path") or character.get("main_reference_image"))
        sync_target_reference_record(
            db,
            character_id,
            {"target_reference_path": target_reference},
            character.get("created_from_episode_id"),
        )
        db.execute("UPDATE characters SET main_reference_image=?, confirmed=1, priority_level='Locked', confirmation_status='confirmed', confirmed_at=?, updated_at=? WHERE id=?", (target_reference, timestamp, timestamp, character_id))
        db.execute("UPDATE episode_character_candidates SET status='locked', updated_at=? WHERE character_id=?", (now_iso(), character_id))
        if character.get("created_from_episode_id"):
            pending = db.execute("SELECT COUNT(*) AS count FROM characters WHERE created_from_episode_id=? AND priority_level='Temporary'", (character["created_from_episode_id"],)).fetchone()["count"]
            if pending == 0:
                db.execute("UPDATE episodes SET status='Ready', updated_at=? WHERE id=? AND analysis_status='completed'", (now_iso(), character["created_from_episode_id"]))
    return character_or_404(character_id)


@app.post("/api/characters/{character_id}/unconfirm")
def unconfirm_character(character_id: str) -> dict[str, Any]:
    character = character_or_404(character_id)
    with get_connection() as db:
        timestamp = now_iso()
        db.execute("UPDATE characters SET confirmed=0, priority_level='Temporary', confirmation_status='temporary', confirmed_at=NULL, updated_at=? WHERE id=?", (timestamp, character_id))
        db.execute("UPDATE episode_character_candidates SET status='temporary', updated_at=? WHERE character_id=? AND status='locked'", (timestamp, character_id))
        if character.get("created_from_episode_id"):
            db.execute("UPDATE episodes SET status='Character Review', updated_at=? WHERE id=?", (timestamp, character["created_from_episode_id"]))
    return character_or_404(character_id)


@app.post("/api/characters/{character_id}/upgrade")
def upgrade_character(character_id: str) -> dict[str, Any]:
    character = character_or_404(character_id)
    project = project_or_404(character["project_id"])
    if project.get("processing_mode") == "character-consistency" and character.get("priority_level") != "Locked":
        with get_connection() as db:
            locked_count = db.execute("SELECT COUNT(*) AS count FROM characters WHERE project_id=? AND priority_level='Locked'", (character["project_id"],)).fetchone()["count"]
        if locked_count >= 5:
            raise HTTPException(status_code=409, detail="Character Consistency projects support 1-5 locked core characters in this MVP")
    problems = confirmation_problems(character)
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems))
    with get_connection() as db:
        timestamp = now_iso()
        db.execute("UPDATE characters SET confirmed=1, priority_level='Locked', confirmation_status='confirmed', confirmed_at=?, updated_at=? WHERE id=?", (timestamp, timestamp, character_id))
        db.execute("UPDATE episode_character_candidates SET status='locked', updated_at=? WHERE character_id=?", (now_iso(), character_id))
    return character_or_404(character_id)


@app.post("/api/characters/{character_id}/mock-styled-assets")
def generate_mock_styled_assets(character_id: str) -> dict[str, Any]:
    character = character_or_404(character_id)
    source = next((character.get(field) for field in REFERENCE_FIELDS if character.get(field)), None)
    if not source:
        raise HTTPException(status_code=400, detail="Add an original reference image before generating styled references")
    with get_connection() as db:
        db.execute("UPDATE characters SET styled_front_view_image=?, styled_angle_view_image=?, styled_side_view_image=?, styled_full_body_image=?, styled_asset_status='mock_generated', updated_at=? WHERE id=?", (source, source, source, source, now_iso(), character_id))
    return character_or_404(character_id)


@app.post("/api/projects/{project_id}/characters/mock-styled-assets")
def generate_all_mock_styled_assets(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    generated = 0
    for character in characters_for(project_id):
        if any(character.get(field) for field in REFERENCE_FIELDS):
            generate_mock_styled_assets(character["id"])
            generated += 1
    return {"generated": generated, "characters": characters_for(project_id), "mock": True}


@app.post("/api/projects/{project_id}/characters/confirm-all")
def confirm_all_characters(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    confirmed: list[str] = []
    skipped: list[dict[str, Any]] = []
    for character in characters_for(project_id):
        problems = confirmation_problems(character)
        if problems:
            skipped.append({"character_id": character["id"], "problems": problems})
        else:
            confirm_character(character["id"])
            confirmed.append(character["id"])
    return {"confirmed": confirmed, "skipped": skipped, "characters": characters_for(project_id)}


class MergePayload(BaseModel):
    character_ids: list[str]
    keep_character_id: str | None = None


@app.post("/api/characters/merge")
def merge_characters(payload: MergePayload, request: Request) -> dict[str, Any]:
    if len(payload.character_ids) < 2:
        raise HTTPException(status_code=400, detail="Select at least two characters to merge")
    keep_id = payload.keep_character_id or payload.character_ids[0]
    keep = character_or_404(keep_id)
    require_project_access(require_request_user(request), keep["project_id"])
    with get_connection() as db:
        rows = db.execute(
            f"SELECT id, project_id FROM characters WHERE id IN ({','.join('?' for _ in payload.character_ids)})",
            tuple(payload.character_ids),
        ).fetchall()
        if len(rows) != len(set(payload.character_ids)) or any(row["project_id"] != keep["project_id"] for row in rows):
            raise HTTPException(status_code=404, detail="Resource not found.")
        for character_id in payload.character_ids:
            if character_id != keep_id:
                db.execute("DELETE FROM characters WHERE id=? AND project_id=?", (character_id, keep["project_id"]))
    return character_or_404(keep_id)


@app.get("/api/characters/{character_id}/looks")
def get_character_looks(character_id: str) -> list[dict[str, Any]]:
    character_or_404(character_id)
    return looks_for(character_id)


@app.post("/api/characters/{character_id}/looks")
def create_character_look(character_id: str, payload: LookPayload) -> dict[str, Any]:
    character_or_404(character_id)
    look_id = f"look-{uuid.uuid4().hex[:8]}"
    timestamp = now_iso()
    with get_connection() as db:
        if payload.is_default:
            db.execute("UPDATE character_looks SET is_default=0 WHERE character_id=?", (character_id,))
        db.execute("INSERT INTO character_looks (id, character_id, name, description, reference_image, clothing_description, hair_override, headwear_description, source_episode_id, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (look_id, character_id, payload.name, payload.description, payload.reference_image, payload.clothing_description, payload.hair_override, payload.headwear_description, payload.source_episode_id, bool_int(payload.is_default), timestamp, timestamp))
    return next(item for item in looks_for(character_id) if item["id"] == look_id)


@app.put("/api/character-looks/{look_id}")
def update_character_look(look_id: str, payload: LookPayload) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT * FROM character_looks WHERE id=?", (look_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Character look not found")
        if payload.is_default:
            db.execute("UPDATE character_looks SET is_default=0 WHERE character_id=?", (row["character_id"],))
        db.execute("UPDATE character_looks SET name=?, description=?, reference_image=?, clothing_description=?, hair_override=?, headwear_description=?, source_episode_id=?, is_default=?, updated_at=? WHERE id=?", (payload.name, payload.description, payload.reference_image, payload.clothing_description, payload.hair_override, payload.headwear_description, payload.source_episode_id, bool_int(payload.is_default), now_iso(), look_id))
        character_id = row["character_id"]
    return next(item for item in looks_for(character_id) if item["id"] == look_id)


@app.delete("/api/character-looks/{look_id}")
def delete_character_look(look_id: str) -> dict[str, bool]:
    with get_connection() as db:
        cursor = db.execute("DELETE FROM character_looks WHERE id=?", (look_id,))
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Character look not found")
    return {"deleted": True}


@app.get("/api/projects/{project_id}/voice-profiles")
def get_voice_profiles(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    return voice_profiles_for(project_id)


@app.post("/api/projects/{project_id}/voice-profiles")
def create_voice_profile(project_id: str, payload: VoiceProfilePayload) -> dict[str, Any]:
    project_or_404(project_id)
    profile_id = f"voice-{uuid.uuid4().hex[:8]}"
    timestamp = now_iso()
    with get_connection() as db:
        db.execute("INSERT INTO voice_profiles (id, project_id, name, source_type, provider_id, model_id, external_voice_id, base_voice, voice_age, voice_style, speech_speed, pitch, emotion_strength, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (profile_id, project_id, payload.name, payload.source_type, payload.provider_id, payload.model_id, payload.external_voice_id, payload.base_voice, payload.voice_age, payload.voice_style, payload.speech_speed, payload.pitch, payload.emotion_strength, payload.status, timestamp, timestamp))
    return next(item for item in voice_profiles_for(project_id) if item["id"] == profile_id)


@app.put("/api/voice-profiles/{voice_profile_id}")
def update_voice_profile(voice_profile_id: str, payload: VoiceProfilePayload) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT project_id FROM voice_profiles WHERE id=?", (voice_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Voice profile not found")
        db.execute("UPDATE voice_profiles SET name=?, source_type=?, provider_id=?, model_id=?, external_voice_id=?, base_voice=?, voice_age=?, voice_style=?, speech_speed=?, pitch=?, emotion_strength=?, status=?, updated_at=? WHERE id=?", (payload.name, payload.source_type, payload.provider_id, payload.model_id, payload.external_voice_id, payload.base_voice, payload.voice_age, payload.voice_style, payload.speech_speed, payload.pitch, payload.emotion_strength, payload.status, now_iso(), voice_profile_id))
        project_id = row["project_id"]
    return next(item for item in voice_profiles_for(project_id) if item["id"] == voice_profile_id)


@app.delete("/api/voice-profiles/{voice_profile_id}")
def delete_voice_profile(voice_profile_id: str) -> dict[str, bool]:
    with get_connection() as db:
        row = db.execute("SELECT project_id, uploaded_audio_path FROM voice_profiles WHERE id=?", (voice_profile_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Voice profile not found")
        db.execute("UPDATE characters SET voice_profile_id=NULL WHERE voice_profile_id=?", (voice_profile_id,))
        db.execute("DELETE FROM voice_profiles WHERE id=?", (voice_profile_id,))
    if row["uploaded_audio_path"]:
        Path(row["uploaded_audio_path"]).unlink(missing_ok=True)
    return {"deleted": True}


@app.post("/api/voice-profiles/{voice_profile_id}/upload-sample")
async def upload_voice_sample(voice_profile_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT project_id FROM voice_profiles WHERE id=?", (voice_profile_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    suffix = Path(file.filename or "sample.wav").suffix.lower()
    if suffix not in {".mp3", ".wav", ".m4a"}:
        raise HTTPException(status_code=400, detail="Only MP3, WAV, and M4A voice samples are supported")
    target = project_upload_directory(row["project_id"]) / f"{voice_profile_id}{suffix}"
    target.write_bytes(await file.read())
    with get_connection() as db:
        db.execute("UPDATE voice_profiles SET uploaded_audio_path=?, source_type='upload-sample', status='Sample Uploaded / Not Trained / Mock Only', updated_at=? WHERE id=?", (str(target), now_iso(), voice_profile_id))
    return next(item for item in voice_profiles_for(row["project_id"]) if item["id"] == voice_profile_id)


@app.get("/api/voice-profiles/{voice_profile_id}/preview")
def preview_voice_profile(voice_profile_id: str) -> dict[str, Any]:
    with get_connection() as db:
        row = db.execute("SELECT id, project_id, name, source_type, status FROM voice_profiles WHERE id=?", (voice_profile_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    return {"profile_id": voice_profile_id, "mock": True, "preview": "Local mock voice preview", "status": row["status"]}


@app.get("/api/voice-providers")
def list_voice_providers() -> list[dict[str, Any]]:
    return VOICE_PROVIDERS


@app.get("/api/voice-providers/{provider_id}/models")
def list_voice_models(provider_id: str) -> list[dict[str, Any]]:
    return [] if provider_id == "none" else [{"id": "mock-tts-v1", "name": "Mock TTS Model"}]


@app.get("/api/voice-providers/{provider_id}/voices")
def list_provider_voices(provider_id: str, model_id: str = "") -> list[dict[str, Any]]:
    return [] if provider_id == "none" else [{"id": "mock-voice-01", "name": "Mock Provider Voice", "model_id": model_id}]


@app.get("/api/projects/{project_id}/defaults")
def get_project_defaults(project_id: str) -> dict[str, Any]:
    return project_defaults_for(project_id)


@app.put("/api/projects/{project_id}/defaults")
def update_project_defaults(project_id: str, payload: SettingsPayload) -> dict[str, Any]:
    project_or_404(project_id)
    update_settings(project_id, payload)
    return project_detail(project_id)


@app.get("/api/episodes/{episode_id}/character-matches")
def get_episode_matches(episode_id: str) -> list[dict[str, Any]]:
    episode_or_404(episode_id)
    with get_connection() as db:
        rows = db.execute("SELECT * FROM episode_character_matches WHERE episode_id=? ORDER BY created_at", (episode_id,)).fetchall()
    return [dict(row) | {"use_project_asset": bool(row["use_project_asset"])} for row in rows]


@app.get("/api/episodes/{episode_id}/character-candidates")
def get_episode_candidates(episode_id: str) -> dict[str, Any]:
    episode = episode_detail(episode_id)
    return {"episode_id": episode_id, "total": episode["candidate_count"], "counts": episode["candidate_counts"], "candidates": episode["candidates"]}


def create_temporary_asset_from_candidate(episode: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate.get("character_id"):
        with get_connection() as db:
            row = db.execute("SELECT id FROM characters WHERE id=?", (candidate["character_id"],)).fetchone()
        if row:
            return character_or_404(row["id"])
    source_url = upload_url_for_path(candidate.get("representative_frame"))
    if not source_url:
        raise HTTPException(status_code=400, detail="Candidate representative frame file is missing; upload a thumbnail or add an original reference image")
    source_path = upload_path_for_url(source_url)
    reference_dir = project_upload_directory(episode["project_id"]) / "character-references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix.lower() if source_path.suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
    character_id = f"char-{uuid.uuid4().hex[:8]}"
    target = reference_dir / f"{character_id}-candidate-source{suffix}"
    shutil.copy2(source_path, target)
    reference_url = upload_url_for_target(target)
    with get_connection() as db:
        existing_codes = {row["character_code"] for row in db.execute("SELECT character_code FROM characters WHERE project_id=?", (episode["project_id"],)).fetchall() if row["character_code"]}
    detected_number = re.search(r"(\d+)$", candidate["detected_name"])
    number = int(detected_number.group(1)) if detected_number else 1
    while f"C{number:02d}" in existing_codes:
        number += 1
    code = f"C{number:02d}"
    display_name = candidate["detected_name"]
    created = insert_character(
        episode["project_id"],
        CharacterPayload(
            name=display_name,
            role="Supporting",
            appearance_description="身份以原视频主参考图为准；备注可选。",
            identity_description="身份以原视频主参考图为准；备注可选。",
            original_reference_image=reference_url,
            primary_reference_type="original_reference_image",
            character_code=code,
            character_type=candidate["entity_type"] if candidate["entity_type"] in ENTITY_TYPES else "Other",
            priority_level="Temporary",
            source="ai_detected_mock",
            created_from_episode_id=episode["id"],
        ),
        character_id=character_id,
    )
    with get_connection() as db:
        db.execute("UPDATE character_reference_images SET source_type='ai_detected_mock', source_episode_id=?, source_timestamp=?, updated_at=? WHERE character_id=?", (episode["id"], candidate.get("representative_timestamp"), now_iso(), character_id))
        db.execute("UPDATE episode_character_candidates SET status='temporary', character_id=?, updated_at=? WHERE id=?", (character_id, now_iso(), candidate["id"]))
    return character_or_404(character_id)


@app.put("/api/episodes/{episode_id}/character-candidates/{candidate_id}")
def update_episode_candidate(episode_id: str, candidate_id: str, payload: CandidateUpdatePayload) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    allowed = {"candidate", "retained", "matched", "temporary", "background", "ignored"}
    if payload.status not in allowed:
        raise HTTPException(status_code=400, detail="Candidate status must be candidate, retained, matched, temporary, background, or ignored")
    with get_connection() as db:
        row = db.execute("SELECT * FROM episode_character_candidates WHERE id=? AND episode_id=?", (candidate_id, episode_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Episode candidate not found")
    current = dict(row)
    if current["status"] == "locked":
        raise HTTPException(status_code=409, detail="Unconfirm the locked project character before changing this candidate")
    if payload.entity_type and payload.entity_type not in ENTITY_TYPES:
        raise HTTPException(status_code=400, detail="Entity type must be Human, Pet, Animal, or Other")
    character_id = current.get("character_id")
    if payload.status == "matched":
        if not payload.character_id:
            raise HTTPException(status_code=400, detail="Choose an existing locked character to match")
        with get_connection() as db:
            target = db.execute("SELECT id FROM characters WHERE id=? AND project_id=? AND priority_level='Locked'", (payload.character_id, episode["project_id"])).fetchone()
        if not target:
            raise HTTPException(status_code=400, detail="Matched character must be a locked project character")
        if character_id and character_id != payload.character_id:
            with get_connection() as db:
                db.execute("DELETE FROM characters WHERE id=? AND created_from_episode_id=? AND priority_level='Temporary'", (character_id, episode_id))
        character_id = payload.character_id
        with get_connection() as db:
            db.execute("DELETE FROM episode_character_matches WHERE episode_id=? AND detected_name=?", (episode_id, current["detected_name"]))
            db.execute("INSERT INTO episode_character_matches (id, episode_id, character_id, detected_name, match_status, confidence, use_project_asset, created_at, updated_at) VALUES (?, ?, ?, ?, 'user-match', ?, 1, ?, ?)", (f"match-{uuid.uuid4().hex[:8]}", episode_id, character_id, current["detected_name"], current["confidence"], now_iso(), now_iso()))
    elif payload.status == "temporary":
        if payload.entity_type:
            current["entity_type"] = payload.entity_type
        created = create_temporary_asset_from_candidate(episode, current)
        character_id = created["id"]
    elif payload.status in {"candidate", "retained", "background", "ignored"}:
        if character_id:
            with get_connection() as db:
                db.execute("DELETE FROM characters WHERE id=? AND created_from_episode_id=? AND priority_level='Temporary'", (character_id, episode_id))
                db.execute("DELETE FROM episode_character_matches WHERE episode_id=? AND character_id=?", (episode_id, character_id))
        character_id = None
    with get_connection() as db:
        db.execute("UPDATE episode_character_candidates SET status=?, character_id=?, entity_type=COALESCE(?, entity_type), updated_at=? WHERE id=?", (payload.status, character_id, payload.entity_type, now_iso(), candidate_id))
    return get_episode_candidates(episode_id)


@app.post("/api/episodes/{episode_id}/character-candidates/confirm-retained")
def confirm_retained_candidates(episode_id: str) -> dict[str, Any]:
    episode = episode_or_404(episode_id)
    with get_connection() as db:
        rows = db.execute("SELECT * FROM episode_character_candidates WHERE episode_id=? AND status IN ('retained', 'temporary') ORDER BY source_index", (episode_id,)).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail="Keep at least one core character before creating character cards")
    created: list[dict[str, Any]] = []
    for row in rows:
        created.append(create_temporary_asset_from_candidate(episode, dict(row)))
    with get_connection() as db:
        db.execute("UPDATE episodes SET status='Character Review', updated_at=? WHERE id=?", (now_iso(), episode_id))
    return {"episode": episode_detail(episode_id), "created": created, "project": project_detail(episode["project_id"]), "mock": True}


@app.put("/api/episodes/{episode_id}/character-matches/{match_id}")
def update_episode_match(episode_id: str, match_id: str, payload: MatchUpdatePayload) -> dict[str, Any]:
    episode_or_404(episode_id)
    values = payload.model_dump(exclude_none=True)
    if "use_project_asset" in values:
        values["use_project_asset"] = bool_int(values["use_project_asset"])
    if values:
        values["updated_at"] = now_iso()
        with get_connection() as db:
            db.execute(f"UPDATE episode_character_matches SET {', '.join(f'{key}=?' for key in values)} WHERE id=? AND episode_id=?", (*values.values(), match_id, episode_id))
    with get_connection() as db:
        row = db.execute("SELECT * FROM episode_character_matches WHERE id=? AND episode_id=?", (match_id, episode_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Character match not found")
    return dict(row) | {"use_project_asset": bool(row["use_project_asset"])}


@app.get("/api/styles")
def get_styles() -> list[dict[str, Any]]:
    return STYLE_CATALOG


@app.get("/api/languages")
def get_languages() -> list[dict[str, Any]]:
    return LANGUAGES


@app.get("/api/voices")
def get_voices() -> list[dict[str, Any]]:
    return VOICE_CATALOG


@app.put("/api/projects/{project_id}/settings")
def update_settings(project_id: str, payload: SettingsPayload) -> dict[str, Any]:
    project_or_404(project_id)
    values = payload.model_dump()
    if values["target_language"].strip().lower() not in SUPPORTED_TARGET_LANGUAGES:
        raise HTTPException(status_code=400, detail={"code": "TARGET_LANGUAGE_UNSUPPORTED", "message": "This target language is not enabled in the current workflow."})
    with get_connection() as db:
        previous = db.execute("SELECT target_language FROM settings WHERE project_id=?", (project_id,)).fetchone()
        target_language_changed = bool(previous and previous["target_language"] != values["target_language"])
        db.execute("INSERT INTO settings (project_id, style_id, source_language, target_language, language_mode, character_consistency, transformation_strength, scene_processing_mode, subtitle_enabled, lip_sync_enabled, visual_scope, character_appearance_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET style_id=excluded.style_id, source_language=excluded.source_language, target_language=excluded.target_language, language_mode=excluded.language_mode, character_consistency=excluded.character_consistency, transformation_strength=excluded.transformation_strength, scene_processing_mode=excluded.scene_processing_mode, subtitle_enabled=excluded.subtitle_enabled, lip_sync_enabled=excluded.lip_sync_enabled, visual_scope=excluded.visual_scope, character_appearance_policy=excluded.character_appearance_policy", (project_id, values["style_id"], values["source_language"], values["target_language"], values["language_mode"], values["character_consistency"], values["transformation_strength"], values["scene_processing_mode"], bool_int(values["subtitle_enabled"]), bool_int(values["lip_sync_enabled"]), values["visual_scope"], values["character_appearance_policy"]))
        db.execute("UPDATE projects SET source_language=?, target_language=?, selected_style=?, subtitle_enabled=?, lip_sync_enabled=?, default_style_id=?, default_source_language=?, default_target_language=?, default_language_mode=?, default_subtitle_enabled=?, default_lip_sync_enabled=?, default_character_consistency=?, default_transformation_strength=?, default_scene_processing_mode=?, default_visual_scope=?, character_appearance_policy=?, updated_at=? WHERE id=?", (values["source_language"], values["target_language"], values["style_id"], bool_int(values["subtitle_enabled"]), bool_int(values["lip_sync_enabled"]), values["style_id"], values["source_language"], values["target_language"], values["language_mode"], bool_int(values["subtitle_enabled"]), bool_int(values["lip_sync_enabled"]), values["character_consistency"], values["transformation_strength"], values["scene_processing_mode"], values["visual_scope"], values["character_appearance_policy"], now_iso(), project_id))
        if target_language_changed:
            timestamp = now_iso()
            db.execute("UPDATE translation_documents SET status='stale', updated_at=? WHERE transcript_document_id IN (SELECT id FROM transcript_documents WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?))", (timestamp, project_id))
            db.execute("UPDATE subtitle_outputs SET status='stale' WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?) AND output_type IN ('translated_subtitles', 'translated_subtitle_preview')", (project_id,))
            db.execute("UPDATE voice_segment_outputs SET status='stale', error_code='TARGET_LANGUAGE_CHANGED', error_message='Target language changed; regenerate translation and voice.', updated_at=? WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?) AND status='completed'", (timestamp, project_id))
            db.execute("UPDATE dubbed_outputs SET status='stale', updated_at=? WHERE episode_id IN (SELECT id FROM episodes WHERE project_id=?) AND status='completed'", (timestamp, project_id))
    return settings_for(project_id)


def create_task(project_id: str, episode_id: str | None = None) -> str:
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    with get_connection() as db:
        db.execute("INSERT INTO tasks (id, project_id, episode_id, status, progress, current_step, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (task_id, project_id, episode_id, "running", 0, TASK_STEPS[0], now_iso()))
        db.executemany("INSERT INTO task_steps (task_id, step_index, name, status) VALUES (?, ?, ?, ?)", [(task_id, index, name, "waiting") for index, name in enumerate(TASK_STEPS)])
    return task_id


def run_task(task_id: str, project_id: str, simulate_failure: bool = False) -> None:
    """Run the visible pipeline as small replaceable service calls."""
    project = project_or_404(project_id)
    settings = settings_for(project_id)
    characters = characters_for(project_id)
    service_calls = [
        lambda: ai_service.validate_video(project.get("video_path")), lambda: ai_service.detect_source_language(project.get("video_path")), lambda: ai_service.extract_audio(project.get("video_path")), lambda: ai_service.extract_keyframes(project.get("video_path")), lambda: ai_service.extract_characters(project.get("video_path")), lambda: ai_service.extract_scenes(project.get("video_path")), lambda: [ai_service.generate_character_assets(character) for character in characters], lambda: ai_service.segment_video(project.get("video_path")), lambda: ai_service.transform_character_in_segment({"id": "seg-01"}, settings["style_id"]), lambda: ai_service.transform_scene_in_segment({"id": "seg-01"}, settings["style_id"]), lambda: ai_service.transform_objects_in_segment({"id": "seg-01"}, settings["style_id"]), lambda: ai_service.translate_dialogue(settings["source_language"], settings["target_language"]), lambda: [ai_service.generate_character_voice(character, settings["target_language"]) for character in characters], lambda: ai_service.run_lip_sync(project.get("video_path"), settings["lip_sync_enabled"]), lambda: ai_service.generate_subtitles(settings["target_language"], settings["subtitle_enabled"]), lambda: ai_service.render_video_segment({"id": "seg-01"}), lambda: ai_service.merge_video_segments([{"id": "seg-01"}, {"id": "seg-02"}]), lambda: ai_service.align_audio_video(project.get("video_path")), lambda: ai_service.quality_check("mock-assets/generated-result.mp4"), lambda: ai_service.export_final_video("mock-assets/generated-result.mp4"),
    ]
    try:
        for index, step_name in enumerate(TASK_STEPS):
            with get_connection() as db:
                task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                if not task or task["status"] == "cancelled":
                    return
                db.execute("UPDATE task_steps SET status='processing', progress=10 WHERE task_id=? AND step_index=?", (task_id, index))
                db.execute("UPDATE tasks SET current_step=?, progress=? WHERE id=?", (step_name, round(index / len(TASK_STEPS) * 100), task_id))
            time.sleep(0.35)
            if simulate_failure and index == 8:
                with get_connection() as db:
                    db.execute("UPDATE task_steps SET status='failed', error_message=? WHERE task_id=? AND step_index=?", ("Mock transformation provider timed out", task_id, index))
                    db.execute("UPDATE tasks SET status='failed', error_message=?, current_step=? WHERE id=?", ("Mock transformation provider timed out", step_name, task_id))
                    db.execute("UPDATE mock_results SET status='failed', updated_at=? WHERE task_id=?", (now_iso(), task_id))
                    db.execute("UPDATE projects SET status='Failed', updated_at=? WHERE id=?", (now_iso(), project_id))
                return
            service_calls[index]()
            with get_connection() as db:
                db.execute("UPDATE task_steps SET status='completed', progress=100 WHERE task_id=? AND step_index=?", (task_id, index))
                db.execute("UPDATE tasks SET progress=? WHERE id=?", (round((index + 1) / len(TASK_STEPS) * 100), task_id))
        with get_connection() as db:
            db.execute("UPDATE tasks SET status='completed', progress=100, current_step=?, completed_at=? WHERE id=?", (TASK_STEPS[-1], now_iso(), task_id))
            db.execute("UPDATE mock_results SET status='completed', generated_video_path=original_video_path, updated_at=? WHERE task_id=?", (now_iso(), task_id))
            db.execute("UPDATE projects SET status='Completed', updated_at=? WHERE id=?", (now_iso(), project_id))
    except Exception as error:
        with get_connection() as db:
            db.execute("UPDATE tasks SET status='failed', error_message=? WHERE id=?", (str(error), task_id))
            db.execute("UPDATE mock_results SET status='failed', updated_at=? WHERE task_id=?", (now_iso(), task_id))
            db.execute("UPDATE projects SET status='Failed', updated_at=? WHERE id=?", (now_iso(), project_id))


def get_task(task_id: str) -> dict[str, Any]:
    with get_connection() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        steps = db.execute("SELECT step_index, name, status, progress, error_message FROM task_steps WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
    item = row_to_dict(task)
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")
    item["steps"] = [row_to_dict(step) for step in steps]
    return item


@app.post("/api/projects/{project_id}/generate")
def generate_project(project_id: str, payload: GeneratePayload, background_tasks: BackgroundTasks) -> dict[str, Any]:
    project = project_or_404(project_id)
    if not project.get("video_path"):
        raise HTTPException(status_code=400, detail="Upload a video before generating")
    if project.get("processing_mode") == "character-consistency" and not any(item.get("priority_level") == "Locked" and item.get("confirmed") for item in characters_for(project_id)):
        raise HTTPException(status_code=400, detail="Analyze the video before generating")
    task_id = create_task(project_id, project.get("active_episode_id"))
    with get_connection() as db:
        db.execute("UPDATE projects SET status='Generating', updated_at=? WHERE id=?", (now_iso(), project_id))
        result_id = f"result-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO mock_results (id, project_id, episode_id, task_id, status, original_video_path, generated_video_path, mock_notice, created_at, updated_at) VALUES (?, ?, ?, ?, 'processing', ?, NULL, ?, ?, ?)", (result_id, project_id, project.get("active_episode_id"), task_id, project.get("video_path"), "Mock result reuses the original episode video; no real transformation, translation, dubbing, subtitles, or lip sync was performed.", now_iso(), now_iso()))
    background_tasks.add_task(run_task, task_id, project_id, payload.simulate_failure)
    return get_task(task_id)


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict[str, Any]:
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    with get_connection() as db:
        db.execute("UPDATE tasks SET status='cancelled', error_message=? WHERE id=? AND status='running'", ("Cancelled by user", task_id))
        db.execute("UPDATE mock_results SET status='cancelled', updated_at=? WHERE task_id=?", (now_iso(), task_id))
        db.execute("UPDATE projects SET status='Ready', updated_at=? WHERE id=?", (now_iso(), task["project_id"]))
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    task = get_task(task_id)
    if task["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Only failed or cancelled tasks can be retried")
    new_task_id = create_task(task["project_id"], task.get("episode_id"))
    project = project_or_404(task["project_id"])
    with get_connection() as db:
        db.execute("UPDATE projects SET status='Generating', updated_at=? WHERE id=?", (now_iso(), task["project_id"]))
        db.execute("INSERT INTO mock_results (id, project_id, episode_id, task_id, status, original_video_path, mock_notice, created_at, updated_at) VALUES (?, ?, ?, ?, 'processing', ?, ?, ?, ?)", (f"result-{uuid.uuid4().hex[:8]}", task["project_id"], task.get("episode_id"), new_task_id, project.get("video_path"), "Mock result reuses the original episode video; no real AI processing was performed.", now_iso(), now_iso()))
    background_tasks.add_task(run_task, new_task_id, task["project_id"], False)
    return get_task(new_task_id)


@app.get("/api/projects/{project_id}/result")
def project_result(project_id: str, episode_id: str | None = None) -> dict[str, Any]:
    project = project_detail(project_id)
    selected_episode_id = episode_id or project.get("active_episode_id")
    with get_connection() as db:
        result = db.execute("SELECT * FROM mock_results WHERE project_id=? AND episode_id IS ? AND status='completed' ORDER BY updated_at DESC LIMIT 1", (project_id, selected_episode_id)).fetchone()
        if not result:
            result = db.execute("SELECT * FROM mock_results WHERE project_id=? AND episode_id IS NULL AND status='completed' ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        if not result and episode_id is None:
            result = db.execute("SELECT * FROM mock_results WHERE project_id=? AND status='completed' ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        latest_task = db.execute("SELECT status, error_message FROM tasks WHERE project_id=? AND episode_id IS ? ORDER BY started_at DESC LIMIT 1", (project_id, selected_episode_id)).fetchone()
    if not result:
        if latest_task and latest_task["status"] == "running":
            raise HTTPException(status_code=409, detail="Mock generation is still processing. Return to Generate to view progress.")
        if latest_task and latest_task["status"] == "failed":
            raise HTTPException(status_code=422, detail=f"Mock generation failed: {latest_task['error_message'] or 'Unknown task error'}")
        raise HTTPException(status_code=404, detail="No Mock result exists for this episode. Start Mock generation first.")
    item = dict(result)
    original_url = upload_url_for_path(item.get("original_video_path"))
    generated_url = upload_url_for_path(item.get("generated_video_path")) or original_url
    return {"id": item["id"], "task_id": item["task_id"], "episode_id": item["episode_id"], "status": item["status"], "project": project, "demo_label": "Demo Generated Result / Mock", "original_video_url": original_url, "generated_video_url": generated_url, "generated_at": item["updated_at"], "comparison_note": item["mock_notice"], "mock": True}


@app.on_event("startup")
def ensure_dirs() -> None:
    init_db()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
