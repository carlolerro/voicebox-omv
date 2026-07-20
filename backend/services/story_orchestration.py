"""Persistent orchestration for MCP-created multi-voice Stories.

The existing Generation route, serial TTS queue, Story timeline service, and
Story mixer remain the source of truth. This module only coordinates those
pieces and persists enough state to stop, inspect, and resume safely.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import Generation as DBGeneration
from ..database import ProfileSample as DBProfileSample
from ..database import Story as DBStory
from ..database import StoryItem as DBStoryItem
from ..database import StorySegment as DBStorySegment
from ..database import VoiceProfile as DBVoiceProfile
from ..database import get_db
from ..routes import generations as generation_routes
from . import preset_voices as preset_voices_service
from . import profiles as profiles_service
from . import stories as stories_service
from . import story_rendering
from .task_queue import create_background_task

logger = logging.getLogger(__name__)

ACTIVE_STORY_STATUSES = {"queued", "generating", "rendering"}
ACTIVE_GENERATION_STATUSES = {"generating", "loading_model"}
TERMINAL_GENERATION_STATUSES = {"completed", "failed"}
POLL_INTERVAL_SECONDS = 0.5
MAX_STORY_ERROR_CHARS = 500
MAX_STORY_SEGMENTS = 100
MAX_STORY_SEGMENT_CHARS = 10_000
MAX_STORY_TOTAL_CHARS = 100_000
MAX_STORY_TITLE_CHARS = 100
MAX_STORY_DESCRIPTION_CHARS = 500
MAX_STORY_PROFILE_REF_CHARS = 100

_active_story_ids: set[str] = set()
_story_tasks: dict[str, asyncio.Task] = {}


@dataclass(frozen=True)
class StorySegmentSpec:
    profile: str
    text: str


@contextmanager
def _session_scope() -> Iterator[Session]:
    dependency = get_db()
    db = next(dependency)
    try:
        yield db
    finally:
        try:
            dependency.close()
        except Exception:
            db.close()


def resolve_story_profile(value: str, db: Session) -> DBVoiceProfile:
    """Resolve an exact profile id or one unambiguous case-insensitive name."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("profile must not be empty")
    if len(candidate) > MAX_STORY_PROFILE_REF_CHARS:
        raise ValueError(
            f"profile cannot exceed {MAX_STORY_PROFILE_REF_CHARS} characters"
        )

    exact = (
        db.query(DBVoiceProfile)
        .filter(DBVoiceProfile.id == candidate)
        .first()
    )
    if exact is not None:
        return exact

    matches = (
        db.query(DBVoiceProfile)
        .filter(func.lower(DBVoiceProfile.name) == candidate.lower())
        .all()
    )
    if not matches:
        raise ValueError(f"Voice profile '{candidate}' was not found.")
    if len(matches) != 1:
        raise ValueError(f"Voice profile name '{candidate}' is ambiguous.")
    return matches[0]


def resolve_story_engine(profile: DBVoiceProfile) -> str:
    """Use the normal Generation engine precedence without the qwen default."""
    request = models.GenerationRequest(
        profile_id=profile.id,
        text="validation",
        language=profile.language or "en",
        engine=None,
    )
    return generation_routes._resolve_generation_engine(request, profile)


def validate_story_profile(profile: DBVoiceProfile, db: Session) -> None:
    """Require a ready preset or cloned profile for Story generation."""
    voice_type = getattr(profile, "voice_type", None) or "cloned"
    if voice_type not in {"preset", "cloned"}:
        raise ValueError(
            f"Voice profile '{profile.name}' type '{voice_type}' is not supported "
            "by MCP Story mode."
        )

    engine = resolve_story_engine(profile)
    profiles_service.validate_profile_engine(profile, engine)

    if voice_type == "preset":
        preset_engine = getattr(profile, "preset_engine", None)
        preset_voice_id = getattr(profile, "preset_voice_id", None)
        try:
            valid_ids = preset_voices_service.get_preset_voice_ids(
                preset_engine or ""
            )
        except ValueError as exc:
            raise ValueError(
                f"Voice profile '{profile.name}' is not ready for generation."
            ) from exc
        if not preset_voice_id or preset_voice_id not in valid_ids:
            raise ValueError(
                f"Voice profile '{profile.name}' is not ready for generation."
            )
        return

    sample_count = (
        db.query(func.count(DBProfileSample.id))
        .filter(DBProfileSample.profile_id == profile.id)
        .scalar()
        or 0
    )
    if sample_count < 1:
        raise ValueError(
            f"Voice profile '{profile.name}' is not ready for generation: "
            "cloned profiles require at least one sample."
        )


