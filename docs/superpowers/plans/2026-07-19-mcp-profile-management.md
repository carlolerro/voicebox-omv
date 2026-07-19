# MCP Profile Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add MCP tools for preset discovery, profile creation, complete profile retrieval, and cloned-voice sample attachment from Base64 or an existing Capture, while retaining `voicebox.speak` for generation.

**Architecture:** Create `backend/mcp_server/profile_tools.py` with core functions that accept a SQLAlchemy session and thin FastMCP wrappers that own production sessions. Reuse Voicebox profile services and storage helpers. Move preset discovery into `services/profiles.py` so REST and MCP share the same logic.

**Tech Stack:** Python 3.11, FastMCP 3.x, FastAPI, Pydantic 2.x, SQLAlchemy 2.x, SQLite, standard-library `unittest`, Docker.

## Global Constraints

- Branch: `feature/mcp-profile-management`.
- New tools: `voicebox.list_preset_voices`, `voicebox.create_profile`, `voicebox.get_profile`, `voicebox.add_profile_sample`.
- Existing `/mcp` endpoint and `voicebox.speak` generation flow remain unchanged.
- Creation accepts only `preset` and `cloned`; it never accepts audio and never generates audio.
- Preset engines: `kokoro`, `qwen_custom_voice`.
- Cloning engines remain those in `profiles.CLONING_ENGINES`.
- Base64 decoding is strict; decoded maximum is 50 MiB; no client path parameter is exposed.
- Capture text precedence: explicit `reference_text`, then `transcript_raw`, then error. Never select `transcript_refined` automatically.
- Only cloned profiles accept samples.
- No migrations, frontend changes, test/comparison entities, export/import changes, parallel TTS, new service, or new Dockerfile.
- Tests use valid synthetic reference audio but never invoke TTS inference or require a GPU.

## File Map

- Create `backend/mcp_server/profile_tools.py` — core operations and MCP registration.
- Create `backend/tests/__init__.py` — package marker.
- Create `backend/tests/test_mcp_profile_tools.py` — isolated database/storage tests.
- Modify `backend/services/profiles.py` — shared preset discovery.
- Modify `backend/routes/profiles.py` — use shared discovery.
- Modify `backend/mcp_server/tools.py` — register the new module.
- Modify `backend/mcp_server/server.py` — update MCP instructions.
- Modify `docs/content/docs/overview/mcp-server.mdx` — document the workflow.

---

### Task 1: Shared Preset Discovery

**Files:**
- Modify: `backend/services/profiles.py`
- Modify: `backend/routes/profiles.py:71-109`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces: `profiles.list_preset_voices(engine: str) -> dict[str, object]`.

- [ ] **Step 1: Write the failing tests and fixture**

Create empty `backend/tests/__init__.py` and create `backend/tests/test_mcp_profile_tools.py`:

```python
import base64
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config
from backend.database import Base
from backend.services import profiles as profiles_service


def make_wav_bytes() -> bytes:
    """Three seconds of non-silent 16 kHz mono PCM, valid for clone checks."""
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


class MCPProfileToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
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

    def test_list_kokoro_preset_voices(self) -> None:
        result = profiles_service.list_preset_voices("kokoro")
        self.assertEqual(result["engine"], "kokoro")
        self.assertGreater(len(result["voices"]), 0)
        self.assertEqual(
            set(result["voices"][0]),
            {"voice_id", "name", "gender", "language"},
        )

    def test_list_qwen_custom_preset_voices(self) -> None:
        result = profiles_service.list_preset_voices("qwen_custom_voice")
        self.assertEqual(result["engine"], "qwen_custom_voice")
        self.assertGreater(len(result["voices"]), 0)

    def test_list_preset_voices_rejects_unknown_engine(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported preset engine"):
            profiles_service.list_preset_voices("unknown")
```

- [ ] **Step 2: Prove the tests fail**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: failures because `list_preset_voices` is absent.

- [ ] **Step 3: Implement shared discovery**

Add after `CLONING_ENGINES` in `backend/services/profiles.py`:

```python
PRESET_ENGINES = {"kokoro", "qwen_custom_voice"}


def list_preset_voices(engine: str) -> dict[str, object]:
    if engine == "kokoro":
        from ..backends.kokoro_backend import KOKORO_VOICES

        return {
            "engine": engine,
            "voices": [
                {
                    "voice_id": voice_id,
                    "name": name,
                    "gender": gender,
                    "language": language,
                }
                for voice_id, name, gender, language in KOKORO_VOICES
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
                    "language": language,
                }
                for speaker_id, display_name, gender, language, _description
                in QWEN_CUSTOM_VOICES
            ],
        }
    supported = ", ".join(sorted(PRESET_ENGINES))
    raise ValueError(
        f"Unsupported preset engine '{engine}'. Supported engines: {supported}."
    )
```

