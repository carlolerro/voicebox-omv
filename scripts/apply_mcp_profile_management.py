from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(relative_path: str, old: str, new: str) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected block not found in {relative_path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(relative_path: str, content: str) -> None:
    path = ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


write(
    "backend/services/preset_voices.py",
    '''"""Shared discovery of built-in preset voices."""

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
    """Return valid voice identifiers for a supported preset engine."""
    return {
        voice["voice_id"]
        for voice in list_preset_voices(engine)["voices"]
    }
''',
)

replace_once(
    "backend/services/profiles.py",
    "from ..utils.images import process_avatar, validate_image\n",
    "from ..utils.images import process_avatar, validate_image\n"
    "from .preset_voices import get_preset_voice_ids, list_preset_voices\n",
)
replace_once(
    "backend/services/profiles.py",
    '''def _get_preset_voice_ids(engine: str) -> set[str]:
    if engine == "kokoro":
        from ..backends.kokoro_backend import KOKORO_VOICES

        return {voice_id for voice_id, _name, _gender, _lang in KOKORO_VOICES}

    if engine == "qwen_custom_voice":
        from ..backends.qwen_custom_voice_backend import QWEN_CUSTOM_VOICES

        return {voice_id for voice_id, _name, _gender, _lang, _desc in QWEN_CUSTOM_VOICES}

    return set()
''',
    '''def _get_preset_voice_ids(engine: str) -> set[str]:
    try:
        return get_preset_voice_ids(engine)
    except ValueError:
        return set()
''',
)

replace_once(
    "backend/routes/profiles.py",
    '''@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    """List available preset voices for an engine."""
    if engine == "kokoro":
        from ..backends.kokoro_backend import KOKORO_VOICES

        return {
            "engine": engine,
            "voices": [
                {
                    "voice_id": vid,
                    "name": name,
                    "gender": gender,
                    "language": lang,
                }
                for vid, name, gender, lang in KOKORO_VOICES
            ],
        }
    if engine == "qwen_custom_voice":
        from ..backends.qwen_custom_voice_backend import QWEN_CUSTOM_VOICES

        return {
            "engine": engine,
            "voices": [
                {
                    "voice_id": speaker_id,
                    "name": display_name,
                    "gender": gender,
                    "language": lang,
                }
                for speaker_id, display_name, gender, lang, _desc in QWEN_CUSTOM_VOICES
            ],
        }
    return {"engine": engine, "voices": []}
''',
    '''@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    """List available preset voices for an engine."""
    try:
        return profiles.list_preset_voices(engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
''',
)

write(
    "backend/mcp_server/profile_tools.py",
    '''"""MCP tools for creating and inspecting Voicebox profiles."""

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
                voice_ids = {
                    voice["voice_id"]
                    for voice in profiles_service.list_preset_voices(
                        profile.preset_engine
                    )["voices"]
                }
                ready = profile.preset_voice_id in voice_ids
            except ValueError:
                ready = False
    else:
        ready = sample_count > 0

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
        profiles_service.list_preset_voices(preset_engine)

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
    created = await profiles_service.create_profile(request, db)
    row = db.query(DBVoiceProfile).filter_by(id=created.id).one()
    return serialize_profile(row, db)


async def get_profile(profile: str, db: Session) -> dict[str, Any]:
    """Get a profile by UUID or case-insensitive name."""
    return serialize_profile(_resolve_profile(profile, db), db)


def _sample_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SAMPLE_SUFFIXES else ".wav"


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
        return profiles_service.list_preset_voices(engine)

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
''',
)

replace_once(
    "backend/mcp_server/tools.py",
    "from .context import current_client_id, request_is_loopback\n",
    "from .context import current_client_id, request_is_loopback\n"
    "from .profile_tools import register_profile_tools\n",
)
replace_once(
    "backend/mcp_server/tools.py",
    '''        finally:
            db.close()


# ─── Speak helper ──────────────────────────────────────────────────────────
''',
    '''        finally:
            db.close()

    register_profile_tools(mcp)


# ─── Speak helper ──────────────────────────────────────────────────────────
''',
)

replace_once(
    "backend/mcp_server/server.py",
    '''        instructions=(
            "Voicebox is a local voice I/O layer. Use `voicebox.speak` to "
            "play text in a voice profile, `voicebox.transcribe` for "
            "audio→text, and the `list_*` tools to discover profiles and "
            "captures."
        ),
''',
    '''        instructions=(
            "Voicebox is a local voice I/O layer. Inspect profiles with "
            "`voicebox.list_profiles` and `voicebox.get_profile`; create them "
            "with `voicebox.list_preset_voices`, `voicebox.create_profile`, "
            "and `voicebox.add_profile_sample`; generate speech with "
            "`voicebox.speak`; and transcribe audio with `voicebox.transcribe`."
        ),
''',
)

write("backend/tests/__init__.py", "")
write(
    "backend/tests/test_mcp_profile_tools.py",
    '''from __future__ import annotations

import base64
import io
import sys
import tempfile
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config
from backend.database import Base
from backend.database import Capture as DBCapture
from backend.database import ProfileSample as DBProfileSample
from backend.database import VoiceProfile as DBVoiceProfile
from backend.mcp_server.profile_tools import (
    MAX_PROFILE_SAMPLE_BYTES,
    add_profile_sample,
    create_profile,
    decoded_audio_file,
    get_profile,
)
from backend.mcp_server.tools import register_tools
from backend.services import profiles as profiles_service


def make_wav_bytes() -> bytes:
    """Return three seconds of non-silent 16 kHz mono PCM."""
    frames = bytearray()
    for index in range(16_000 * 3):
        sample = 4_000 if (index // 80) % 2 == 0 else -4_000
        frames.extend(sample.to_bytes(2, "little", signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


@contextmanager
def fake_preset_modules():
    kokoro = ModuleType("backend.backends.kokoro_backend")
    kokoro.KOKORO_VOICES = [
        ("if_sara", "Sara", "female", "it"),
        ("im_nicola", "Nicola", "male", "it"),
    ]
    qwen = ModuleType("backend.backends.qwen_custom_voice_backend")
    qwen.QWEN_CUSTOM_VOICES = [
        ("Ryan", "Ryan", "male", "en", "Dynamic voice"),
    ]
    with patch.dict(
        sys.modules,
        {
            "backend.backends.kokoro_backend": kokoro,
            "backend.backends.qwen_custom_voice_backend": qwen,
        },
    ):
        yield


class FakeMCP:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def decorator(function):
            self.registered[name] = function
            return function

        return decorator


class MCPProfileToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_data_dir = config.get_data_dir()
        self.temp_dir = tempfile.TemporaryDirectory()
        config.set_data_dir(self.temp_dir.name)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
        )
        self.db = self.Session()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    async def _create_clone(self, name: str) -> dict:
        return await create_profile(
            name=name,
            description=None,
            language="en",
            voice_type="cloned",
            personality=None,
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )

    def _capture(
        self,
        capture_id: str,
        transcript_raw: str,
        transcript_refined: str | None = None,
        write_audio: bool = True,
    ) -> None:
        path = config.get_captures_dir() / f"{capture_id}.wav"
        if write_audio:
            path.write_bytes(make_wav_bytes())
        self.db.add(
            DBCapture(
                id=capture_id,
                audio_path=config.to_storage_path(path),
                source="file",
                language="en",
                transcript_raw=transcript_raw,
                transcript_refined=transcript_refined,
            )
        )
        self.db.commit()

    def test_list_preset_voices(self) -> None:
        with fake_preset_modules():
            kokoro = profiles_service.list_preset_voices("kokoro")
            qwen = profiles_service.list_preset_voices("qwen_custom_voice")
        self.assertEqual(kokoro["engine"], "kokoro")
        self.assertEqual(
            set(kokoro["voices"][0]),
            {"voice_id", "name", "gender", "language"},
        )
        self.assertEqual(qwen["voices"][0]["voice_id"], "Ryan")

    def test_list_preset_voices_rejects_unknown_engine(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported preset engine"):
            profiles_service.list_preset_voices("unknown")

    async def test_create_clone_returns_complete_metadata(self) -> None:
        result = await create_profile(
            name="Carlo",
            description="Voce italiana per spiegazioni tecniche.",
            language="it",
            voice_type="cloned",
            personality="Carlo parla in modo concreto.",
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )
        self.assertEqual(result["description"], "Voce italiana per spiegazioni tecniche.")
        self.assertEqual(result["personality"], "Carlo parla in modo concreto.")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["generation_count"], 0)
        self.assertFalse(result["ready_for_generation"])

    async def test_create_valid_preset_is_ready(self) -> None:
        with fake_preset_modules():
            result = await create_profile(
                name="Preset Person",
                description=None,
                language="it",
                voice_type="preset",
                personality=None,
                default_engine=None,
                preset_engine="kokoro",
                preset_voice_id="if_sara",
                db=self.db,
            )
        self.assertEqual(result["default_engine"], "kokoro")
        self.assertTrue(result["ready_for_generation"])

    async def test_create_rejects_invalid_types_engines_and_lengths(self) -> None:
        with self.assertRaisesRegex(ValueError, "voice_type must be"):
            await create_profile(
                name="Designed",
                description=None,
                language="en",
                voice_type="designed",
                personality=None,
                default_engine=None,
                preset_engine=None,
                preset_voice_id=None,
                db=self.db,
            )
        with self.assertRaisesRegex(ValueError, "Unsupported preset engine"):
            await create_profile(
                name="Unknown Preset",
                description=None,
                language="en",
                voice_type="preset",
                personality=None,
                default_engine=None,
                preset_engine="unknown",
                preset_voice_id="voice",
                db=self.db,
            )
        with self.assertRaises(ValueError):
            await create_profile(
                name="Too Long",
                description="x" * 501,
                language="en",
                voice_type="cloned",
                personality=None,
                default_engine="qwen",
                preset_engine=None,
                preset_voice_id=None,
                db=self.db,
            )

    async def test_create_preserves_native_validation_and_uniqueness(self) -> None:
        with fake_preset_modules():
            with self.assertRaisesRegex(ValueError, "Preset profiles require"):
                await create_profile(
                    name="Missing Voice",
                    description=None,
                    language="en",
                    voice_type="preset",
                    personality=None,
                    default_engine=None,
                    preset_engine="kokoro",
                    preset_voice_id=None,
                    db=self.db,
                )
        with self.assertRaisesRegex(ValueError, "Cloned profiles cannot use default engine"):
            await create_profile(
                name="Invalid Clone",
                description=None,
                language="en",
                voice_type="cloned",
                personality=None,
                default_engine="kokoro",
                preset_engine=None,
                preset_voice_id=None,
                db=self.db,
            )

        await self._create_clone("Duplicate")
        with self.assertRaisesRegex(ValueError, "already exists"):
            await self._create_clone("Duplicate")

    async def test_get_by_id_or_case_insensitive_name(self) -> None:
        created = await create_profile(
            name="Case Display",
            description="Metadata",
            language="it",
            voice_type="cloned",
            personality="Direct.",
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )
        by_name = await get_profile("case display", self.db)
        by_id = await get_profile(created["profile_id"], self.db)
        self.assertEqual(by_name, by_id)
        with self.assertRaisesRegex(ValueError, "was not found"):
            await get_profile("missing", self.db)

    async def test_clone_readiness_uses_sample_count(self) -> None:
        created = await self._create_clone("Ready Later")
        self.db.add(
            DBProfileSample(
                id="sample-1",
                profile_id=created["profile_id"],
                audio_path="profiles/sample.wav",
                reference_text="Sample text",
            )
        )
        self.db.commit()
        result = await get_profile(created["profile_id"], self.db)
        self.assertEqual(result["sample_count"], 1)
        self.assertTrue(result["ready_for_generation"])

    def test_decoded_file_is_valid_and_removed(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.wav") as path:
            self.assertEqual(path.read_bytes(), make_wav_bytes())
            saved = path
        self.assertFalse(saved.exists())

    def test_decoded_file_validation_and_cleanup(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.exe") as path:
            self.assertEqual(path.suffix, ".wav")
        with self.assertRaisesRegex(ValueError, "Invalid audio_base64"):
            with decoded_audio_file("invalid%%%", "voice.wav"):
                self.fail("invalid data must not yield")
        with patch("backend.mcp_server.profile_tools.MAX_PROFILE_SAMPLE_BYTES", 8):
            too_large = base64.b64encode(b"123456789").decode("ascii")
            with self.assertRaisesRegex(ValueError, "cannot exceed 50 MB"):
                with decoded_audio_file(too_large, "voice.wav"):
                    self.fail("oversized data must not yield")
        saved = None
        with self.assertRaisesRegex(RuntimeError, "consumer failure"):
            with decoded_audio_file(encoded, "voice.wav") as path:
                saved = path
                raise RuntimeError("consumer failure")
        self.assertIsNotNone(saved)
        self.assertFalse(saved.exists())
        self.assertEqual(MAX_PROFILE_SAMPLE_BYTES, 50 * 1024 * 1024)

    async def test_base64_sample_makes_clone_ready(self) -> None:
        profile = await self._create_clone("Base64 Clone")
        result = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
            capture_id=None,
            filename="reference.wav",
            reference_text="Reference sample.",
            db=self.db,
        )
        self.assertEqual(result["source"], "base64")
        self.assertEqual(result["reference_text_source"], "explicit")
        self.assertEqual(result["sample_count"], 1)
        self.assertTrue(result["ready_for_generation"])

    async def test_sample_source_rules_and_profile_type(self) -> None:
        clone = await self._create_clone("Rules")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await add_profile_sample(
                profile=clone["profile_id"],
                audio_base64=None,
                capture_id=None,
                filename=None,
                reference_text="Text",
                db=self.db,
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await add_profile_sample(
                profile=clone["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id="capture",
                filename="voice.wav",
                reference_text="Text",
                db=self.db,
            )
        with self.assertRaisesRegex(ValueError, "reference_text is required"):
            await add_profile_sample(
                profile=clone["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="voice.wav",
                reference_text="   ",
                db=self.db,
            )

        with fake_preset_modules():
            preset = await create_profile(
                name="Preset",
                description=None,
                language="it",
                voice_type="preset",
                personality=None,
                default_engine=None,
                preset_engine="kokoro",
                preset_voice_id="if_sara",
                db=self.db,
            )
        with self.assertRaisesRegex(ValueError, "Only cloned profiles"):
            await add_profile_sample(
                profile=preset["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="voice.wav",
                reference_text="Text",
                db=self.db,
            )

    async def test_capture_text_precedence(self) -> None:
        profile = await self._create_clone("Capture")
        self._capture("explicit", "Raw text.", "Refined text.")
        explicit = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=None,
            capture_id="explicit",
            filename=None,
            reference_text="Manual exact text.",
            db=self.db,
        )
        explicit_sample = self.db.query(DBProfileSample).filter_by(
            id=explicit["sample_id"]
        ).one()
        self.assertEqual(explicit["reference_text_source"], "explicit")
        self.assertEqual(explicit_sample.reference_text, "Manual exact text.")

        self._capture("raw", "Raw only.", "Must not be selected.")
        raw = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=None,
            capture_id="raw",
            filename=None,
            reference_text=None,
            db=self.db,
        )
        raw_sample = self.db.query(DBProfileSample).filter_by(
            id=raw["sample_id"]
        ).one()
        self.assertEqual(raw["reference_text_source"], "transcript_raw")
        self.assertEqual(raw_sample.reference_text, "Raw only.")

    async def test_capture_errors_never_use_refined_text(self) -> None:
        profile = await self._create_clone("Capture Errors")
        with self.assertRaisesRegex(ValueError, "Capture 'missing' was not found"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="missing",
                filename=None,
                reference_text=None,
                db=self.db,
            )
        self._capture("missing-audio", "Raw.", write_audio=False)
        with self.assertRaisesRegex(ValueError, "audio file is unavailable"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="missing-audio",
                filename=None,
                reference_text=None,
                db=self.db,
            )
        self._capture("refined-only", "   ", "Must not be used.")
        with self.assertRaisesRegex(ValueError, "no usable transcript_raw"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="refined-only",
                filename=None,
                reference_text=None,
                db=self.db,
            )

    async def test_failed_sample_preserves_profile(self) -> None:
        profile = await self._create_clone("Survives")
        with self.assertRaises(ValueError):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64="invalid%%%",
                capture_id=None,
                filename="bad.wav",
                reference_text="Text",
                db=self.db,
            )
        self.assertIsNotNone(
            self.db.query(DBVoiceProfile)
            .filter_by(id=profile["profile_id"])
            .one_or_none()
        )
        self.assertEqual(
            self.db.query(DBProfileSample)
            .filter_by(profile_id=profile["profile_id"])
            .count(),
            0,
        )

    def test_all_existing_and_new_tools_are_registered(self) -> None:
        fake = FakeMCP()
        register_tools(fake)
        self.assertEqual(
            set(fake.registered),
            {
                "voicebox.speak",
                "voicebox.transcribe",
                "voicebox.list_captures",
                "voicebox.list_profiles",
                "voicebox.list_preset_voices",
                "voicebox.create_profile",
                "voicebox.get_profile",
                "voicebox.add_profile_sample",
            },
        )


if __name__ == "__main__":
    unittest.main()
''',
)

replace_once(
    "docs/content/docs/overview/mcp-server.mdx",
    '''| `voicebox.list_profiles` | Available voice profiles (cloned + preset). |
''',
    '''| `voicebox.list_profiles` | Available voice profiles (cloned + preset). |
| `voicebox.list_preset_voices` | Discover built-in Kokoro or Qwen CustomVoice speakers. |
| `voicebox.create_profile` | Create preset or cloned profile metadata for one person. |
| `voicebox.get_profile` | Read complete profile metadata, counts, and readiness. |
| `voicebox.add_profile_sample` | Add a Base64 or existing Capture sample to a cloned profile. |
''',
)
replace_once(
    "docs/content/docs/overview/mcp-server.mdx",
    '''No args → `{ profiles: [{ id, name, voice_type, language, has_personality }] }`.

## Voice resolution
''',
    '''No args → `{ profiles: [{ id, name, voice_type, language, has_personality }] }`.

## Create a voice profile

Profile creation and speech generation are separate:

1. Discover a preset with `voicebox.list_preset_voices`, or choose a cloning engine.
2. Create the person's metadata with `voicebox.create_profile`.
3. For cloned profiles, attach one or more references with `voicebox.add_profile_sample`.
4. Generate audio with the existing `voicebox.speak` tool.

`description` is informational. `personality` controls in-character rewriting
when `voicebox.speak` is called with `personality: true`.

Base64 samples require an exact `reference_text`. Capture samples use an
explicit `reference_text` first and otherwise `transcript_raw`;
`transcript_refined` is never selected automatically.

A preset profile is ready immediately after successful creation. A cloned
profile becomes ready after its first valid sample.

## Voice resolution
''',
)

write(
    ".github/workflows/ci.yml",
    '''name: CI

on:
  pull_request:
  push:
    branches:
      - main

jobs:
  frontend-quality:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Setup Bun
        uses: oven-sh/setup-bun@v2

      - name: Install dependencies
        run: bun install --frozen-lockfile

      - name: Typecheck app + web
        run: bun run typecheck

      - name: Build web smoke test
        run: bun run build:web

  backend-mcp-profile-tools:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install lightweight backend test dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install \
            "numpy>=1.24,<2" \
            "fastapi>=0.109" \
            "pydantic>=2.5" \
            "sqlalchemy>=2" \
            "fastmcp>=3,<4" \
            "soundfile>=0.12" \
            "librosa>=0.10" \
            "Pillow>=10" \
            "python-multipart>=0.0.6"

      - name: Run MCP profile tests
        run: python -m unittest backend.tests.test_mcp_profile_tools -v

      - name: Compile backend
        run: python -m compileall -q backend
''',
)

print("MCP profile management files applied successfully")
