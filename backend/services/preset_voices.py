"""Shared discovery of built-in preset voices."""

from __future__ import annotations

from typing import Any

PRESET_ENGINES = {"kokoro", "qwen_custom_voice"}


def list_preset_voices(engine: str) -> dict[str, Any]:
    """Return normalized built-in voice metadata for a supported engine."""
    if engine == "kokoro":
        from ..backends.kokoro_backend import KOKORO_VOICES

        voices = [
            {
                "voice_id": voice_id,
                "name": name,
                "gender": gender,
                "language": language,
            }
            for voice_id, name, gender, language in KOKORO_VOICES
        ]
        return {"engine": engine, "voices": voices}

    if engine == "qwen_custom_voice":
        from ..backends.qwen_custom_voice_backend import QWEN_CUSTOM_VOICES

        voices = [
            {
                "voice_id": speaker_id,
                "name": display_name,
                "gender": gender,
                "language": language,
            }
            for speaker_id, display_name, gender, language, _description
            in QWEN_CUSTOM_VOICES
        ]
        return {"engine": engine, "voices": voices}

    supported = ", ".join(sorted(PRESET_ENGINES))
    raise ValueError(
        f"Unsupported preset engine '{engine}'. Supported engines: {supported}."
    )


def get_preset_voice_ids(engine: str) -> set[str]:
    """Return valid identifiers for a supported preset engine."""
    return {
        voice["voice_id"]
        for voice in list_preset_voices(engine)["voices"]
    }