Replace the REST route body:

```python
@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    try:
        return profiles.list_preset_voices(engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 4: Verify passing tests**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/services/profiles.py backend/routes/profiles.py
```

Expected: tests pass; compilation exits 0.

- [ ] **Step 5: Commit**

```bash
git add backend/services/profiles.py backend/routes/profiles.py backend/tests
git commit -m "refactor: share preset voice discovery"
```

---

### Task 2: Profile Creation, Serialization, and Retrieval

**Files:**
- Create: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces `serialize_profile`, `create_profile`, and `get_profile`.

- [ ] **Step 1: Add failing tests**

Append imports:

```python
from backend.database import ProfileSample as DBProfileSample
from backend.database import VoiceProfile as DBVoiceProfile
from backend.mcp_server.profile_tools import create_profile, get_profile
```

Append tests:

```python
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
        voice_id = profiles_service.list_preset_voices("kokoro")["voices"][0]["voice_id"]
        result = await create_profile(
            name="Preset Person",
            description=None,
            language="en",
            voice_type="preset",
            personality=None,
            default_engine=None,
            preset_engine="kokoro",
            preset_voice_id=voice_id,
            db=self.db,
        )
        self.assertEqual(result["default_engine"], "kokoro")
        self.assertTrue(result["ready_for_generation"])

    async def test_create_rejects_unsupported_profile_and_preset_engine(self) -> None:
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

    async def test_create_preserves_native_validation(self) -> None:
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

    async def test_create_rejects_duplicate_name(self) -> None:
        arguments = dict(
            name="Duplicate",
            description=None,
            language="en",
            voice_type="cloned",
            personality=None,
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )
        await create_profile(**arguments)
        with self.assertRaisesRegex(ValueError, "already exists"):
            await create_profile(**arguments)

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
        self.assertEqual(
            await get_profile("case display", self.db),
            await get_profile(created["profile_id"], self.db),
        )

    async def test_get_unknown_profile_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Voice profile 'missing' was not found"):
            await get_profile("missing", self.db)

    async def test_clone_readiness_uses_sample_count(self) -> None:
        created = await create_profile(
            name="Ready Later",
            description=None,
            language="en",
            voice_type="cloned",
            personality=None,
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )
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
```

- [ ] **Step 2: Prove the module is missing**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: `ModuleNotFoundError` for `profile_tools`.

- [ ] **Step 3: Implement the core module**

Create `backend/mcp_server/profile_tools.py`:

```python
"""MCP tools for Voicebox profile management."""

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
    ".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".opus"
}
MCP_PROFILE_TYPES = {"cloned", "preset"}


def serialize_profile(profile: DBVoiceProfile, db: Session) -> dict[str, Any]:
    sample_count = db.query(func.count(DBProfileSample.id)).filter(
        DBProfileSample.profile_id == profile.id
    ).scalar() or 0
    generation_count = db.query(func.count(DBGeneration.id)).filter(
        DBGeneration.profile_id == profile.id
    ).scalar() or 0
    voice_type = getattr(profile, "voice_type", None) or "cloned"
    ready = (
        bool(profile.preset_engine and profile.preset_voice_id)
        if voice_type == "preset"
        else sample_count > 0
    )
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
    return serialize_profile(_resolve_profile(profile, db), db)
```

