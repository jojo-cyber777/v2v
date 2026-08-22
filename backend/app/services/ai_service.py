"""Replaceable Mock AI service contracts for the transformation pipeline."""

from __future__ import annotations

from typing import Any


class MockAIError(RuntimeError):
    """Raised when a mock AI operation cannot accept its input."""


def _result(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"service": name, "mock": True, "payload": payload}


def validate_video(video_path: str | None) -> dict[str, Any]:
    """Validate a local video file. TODO: inspect codec, duration, and frame integrity."""
    if not video_path:
        raise MockAIError("A video path is required for validation")
    return _result("validate_video", {"valid": True, "format": "mp4-or-mov", "duration_limit": "under-3-minutes"})


def detect_source_language(video_path: str | None) -> dict[str, Any]:
    """Detect source language. TODO: connect a transcription provider."""
    if not video_path:
        raise MockAIError("A video path is required for language detection")
    return _result("detect_source_language", {"language": "Mandarin Chinese", "confidence": 0.94})


def extract_audio(video_path: str | None) -> dict[str, Any]:
    """Extract audio. TODO: use FFmpeg for real extraction."""
    if not video_path:
        raise MockAIError("A video path is required for audio extraction")
    return _result("extract_audio", {"audio_path": "mock-assets/audio-track.wav", "duration": 47})


def extract_keyframes(video_path: str | None) -> dict[str, Any]:
    """Extract keyframes. TODO: extract real frames with FFmpeg."""
    if not video_path:
        raise MockAIError("A video path is required for keyframe extraction")
    return _result("extract_keyframes", {"frames": ["frame-01", "frame-02", "frame-03"], "count": 3})


def analyze_video(video_path: str | None) -> dict[str, Any]:
    """Analyze video. TODO: orchestrate real vision and audio analysis."""
    if not video_path:
        raise MockAIError("A video path is required for video analysis")
    return _result("analyze_video", {"duration": 47, "shot_count": 8, "detected_language": "Mandarin Chinese"})


def extract_characters(video_path: str | None) -> list[dict[str, Any]]:
    """Extract characters. TODO: replace with multi-frame character tracking."""
    if not video_path:
        raise MockAIError("A video path is required for character extraction")
    return [
        {"id": "char-lin", "name": "Lin Yue", "gender": "Female", "estimated_age": "Late 20s", "role": "Lead", "appearance_description": "Sharp eyes, oval face, composed presence", "hair_description": "Shoulder-length dark hair with a soft wave", "headwear_description": "None", "clothing_description": "Ivory blouse with a charcoal tailored coat", "appearance_time": "00:03 - 00:42", "original_reference_image": "mock-character-lin", "voice_id": "voice-lin-01", "voice_settings": {"base_voice": "Female", "voice_age": "Young Adult", "voice_style": "Warm", "speech_speed": 1.0, "pitch": 0, "emotion_strength": 65}},
        {"id": "char-chen", "name": "Chen Hao", "gender": "Male", "estimated_age": "Early 30s", "role": "Supporting", "appearance_description": "Long face, defined brow, quiet intensity", "hair_description": "Short textured black hair, neat sides", "headwear_description": "None", "clothing_description": "Black crewneck under a sand-colored overshirt", "appearance_time": "00:12 - 00:39", "original_reference_image": "mock-character-chen", "voice_id": "voice-chen-01", "voice_settings": {"base_voice": "Male", "voice_age": "Young Adult", "voice_style": "Deep", "speech_speed": 0.96, "pitch": -2, "emotion_strength": 58}},
    ]


def extract_scenes(video_path: str | None) -> list[dict[str, Any]]:
    """Extract scenes. TODO: replace with shot boundary understanding."""
    if not video_path:
        raise MockAIError("A video path is required for scene extraction")
    return [
        {"id": "scene-cafe", "name": "Night Cafe", "location": "Interior / Cafe", "description": "A quiet corner table under warm pendant lights.", "main_objects": ["coffee cup", "window", "wooden table"], "reference_image": "mock-scene-cafe"},
        {"id": "scene-street", "name": "Rainy Street", "location": "Exterior / Downtown", "description": "Wet pavement, neon signage, and passing headlights frame the exchange.", "main_objects": ["neon sign", "umbrella", "taxi", "street lamp"], "reference_image": "mock-scene-street"},
    ]


def generate_character_assets(character: dict[str, Any]) -> dict[str, Any]:
    """Generate multi-view assets. TODO: call a character sheet API."""
    if not character.get("id"):
        raise MockAIError("Character id is required")
    base = character["id"]
    return _result("generate_character_assets", {"front": f"{base}-front", "angle": f"{base}-angle", "side": f"{base}-side", "full_body": f"{base}-full-body"})


def segment_video(video_path: str | None) -> dict[str, Any]:
    """Segment a video. TODO: use shot boundaries and model context limits."""
    if not video_path:
        raise MockAIError("A video path is required for segmentation")
    return _result("segment_video", {"segments": [{"id": "seg-01", "start": 0, "end": 23}, {"id": "seg-02", "start": 23, "end": 47}]})


def transform_character_in_segment(segment: dict[str, Any], style: str) -> dict[str, Any]:
    """Transform character pixels. TODO: replace with a temporal model."""
    return _result("transform_character_in_segment", {"segment_id": segment.get("id"), "style": style})


