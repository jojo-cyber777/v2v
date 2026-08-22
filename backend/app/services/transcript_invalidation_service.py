"""Single-transaction invalidation for assets derived from a transcript."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence


FaultInjector = Callable[[str], None]


def invalidate_after_transcript_change(
    db: sqlite3.Connection,
    *,
    episode_id: str,
    transcript_document_id: str,
    segment_ids: Sequence[str],
    timestamp: str,
    reason: str = "Transcript changed; regenerate dependent assets.",
    fault_injector: FaultInjector | None = None,
) -> dict[str, int]:
    """Mark only transcript-derived assets stale while preserving media and visual data."""

    counts: dict[str, int] = {}
    cursor = db.execute(
        "UPDATE translation_documents SET status='stale', error_message=?, updated_at=? "
        "WHERE transcript_document_id=? AND status!='stale'",
        (reason, timestamp, transcript_document_id),
    )
    counts["translation_documents"] = cursor.rowcount
    if segment_ids:
        placeholders = ",".join("?" for _ in segment_ids)
        cursor = db.execute(
            f"UPDATE voice_segment_outputs SET status='stale', error_code='TRANSCRIPT_CHANGED', "
            f"error_message=?, updated_at=? WHERE episode_id=? AND transcript_segment_id IN ({placeholders}) "
            "AND status!='stale'",
            (reason, timestamp, episode_id, *segment_ids),
        )
        counts["voice_segment_outputs"] = cursor.rowcount
    else:
        counts["voice_segment_outputs"] = 0
    cursor = db.execute(
        "UPDATE dubbed_outputs SET status='stale', error_code='TRANSCRIPT_CHANGED', error_message=?, updated_at=? "
        "WHERE episode_id=? AND status!='stale'",
        (reason, timestamp, episode_id),
    )
    counts["dubbed_outputs"] = cursor.rowcount
    cursor = db.execute(
        "UPDATE subtitle_outputs SET status='stale' WHERE episode_id=? "
        "AND output_type IN ('translated_subtitles','translated_subtitle_preview','subtitle_preview') "
        "AND status!='stale'",
        (episode_id,),
    )
    counts["subtitle_outputs"] = cursor.rowcount
    cursor = db.execute(
        "UPDATE subtitle_styles SET preview_stale=1, preview_stale_reason=?, updated_at=? WHERE episode_id=?",
        (reason, timestamp, episode_id),
    )
    counts["subtitle_styles"] = cursor.rowcount
    cursor = db.execute(
        "UPDATE audio_localization_tasks SET status='failed', error_code='TRANSCRIPT_CHANGED', "
        "error_message=?, progress_message=?, updated_at=? WHERE episode_id=? AND status!='failed'",
        (reason, reason, timestamp, episode_id),
    )
    counts["audio_localization_tasks"] = cursor.rowcount
    cursor = db.execute(
        "UPDATE overseas_episode_tasks SET status='failed', stage='failed', error_code='TRANSCRIPT_CHANGED', "
        "error_message=?, progress_message=?, updated_at=? WHERE episode_id=? AND status!='failed'",
        (reason, reason, timestamp, episode_id),
    )
    counts["overseas_episode_tasks"] = cursor.rowcount
    if fault_injector:
        fault_injector("after_stale_invalidation")
    return counts

