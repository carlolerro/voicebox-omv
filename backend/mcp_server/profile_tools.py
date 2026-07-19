"""MCP tools for creating and inspecting Voicebox profiles."""

from __future__ import annotations

import base64 as b64
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastmcp import FastMCP
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config, models
from ..database import Capture as DBCapture
from ..database import Generation as DBGeneration
from ..database import ProfileSample as DBProfileSample
from ..database import VoiceProfile as DBVoiceProfile
from ..database import get_db
from ..services import preset_voices as preset_voices_service
from ..services import profiles as profiles_service

MAX_PROFILE_SAMPLE_BYTES = 50 * 1024 * 1024
ALLOWED_SAMPLE_SUFFIXES = {
    ".wav",
    ".mp3",
    ".m4a",
    ".ogg",
    ".flac",
    ".aac",
    ".webm",
    ".opus",
}
MCP_PROFILE_TYPES = {"cloned", "preset"}


def serialize_profile(profile: DBVoiceProfile, db: Session) -> dict[str, Any]:
    """Return complete MCP-facing metadata for a profile."""
    sample_count = (
        db.query(func.count(DBProfileSample.id))
        .filter(DBProfileSample.profile_id == profile.id)
        .scalar()
        or 0
    )
    generation_count = (
        db.query(func.count(DBGeneration.id))
        .filter(DBGeneration.profile_id == profile.id)
        .scalar()
        or 0
    )
    voice_type = getattr(profile, "voice_type", None) or "cloned"

    if voice_type == "preset":
        ready = False
        if profile.preset_engine and profile.preset_voice_id:
            try:
                voice_ids = preset_voices_service.get_preset_voice_ids(
                    profile.preset_engine
                )
                ready = profile.preset_voice_id in voice_ids
            except ValueError:
                ready = False
    elif voice_type == "cloned":
        ready = sample_count > 0
    else:
        ready = False

    return {
        "profile_id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "personality": profile.personality,
        "language": profile.language,
        "voice_type": voice_type,
        "preset_engine": profile.preset_engine,
        "preset_voice_id": profile.preset_voice_id,
        "default_engine": profile.default_engine,
        "sample_count": sample_count,
        "generation_count": generation_count,
        "ready_for_generation": ready,
    }


def _resolve_profile(profile: str, db: Session) -> DBVoiceProfile:
    row = profiles_service.get_profile_orm_by_name_or_id(profile, db)
    if row is None:
        raise ValueError(f"Voice profile '{profile}' was not found.")
    return row


def _reject_case_insensitive_duplicate(name: str, db: Session) -> None:
    """Keep profile names unambiguous for case-insensitive MCP lookup."""
    existing = (
        db.query(DBVoiceProfile)
        .filter(func.lower(DBVoiceProfile.name) == name.lower())
        .first()
    )
    if existing is not None:
        raise ValueError(
            f"A profile with the name '{name}' already exists. "
            "Please choose a different name."
        )


async def create_profile(
    *,
    name: str,
    description: str | None,
    language: str,
    voice_type: str,
    personality: str | None,
    default_engine: str | None,
    preset_engine: str | None,
    preset_voice_id: str | None,
    db: Session,
) -> dict[str, Any]:
    """Create profile metadata without accepting or generating audio."""
    if voice_type not in MCP_PROFILE_TYPES:
        raise ValueError("voice_type must be 'cloned' or 'preset'.")
    if voice_type == "preset" and preset_engine is not None:
        preset_voices_service.list_preset_voices(preset_engine)

    request = models.VoiceProfileCreate(
        name=name,
        description=description,
        language=language,
        voice_type=voice_type,
        personality=personality,
        default_engine=default_engine,
        preset_engine=preset_engine,
        preset_voice_id=preset_voice_id,
    )
    _reject_case_insensitive_duplicate(request.name, db)

    created = await profiles_service.create_profile(request, db)
    row = db.query(DBVoiceProfile).filter_by(id=created.id).one()
    return serialize_profile(row, db)


async def get_profile(profile: str, db: Session) -> dict[str, Any]:
    """Get a profile by UUID or case-insensitive name."""
    return serialize_profile(_resolve_profile(profile, db), db)


def _sample_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SAMPLE_SUFFIXES else ".wav"


def _validate_sample_file_size(path: Path) -> None:
    """Apply the same decoded-size ceiling to Base64 and Capture samples."""
    if path.stat().st_size > MAX_PROFILE_SAMPLE_BYTES:
        raise ValueError("Profile samples cannot exceed 50 MB.")