def transform_scene_in_segment(segment: dict[str, Any], style: str) -> dict[str, Any]:
    """Transform scene pixels. TODO: replace with a temporal scene model."""
    return _result("transform_scene_in_segment", {"segment_id": segment.get("id"), "style": style})


def transform_objects_in_segment(segment: dict[str, Any], style: str) -> dict[str, Any]:
    """Transform objects and wardrobe. TODO: replace with object-level controls."""
    return _result("transform_objects_in_segment", {"segment_id": segment.get("id"), "style": style})


def translate_dialogue(source_language: str, target_language: str) -> dict[str, Any]:
    """Translate dialogue. TODO: preserve timed dialogue with a translation API."""
    return _result("translate_dialogue", {"source": source_language, "target": target_language, "segments": 12})


def generate_character_voice(character: dict[str, Any], target_language: str) -> dict[str, Any]:
    """Generate character voice. TODO: connect a TTS or cloning provider."""
    return _result("generate_character_voice", {"character_id": character.get("id"), "voice_id": character.get("voice_id"), "language": target_language})


def run_lip_sync(video_path: str | None, enabled: bool) -> dict[str, Any]:
    """Run lip sync. TODO: connect a real lip-sync API."""
    return _result("run_lip_sync", {"enabled": enabled, "status": "mock-complete" if enabled else "skipped"})


def generate_subtitles(target_language: str, enabled: bool) -> dict[str, Any]:
    """Generate subtitles. TODO: render VTT/SRT from timed dialogue."""
    return _result("generate_subtitles", {"enabled": enabled, "language": target_language, "format": "vtt"})


def render_video_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """Render one segment. TODO: render with FFmpeg."""
    return _result("render_video_segment", {"segment_id": segment.get("id"), "path": "mock-assets/rendered-segment.mp4"})


def merge_video_segments(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge segments. TODO: concatenate rendered segments."""
    return _result("merge_video_segments", {"segment_count": len(segments), "path": "mock-assets/generated-result.mp4"})


def align_audio_video(video_path: str | None) -> dict[str, Any]:
    """Align audio and video. TODO: perform real muxing and timing alignment."""
    return _result("align_audio_video", {"video_path": video_path, "aligned": True})


def quality_check(result_path: str) -> dict[str, Any]:
    """Run quality checks. TODO: add frame, audio, and subtitle validation."""
    return _result("quality_check", {"result_path": result_path, "passed": True, "score": 96})


def export_final_video(result_path: str) -> dict[str, Any]:
    """Export final local result. TODO: export a newly rendered video artifact."""
    return _result("export_final_video", {"result_path": result_path, "label": "Demo Generated Result"})


def detect_episode_characters(video_path: str | None) -> list[dict[str, Any]]:
    """Round-two contract for episode-level character detection."""
    if not video_path:
        raise MockAIError("A video path is required for episode character detection")
    return extract_characters(video_path)


def match_characters_to_project_library(detected: list[dict[str, Any]], library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"detected_name": item.get("name"), "character_id": library[index].get("id") if index < len(library) else None, "confidence": 0.94 - index * 0.03, "mock": True} for index, item in enumerate(detected)]


def extract_character_reference_set(video_path: str | None) -> dict[str, Any]:
    if not video_path:
        raise MockAIError("A video path is required for reference extraction")
    return _result("extract_character_reference_set", {"assets": ["original-frame", "front-view", "three-quarter-view", "side-view"], "mock": True})


def generate_character_reference_set(character: dict[str, Any]) -> dict[str, Any]:
    return _result("generate_character_reference_set", {"character_id": character.get("id"), "mock_assets": True})


def detect_new_characters(detected: list[dict[str, Any]], library: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return detected[len(library):] if len(detected) > len(library) else []


def classify_character_priority(character: dict[str, Any]) -> dict[str, Any]:
    return _result("classify_character_priority", {"character_id": character.get("id"), "priority_level": "Temporary", "mock": True})


def create_character_look(character_id: str, name: str) -> dict[str, Any]:
    return _result("create_character_look", {"character_id": character_id, "name": name, "mock": True})


def reuse_character_assets(character_id: str, episode_id: str) -> dict[str, Any]:
    return _result("reuse_character_assets", {"character_id": character_id, "episode_id": episode_id, "mock": True})


def list_voice_providers() -> list[dict[str, Any]]:
    return [{"id": "none", "name": "No provider connected", "mock": True}]


def list_voice_models(provider_id: str) -> list[dict[str, Any]]:
    return [] if provider_id == "none" else [{"id": "mock-tts-v1", "name": "Mock TTS Model", "mock": True}]


def list_provider_voices(provider_id: str, model_id: str) -> list[dict[str, Any]]:
    return [] if provider_id == "none" else [{"id": "mock-voice-01", "name": "Mock Provider Voice", "model_id": model_id, "mock": True}]


def upload_voice_sample(sample_path: str) -> dict[str, Any]:
    return _result("upload_voice_sample", {"sample_path": sample_path, "status": "Sample Uploaded / Not Trained / Mock Only"})


def preview_voice(profile_id: str) -> dict[str, Any]:
    return _result("preview_voice", {"profile_id": profile_id, "audio": "local-mock-preview"})


def synthesize_speech(profile_id: str, text: str) -> dict[str, Any]:
    return _result("synthesize_speech", {"profile_id": profile_id, "text": text, "audio": "mock-audio"})


def clone_voice(sample_path: str) -> dict[str, Any]:
    return _result("clone_voice", {"sample_path": sample_path, "status": "Coming Soon", "mock": True})
