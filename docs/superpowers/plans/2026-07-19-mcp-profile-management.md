# MCP Profile Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four MCP tools that discover preset voices, create Voicebox profiles, retrieve complete profile metadata, and attach cloned-voice samples from strict Base64 audio or an existing Capture, while continuing to use `voicebox.speak` for generation.

**Architecture:** Add a focused `backend/mcp_server/profile_tools.py` adapter with testable core functions that receive a SQLAlchemy session and thin FastMCP wrappers that open and close production sessions. Reuse `models.VoiceProfileCreate`, `services.profiles.create_profile`, `services.profiles.add_profile_sample`, Voicebox storage helpers, and the existing profile resolver. Move preset-voice discovery into `services/profiles.py` so REST and MCP use one implementation.

**Tech Stack:** Python 3.11, FastMCP 3.x, FastAPI, Pydantic 2.x, SQLAlchemy 2.x, SQLite, standard-library `unittest`, Docker multi-stage build.

## Global Constraints

- Work only on `feature/mcp-profile-management`.
- Keep the MCP endpoint at `/mcp`; do not add another service or container.
- Add exactly `voicebox.list_preset_voices`, `voicebox.create_profile`, `voicebox.get_profile`, and `voicebox.add_profile_sample`.
- Keep `voicebox.speak` as the only generation tool; profile creation never generates audio.
- Support only `voice_type="preset"` and `voice_type="cloned"` in the new creation tool.
- Initial preset engines are exactly `kokoro` and `qwen_custom_voice`.
- Cloning engines remain exactly `qwen`, `luxtts`, `chatterbox`, `chatterbox_turbo`, and `tada`.
- Base64 samples use strict decoding, are limited to 50 MiB decoded, and never expose a client-supplied path.
- Capture text precedence is explicit non-empty `reference_text`, then non-empty `transcript_raw`, then error; never use `transcript_refined` automatically.
- Reject sample addition for all non-cloned profiles.
- Do not add migrations, frontend changes, profile-test entities, comparison workflows, parallel TTS inference, export/import changes, or a new Docker build definition.
- Tests must not run TTS inference or require a GPU.

---

## File Structure

- Create `backend/mcp_server/profile_tools.py`: core profile operations, Base64/Capture source handling, serialization, and MCP registration.
- Create `backend/tests/__init__.py`: test package marker.
- Create `backend/tests/test_mcp_profile_tools.py`: in-memory database and temporary-storage tests.
- Modify `backend/services/profiles.py`: shared preset discovery.
- Modify `backend/routes/profiles.py`: REST delegation to shared preset discovery.
- Modify `backend/mcp_server/tools.py`: register profile tools.
- Modify `backend/mcp_server/server.py`: advertise the new workflow.
- Modify `docs/content/docs/overview/mcp-server.mdx`: document create → sample → speak.

---

### Task 1: Shared Preset-Voice Discovery

**Files:**
- Modify: `backend/services/profiles.py`
- Modify: `backend/routes/profiles.py:71-109`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: existing `KOKORO_VOICES` and `QWEN_CUSTOM_VOICES` constants.
- Produces: `profiles.list_preset_voices(engine: str) -> dict[str, object]`.

- [ ] **Step 1: Write the failing preset-discovery tests**

Create an empty `backend/tests/__init__.py`.

Create `backend/tests/test_mcp_profile_tools.py`:

```python
import base64
import io
import tempfile
import unittest
import wave
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config
from backend.database import Base
from backend.services import profiles as profiles_service


def make_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 1_600)
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

- [ ] **Step 2: Run the tests and verify the failure**

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_kokoro_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_qwen_custom_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_preset_voices_rejects_unknown_engine \
  -v
```

Expected: three failures because `profiles_service.list_preset_voices` does not exist.

- [ ] **Step 3: Implement the shared service and REST delegation**

Add after `CLONING_ENGINES` in `backend/services/profiles.py`:

```python
PRESET_ENGINES = {"kokoro", "qwen_custom_voice"}


def list_preset_voices(engine: str) -> dict[str, object]:
    """Return normalized preset voice metadata for a supported engine."""
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
            for speaker_id, display_name, gender, language, _description in QWEN_CUSTOM_VOICES
        ]
        return {"engine": engine, "voices": voices}

    supported = ", ".join(sorted(PRESET_ENGINES))
    raise ValueError(
        f"Unsupported preset engine '{engine}'. Supported engines: {supported}."
    )
```

Replace the current route implementation in `backend/routes/profiles.py`:

