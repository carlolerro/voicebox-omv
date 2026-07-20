"""Strict persistent rendering for MCP-created Stories.

The existing Story mixer remains the single implementation of timeline mixing.
This module adds a strict preflight and durable, atomic storage for workflows
that must not silently omit failed or missing clips.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .. import config
from ..database import Generation as DBGeneration
from ..database import GenerationVersion as DBGenerationVersion
from ..database import Story as DBStory
from ..database import StoryItem as DBStoryItem
from ..utils.audio import load_audio
from . import stories

_SAFE_STORY_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _expected_render_path(story_id: str) -> Path:
    """Return the only filesystem path a persisted Story render may use."""
    if not story_id or not _SAFE_STORY_ID.fullmatch(story_id):
        raise ValueError("Story id cannot be used as a storage directory")
    return (config.get_stories_dir() / story_id / "story.wav").resolve()


async def validate_render_inputs(story_id: str, db: Session) -> None:
    """Require every Story item to reference completed, readable audio."""
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story is None:
        raise ValueError(f"Story '{story_id}' was not found")

    items = (
        db.query(DBStoryItem, DBGeneration)
        .join(DBGeneration, DBStoryItem.generation_id == DBGeneration.id)
        .filter(DBStoryItem.story_id == story_id)
        .order_by(DBStoryItem.start_time_ms)
        .all()
    )
    if not items:
        raise ValueError("Story has no audio items")

    for item, generation in items:
        if generation.status != "completed":
            raise ValueError(
                f"Story segment generation is not completed: {generation.id}"
            )

        stored_path = generation.audio_path
        if item.version_id:
            version = (
                db.query(DBGenerationVersion)
                .filter_by(id=item.version_id, generation_id=generation.id)
                .first()
            )
            if version is None:
                raise ValueError(
                    f"Story segment version is missing: {item.version_id}"
                )
            stored_path = version.audio_path

        audio_path = config.resolve_storage_path(stored_path)
        if audio_path is None or not audio_path.is_file():
            raise ValueError(
                f"Story segment audio is missing or unreadable: {generation.id}"
            )

        try:
            await asyncio.to_thread(
                load_audio,
                str(audio_path),
                sample_rate=24_000,
            )
        except Exception as exc:
            raise ValueError(
                f"Story segment audio is missing or unreadable: {generation.id}"
            ) from exc


async def render_story_persistent(story_id: str, db: Session) -> str:
    """Render a complete Story and atomically persist its final WAV.

    Returns a storage-relative path suitable for the database and HTTP export.
    """
    await validate_render_inputs(story_id, db)

    audio_bytes = await stories.export_story_audio(story_id, db)
    if not audio_bytes:
        raise ValueError("Story mixer produced no audio")

    story = db.query(DBStory).filter_by(id=story_id).first()
    if story is None:
        raise ValueError(f"Story '{story_id}' was not found")

    final_path = _expected_render_path(story_id)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_name("story.wav.tmp")

    try:
        with temporary_path.open("wb") as output:
            output.write(audio_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, final_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass

    stored_path = config.to_storage_path(final_path)
    story.render_audio_path = stored_path
    story.rendered_at = datetime.utcnow()
    story.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(story)
    return stored_path


def resolve_valid_render_path(story: DBStory) -> Path | None:
    """Resolve a persisted render only inside its fixed Story directory."""
    try:
        expected = _expected_render_path(story.id)
    except ValueError:
        return None
    path = config.resolve_storage_path(story.render_audio_path)
    if path is None:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved == expected and resolved.is_file() else None


def remove_persistent_render(story: DBStory) -> None:
    """Best-effort removal of a Story render plus metadata invalidation."""
    try:
        expected = _expected_render_path(story.id)
    except ValueError:
        expected = None
    path = config.resolve_storage_path(story.render_audio_path)
    if path is not None and expected is not None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = None
        if resolved == expected:
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                resolved.parent.rmdir()
            except OSError:
                pass
    story.render_audio_path = None
    story.rendered_at = None


def invalidate_story_render(story: DBStory, db: Session) -> None:
    """Invalidate a stale render after a successful manual timeline edit."""
    remove_persistent_render(story)
    if story.status == "completed":
        story.status = "draft"
        story.error = None
        story.current_segment_index = None
        story.failed_segment_index = None
    story.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(story)