def _clean_segment_spec(segment: StorySegmentSpec) -> StorySegmentSpec:
    profile = (segment.profile or "").strip()
    text = (segment.text or "").strip()
    if not profile:
        raise ValueError("profile must not be empty")
    if len(profile) > MAX_STORY_PROFILE_REF_CHARS:
        raise ValueError(
            f"profile cannot exceed {MAX_STORY_PROFILE_REF_CHARS} characters"
        )
    if not text:
        raise ValueError("Story segment text must not be empty")
    if len(text) > MAX_STORY_SEGMENT_CHARS:
        raise ValueError(
            f"Story segment cannot exceed {MAX_STORY_SEGMENT_CHARS} characters"
        )
    return StorySegmentSpec(profile=profile, text=text)


async def create_story_workflow(
    *,
    title: str,
    description: str | None,
    segments: list[StorySegmentSpec],
    db: Session,
) -> DBStory:
    """Validate the complete script, then persist one queued Story atomically."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Story title must not be empty")
    if len(clean_title) > MAX_STORY_TITLE_CHARS:
        raise ValueError(
            f"Story title cannot exceed {MAX_STORY_TITLE_CHARS} characters"
        )
    if not segments:
        raise ValueError("Story requires at least one segment")
    if len(segments) > MAX_STORY_SEGMENTS:
        raise ValueError(
            f"Story cannot contain more than {MAX_STORY_SEGMENTS} segments"
        )

    clean_description = (description or "").strip() or None
    if (
        clean_description
        and len(clean_description) > MAX_STORY_DESCRIPTION_CHARS
    ):
        raise ValueError(
            "Story description cannot exceed "
            f"{MAX_STORY_DESCRIPTION_CHARS} characters"
        )

    resolved: list[tuple[DBVoiceProfile, str]] = []
    total_chars = 0
    for raw_segment in segments:
        segment = _clean_segment_spec(raw_segment)
        total_chars += len(segment.text)
        if total_chars > MAX_STORY_TOTAL_CHARS:
            raise ValueError(
                f"Story text cannot exceed {MAX_STORY_TOTAL_CHARS} "
                "characters in total"
            )
        profile = resolve_story_profile(segment.profile, db)
        validate_story_profile(profile, db)
        resolved.append((profile, segment.text))

    now = datetime.utcnow()
    story = DBStory(
        id=str(uuid.uuid4()),
        name=clean_title,
        description=clean_description,
        status="queued",
        error=None,
        total_segments=len(resolved),
        completed_segments=0,
        current_segment_index=None,
        failed_segment_index=None,
        created_at=now,
        updated_at=now,
    )
    db.add(story)
    for position, (profile, text) in enumerate(resolved, start=1):
        db.add(
            DBStorySegment(
                id=str(uuid.uuid4()),
                story_id=story.id,
                position=position,
                profile_id=profile.id,
                text=text,
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()
    db.refresh(story)
    return story


def _safe_error(error: object) -> str:
    detail = getattr(error, "detail", None)
    text = str(detail if detail is not None else error)
    text = " ".join(text.split())
    if "[SQL:" in text:
        text = text.split("[SQL:", 1)[0].strip()
    text = re.sub(
        r"(?i)\b(token|secret|password|authorization|api[_-]?key)\b"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    # Do not expose local Unix or Windows filesystem paths through MCP status.
    text = re.sub(r"/(?:[^/\s]+/)+[^\s,;]*", "<path>", text)
    text = re.sub(
        r"[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])+[^\s,;]*",
        "<path>",
        text,
    )
    if not text:
        text = "Story processing failed"
    return text[:MAX_STORY_ERROR_CHARS]


def _generation_id(result: object) -> str:
    value = getattr(result, "id", None)
    if value is None and isinstance(result, dict):
        value = result.get("id")
    if not value:
        raise ValueError("Generation service returned no generation id")
    return str(value)


def _item_exists(db: Session, story_id: str, generation_id: str) -> bool:
    return (
        db.query(DBStoryItem)
        .filter_by(story_id=story_id, generation_id=generation_id)
        .first()
        is not None
    )


def _recompute_progress(story: DBStory, db: Session) -> int:
    db.flush()
    completed = (
        db.query(func.count(DBStorySegment.id))
        .filter(
            DBStorySegment.story_id == story.id,
            DBStorySegment.status == "completed",
        )
        .scalar()
        or 0
    )
    story.completed_segments = completed
    return completed


def _validate_story_ready_for_render(story: DBStory, db: Session) -> None:
    """Refuse to render an incomplete or structurally inconsistent script."""
    segments = (
        db.query(DBStorySegment)
        .filter_by(story_id=story.id)
        .order_by(DBStorySegment.position)
        .all()
    )
    if len(segments) != story.total_segments:
        raise ValueError(
            "Story segment count does not match its persisted workflow state"
        )
    if not segments:
        raise ValueError("Story has no segments")

    generation_ids: set[str] = set()
    for segment in segments:
        if segment.status != "completed":
            raise ValueError(
                f"Story segment {segment.position} is not completed"
            )
        if not segment.generation_id:
            raise ValueError(
                f"Story segment {segment.position} has no generation"
            )
        if segment.generation_id in generation_ids:
            raise ValueError("Multiple Story segments share one generation")
        generation_ids.add(segment.generation_id)
        if not _item_exists(db, story.id, segment.generation_id):
            raise ValueError(
                f"Story segment {segment.position} is not attached to the timeline"
            )

    completed = _recompute_progress(story, db)
    if completed != story.total_segments:
        raise ValueError("Story progress is incomplete")


async def _attach_completed_generation(
    story_id: str,
    segment_id: str,
    generation_id: str,
) -> None:
    with _session_scope() as db:
        story = db.query(DBStory).filter_by(id=story_id).first()
        segment = db.query(DBStorySegment).filter_by(id=segment_id).first()
        generation = db.query(DBGeneration).filter_by(id=generation_id).first()
        if story is None or segment is None or generation is None:
            raise ValueError("Story segment state disappeared during processing")
        if (generation.status or "completed") != "completed":
            raise ValueError(f"Generation {generation_id} did not complete")

        if not _item_exists(db, story_id, generation_id):
            item = await stories_service.add_item_to_story(
                story_id,
                models.StoryItemCreate(generation_id=generation_id),
                db,
            )
            if item is None:
                raise ValueError(
                    "Completed generation could not be attached to Story"
                )

        segment.status = "completed"
        segment.error = None
        segment.updated_at = datetime.utcnow()
        _recompute_progress(story, db)
        story.updated_at = datetime.utcnow()
        db.commit()


async def _wait_for_generation(generation_id: str) -> tuple[str, str | None]:
    while True:
        with _session_scope() as db:
            generation = db.query(DBGeneration).filter_by(
                id=generation_id
            ).first()
            if generation is None:
                return "failed", "Generation record disappeared during processing"
            status = generation.status or "completed"
            if status in TERMINAL_GENERATION_STATUSES:
                return status, generation.error
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _prepare_generation(story_id: str, segment_id: str) -> str:
    with _session_scope() as db:
        story = db.query(DBStory).filter_by(id=story_id).first()
        segment = db.query(DBStorySegment).filter_by(id=segment_id).first()
        if story is None or segment is None:
            raise ValueError("Story segment was not found")

        profile = db.query(DBVoiceProfile).filter_by(
            id=segment.profile_id
        ).first()
        if profile is None:
            raise ValueError("Story voice profile was deleted")
        validate_story_profile(profile, db)

        generation = None
        if segment.generation_id:
            generation = (
                db.query(DBGeneration)
                .filter_by(id=segment.generation_id)
                .first()
            )
            if generation is None:
                segment.generation_id = None

        story.status = "generating"
        story.current_segment_index = segment.position
        story.failed_segment_index = None
        story.error = None
        segment.status = "generating"
        segment.error = None
        segment.updated_at = datetime.utcnow()
        story.updated_at = datetime.utcnow()
        db.commit()

        if generation is not None:
            status = generation.status or "completed"
            if status == "completed" or status in ACTIVE_GENERATION_STATUSES:
                return generation.id
            if status == "failed":
                result = await generation_routes.retry_generation(
                    generation.id,
                    db,
                )
                generation = db.query(DBGeneration).filter_by(
                    id=generation.id
                ).one()
                generation.source = "mcp_story"
                db.commit()
                return _generation_id(result)
            raise ValueError(
                f"Generation {generation.id} has unsupported status '{status}'"
            )

        personality_text = getattr(profile, "personality", None) or ""
        request = models.GenerationRequest(
            profile_id=profile.id,
            text=segment.text,
            language=profile.language or "en",
            engine=None,
            personality=bool(personality_text.strip()),
        )
        result = await generation_routes.generate_speech(request, db)
        generation_id = _generation_id(result)
        generation = db.query(DBGeneration).filter_by(
            id=generation_id
        ).first()
        if generation is None:
            raise ValueError("Generation service did not persist its result")
        generation.source = "mcp_story"
        segment.generation_id = generation_id
        db.commit()
        return generation_id


def _mark_segment_failure(
    story_id: str,
    segment_id: str,
    error: object,
) -> None:
    message = _safe_error(error)
    with _session_scope() as db:
        story = db.query(DBStory).filter_by(id=story_id).first()
        segment = db.query(DBStorySegment).filter_by(id=segment_id).first()
        if story is None:
            return
        story.status = "failed"
        story.error = message
        story.current_segment_index = None
        if segment is not None:
            segment.status = "failed"
            segment.error = message
            segment.updated_at = datetime.utcnow()
            story.failed_segment_index = segment.position
        _recompute_progress(story, db)
        story.updated_at = datetime.utcnow()
        db.commit()


def _mark_render_failure(story_id: str, error: object) -> None:
    with _session_scope() as db:
        story = db.query(DBStory).filter_by(id=story_id).first()
        if story is None:
            return
        story.status = "failed"
        story.error = _safe_error(error)
        story.current_segment_index = None
        story.failed_segment_index = None
        _recompute_progress(story, db)
        story.updated_at = datetime.utcnow()
        db.commit()


async def _process_segment(story_id: str, segment_id: str) -> bool:
    with _session_scope() as db:
        segment = db.query(DBStorySegment).filter_by(id=segment_id).first()
        if segment is None:
            raise ValueError("Story segment was not found")
        generation_id = segment.generation_id
        if generation_id and _item_exists(db, story_id, generation_id):
            story = db.query(DBStory).filter_by(id=story_id).one()
            segment.status = "completed"
            segment.error = None
            _recompute_progress(story, db)
            db.commit()
            return True
        generation = (
            db.query(DBGeneration).filter_by(id=generation_id).first()
            if generation_id
            else None
        )
        if (
            generation is not None
            and (generation.status or "completed") == "completed"
        ):
            completed_generation_id = generation.id
        else:
            completed_generation_id = None

    try:
        if completed_generation_id is not None:
            await _attach_completed_generation(
                story_id,
                segment_id,
                completed_generation_id,
            )
            return True

        generation_id = await _prepare_generation(story_id, segment_id)
        status, generation_error = await _wait_for_generation(generation_id)
        if status != "completed":
            _mark_segment_failure(
                story_id,
                segment_id,
                generation_error or "Generation failed",
            )
            return False
        await _attach_completed_generation(story_id, segment_id, generation_id)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _mark_segment_failure(story_id, segment_id, exc)
        return False


async def _run_story_workflow(story_id: str) -> None:
    """Process pending segments in order, stop on failure, then render once."""
    _active_story_ids.add(story_id)
    try:
        with _session_scope() as db:
            story = db.query(DBStory).filter_by(id=story_id).first()
            if story is None:
                return
            if (
                story.status == "completed"
                and story_rendering.resolve_valid_render_path(story)
            ):
                return
            segment_ids = [
                row.id
                for row in (
                    db.query(DBStorySegment)
                    .filter_by(story_id=story_id)
                    .order_by(DBStorySegment.position)
                    .all()
                )
            ]
            if not segment_ids:
                _mark_render_failure(story_id, "Story has no segments")
                return

        for segment_id in segment_ids:
            if not await _process_segment(story_id, segment_id):
                return

        with _session_scope() as db:
            story = db.query(DBStory).filter_by(id=story_id).first()
            if story is None:
                return
            _validate_story_ready_for_render(story, db)
            story.status = "rendering"
            story.error = None
            story.current_segment_index = None
            story.failed_segment_index = None
            story.updated_at = datetime.utcnow()
            db.commit()
            try:
                await story_rendering.render_story_persistent(story_id, db)
            except Exception as exc:
                db.rollback()
                _mark_render_failure(story_id, exc)
                return

            story = db.query(DBStory).filter_by(id=story_id).one()
            story.status = "completed"
            story.error = None
            story.current_segment_index = None
            story.failed_segment_index = None
            _recompute_progress(story, db)
            story.updated_at = datetime.utcnow()
            db.commit()
    except asyncio.CancelledError:
        _mark_render_failure(
            story_id,
            "Server was shut down during Story processing",
        )
        raise
    except Exception as exc:
        logger.exception("Story workflow %s failed", story_id)
        _mark_render_failure(story_id, exc)
    finally:
        _active_story_ids.discard(story_id)
        _story_tasks.pop(story_id, None)


def _consume_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        exception = task.exception()
        if exception is not None:
            logger.error("Story background task failed: %s", exception)
    except Exception:
        logger.exception("Could not inspect Story background task")


def start_story_workflow(story_id: str) -> None:
    """Start one local background coordinator without duplicating work."""
    existing = _story_tasks.get(story_id)
    if story_id in _active_story_ids or (
        existing is not None and not existing.done()
    ):
        raise ValueError("Story is already processing")

    _active_story_ids.add(story_id)
    try:
        task = create_background_task(_run_story_workflow(story_id))
    except Exception:
        _active_story_ids.discard(story_id)
        raise
    _story_tasks[story_id] = task
    task.add_done_callback(_consume_task_result)


def _install_task_callback(story_id: str) -> None:
    """Compatibility helper; new starts install their callback automatically."""
    task = _story_tasks.get(story_id)
    if task is not None:
        callbacks = getattr(task, "_callbacks", None) or []
        if _consume_task_result not in callbacks:
            task.add_done_callback(_consume_task_result)


async def wait_for_story_workflow(story_id: str) -> None:
    """Await a local coordinator when present; primarily useful to tests/admins."""
    task = _story_tasks.get(story_id)
    if task is not None:
        await task


def is_story_resumable(story: DBStory, db: Session) -> bool:
    if story.status != "failed" or story.id in _active_story_ids:
        return False
    total = (
        db.query(func.count(DBStorySegment.id))
        .filter(DBStorySegment.story_id == story.id)
        .scalar()
        or 0
    )
    if total < 1:
        return False
    completed = (
        db.query(func.count(DBStorySegment.id))
        .filter(
            DBStorySegment.story_id == story.id,
            DBStorySegment.status == "completed",
        )
        .scalar()
        or 0
    )
    return (
        completed < total
        or story_rendering.resolve_valid_render_path(story) is None
    )


async def resume_story_workflow(story_id: str, db: Session) -> DBStory:
    """Reconcile durable state and continue a failed Story idempotently."""
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story is None:
        raise ValueError(f"Story '{story_id}' was not found")
    if story_id in _active_story_ids or story.status in ACTIVE_STORY_STATUSES:
        raise ValueError("Story is already processing")
    if story.status != "failed":
        raise ValueError("Only failed Stories can be resumed")
    if not is_story_resumable(story, db):
        raise ValueError("Story has no incomplete work to resume")

    segments = (
        db.query(DBStorySegment)
        .filter_by(story_id=story_id)
        .order_by(DBStorySegment.position)
        .all()
    )
    if not segments:
        raise ValueError("Story has no segments to resume")

    for segment in segments:
        profile = db.query(DBVoiceProfile).filter_by(
            id=segment.profile_id
        ).first()
        if profile is None:
            raise ValueError("Story voice profile was deleted")
        validate_story_profile(profile, db)

        if segment.generation_id and _item_exists(
            db,
            story_id,
            segment.generation_id,
        ):
            segment.status = "completed"
            segment.error = None
            continue

        generation = (
            db.query(DBGeneration).filter_by(
                id=segment.generation_id
            ).first()
            if segment.generation_id
            else None
        )
        if generation is None:
            segment.generation_id = None
            segment.status = "pending"
            segment.error = None
        else:
            status = generation.status or "completed"
            if status == "completed":
                segment.status = "pending"
                segment.error = None
            elif status == "failed":
                segment.status = "failed"
            elif status in ACTIVE_GENERATION_STATUSES:
                raise ValueError(
                    f"Generation {generation.id} is still processing"
                )
            else:
                segment.status = "failed"
                segment.error = f"Unsupported generation status: {status}"

    story.status = "queued"
    story.error = None
    story.current_segment_index = None
    story.failed_segment_index = None
    _recompute_progress(story, db)
    story.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(story)

    start_story_workflow(story_id)
    return story


def recover_interrupted_story_workflows(db: Session) -> int:
    """Mark active workflows failed/resumable after an unclean shutdown."""
    stories = (
        db.query(DBStory)
        .filter(DBStory.status.in_(ACTIVE_STORY_STATUSES))
        .all()
    )
    if not stories:
        return 0

    interrupted_error = "Server was shut down during Story processing"
    now = datetime.utcnow()
    for story in stories:
        segments = (
            db.query(DBStorySegment)
            .filter_by(story_id=story.id)
            .order_by(DBStorySegment.position)
            .all()
        )
        first_failed: int | None = None
        for segment in segments:
            if segment.generation_id and _item_exists(
                db,
                story.id,
                segment.generation_id,
            ):
                segment.status = "completed"
                segment.error = None
                continue

            generation = (
                db.query(DBGeneration)
                .filter_by(id=segment.generation_id)
                .first()
                if segment.generation_id
                else None
            )
            if (
                generation is not None
                and (generation.status or "completed") == "completed"
            ):
                # Resume will attach the already-completed generation.
                segment.status = "pending"
                segment.error = None
                continue

            generation_was_active = (
                generation is not None
                and (generation.status or "completed")
                in ACTIVE_GENERATION_STATUSES
            )
            if generation_was_active:
                generation.status = "failed"
                generation.error = interrupted_error

            if segment.status in {"generating", "rendering"} or generation_was_active:
                segment.status = "failed"
                segment.error = interrupted_error
                if first_failed is None:
                    first_failed = segment.position
            segment.updated_at = now

        story.status = "failed"
        story.error = interrupted_error
        story.current_segment_index = None
        story.failed_segment_index = first_failed
        _recompute_progress(story, db)
        story.updated_at = now

    db.commit()
    return len(stories)
