"""MCP tools for durable multi-profile Story generation."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..database import Generation as DBGeneration
from ..database import Story as DBStory
from ..database import StorySegment as DBStorySegment
from ..database import VoiceProfile as DBVoiceProfile
from ..database import get_db
from ..services import story_orchestration as orchestration
from ..services import story_rendering

MAX_STORY_SEGMENTS = 100
MAX_STORY_SEGMENT_CHARS = 10_000
MAX_STORY_TOTAL_CHARS = 100_000


class StoryToolSegment(BaseModel):
    profile: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=MAX_STORY_SEGMENT_CHARS)

    @field_validator("profile", "text")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("must not be empty")
        return clean


def _coerce_segments(segments: list[StoryToolSegment | dict[str, Any]]) -> list[StoryToolSegment]:
    if not segments:
        raise ValueError("Story requires at least one segment")
    if len(segments) > MAX_STORY_SEGMENTS:
        raise ValueError("Story cannot contain more than 100 segments")

    normalized: list[StoryToolSegment] = []
    total_chars = 0
    for position, raw in enumerate(segments, start=1):
        try:
            segment = raw if isinstance(raw, StoryToolSegment) else StoryToolSegment.model_validate(raw)
        except Exception as exc:
            message = str(exc)
            if "10000" in message or "10,000" in message:
                raise ValueError(
                    f"Story segment {position} cannot exceed 10000 characters"
                ) from exc
            raise ValueError(f"Invalid Story segment {position}: {message}") from exc
        total_chars += len(segment.text)
        if total_chars > MAX_STORY_TOTAL_CHARS:
            raise ValueError("Story text cannot exceed 100000 characters in total")
        normalized.append(segment)
    return normalized


def _load_story(story_id: str, db: Session) -> DBStory:
    story = db.query(DBStory).filter_by(id=story_id).first()
    if story is None:
        raise ValueError(f"Story '{story_id}' was not found")
    return story


def _status_payload(story: DBStory, db: Session) -> dict[str, Any]:
    render_exists = story_rendering.resolve_valid_render_path(story) is not None
    return {
        "story_id": story.id,
        "title": story.name,
        "status": story.status,
        "total_segments": story.total_segments,
        "completed_segments": story.completed_segments,
        "current_segment": story.current_segment_index,
        "failed_segment": story.failed_segment_index,
        "error": story.error,
        "resumable": orchestration.is_story_resumable(story, db),
        "download_url": (
            f"/stories/{story.id}/export-audio" if render_exists else None
        ),
    }


async def create_story(
    *,
    title: str,
    description: str | None,
    segments: list[StoryToolSegment | dict[str, Any]],
    db: Session,
) -> dict[str, Any]:
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Story title must not be empty")
    if len(clean_title) > 100:
        raise ValueError("Story title cannot exceed 100 characters")
    clean_description = (description or "").strip() or None
    if clean_description and len(clean_description) > 500:
        raise ValueError("Story description cannot exceed 500 characters")

    normalized = _coerce_segments(segments)
    story = await orchestration.create_story_workflow(
        title=clean_title,
        description=clean_description,
        segments=[
            orchestration.StorySegmentSpec(
                profile=segment.profile,
                text=segment.text,
            )
            for segment in normalized
        ],
        db=db,
    )
    try:
        orchestration.start_story_workflow(story.id)
        orchestration._install_task_callback(story.id)
    except Exception as exc:
        story.status = "failed"
        story.error = orchestration._safe_error(exc)
        db.commit()
        raise

    result = _status_payload(story, db)
    result["status_tool"] = "voicebox.get_story_status"
    return result


def get_story_status(story_id: str, db: Session) -> dict[str, Any]:
    return _status_payload(_load_story(story_id, db), db)


def get_story(story_id: str, db: Session) -> dict[str, Any]:
    story = _load_story(story_id, db)
    rows = (
        db.query(
            DBStorySegment,
            DBVoiceProfile.name.label("profile_name"),
            DBGeneration.status.label("generation_status"),
            DBGeneration.duration.label("generation_duration"),
        )
        .join(DBVoiceProfile, DBStorySegment.profile_id == DBVoiceProfile.id)
        .outerjoin(DBGeneration, DBStorySegment.generation_id == DBGeneration.id)
        .filter(DBStorySegment.story_id == story_id)
        .order_by(DBStorySegment.position)
        .all()
    )
    result = _status_payload(story, db)
    result["description"] = story.description
    result["segments"] = [
        {
            "position": segment.position,
            "profile_id": segment.profile_id,
            "profile_name": profile_name,
            "text": segment.text,
            "status": segment.status,
            "error": segment.error,
            "generation_id": segment.generation_id,
            "generation_status": generation_status,
            "duration": generation_duration,
        }
        for segment, profile_name, generation_status, generation_duration in rows
    ]
    return result


async def resume_story(story_id: str, db: Session) -> dict[str, Any]:
    story = await orchestration.resume_story_workflow(story_id, db)
    return _status_payload(story, db)


def register_story_tools(mcp: FastMCP) -> None:
    """Register the asynchronous Story workflow surface."""

    @mcp.tool(
        name="voicebox.create_story",
        description=(
            "Create an asynchronous multi-voice Story from ordered profile/text "
            "segments. Poll voicebox.get_story_status for completion."
        ),
    )
    async def voicebox_create_story(
        title: str,
        segments: list[StoryToolSegment],
        description: str | None = None,
    ) -> dict[str, Any]:
        db = next(get_db())
        try:
            return await create_story(
                title=title,
                description=description,
                segments=segments,
                db=db,
            )
        finally:
            db.close()

    @mcp.tool(
        name="voicebox.get_story_status",
        description="Get Story progress, failure details, resumability, and final download URL.",
    )
    async def voicebox_get_story_status(story_id: str) -> dict[str, Any]:
        db = next(get_db())
        try:
            return get_story_status(story_id, db)
        finally:
            db.close()

    @mcp.tool(
        name="voicebox.get_story",
        description="Get ordered Story segments and generation state without local file paths.",
    )
    async def voicebox_get_story(story_id: str) -> dict[str, Any]:
        db = next(get_db())
        try:
            return get_story(story_id, db)
        finally:
            db.close()

    @mcp.tool(
        name="voicebox.resume_story",
        description=(
            "Resume a failed Story from its first incomplete segment without "
            "regenerating completed segments."
        ),
    )
    async def voicebox_resume_story(story_id: str) -> dict[str, Any]:
        db = next(get_db())
        try:
            return await resume_story(story_id, db)
        finally:
            db.close()
