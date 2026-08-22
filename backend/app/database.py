"""SQLite repository and additive migrations for the local MVP."""

from __future__ import annotations

import json
import os
import sqlite3
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .migrations.stage_3a1 import apply_stage_3a1_migration
from .migrations.stage_3a1_1 import apply_stage_3a1_1_migration
from .migrations.stage_3a2_1 import apply_stage_3a2_1_migration
from .migrations.stage_3a_workflow_recovery import apply_stage_3a_workflow_recovery
from .migrations.stage_3a_workflow_fixes import apply_stage_3a_workflow_fixes
from .migrations.stage_3b1_visual_transformation import apply_stage_3b1_migration
from .migrations.stage_3b1_workflow import apply_stage_3b1_workflow_migration
from .migrations.stage_3b1_xmax_realtime import apply_stage_3b1_xmax_realtime_migration
from .migrations.stage_3b1_character_reference_bindings import apply_stage_3b1_character_reference_bindings
from .migrations.stage_3c_task_state import apply_stage_3c_task_state_migration
from .migrations.stage_3b1_xmax_start_gate import apply_stage_3b1_xmax_start_gate_migration
from .migrations.stage_3b1_xmax_session_confirmation import apply_stage_3b1_xmax_session_confirmation_migration
from .migrations.stage_3b1_xmax_attempt_audit import apply_stage_3b1_xmax_attempt_audit_migration
from .migrations.stage_3b1_xmax_attempt_audit_v2 import apply_stage_3b1_xmax_attempt_audit_v2_migration
from .migrations.stage_3b1_xmax_attempt_audit_v3 import apply_stage_3b1_xmax_attempt_audit_v3_migration
from .migrations.stage_3b1_xmax_single_shot import apply as apply_stage_3b1_xmax_single_shot_migration
from .migrations.stage_3b1_xmax_thirty_second import apply as apply_stage_3b1_xmax_thirty_second_migration
from .migrations.stage_3b1_xmax_thirty_second_lifecycle import apply as apply_stage_3b1_xmax_thirty_second_lifecycle_migration
from .migrations.stage_3b1_xmax_thirty_second_merge import apply as apply_stage_3b1_xmax_thirty_second_merge_migration
from .migrations.stage_3b1_xmax_thirty_second_scene_mapping import apply as apply_stage_3b1_xmax_thirty_second_scene_mapping_migration
from .migrations.stage_3b1_xmax_thirty_second_quality import apply as apply_stage_3b1_xmax_thirty_second_quality_migration
from .migrations.stage_3b1_xmax_thirty_second_v11_quality import apply as apply_stage_3b1_xmax_thirty_second_v11_quality_migration
from .migrations.stage_3a2_demucs_localization import apply as apply_stage_3a2_demucs_localization_migration
from .migrations.stage_3a2_tts_mix_revision import apply as apply_stage_3a2_tts_mix_revision_migration
from .migrations.stage_3d_overseas_episode import apply as apply_stage_3d_overseas_episode_migration
from .migrations.beta_p1_auth_isolation import apply as apply_beta_p1_auth_isolation_migration
from .migrations.beta_p3_xmax_temporary_keys import apply as apply_beta_p3_xmax_temporary_keys_migration
from .migrations.beta_p4_browser_xmax_attempts import apply as apply_beta_p4_browser_xmax_attempts_migration
from .migrations.beta_p5_xmax_offline_attempts import apply as apply_beta_p5_xmax_offline_attempts_migration
from .migrations.beta_self_service_closure import apply as apply_beta_self_service_closure_migration

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "backend" / "drama_ai_tool.db"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
UPLOADS_DIR = ROOT / "uploads"
MOCK_ASSETS_DIR = ROOT / "mock-assets"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _add_columns(db: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    UPLOADS_DIR.mkdir(exist_ok=True)
    MOCK_ASSETS_DIR.mkdir(exist_ok=True)
    with get_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                video_filename TEXT, video_path TEXT, video_duration REAL,
                video_width INTEGER, video_height INTEGER, aspect_ratio REAL,
                orientation TEXT, source_language TEXT, target_language TEXT,
                selected_style TEXT, subtitle_enabled INTEGER NOT NULL DEFAULT 1,
                lip_sync_enabled INTEGER NOT NULL DEFAULT 1,
                processing_mode TEXT NOT NULL DEFAULT 'quick-transformation',
                active_episode_id TEXT, default_style_id TEXT,
                default_source_language TEXT, default_target_language TEXT,
                default_language_mode TEXT, default_subtitle_enabled INTEGER NOT NULL DEFAULT 1,
                default_lip_sync_enabled INTEGER NOT NULL DEFAULT 1,
                use_character_library INTEGER NOT NULL DEFAULT 0,
                default_character_consistency TEXT NOT NULL DEFAULT 'strong',
                default_transformation_strength TEXT NOT NULL DEFAULT 'medium',
                default_scene_processing_mode TEXT NOT NULL DEFAULT 'all-elements',
                default_visual_scope TEXT NOT NULL DEFAULT 'all-elements',
                character_appearance_policy TEXT NOT NULL DEFAULT 'follow-original-outfit',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                episode_number INTEGER NOT NULL, title TEXT NOT NULL,
                video_filename TEXT, video_path TEXT, duration REAL,
                width INTEGER, height INTEGER, aspect_ratio REAL, orientation TEXT,
                thumbnail_path TEXT, file_size INTEGER, analysis_status TEXT NOT NULL DEFAULT 'not_started',
                source_language TEXT, status TEXT NOT NULL DEFAULT 'Draft',
                character_handling TEXT NOT NULL DEFAULT 'use-project-library',
                settings_override TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id, episode_number)
            );
            CREATE TABLE IF NOT EXISTS episode_batch_jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                job_type TEXT NOT NULL, strategy TEXT NOT NULL DEFAULT 'selected',
                status TEXT NOT NULL DEFAULT 'queued', total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS episode_batch_items (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES episode_batch_jobs(id) ON DELETE CASCADE,
                episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
                filename TEXT, item_order INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0, error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS characters (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL, gender TEXT, estimated_age TEXT, role TEXT,
                appearance_description TEXT, hair_description TEXT, headwear_description TEXT,
                clothing_description TEXT, original_reference_image TEXT,
                front_view_image TEXT, angle_view_image TEXT, side_view_image TEXT,
                full_body_image TEXT, voice_id TEXT, voice_settings TEXT NOT NULL DEFAULT '{}',
                confirmed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS character_looks (
                id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', reference_image TEXT,
                clothing_description TEXT NOT NULL DEFAULT '', hair_override TEXT NOT NULL DEFAULT '',
                headwear_description TEXT NOT NULL DEFAULT '', source_episode_id TEXT,
                is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_profiles (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL, source_type TEXT NOT NULL, provider_id TEXT,
                model_id TEXT, external_voice_id TEXT, uploaded_audio_path TEXT,
                base_voice TEXT, voice_age TEXT, voice_style TEXT,
                speech_speed REAL NOT NULL DEFAULT 1, pitch REAL NOT NULL DEFAULT 0,
                emotion_strength INTEGER NOT NULL DEFAULT 60, status TEXT NOT NULL DEFAULT 'Mock Only',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS episode_character_matches (
                id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
                detected_name TEXT NOT NULL, match_status TEXT NOT NULL DEFAULT 'mock-match',
                confidence REAL NOT NULL DEFAULT 0.92, use_project_asset INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS episode_character_candidates (
                id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                detected_name TEXT NOT NULL, entity_type TEXT NOT NULL DEFAULT 'Human',
                status TEXT NOT NULL DEFAULT 'candidate', character_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
                confidence REAL NOT NULL DEFAULT 0, source_index INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS character_reference_images (
                id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL, image_path TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual_upload',
                source_episode_id TEXT REFERENCES episodes(id) ON DELETE SET NULL,
                source_timestamp REAL, is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(character_id, field_name)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_character_reference_primary
            ON character_reference_images(character_id) WHERE is_primary=1;
            CREATE TABLE IF NOT EXISTS mock_results (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                episode_id TEXT REFERENCES episodes(id) ON DELETE CASCADE,
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'processing', original_video_path TEXT,
                generated_video_path TEXT, mock_notice TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL, location TEXT, description TEXT, main_objects TEXT, reference_image TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                style_id TEXT NOT NULL, source_language TEXT NOT NULL, target_language TEXT NOT NULL,
                language_mode TEXT NOT NULL, character_consistency TEXT NOT NULL,
                transformation_strength TEXT NOT NULL, scene_processing_mode TEXT NOT NULL,
                subtitle_enabled INTEGER NOT NULL DEFAULT 1, lip_sync_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                episode_id TEXT,
                status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, current_step TEXT,
                error_message TEXT, started_at TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                step_index INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, error_message TEXT
            );
            """
        )
        apply_stage_3a1_migration(db)
        apply_stage_3a1_1_migration(db)
        apply_stage_3a2_1_migration(db)
        apply_stage_3a_workflow_fixes(db)
        apply_stage_3a_workflow_recovery(db)
        apply_stage_3b1_migration(db)
        apply_stage_3b1_workflow_migration(db)
        apply_stage_3b1_xmax_realtime_migration(db)
        apply_stage_3b1_character_reference_bindings(db)
        apply_stage_3c_task_state_migration(db)
        apply_stage_3b1_xmax_start_gate_migration(db)
        apply_stage_3b1_xmax_session_confirmation_migration(db)
        apply_stage_3b1_xmax_attempt_audit_migration(db)
        apply_stage_3b1_xmax_attempt_audit_v2_migration(db)
        apply_stage_3b1_xmax_attempt_audit_v3_migration(db)
        apply_stage_3b1_xmax_single_shot_migration(db)
        apply_stage_3b1_xmax_thirty_second_migration(db)
        apply_stage_3b1_xmax_thirty_second_lifecycle_migration(db)
        apply_stage_3b1_xmax_thirty_second_merge_migration(db)
        apply_stage_3b1_xmax_thirty_second_scene_mapping_migration(db)
        apply_stage_3b1_xmax_thirty_second_quality_migration(db)
        apply_stage_3b1_xmax_thirty_second_v11_quality_migration(db)
        apply_stage_3a2_demucs_localization_migration(db)
        apply_stage_3a2_tts_mix_revision_migration(db)
        apply_stage_3d_overseas_episode_migration(db)
        apply_beta_p1_auth_isolation_migration(db)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credit_wallets'").fetchone():
            apply_beta_p3_xmax_temporary_keys_migration(db)
            apply_beta_p4_browser_xmax_attempts_migration(db)
            apply_beta_p5_xmax_offline_attempts_migration(db)
            apply_beta_self_service_closure_migration(db)
        _add_columns(db, "projects", {
            "video_width": "INTEGER", "video_height": "INTEGER", "aspect_ratio": "REAL", "orientation": "TEXT",
            "processing_mode": "TEXT NOT NULL DEFAULT 'quick-transformation'", "active_episode_id": "TEXT",
            "default_style_id": "TEXT", "default_source_language": "TEXT", "default_target_language": "TEXT",
            "default_language_mode": "TEXT", "default_subtitle_enabled": "INTEGER NOT NULL DEFAULT 1",
            "default_lip_sync_enabled": "INTEGER NOT NULL DEFAULT 1", "use_character_library": "INTEGER NOT NULL DEFAULT 0",
            "default_character_consistency": "TEXT NOT NULL DEFAULT 'strong'", "default_transformation_strength": "TEXT NOT NULL DEFAULT 'medium'",
            "default_scene_processing_mode": "TEXT NOT NULL DEFAULT 'all-elements'", "default_visual_scope": "TEXT NOT NULL DEFAULT 'all-elements'",
            "character_appearance_policy": "TEXT NOT NULL DEFAULT 'follow-original-outfit'",
        })
        _add_columns(db, "episodes", {
            "thumbnail_path": "TEXT", "file_size": "INTEGER", "analysis_status": "TEXT NOT NULL DEFAULT 'not_started'",
            "sort_order": "INTEGER", "upload_status": "TEXT NOT NULL DEFAULT 'not_uploaded'",
            "upload_error": "TEXT", "analysis_error": "TEXT", "conflict_count": "INTEGER NOT NULL DEFAULT 0",
        })
        _add_columns(db, "tasks", {"episode_id": "TEXT"})
        _add_columns(db, "characters", {
            "character_code": "TEXT", "character_type": "TEXT NOT NULL DEFAULT 'Human'", "primary_reference_type": "TEXT",
            "priority_level": "TEXT NOT NULL DEFAULT 'Temporary'",
            "identity_description": "TEXT", "face_description": "TEXT", "body_description": "TEXT",
            "default_clothing_description": "TEXT", "voice_profile_id": "TEXT", "source": "TEXT NOT NULL DEFAULT 'mock-analysis'",
            "created_from_episode_id": "TEXT", "created_at": "TEXT", "updated_at": "TEXT",
            "entity_type": "TEXT NOT NULL DEFAULT 'Human'", "optional_reference_images": "TEXT NOT NULL DEFAULT '[]'",
            "styled_front_view_image": "TEXT", "styled_angle_view_image": "TEXT",
            "styled_side_view_image": "TEXT", "styled_full_body_image": "TEXT",
            "styled_asset_status": "TEXT NOT NULL DEFAULT 'not_generated'",
            "confirmation_status": "TEXT NOT NULL DEFAULT 'candidate'",
            "source_reference_image": "TEXT", "main_reference_image": "TEXT", "confirmed_at": "TEXT",
        })
        _add_columns(db, "episode_character_candidates", {
            "representative_frame": "TEXT", "representative_timestamp": "REAL NOT NULL DEFAULT 0",
            "occurrence_frames": "TEXT NOT NULL DEFAULT '[]'", "occurrence_timestamps": "TEXT NOT NULL DEFAULT '[]'",
            "bounding_box": "TEXT NOT NULL DEFAULT '{}'",
        })
        _add_columns(db, "settings", {"visual_scope": "TEXT NOT NULL DEFAULT 'all-elements'", "character_appearance_policy": "TEXT NOT NULL DEFAULT 'follow-original-outfit'"})
        db.execute("UPDATE projects SET processing_mode=COALESCE(processing_mode, 'quick-transformation'), default_style_id=COALESCE(default_style_id, selected_style), default_source_language=COALESCE(default_source_language, source_language), default_target_language=COALESCE(default_target_language, target_language), default_language_mode=COALESCE(default_language_mode, 'translate-dub'), default_subtitle_enabled=COALESCE(default_subtitle_enabled, subtitle_enabled), default_lip_sync_enabled=COALESCE(default_lip_sync_enabled, lip_sync_enabled), use_character_library=CASE WHEN processing_mode='character-consistency' THEN 1 ELSE COALESCE(use_character_library, 0) END, default_visual_scope=COALESCE(default_visual_scope, 'all-elements'), character_appearance_policy=COALESCE(character_appearance_policy, 'follow-original-outfit')")
        timestamp = now_iso()
        db.execute("UPDATE episodes SET sort_order=episode_number WHERE sort_order IS NULL")
        db.execute("UPDATE episodes SET upload_status=CASE WHEN video_path IS NOT NULL AND video_path <> '' THEN 'completed' ELSE 'not_uploaded' END WHERE upload_status IS NULL OR upload_status='not_uploaded'")
        db.execute("UPDATE projects SET active_episode_id=NULL WHERE active_episode_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM episodes WHERE episodes.id=projects.active_episode_id)")
        db.execute("UPDATE episodes SET analysis_status='completed' WHERE status IN ('Character Review', 'Ready', 'Completed') AND (analysis_status IS NULL OR analysis_status='not_started')")
        db.execute("UPDATE characters SET confirmation_status='confirmed' WHERE confirmed=1 OR priority_level='Locked'")
        characters = db.execute("SELECT * FROM characters ORDER BY rowid").fetchall()
        counters: dict[str, int] = {}
        for character in characters:
            counters[character["project_id"]] = counters.get(character["project_id"], 0) + 1
            code = character["character_code"] or f"C{counters[character['project_id']]:02d}"
            db.execute("UPDATE characters SET character_code=?, primary_reference_type=COALESCE(primary_reference_type, CASE WHEN original_reference_image IS NOT NULL AND original_reference_image <> '' THEN 'original_reference_image' WHEN front_view_image IS NOT NULL AND front_view_image <> '' THEN 'front_view_image' WHEN angle_view_image IS NOT NULL AND angle_view_image <> '' THEN 'angle_view_image' WHEN side_view_image IS NOT NULL AND side_view_image <> '' THEN 'side_view_image' WHEN full_body_image IS NOT NULL AND full_body_image <> '' THEN 'full_body_image' END), identity_description=COALESCE(identity_description, appearance_description), face_description=COALESCE(face_description, appearance_description), body_description=COALESCE(body_description, ''), default_clothing_description=COALESCE(default_clothing_description, clothing_description), created_at=COALESCE(created_at, ?), updated_at=COALESCE(updated_at, ?) WHERE id=?", (code, character["created_at"] or timestamp, character["updated_at"] or timestamp, character["id"]))
        reference_dir = UPLOADS_DIR / "character-references"
        reference_dir.mkdir(exist_ok=True)
        reference_fields = ("original_reference_image", "front_view_image", "angle_view_image", "side_view_image", "full_body_image")
        for character in characters:
            valid: list[tuple[str, str]] = []
            for field_name in reference_fields:
                value = character[field_name]
                image_path: str | None = None
                if isinstance(value, str) and value.startswith("data:image/"):
                    try:
                        header, encoded = value.split(",", 1)
                        extension = "png" if "png" in header.lower() else "jpg"
                        target = reference_dir / f"{character['id']}-{field_name}.{extension}"
                        target.write_bytes(base64.b64decode(encoded))
                        image_path = f"/uploads/character-references/{target.name}"
                        db.execute(f"UPDATE characters SET {field_name}=? WHERE id=?", (image_path, character["id"]))
                    except (ValueError, base64.binascii.Error):
                        image_path = None
                elif isinstance(value, str) and value.startswith("/uploads/"):
                    target = ROOT / value.lstrip("/")
                    if target.is_file():
                        image_path = value
                elif isinstance(value, str):
                    target = Path(value)
                    try:
                        if target.is_file() and target.resolve().is_relative_to(UPLOADS_DIR.resolve()):
                            image_path = f"/uploads/{target.resolve().relative_to(UPLOADS_DIR.resolve()).as_posix()}"
                            db.execute(f"UPDATE characters SET {field_name}=? WHERE id=?", (image_path, character["id"]))
                    except (OSError, ValueError):
                        image_path = None
                if image_path:
                    valid.append((field_name, image_path))
                    db.execute("INSERT OR IGNORE INTO character_reference_images (id, character_id, field_name, image_path, source_type, source_episode_id, is_primary, created_at, updated_at) VALUES (?, ?, ?, ?, 'legacy_migration', ?, 0, ?, ?)", (f"ref-{character['id']}-{field_name}", character["id"], field_name, image_path, character["created_from_episode_id"], timestamp, timestamp))
            preferred = character["primary_reference_type"]
            primary = next(((field_name, image_path) for field_name, image_path in valid if field_name == preferred), valid[0] if valid else None)
            db.execute("UPDATE character_reference_images SET is_primary=0 WHERE character_id=?", (character["id"],))
            if primary:
                db.execute("UPDATE character_reference_images SET is_primary=1, updated_at=? WHERE character_id=? AND field_name=?", (timestamp, character["id"], primary[0]))
                source = next((path for field_name, path in valid if field_name == "original_reference_image"), valid[0][1])
                db.execute("UPDATE characters SET primary_reference_type=?, source_reference_image=?, main_reference_image=? WHERE id=?", (primary[0], source, primary[1], character["id"]))
            else:
                db.execute("UPDATE characters SET primary_reference_type=NULL, source_reference_image=NULL, main_reference_image=NULL WHERE id=?", (character["id"],))
        completed_tasks = db.execute("SELECT tasks.id, tasks.project_id, tasks.episode_id, tasks.completed_at, projects.video_path AS project_video_path, episodes.video_path AS episode_video_path FROM tasks JOIN projects ON projects.id=tasks.project_id LEFT JOIN episodes ON episodes.id=tasks.episode_id WHERE tasks.status='completed' AND NOT EXISTS (SELECT 1 FROM mock_results WHERE mock_results.task_id=tasks.id)").fetchall()
        for task in completed_tasks:
            video_path = task["episode_video_path"] or task["project_video_path"]
            db.execute("INSERT INTO mock_results (id, project_id, episode_id, task_id, status, original_video_path, generated_video_path, mock_notice, created_at, updated_at) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?)", (f"result-{task['id']}", task["project_id"], task["episode_id"], task["id"], video_path, video_path, "Backfilled Mock result; the original video is reused and no real AI transformation was performed.", task["completed_at"] or timestamp, task["completed_at"] or timestamp))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def bool_int(value: bool) -> int:
    return 1 if value else 0