@contextmanager
def decoded_audio_file(
    audio_base64: str, filename: str | None
) -> Iterator[Path]:
    """Decode strict Base64 into a bounded temporary audio file."""
    try:
        raw = b64.b64decode(audio_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid audio_base64: {exc}") from exc

    if len(raw) > MAX_PROFILE_SAMPLE_BYTES:
        raise ValueError("Profile samples cannot exceed 50 MB.")

    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=_sample_suffix(filename), delete=False
        ) as temporary:
            temporary.write(raw)
            path = Path(temporary.name)
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _sample_response(
    profile: DBVoiceProfile,
    sample_id: str,
    source: str,
    reference_text_source: str,
    db: Session,
) -> dict[str, Any]:
    metadata = serialize_profile(profile, db)
    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "sample_id": sample_id,
        "source": source,
        "reference_text_source": reference_text_source,
        "sample_count": metadata["sample_count"],
        "ready_for_generation": metadata["ready_for_generation"],
    }


async def add_profile_sample(
    *,
    profile: str,
    audio_base64: str | None,
    capture_id: str | None,
    filename: str | None,
    reference_text: str | None,
    db: Session,
) -> dict[str, Any]:
    """Attach one Base64 or Capture sample to a cloned profile."""
    row = _resolve_profile(profile, db)
    if (getattr(row, "voice_type", None) or "cloned") != "cloned":
        raise ValueError("Only cloned profiles can receive cloned voice samples.")
    if bool(audio_base64) == bool(capture_id):
        raise ValueError("Pass exactly one of audio_base64 or capture_id.")

    if audio_base64 is not None:
        clean_text = (reference_text or "").strip()
        if not clean_text:
            raise ValueError(
                "reference_text is required for audio_base64 samples."
            )
        clean_text = models.ProfileSampleCreate(
            reference_text=clean_text
        ).reference_text
        with decoded_audio_file(audio_base64, filename) as path:
            sample = await profiles_service.add_profile_sample(
                row.id, str(path), clean_text, db
            )
        return _sample_response(row, sample.id, "base64", "explicit", db)

    capture = db.query(DBCapture).filter_by(id=capture_id).first()
    if capture is None:
        raise ValueError(f"Capture '{capture_id}' was not found.")

    path = config.resolve_storage_path(capture.audio_path)
    if path is None or not path.is_file():
        raise ValueError(
            f"Capture '{capture_id}' exists, but its audio file is unavailable."
        )
    _validate_sample_file_size(path)

    explicit = (reference_text or "").strip()
    if explicit:
        clean_text = models.ProfileSampleCreate(
            reference_text=explicit
        ).reference_text
        text_source = "explicit"
    else:
        raw = (capture.transcript_raw or "").strip()
        if not raw:
            raise ValueError(
                f"Capture '{capture_id}' has no usable transcript_raw; "
                "pass reference_text explicitly."
            )
        clean_text = models.ProfileSampleCreate(
            reference_text=raw
        ).reference_text
        text_source = "transcript_raw"

    sample = await profiles_service.add_profile_sample(
        row.id, str(path), clean_text, db
    )
    return _sample_response(row, sample.id, "capture", text_source, db)


def register_profile_tools(mcp: FastMCP) -> None:
    """Register the MCP profile-management surface."""

    @mcp.tool(
        name="voicebox.list_preset_voices",
        description="List built-in voices for kokoro or qwen_custom_voice.",
    )
    async def voicebox_list_preset_voices(engine: str) -> dict[str, Any]:
        return preset_voices_service.list_preset_voices(engine)

    @mcp.tool(
        name="voicebox.create_profile",
        description=(
            "Create metadata for one person as a preset or cloned profile. "
            "This tool never accepts audio or generates speech."
        ),
    )
    async def voicebox_create_profile(
        name: str,
        description: str | None = None,
        language: str = "en",
        voice_type: str = "cloned",
        personality: str | None = None,
        default_engine: str | None = None,
        preset_engine: str | None = None,
        preset_voice_id: str | None = None,
    ) -> dict[str, Any]:
        db = next(get_db())
        try:
            return await create_profile(
                name=name,
                description=description,
                language=language,
                voice_type=voice_type,
                personality=personality,
                default_engine=default_engine,
                preset_engine=preset_engine,
                preset_voice_id=preset_voice_id,
                db=db,
            )
        finally:
            db.close()

    @mcp.tool(
        name="voicebox.get_profile",
        description=(
            "Get complete profile metadata by UUID or case-insensitive name, "
            "including description, personality, counts, and readiness."
        ),
    )
    async def voicebox_get_profile(profile: str) -> dict[str, Any]:
        db = next(get_db())
        try:
            return await get_profile(profile, db)
        finally:
            db.close()

    @mcp.tool(
        name="voicebox.add_profile_sample",
        description=(
            "Attach a sample to a cloned profile. Pass exactly one of "
            "audio_base64 or capture_id."
        ),
    )
    async def voicebox_add_profile_sample(
        profile: str,
        audio_base64: str | None = None,
        capture_id: str | None = None,
        filename: str | None = None,
        reference_text: str | None = None,
    ) -> dict[str, Any]:
        db = next(get_db())
        try:
            return await add_profile_sample(
                profile=profile,
                audio_base64=audio_base64,
                capture_id=capture_id,
                filename=filename,
                reference_text=reference_text,
                db=db,
            )
        finally:
            db.close()