- [ ] **Step 4: Verify tests**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/mcp_server/profile_tools.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add MCP profile creation and retrieval"
```

---

### Task 3: Strict Base64 Temporary File Handling

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces `decoded_audio_file(audio_base64, filename) -> Iterator[Path]` with guaranteed cleanup.

- [ ] **Step 1: Add failing tests**

Update the import to include `MAX_PROFILE_SAMPLE_BYTES` and `decoded_audio_file`, then append:

```python
    def test_decoded_file_is_valid_and_removed(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.wav") as path:
            self.assertEqual(path.read_bytes(), make_wav_bytes())
            saved = path
        self.assertFalse(saved.exists())

    def test_unsupported_filename_suffix_becomes_wav(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.exe") as path:
            self.assertEqual(path.suffix, ".wav")

    def test_invalid_base64_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid audio_base64"):
            with decoded_audio_file("invalid%%%", "voice.wav"):
                self.fail("invalid data must not yield")

    def test_size_limit_is_checked_on_decoded_bytes(self) -> None:
        with patch("backend.mcp_server.profile_tools.MAX_PROFILE_SAMPLE_BYTES", 8):
            encoded = base64.b64encode(b"123456789").decode("ascii")
            with self.assertRaisesRegex(ValueError, "cannot exceed 50 MB"):
                with decoded_audio_file(encoded, "voice.wav"):
                    self.fail("oversized data must not yield")

    def test_cleanup_occurs_when_consumer_raises(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        saved = None
        with self.assertRaisesRegex(RuntimeError, "consumer failure"):
            with decoded_audio_file(encoded, "voice.wav") as path:
                saved = path
                raise RuntimeError("consumer failure")
        self.assertIsNotNone(saved)
        self.assertFalse(saved.exists())
```

- [ ] **Step 2: Prove the symbol is absent**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import failure for `decoded_audio_file`.

- [ ] **Step 3: Implement strict decoding and cleanup**

Append:

```python
def _sample_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SAMPLE_SUFFIXES else ".wav"


@contextmanager
def decoded_audio_file(audio_base64: str, filename: str | None) -> Iterator[Path]:
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
```

- [ ] **Step 4: Verify tests and commit**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: validate MCP Base64 samples safely"
```

Expected: tests pass and commit succeeds.

---

### Task 4: Base64 and Capture Sample Attachment

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces `add_profile_sample(..., db) -> dict[str, Any]`.

- [ ] **Step 1: Add failing operation tests**

Import `DBCapture` and `add_profile_sample`. Add helpers:

```python
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
```

Add tests:

```python
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

    async def test_exactly_one_source_and_base64_text_are_required(self) -> None:
        profile = await self._create_clone("Rules")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await add_profile_sample(
                profile=profile["profile_id"], audio_base64=None,
                capture_id=None, filename=None, reference_text="Text", db=self.db
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id="capture", filename="voice.wav",
                reference_text="Text", db=self.db
            )
        with self.assertRaisesRegex(ValueError, "reference_text is required"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None, filename="voice.wav",
                reference_text="   ", db=self.db
            )

    async def test_non_clone_rejects_samples(self) -> None:
        voice_id = profiles_service.list_preset_voices("kokoro")["voices"][0]["voice_id"]
        profile = await create_profile(
            name="Preset", description=None, language="en",
            voice_type="preset", personality=None, default_engine=None,
            preset_engine="kokoro", preset_voice_id=voice_id, db=self.db
        )
        with self.assertRaisesRegex(ValueError, "Only cloned profiles"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None, filename="voice.wav",
                reference_text="Text", db=self.db
            )

    async def test_capture_text_precedence(self) -> None:
        profile = await self._create_clone("Capture")
        self._capture("explicit", "Raw text.", "Refined text.")
        explicit = await add_profile_sample(
            profile=profile["profile_id"], audio_base64=None,
            capture_id="explicit", filename=None,
            reference_text="Manual exact text.", db=self.db
        )
        explicit_sample = self.db.query(DBProfileSample).filter_by(
            id=explicit["sample_id"]
        ).one()
        self.assertEqual(explicit["reference_text_source"], "explicit")
        self.assertEqual(explicit_sample.reference_text, "Manual exact text.")

        self._capture("raw", "Raw only.", "Must not be selected.")
        raw = await add_profile_sample(
            profile=profile["profile_id"], audio_base64=None,
            capture_id="raw", filename=None, reference_text=None, db=self.db
        )
        raw_sample = self.db.query(DBProfileSample).filter_by(id=raw["sample_id"]).one()
        self.assertEqual(raw["reference_text_source"], "transcript_raw")
        self.assertEqual(raw_sample.reference_text, "Raw only.")

    async def test_capture_errors_do_not_use_refined_text(self) -> None:
        profile = await self._create_clone("Capture Errors")
        with self.assertRaisesRegex(ValueError, "Capture 'missing' was not found"):
            await add_profile_sample(
                profile=profile["profile_id"], audio_base64=None,
                capture_id="missing", filename=None,
                reference_text=None, db=self.db
            )
        self._capture("missing-audio", "Raw.", write_audio=False)
        with self.assertRaisesRegex(ValueError, "audio file is unavailable"):
            await add_profile_sample(
                profile=profile["profile_id"], audio_base64=None,
                capture_id="missing-audio", filename=None,
                reference_text=None, db=self.db
            )
        self._capture("refined-only", "   ", "Must not be used.")
        with self.assertRaisesRegex(ValueError, "no usable transcript_raw"):
            await add_profile_sample(
                profile=profile["profile_id"], audio_base64=None,
                capture_id="refined-only", filename=None,
                reference_text=None, db=self.db
            )

    async def test_failed_sample_preserves_profile(self) -> None:
        profile = await self._create_clone("Survives")
        with self.assertRaises(ValueError):
            await add_profile_sample(
                profile=profile["profile_id"], audio_base64="invalid%%%",
                capture_id=None, filename="bad.wav",
                reference_text="Text", db=self.db
            )
        self.assertIsNotNone(
            self.db.query(DBVoiceProfile).filter_by(id=profile["profile_id"]).one_or_none()
        )
        self.assertEqual(
            self.db.query(DBProfileSample).filter_by(
                profile_id=profile["profile_id"]
            ).count(),
            0,
        )
```

- [ ] **Step 2: Prove `add_profile_sample` is absent**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import failure for `add_profile_sample`.

- [ ] **Step 3: Implement both source paths**

Append to `profile_tools.py`:

```python
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
    row = _resolve_profile(profile, db)
    if (getattr(row, "voice_type", None) or "cloned") != "cloned":
        raise ValueError("Only cloned profiles can receive cloned voice samples.")
    if bool(audio_base64) == bool(capture_id):
        raise ValueError("Pass exactly one of audio_base64 or capture_id.")

    if audio_base64 is not None:
        clean_text = (reference_text or "").strip()
        if not clean_text:
            raise ValueError("reference_text is required for audio_base64 samples.")
        clean_text = models.ProfileSampleCreate(reference_text=clean_text).reference_text
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
        clean_text = models.ProfileSampleCreate(reference_text=explicit).reference_text
        text_source = "explicit"
    else:
        raw = (capture.transcript_raw or "").strip()
        if not raw:
            raise ValueError(
                f"Capture '{capture_id}' has no usable transcript_raw; "
                "pass reference_text explicitly."
            )
        clean_text = models.ProfileSampleCreate(reference_text=raw).reference_text
        text_source = "transcript_raw"

    sample = await profiles_service.add_profile_sample(row.id, str(path), clean_text, db)
    return _sample_response(row, sample.id, "capture", text_source, db)
```

- [ ] **Step 4: Verify and commit**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add MCP profile sample sources"
```

Expected: all tests pass without TTS inference.

---

### Task 5: Register FastMCP Tools Without Regressing Existing Tools

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/mcp_server/tools.py:21-27,195-222`
- Modify: `backend/mcp_server/server.py:27-39`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces `register_profile_tools(mcp: FastMCP) -> None`.

- [ ] **Step 1: Add a failing registration test**

Append:

```python
from backend.mcp_server.tools import register_tools


class FakeMCP:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def decorator(function):
            self.registered[name] = function
            return function
        return decorator
```

Add:

```python
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
```

- [ ] **Step 2: Prove the new names are missing**

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_all_existing_and_new_tools_are_registered \
  -v
```

Expected: failure listing the four missing names.

- [ ] **Step 3: Register thin wrappers**

Append to `profile_tools.py`:

```python
def register_profile_tools(mcp: FastMCP) -> None:
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
                name=name, description=description, language=language,
                voice_type=voice_type, personality=personality,
                default_engine=default_engine, preset_engine=preset_engine,
                preset_voice_id=preset_voice_id, db=db
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
                profile=profile, audio_base64=audio_base64,
                capture_id=capture_id, filename=filename,
                reference_text=reference_text, db=db
            )
        finally:
            db.close()
```

Add to `backend/mcp_server/tools.py`:

```python
from .profile_tools import register_profile_tools
```

At the end of `register_tools`, before the top-level speak helper:

```python
    register_profile_tools(mcp)
```

Replace server instructions:

```python
        instructions=(
            "Voicebox is a local voice I/O layer. Inspect profiles with "
            "`voicebox.list_profiles` and `voicebox.get_profile`; create them "
            "with `voicebox.list_preset_voices`, `voicebox.create_profile`, "
            "and `voicebox.add_profile_sample`; generate speech with "
            "`voicebox.speak`; and transcribe audio with `voicebox.transcribe`."
        ),
```

- [ ] **Step 4: Verify and commit**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/mcp_server backend/services backend/routes
git add backend/mcp_server backend/tests/test_mcp_profile_tools.py
git commit -m "feat: expose profile management through MCP"
```

Expected: all tests and compilation pass.

---

### Task 6: Documentation, Docker Smoke Test, and Pull Request

**Files:**
- Modify: `docs/content/docs/overview/mcp-server.mdx`
- Verify unchanged: `Dockerfile`, `docker-compose.yml`

**Interfaces:**
- Produces documented workflow and live `/mcp` evidence.

- [ ] **Step 1: Add workflow documentation**

Add:

```markdown
## Create a voice profile

Profile creation and speech generation are separate:

1. Discover a preset with `voicebox.list_preset_voices`, or choose a cloning engine.
2. Create metadata with `voicebox.create_profile`.
3. For cloned profiles, attach references with `voicebox.add_profile_sample`.
4. Generate audio with the existing `voicebox.speak` tool.

`description` is informational. `personality` controls in-character rewriting when `voicebox.speak` is called with `personality: true`.

Base64 samples require an exact `reference_text`. Capture samples use explicit `reference_text` first and otherwise `transcript_raw`; `transcript_refined` is not selected automatically.
```

- [ ] **Step 2: Run all non-GPU checks**

```bash
python -m unittest discover -s backend/tests -p "test_mcp_profile_tools.py" -v
python -m compileall -q backend
bun run typecheck
bun run build:web
```

Expected: all checks pass.

- [ ] **Step 3: Build the existing image and start an isolated container**

```bash
docker build -t voicebox-mcp-profile:test .
docker volume create voicebox-mcp-profile-test-data
docker run -d --rm \
  --name voicebox-mcp-profile-test \
  -p 127.0.0.1:17601:17493 \
  -v voicebox-mcp-profile-test-data:/app/data \
  voicebox-mcp-profile:test
for attempt in $(seq 1 60); do
  curl -fsS http://127.0.0.1:17601/health >/dev/null && break
  sleep 2
done
curl -fsS http://127.0.0.1:17601/health
```

Expected: health succeeds within 120 seconds.

- [ ] **Step 4: Verify live tool discovery**

```bash
docker exec -i voicebox-mcp-profile-test python - <<'PY'
import asyncio
from fastmcp import Client

EXPECTED = {
    "voicebox.speak", "voicebox.transcribe", "voicebox.list_captures",
    "voicebox.list_profiles", "voicebox.list_preset_voices",
    "voicebox.create_profile", "voicebox.get_profile",
    "voicebox.add_profile_sample",
}

async def main():
    async with Client("http://127.0.0.1:17493/mcp") as client:
        names = {tool.name for tool in await client.list_tools()}
        assert not EXPECTED - names, sorted(EXPECTED - names)
        print(sorted(EXPECTED))

asyncio.run(main())
PY
```

Expected: eight tool names are printed.

- [ ] **Step 5: Run a metadata-only live workflow**

```bash
docker exec -i voicebox-mcp-profile-test python - <<'PY'
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:17493/mcp") as client:
        print(await client.call_tool(
            "voicebox.create_profile",
            {
                "name": "MCP Smoke Clone",
                "description": "Temporary smoke profile.",
                "language": "it",
                "voice_type": "cloned",
                "default_engine": "qwen",
                "personality": "Parla in modo chiaro e diretto.",
            },
        ))
        print(await client.call_tool(
            "voicebox.get_profile", {"profile": "mcp smoke clone"}
        ))

asyncio.run(main())
PY
```

Expected: creation and lookup succeed; the clone has zero samples and is not ready.

- [ ] **Step 6: Clean up, verify scope, and commit docs**

```bash
docker stop voicebox-mcp-profile-test
docker volume rm voicebox-mcp-profile-test-data
git add docs/content/docs/overview/mcp-server.mdx
git commit -m "docs: document MCP profile workflow"
git diff --check main...HEAD
if git diff --name-only main...HEAD | grep -E \
  'backend/database/migrations|app/src|backend/services/export_import.py|docker-compose.yml|Dockerfile'; then
  echo "Unexpected out-of-scope file change" >&2
  exit 1
fi
```

Expected: smoke resources removed, documentation committed, no prohibited files changed.

- [ ] **Step 7: Open the pull request**

```bash
gh pr create \
  --base main \
  --head feature/mcp-profile-management \
  --title "feat: manage Voicebox profiles through MCP" \
  --body "Adds preset discovery, profile creation, complete profile lookup, and Base64/Capture sample attachment through MCP. Reuses existing Voicebox services and keeps voicebox.speak as the generation tool. Includes non-GPU tests and Docker MCP smoke verification."
```

Expected: a reviewable, unmerged pull request is created.
