"""Persistent orchestration for MCP-created multi-voice Stories.

This module deliberately builds on the existing Generation and Story services.
Profile helpers live here because Story creation must validate every segment
before any Story rows are written.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import ProfileSample as DBProfileSample
from ..database import VoiceProfile as DBVoiceProfile
from . import preset_voices as preset_voices_service
from . import profiles as profiles_service


def resolve_story_profile(value: str, db: Session) -> DBVoiceProfile:
    """Resolve an exact profile id or one unambiguous case-insensitive name."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("profile must not be empty")

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
    """Use the same engine precedence as normal Generation requests.

    Passing ``engine=None`` is essential: GenerationRequest's public default is
    qwen, which must not override a profile's preset/default engine here.
    """
    from ..routes.generations import _resolve_generation_engine

    request = models.GenerationRequest(
        profile_id=profile.id,
        text="validation",
        language=profile.language or "en",
        engine=None,
    )
    return _resolve_generation_engine(request, profile)


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
            valid_ids = preset_voices_service.get_preset_voice_ids(preset_engine or "")
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