```python
@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    """List available preset voices for an engine."""
    try:
        return profiles.list_preset_voices(engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 4: Run tests and compilation**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/services/profiles.py backend/routes/profiles.py
```

Expected: three tests pass; compilation exits 0.

- [ ] **Step 5: Commit**

```bash
git add backend/services/profiles.py backend/routes/profiles.py backend/tests
git commit -m "refactor: share preset voice discovery"
```

---

### Task 2: Complete Profile Creation and Retrieval Core

**Files:**
- Create: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `models.VoiceProfileCreate`, `profiles_service.create_profile`, and `profiles_service.get_profile_orm_by_name_or_id`.
- Produces:
  - `serialize_profile(profile: DBVoiceProfile, db: Session) -> dict[str, Any]`
  - `create_profile(..., db: Session) -> dict[str, Any]`
  - `get_profile(profile: str, db: Session) -> dict[str, Any]`

- [ ] **Step 1: Add failing creation and retrieval tests**

Append imports:

```python
from backend.database import ProfileSample as DBProfileSample
from backend.database import VoiceProfile as DBVoiceProfile
from backend.mcp_server.profile_tools import create_profile, get_profile
```

Append methods to `MCPProfileToolsTestCase`:

```python
    async def test_create_cloned_profile_returns_complete_metadata(self) -> None:
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
        self.assertEqual(result["name"], "Carlo")
        self.assertEqual(result["description"], "Voce italiana per spiegazioni tecniche.")
        self.assertEqual(result["personality"], "Carlo parla in modo concreto.")
        self.assertEqual(result["voice_type"], "cloned")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["generation_count"], 0)
        self.assertFalse(result["ready_for_generation"])

    async def test_create_preset_profile_is_ready(self) -> None:
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
        self.assertEqual(result["preset_voice_id"], voice_id)
        self.assertTrue(result["ready_for_generation"])

    async def test_create_profile_rejects_designed_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "voice_type must be 'cloned' or 'preset'"):
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

    async def test_create_profile_preserves_native_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Preset profiles require"):
            await create_profile(
                name="Broken Preset",
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
                name="Broken Clone",
                description=None,
                language="en",
                voice_type="cloned",
                personality=None,
                default_engine="kokoro",
                preset_engine=None,
                preset_voice_id=None,
                db=self.db,
            )

    async def test_create_profile_rejects_duplicate_name(self) -> None:
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

    async def test_get_profile_by_case_insensitive_name_and_id(self) -> None:
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
        self.assertEqual(by_name["description"], "Metadata")
        self.assertEqual(by_name["personality"], "Direct.")

    async def test_get_profile_rejects_unknown_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "Voice profile 'missing' was not found"):
            await get_profile("missing", self.db)

    async def test_cloned_profile_readiness_uses_sample_count(self) -> None:
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

- [ ] **Step 2: Run and verify the missing-module failure**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import fails because `backend.mcp_server.profile_tools` does not exist.

- [ ] **Step 3: Implement profile serialization, creation, and retrieval**

Create `backend/mcp_server/profile_tools.py`:

```python
"""MCP tools for Voicebox voice-profile management."""

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
    row = db.query(DBVoiceProfile).filter(DBVoiceProfile.id == created.id).one()
    return serialize_profile(row, db)


async def get_profile(profile: str, db: Session) -> dict[str, Any]:
    return serialize_profile(_resolve_profile(profile, db), db)
```

- [ ] **Step 4: Run the complete test module**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/mcp_server/profile_tools.py
```

Expected: all tests pass; compilation exits 0.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add MCP profile creation and retrieval core"
```

---

### Task 3: Strict Base64 Temporary-Audio Helper

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Produces: `decoded_audio_file(audio_base64: str, filename: str | None) -> Iterator[Path]`.
- Guarantees: strict decode, 50 MiB limit, supported suffix only, cleanup on normal and exceptional exit.

- [ ] **Step 1: Add failing helper tests**

Update the import:

```python
from backend.mcp_server.profile_tools import (
    MAX_PROFILE_SAMPLE_BYTES,
    create_profile,
    decoded_audio_file,
    get_profile,
)
```

Append methods:

```python
    def test_decoded_audio_file_writes_and_removes_valid_audio(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.wav") as path:
            self.assertEqual(path.suffix, ".wav")
            self.assertEqual(path.read_bytes(), make_wav_bytes())
            saved_path = path
        self.assertFalse(saved_path.exists())

    def test_decoded_audio_file_uses_wav_for_unsupported_suffix(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        with decoded_audio_file(encoded, "voice.exe") as path:
            self.assertEqual(path.suffix, ".wav")

    def test_decoded_audio_file_rejects_invalid_base64(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid audio_base64"):
            with decoded_audio_file("invalid%%%", "voice.wav"):
                self.fail("invalid Base64 must not yield a path")

    def test_decoded_audio_file_rejects_payload_over_50_mb(self) -> None:
        encoded = base64.b64encode(
            b"x" * (MAX_PROFILE_SAMPLE_BYTES + 1)
        ).decode("ascii")
        with self.assertRaisesRegex(ValueError, "cannot exceed 50 MB"):
            with decoded_audio_file(encoded, "voice.wav"):
                self.fail("oversized audio must not yield a path")

    def test_decoded_audio_file_cleans_up_after_consumer_error(self) -> None:
        encoded = base64.b64encode(make_wav_bytes()).decode("ascii")
        saved_path = None
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            with decoded_audio_file(encoded, "voice.wav") as path:
                saved_path = path
                raise RuntimeError("consumer failed")
        self.assertIsNotNone(saved_path)
        self.assertFalse(saved_path.exists())
```

- [ ] **Step 2: Run and verify the missing-symbol failure**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import fails because `decoded_audio_file` is not defined.

- [ ] **Step 3: Implement the complete helper**

Append to `backend/mcp_server/profile_tools.py`:

```python
def _sample_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SAMPLE_SUFFIXES else ".wav"


@contextmanager
def decoded_audio_file(
    audio_base64: str,
    filename: str | None,
) -> Iterator[Path]:
    try:
        raw = b64.b64decode(audio_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid audio_base64: {exc}") from exc
    if len(raw) > MAX_PROFILE_SAMPLE_BYTES:
        raise ValueError("Profile samples cannot exceed 50 MB.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=_sample_suffix(filename),
            delete=False,
        ) as temporary:
            temporary.write(raw)
            temporary_path = Path(temporary.name)
        yield temporary_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run helper and existing tests**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: validate MCP Base64 audio safely"
```

---

### Task 4: Add Samples from Base64 or Capture

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `_resolve_profile`, `decoded_audio_file`, `config.resolve_storage_path`, and `profiles_service.add_profile_sample`.
- Produces: `add_profile_sample(..., db: Session) -> dict[str, Any]` supporting both approved sources.

- [ ] **Step 1: Add failing sample-operation tests**

Append imports:

```python
from backend.database import Capture as DBCapture
from backend.mcp_server.profile_tools import add_profile_sample
```

Append helpers and tests:

```python
    async def _create_cloned_profile(self, name: str) -> dict:
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

    def _create_capture(
        self,
        *,
        capture_id: str,
        transcript_raw: str,
        transcript_refined: str | None = None,
        write_audio: bool = True,
    ) -> DBCapture:
        path = config.get_captures_dir() / f"{capture_id}.wav"
        if write_audio:
            path.write_bytes(make_wav_bytes())
        capture = DBCapture(
            id=capture_id,
            audio_path=config.to_storage_path(path),
            source="file",
            language="en",
            transcript_raw=transcript_raw,
            transcript_refined=transcript_refined,
        )
        self.db.add(capture)
        self.db.commit()
        return capture

    async def test_add_base64_sample_makes_clone_ready(self) -> None:
        profile = await self._create_cloned_profile("Base64 Clone")
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

    async def test_add_sample_requires_exactly_one_source(self) -> None:
        profile = await self._create_cloned_profile("Source Rules")
        with self.assertRaisesRegex(ValueError, "exactly one of audio_base64 or capture_id"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id=None,
                filename=None,
                reference_text="Text",
                db=self.db,
            )
        with self.assertRaisesRegex(ValueError, "exactly one of audio_base64 or capture_id"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id="capture-id",
                filename="voice.wav",
                reference_text="Text",
                db=self.db,
            )

    async def test_add_base64_sample_requires_reference_text(self) -> None:
        profile = await self._create_cloned_profile("Base64 Text")
        with self.assertRaisesRegex(ValueError, "reference_text is required"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="voice.wav",
                reference_text="   ",
                db=self.db,
            )

    async def test_add_sample_rejects_preset_profile(self) -> None:
        voice_id = profiles_service.list_preset_voices("kokoro")["voices"][0]["voice_id"]
        profile = await create_profile(
            name="Preset No Samples",
            description=None,
            language="en",
            voice_type="preset",
            personality=None,
            default_engine=None,
            preset_engine="kokoro",
            preset_voice_id=voice_id,
            db=self.db,
        )
        with self.assertRaisesRegex(ValueError, "cannot receive cloned voice samples"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="voice.wav",
                reference_text="Text",
                db=self.db,
            )

    async def test_capture_uses_explicit_text_before_raw(self) -> None:
        profile = await self._create_cloned_profile("Capture Explicit")
        self._create_capture(
            capture_id="capture-explicit",
            transcript_raw="Raw transcript.",
            transcript_refined="Refined transcript.",
        )
        result = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=None,
            capture_id="capture-explicit",
            filename=None,
            reference_text="Exact manual transcript.",
            db=self.db,
        )
        sample = self.db.query(DBProfileSample).filter_by(id=result["sample_id"]).one()
        self.assertEqual(result["source"], "capture")
        self.assertEqual(result["reference_text_source"], "explicit")
        self.assertEqual(sample.reference_text, "Exact manual transcript.")

    async def test_capture_falls_back_only_to_raw_transcript(self) -> None:
        profile = await self._create_cloned_profile("Capture Raw")
        self._create_capture(
            capture_id="capture-raw",
            transcript_raw="Raw transcript only.",
            transcript_refined="Refined text must not be selected.",
        )
        result = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=None,
            capture_id="capture-raw",
            filename=None,
            reference_text=None,
            db=self.db,
        )
        sample = self.db.query(DBProfileSample).filter_by(id=result["sample_id"]).one()
        self.assertEqual(result["reference_text_source"], "transcript_raw")
        self.assertEqual(sample.reference_text, "Raw transcript only.")

    async def test_capture_errors_are_explicit(self) -> None:
        profile = await self._create_cloned_profile("Capture Errors")
        with self.assertRaisesRegex(ValueError, "Capture 'missing' was not found"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="missing",
                filename=None,
                reference_text=None,
                db=self.db,
            )

        self._create_capture(
            capture_id="missing-audio",
            transcript_raw="Raw.",
            write_audio=False,
        )
        with self.assertRaisesRegex(ValueError, "audio file is unavailable"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="missing-audio",
                filename=None,
                reference_text=None,
                db=self.db,
            )

        self._create_capture(
            capture_id="refined-only",
            transcript_raw="   ",
            transcript_refined="Must not be used.",
        )
        with self.assertRaisesRegex(ValueError, "no usable transcript_raw"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="refined-only",
                filename=None,
                reference_text=None,
                db=self.db,
            )

    async def test_failed_sample_addition_preserves_profile(self) -> None:
        profile = await self._create_cloned_profile("Survives Failure")
        with self.assertRaises(ValueError):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64="invalid%%%",
                capture_id=None,
                filename="bad.wav",
                reference_text="Text",
                db=self.db,
            )
        existing = self.db.query(DBVoiceProfile).filter_by(id=profile["profile_id"]).one_or_none()
        self.assertIsNotNone(existing)
        self.assertEqual(
            self.db.query(DBProfileSample).filter_by(profile_id=profile["profile_id"]).count(),
            0,
        )
```

- [ ] **Step 2: Run and verify the missing-symbol failure**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import fails because `add_profile_sample` is not defined.

- [ ] **Step 3: Implement normalized sample responses and both sources**

Append to `backend/mcp_server/profile_tools.py`:

```python
def _sample_result(
    *,
    profile: DBVoiceProfile,
    sample_id: str,
    source: str,
    reference_text_source: str,
    db: Session,
) -> dict[str, Any]:
    serialized = serialize_profile(profile, db)
    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "sample_id": sample_id,
        "source": source,
        "reference_text_source": reference_text_source,
        "sample_count": serialized["sample_count"],
        "ready_for_generation": serialized["ready_for_generation"],
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
    profile_row = _resolve_profile(profile, db)
    if (getattr(profile_row, "voice_type", None) or "cloned") != "cloned":
        raise ValueError("Preset profiles cannot receive cloned voice samples.")
    if bool(audio_base64) == bool(capture_id):
        raise ValueError("Pass exactly one of audio_base64 or capture_id.")

    if audio_base64 is not None:
        clean_reference = (reference_text or "").strip()
        if not clean_reference:
            raise ValueError("reference_text is required for audio_base64 samples.")
        with decoded_audio_file(audio_base64, filename) as temporary_path:
            sample = await profiles_service.add_profile_sample(
                profile_row.id,
                str(temporary_path),
                clean_reference,
                db,
            )
        return _sample_result(
            profile=profile_row,
            sample_id=sample.id,
            source="base64",
            reference_text_source="explicit",
            db=db,
        )

    capture = db.query(DBCapture).filter(DBCapture.id == capture_id).first()
    if capture is None:
        raise ValueError(f"Capture '{capture_id}' was not found.")
    capture_path = config.resolve_storage_path(capture.audio_path)
    if capture_path is None or not capture_path.is_file():
        raise ValueError(
            f"Capture '{capture_id}' exists, but its audio file is unavailable."
        )

    explicit_reference = (reference_text or "").strip()
    if explicit_reference:
        resolved_reference = explicit_reference
        reference_source = "explicit"
    else:
        raw_reference = (capture.transcript_raw or "").strip()
        if not raw_reference:
            raise ValueError(
                f"Capture '{capture_id}' has no usable transcript_raw; pass reference_text explicitly."
            )
        resolved_reference = raw_reference
        reference_source = "transcript_raw"

    sample = await profiles_service.add_profile_sample(
        profile_row.id,
        str(capture_path),
        resolved_reference,
        db,
    )
    return _sample_result(
        profile=profile_row,
        sample_id=sample.id,
        source="capture",
        reference_text_source=reference_source,
        db=db,
    )
```

- [ ] **Step 4: Run the complete profile suite**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: all tests pass without loading a TTS model.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add MCP profile sample sources"
```

---

### Task 5: FastMCP Registration and Existing-Tool Regression

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/mcp_server/tools.py:21-27,195-222`
- Modify: `backend/mcp_server/server.py:27-39`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: Task 1–4 core functions and `database.get_db`.
- Produces: `register_profile_tools(mcp: FastMCP) -> None` and eight registered tool names.

- [ ] **Step 1: Add the failing registration regression test**

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

Append to the test case:

```python
    def test_registration_contains_existing_and_profile_tools(self) -> None:
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

- [ ] **Step 2: Run and verify the four new names are absent**

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_registration_contains_existing_and_profile_tools \
  -v
```

Expected: FAIL showing only the existing four tools are registered.

- [ ] **Step 3: Add thin FastMCP wrappers**

Append to `backend/mcp_server/profile_tools.py`:

```python
def register_profile_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="voicebox.list_preset_voices",
        description=(
            "List built-in voices for kokoro or qwen_custom_voice before "
            "creating a preset profile."
        ),
    )
    async def voicebox_list_preset_voices(engine: str) -> dict[str, Any]:
        return profiles_service.list_preset_voices(engine)

    @mcp.tool(
        name="voicebox.create_profile",
        description=(
            "Create metadata for one Voicebox person. Use preset with "
            "preset_engine/preset_voice_id, or cloned and attach audio later. "
            "This tool never generates audio."
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
            "audio_base64 or capture_id. Base64 requires reference_text; a "
            "Capture uses explicit reference_text first, then transcript_raw."
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
```

Add to `backend/mcp_server/tools.py`:

```python
from .profile_tools import register_profile_tools
```

At the end of `register_tools`, before the top-level speak-helper section:

```python
    register_profile_tools(mcp)
```

Replace the `instructions` value in `backend/mcp_server/server.py`:

```python
        instructions=(
            "Voicebox is a local voice I/O layer. Use `voicebox.list_profiles` "
            "and `voicebox.get_profile` to inspect voices; "
            "`voicebox.list_preset_voices`, `voicebox.create_profile`, and "
            "`voicebox.add_profile_sample` to create them; `voicebox.speak` "
            "to generate and play speech; and `voicebox.transcribe` for audio→text."
        ),
```

- [ ] **Step 4: Run registration, full tests, and compilation**

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/mcp_server backend/services backend/routes
```

Expected: all tests pass; compilation exits 0.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server backend/tests/test_mcp_profile_tools.py
git commit -m "feat: expose profile management through MCP"
```

---

### Task 6: Documentation and Existing Docker Build Verification

**Files:**
- Modify: `docs/content/docs/overview/mcp-server.mdx`
- Verify unchanged: `Dockerfile`, `docker-compose.yml`

**Interfaces:**
- Consumes: the registered live `/mcp` server.
- Produces: workflow documentation and a non-GPU Docker smoke-test record.

- [ ] **Step 1: Document create → sample → speak**

Add after the existing MCP tool documentation:

```markdown
## Create a voice profile

Profile creation and generation are separate:

1. Discover a built-in voice with `voicebox.list_preset_voices`, or select a cloning engine.
2. Create metadata with `voicebox.create_profile`.
3. For cloned profiles, attach one or more references with `voicebox.add_profile_sample`.
4. Generate audio with `voicebox.speak`.

`description` is informational. `personality` controls in-character text rewriting when `voicebox.speak` is called with `personality: true`.

A Base64 sample must include `reference_text`. A Capture sample may omit it; Voicebox then uses `transcript_raw`. `transcript_refined` is not selected automatically.
```

- [ ] **Step 2: Run non-GPU quality checks**

```bash
python -m unittest discover -s backend/tests -p "test_mcp_profile_tools.py" -v
python -m compileall -q backend
bun run typecheck
bun run build:web
```

Expected: tests and compilation pass; frontend checks complete without regressions.

- [ ] **Step 3: Build the existing Dockerfile**

```bash
docker build -t voicebox-mcp-profile:test .
```

Expected: the existing three-stage image builds; no new Dockerfile is present.

- [ ] **Step 4: Start an isolated container**

```bash
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

- [ ] **Step 5: Verify all eight tools through live MCP**

```bash
docker exec voicebox-mcp-profile-test python - <<'PY'
import asyncio
from fastmcp import Client

EXPECTED = {
    "voicebox.speak",
    "voicebox.transcribe",
    "voicebox.list_captures",
    "voicebox.list_profiles",
    "voicebox.list_preset_voices",
    "voicebox.create_profile",
    "voicebox.get_profile",
    "voicebox.add_profile_sample",
}

async def main():
    async with Client("http://127.0.0.1:17493/mcp") as client:
        names = {tool.name for tool in await client.list_tools()}
        missing = EXPECTED - names
        assert not missing, f"Missing MCP tools: {sorted(missing)}"
        print("Verified:", sorted(EXPECTED))

asyncio.run(main())
PY
```

Expected: all eight names are printed.

- [ ] **Step 6: Run a live metadata-only workflow**

```bash
docker exec voicebox-mcp-profile-test python - <<'PY'
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:17493/mcp") as client:
        created = await client.call_tool(
            "voicebox.create_profile",
            {
                "name": "MCP Smoke Clone",
                "description": "Temporary smoke profile.",
                "language": "it",
                "voice_type": "cloned",
                "default_engine": "qwen",
                "personality": "Parla in modo chiaro e diretto.",
            },
        )
        fetched = await client.call_tool(
            "voicebox.get_profile",
            {"profile": "mcp smoke clone"},
        )
        print(created)
        print(fetched)

asyncio.run(main())
PY
```

Expected: both calls succeed; the fetched clone has zero samples and is not ready for generation.

- [ ] **Step 7: Remove isolated resources**

```bash
docker stop voicebox-mcp-profile-test
docker volume rm voicebox-mcp-profile-test-data
```

Expected: only the smoke-test container and volume are removed; production data is untouched.

- [ ] **Step 8: Commit documentation**

```bash
git add docs/content/docs/overview/mcp-server.mdx
git commit -m "docs: document MCP profile workflow"
```

---

### Task 7: Final Verification and Pull Request

**Files:**
- Review: all changes from Tasks 1–6
- Compare: `feature/mcp-profile-management` against `main`

**Interfaces:**
- Consumes: tested feature branch.
- Produces: a reviewable pull request; no automatic merge or OMV deployment.

- [ ] **Step 1: Check final scope and whitespace**

```bash
git status --short
git diff --stat main...HEAD
git diff --check main...HEAD
```

Expected: clean tree and no whitespace errors.

- [ ] **Step 2: Re-run focused tests**

```bash
python -m unittest discover -s backend/tests -p "test_mcp_profile_tools.py" -v
```

Expected: all tests pass.

- [ ] **Step 3: Prove prohibited files were not changed**

```bash
if git diff --name-only main...HEAD | grep -E \
  'backend/database/migrations|app/src|backend/services/export_import.py|docker-compose.yml|Dockerfile'; then
  echo "Unexpected out-of-scope file change" >&2
  exit 1
fi
```

Expected: no matched paths.

- [ ] **Step 4: Open the pull request**

```bash
gh pr create \
  --base main \
  --head feature/mcp-profile-management \
  --title "feat: manage Voicebox profiles through MCP" \
  --body "Adds preset discovery, profile creation, complete profile lookup, and Base64/Capture sample attachment through MCP. Reuses existing Voicebox services and keeps voicebox.speak as the generation tool. Includes non-GPU tests and Docker MCP smoke verification."
```

Expected: a pull request is created for review and remains unmerged.
