# MCP Profile Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four MCP tools that discover preset voices, create Voicebox profiles, retrieve complete profile metadata, and attach cloned-voice samples from strict Base64 audio or an existing Capture, while continuing to use `voicebox.speak` for generation.

**Architecture:** Add a focused `backend/mcp_server/profile_tools.py` adapter with testable core functions that receive a SQLAlchemy session and thin FastMCP wrappers that open and close production sessions. Reuse `models.VoiceProfileCreate`, `services.profiles.create_profile`, `services.profiles.add_profile_sample`, Voicebox storage helpers, and the existing profile resolver. Move preset-voice discovery into `services/profiles.py` so REST and MCP share one implementation.

**Tech Stack:** Python 3.11, FastMCP 3.x, FastAPI, Pydantic 2.x, SQLAlchemy 2.x, SQLite, standard-library `unittest`, Docker multi-stage build.

## Global Constraints

- Work only on branch `feature/mcp-profile-management`.
- Keep the MCP endpoint at `/mcp`; do not add another service or container.
- Add exactly these v1 tools: `voicebox.list_preset_voices`, `voicebox.create_profile`, `voicebox.get_profile`, `voicebox.add_profile_sample`.
- Keep `voicebox.speak` as the only audio-generation tool; profile creation must not generate audio.
- Support only `voice_type="preset"` and `voice_type="cloned"` in the new creation tool.
- Initial preset engines are exactly `kokoro` and `qwen_custom_voice`.
- Cloning engines remain exactly `qwen`, `luxtts`, `chatterbox`, `chatterbox_turbo`, and `tada`.
- Base64 sample payloads use strict decoding, may not exceed 50 MiB decoded, and never expose a client-supplied filesystem path.
- Capture reference-text precedence is explicit non-empty `reference_text`, then non-empty `transcript_raw`, then error; never auto-select `transcript_refined`.
- Reject sample addition for every non-cloned profile.
- Do not add database migrations, frontend changes, profile test entities, comparison workflows, parallel TTS inference, or export/import changes.
- Tests must not run TTS inference or require a GPU.

---

## File Structure

- Create `backend/mcp_server/profile_tools.py`: MCP registration, input validation, profile serialization, Base64/Capture source resolution, and normalized responses.
- Create `backend/tests/__init__.py`: mark the backend test package.
- Create `backend/tests/test_mcp_profile_tools.py`: in-memory database and temporary-storage unit/integration tests for the new tools.
- Modify `backend/services/profiles.py`: add shared preset-voice discovery used by REST and MCP.
- Modify `backend/routes/profiles.py`: delegate `/profiles/presets/{engine}` to the shared service.
- Modify `backend/mcp_server/tools.py`: register the new profile tool module without changing existing tool behavior.
- Modify `backend/mcp_server/server.py`: update server instructions so agents discover the new profile workflow.
- Modify `docs/content/docs/overview/mcp-server.mdx`: document the four new tools and the create → sample → speak flow.

---

### Task 1: Shared Preset-Voice Discovery

**Files:**
- Modify: `backend/services/profiles.py`
- Modify: `backend/routes/profiles.py:71-109`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `KOKORO_VOICES` and `QWEN_CUSTOM_VOICES` from their existing backend modules.
- Produces: `profiles.list_preset_voices(engine: str) -> dict[str, object]` for REST and MCP.

- [ ] **Step 1: Add the isolated test fixture and failing preset discovery tests**

Create `backend/tests/__init__.py` as an empty file.

Create `backend/tests/test_mcp_profile_tools.py` with:

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

- [ ] **Step 2: Run the tests and verify the intended failure**

Run:

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_kokoro_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_qwen_custom_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_preset_voices_rejects_unknown_engine \
  -v
```

Expected: all three tests fail with `AttributeError: module 'backend.services.profiles' has no attribute 'list_preset_voices'`.

- [ ] **Step 3: Add the shared service implementation**

Add to `backend/services/profiles.py`, immediately after `CLONING_ENGINES`:

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

Replace the body of `backend/routes/profiles.py::list_preset_voices` with:

```python
@router.get("/profiles/presets/{engine}")
async def list_preset_voices(engine: str):
    """List available preset voices for an engine."""
    try:
        return profiles.list_preset_voices(engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 4: Run the focused tests and the REST module compilation check**

Run:

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_kokoro_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_qwen_custom_preset_voices \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_list_preset_voices_rejects_unknown_engine \
  -v
python -m compileall -q backend/services/profiles.py backend/routes/profiles.py
```

Expected: three tests pass and `compileall` exits with status 0.

- [ ] **Step 5: Commit the shared discovery change**

```bash
git add \
  backend/services/profiles.py \
  backend/routes/profiles.py \
  backend/tests/__init__.py \
  backend/tests/test_mcp_profile_tools.py
git commit -m "refactor: share preset voice discovery"
```

---

### Task 2: Profile Serialization, Creation, and Retrieval

**Files:**
- Create: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `profiles_service.create_profile`, `profiles_service.get_profile_orm_by_name_or_id`, `models.VoiceProfileCreate`, `DBProfileSample`, and `DBGeneration`.
- Produces:
  - `serialize_profile(profile: DBVoiceProfile, db: Session) -> dict[str, Any]`
  - `create_profile(..., db: Session) -> dict[str, Any]`
  - `get_profile(profile: str, db: Session) -> dict[str, Any]`

- [ ] **Step 1: Add failing creation, retrieval, readiness, and validation tests**

Append these imports to `backend/tests/test_mcp_profile_tools.py`:

```python
from backend.database import ProfileSample as DBProfileSample
from backend.database import VoiceProfile as DBVoiceProfile
from backend.mcp_server.profile_tools import create_profile, get_profile
```

Append these test methods to `MCPProfileToolsTestCase`:

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
        self.assertEqual(result["default_engine"], "qwen")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["generation_count"], 0)
        self.assertFalse(result["ready_for_generation"])

    async def test_create_preset_profile_is_immediately_ready(self) -> None:
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

        self.assertEqual(result["preset_engine"], "kokoro")
        self.assertEqual(result["preset_voice_id"], voice_id)
        self.assertEqual(result["default_engine"], "kokoro")
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

    async def test_get_profile_accepts_case_insensitive_name(self) -> None:
        created = await create_profile(
            name="Case Sensitive Display",
            description="Metadata",
            language="it",
            voice_type="cloned",
            personality="Direct.",
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )

        result = await get_profile("case sensitive display", self.db)
        self.assertEqual(result["profile_id"], created["profile_id"])
        self.assertEqual(result["description"], "Metadata")
        self.assertEqual(result["personality"], "Direct.")

    async def test_get_profile_rejects_unknown_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "Voice profile 'missing' was not found"):
            await get_profile("missing", self.db)

    async def test_cloned_profile_becomes_ready_after_sample_exists(self) -> None:
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
        sample = DBProfileSample(
            id="sample-1",
            profile_id=created["profile_id"],
            audio_path="profiles/sample.wav",
            reference_text="Sample text",
        )
        self.db.add(sample)
        self.db.commit()

        result = await get_profile(created["profile_id"], self.db)
        self.assertEqual(result["sample_count"], 1)
        self.assertTrue(result["ready_for_generation"])
```

- [ ] **Step 2: Run the new tests and verify import failure**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: test discovery fails with `ModuleNotFoundError: No module named 'backend.mcp_server.profile_tools'`.

- [ ] **Step 3: Implement testable profile serialization, creation, and retrieval**

Create `backend/mcp_server/profile_tools.py` with:

```python
"""MCP tools for Voicebox voice-profile management."""

from __future__ import annotations

import base64 as b64
import tempfile
from pathlib import Path
from typing import Any

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
    """Return complete MCP-facing metadata and calculated readiness."""
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
    ready_for_generation = (
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
        "ready_for_generation": ready_for_generation,
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
    """Create profile metadata only; never attach samples or generate audio."""
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
    """Get a profile by UUID or case-insensitive name."""
    return serialize_profile(_resolve_profile(profile, db), db)
```

- [ ] **Step 4: Run the complete test module**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: all Task 1 and Task 2 tests pass.

- [ ] **Step 5: Commit profile creation and retrieval**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add MCP profile creation and retrieval core"
```

---

### Task 3: Base64 Profile Sample Source

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `_resolve_profile`, `profiles_service.add_profile_sample`, and Voicebox audio validation/storage.
- Produces: `add_profile_sample(..., db: Session) -> dict[str, Any]` with Base64 support and normalized response.

- [ ] **Step 1: Add failing Base64 success and validation tests**

Update the profile-tools import in `backend/tests/test_mcp_profile_tools.py` to:

```python
from backend.mcp_server.profile_tools import (
    MAX_PROFILE_SAMPLE_BYTES,
    add_profile_sample,
    create_profile,
    get_profile,
)
```

Append these methods:

```python
    async def _create_cloned_profile(self, name: str = "Clone") -> dict:
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

    async def test_add_base64_sample_makes_profile_ready(self) -> None:
        profile = await self._create_cloned_profile()
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
        stored = self.db.query(DBProfileSample).filter_by(id=result["sample_id"]).one()
        self.assertTrue(config.resolve_storage_path(stored.audio_path).is_file())

    async def test_add_sample_requires_exactly_one_source(self) -> None:
        profile = await self._create_cloned_profile()
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
                filename="sample.wav",
                reference_text="Text",
                db=self.db,
            )

    async def test_add_base64_sample_requires_reference_text(self) -> None:
        profile = await self._create_cloned_profile()
        with self.assertRaisesRegex(ValueError, "reference_text is required"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="sample.wav",
                reference_text="   ",
                db=self.db,
            )

    async def test_add_base64_sample_rejects_invalid_base64(self) -> None:
        profile = await self._create_cloned_profile()
        with self.assertRaisesRegex(ValueError, "Invalid audio_base64"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64="not-valid%%%",
                capture_id=None,
                filename="sample.wav",
                reference_text="Text",
                db=self.db,
            )

    async def test_add_base64_sample_rejects_oversized_audio_before_processing(self) -> None:
        profile = await self._create_cloned_profile()
        encoded = base64.b64encode(b"x" * (MAX_PROFILE_SAMPLE_BYTES + 1)).decode("ascii")
        with self.assertRaisesRegex(ValueError, "cannot exceed 50 MB"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=encoded,
                capture_id=None,
                filename="sample.wav",
                reference_text="Text",
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
        with self.assertRaisesRegex(ValueError, "Preset profiles cannot receive cloned voice samples"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode("ascii"),
                capture_id=None,
                filename="sample.wav",
                reference_text="Text",
                db=self.db,
            )
```

- [ ] **Step 2: Run the Base64 tests and verify missing function failure**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: import fails because `add_profile_sample` is not defined.

- [ ] **Step 3: Implement strict Base64 decoding, safe suffix selection, cleanup, and response normalization**

Append to `backend/mcp_server/profile_tools.py`:

```python
def _sample_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_SAMPLE_SUFFIXES else ".wav"


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
    """Attach one Base64 or Capture audio source to a cloned profile."""
    profile_row = _resolve_profile(profile, db)
    if (getattr(profile_row, "voice_type", None) or "cloned") != "cloned":
        raise ValueError("Preset profiles cannot receive cloned voice samples.")

    if bool(audio_base64) == bool(capture_id):
        raise ValueError("Pass exactly one of audio_base64 or capture_id.")

    if audio_base64 is not None:
        clean_reference = (reference_text or "").strip()
        if not clean_reference:
            raise ValueError("reference_text is required for audio_base64 samples.")
        try:
            raw = b64.b64decode(audio_base64, validate=True)
        except Exception as exc:
            raise ValueError(f"Invalid audio_base64: {exc}") from exc
        if len(raw) > MAX_PROFILE_SAMPLE_BYTES:
            raise ValueError("Profile samples cannot exceed 50 MB.")

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=_sample_suffix(filename),
                delete=False,
            ) as temporary:
                temporary.write(raw)
                temp_path = Path(temporary.name)
            sample = await profiles_service.add_profile_sample(
                profile_row.id,
                str(temp_path),
                clean_reference,
                db,
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

        return _sample_result(
            profile=profile_row,
            sample_id=sample.id,
            source="base64",
            reference_text_source="explicit",
            db=db,
        )

    raise AssertionError("Capture source is implemented in Task 4.")
```

- [ ] **Step 4: Run the complete test module**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: all tests except future Capture tests pass; no TTS model is loaded.

- [ ] **Step 5: Commit Base64 sample support**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add Base64 MCP profile samples"
```

---

### Task 4: Capture-Based Profile Sample Source

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: `DBCapture`, `config.resolve_storage_path`, `_sample_result`, and `profiles_service.add_profile_sample`.
- Produces: the Capture branch of `add_profile_sample`, with exact reference-text precedence and no arbitrary path input.

- [ ] **Step 1: Add failing Capture source tests**

Append this import:

```python
from backend.database import Capture as DBCapture
```

Append these methods:

```python
    def _create_capture(
        self,
        *,
        capture_id: str,
        transcript_raw: str,
        transcript_refined: str | None = None,
        write_audio: bool = True,
    ) -> DBCapture:
        capture_path = config.get_captures_dir() / f"{capture_id}.wav"
        if write_audio:
            capture_path.write_bytes(make_wav_bytes())
        capture = DBCapture(
            id=capture_id,
            audio_path=config.to_storage_path(capture_path),
            source="file",
            language="en",
            transcript_raw=transcript_raw,
            transcript_refined=transcript_refined,
        )
        self.db.add(capture)
        self.db.commit()
        return capture

    async def test_add_capture_sample_uses_explicit_reference_text_first(self) -> None:
        profile = await self._create_cloned_profile("Explicit Capture")
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
        self.assertEqual(result["source"], "capture")
        self.assertEqual(result["reference_text_source"], "explicit")
        sample = self.db.query(DBProfileSample).filter_by(id=result["sample_id"]).one()
        self.assertEqual(sample.reference_text, "Exact manual transcript.")

    async def test_add_capture_sample_falls_back_to_raw_transcript(self) -> None:
        profile = await self._create_cloned_profile("Raw Capture")
        self._create_capture(
            capture_id="capture-raw",
            transcript_raw="Raw transcript only.",
            transcript_refined="Text that must not be selected.",
        )
        result = await add_profile_sample(
            profile=profile["profile_id"],
            audio_base64=None,
            capture_id="capture-raw",
            filename=None,
            reference_text=None,
            db=self.db,
        )
        self.assertEqual(result["reference_text_source"], "transcript_raw")
        sample = self.db.query(DBProfileSample).filter_by(id=result["sample_id"]).one()
        self.assertEqual(sample.reference_text, "Raw transcript only.")

    async def test_add_capture_sample_rejects_missing_capture(self) -> None:
        profile = await self._create_cloned_profile("Missing Capture")
        with self.assertRaisesRegex(ValueError, "Capture 'missing' was not found"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="missing",
                filename=None,
                reference_text=None,
                db=self.db,
            )

    async def test_add_capture_sample_rejects_missing_audio(self) -> None:
        profile = await self._create_cloned_profile("Missing Audio")
        self._create_capture(
            capture_id="capture-no-audio",
            transcript_raw="Raw transcript.",
            write_audio=False,
        )
        with self.assertRaisesRegex(ValueError, "audio file is unavailable"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="capture-no-audio",
                filename=None,
                reference_text=None,
                db=self.db,
            )

    async def test_add_capture_sample_never_falls_back_to_refined_transcript(self) -> None:
        profile = await self._create_cloned_profile("No Refined Fallback")
        self._create_capture(
            capture_id="capture-refined-only",
            transcript_raw="   ",
            transcript_refined="Refined text must not be used.",
        )
        with self.assertRaisesRegex(ValueError, "no usable transcript_raw"):
            await add_profile_sample(
                profile=profile["profile_id"],
                audio_base64=None,
                capture_id="capture-refined-only",
                filename=None,
                reference_text=None,
                db=self.db,
            )
```

- [ ] **Step 2: Run Capture tests and verify the placeholder branch fails**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: Capture success tests fail with `AssertionError: Capture source is implemented in Task 4.`

- [ ] **Step 3: Implement Capture lookup, safe path resolution, transcript precedence, and sample creation**

Replace the final `raise AssertionError(...)` in `add_profile_sample` with:

```python
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
        reference_text_source = "explicit"
    else:
        raw_reference = (capture.transcript_raw or "").strip()
        if not raw_reference:
            raise ValueError(
                f"Capture '{capture_id}' has no usable transcript_raw; pass reference_text explicitly."
            )
        resolved_reference = raw_reference
        reference_text_source = "transcript_raw"

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
        reference_text_source=reference_text_source,
        db=db,
    )
```

- [ ] **Step 4: Run all profile tool tests and verify the profile survives failed sample operations**

Append this regression test:

```python
    async def test_failed_sample_addition_does_not_delete_profile(self) -> None:
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

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
```

Expected: every test passes.

- [ ] **Step 5: Commit Capture sample support**

```bash
git add backend/mcp_server/profile_tools.py backend/tests/test_mcp_profile_tools.py
git commit -m "feat: add Capture MCP profile samples"
```

---

### Task 5: FastMCP Registration and Regression Protection

**Files:**
- Modify: `backend/mcp_server/profile_tools.py`
- Modify: `backend/mcp_server/tools.py:21-27,195-222`
- Modify: `backend/mcp_server/server.py:27-39`
- Modify: `backend/tests/test_mcp_profile_tools.py`

**Interfaces:**
- Consumes: all Task 1–4 core functions and `database.get_db`.
- Produces: `register_profile_tools(mcp: FastMCP) -> None` and eight registered tools total, including the four existing tools.

- [ ] **Step 1: Add a fake MCP registry and failing registration regression test**

Append to `backend/tests/test_mcp_profile_tools.py`:

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

Append this test method:

```python
    def test_register_tools_keeps_existing_and_adds_profile_tools(self) -> None:
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

- [ ] **Step 2: Run the registration test and verify only the existing four tools are present**

Run:

```bash
python -m unittest \
  backend.tests.test_mcp_profile_tools.MCPProfileToolsTestCase.test_register_tools_keeps_existing_and_adds_profile_tools \
  -v
```

Expected: FAIL showing that the four `voicebox.*profile*` tools are missing.

- [ ] **Step 3: Register thin FastMCP wrappers in the dedicated module**

Append to `backend/mcp_server/profile_tools.py`:

```python
def register_profile_tools(mcp: FastMCP) -> None:
    """Register profile-management tools on an existing FastMCP server."""

    @mcp.tool(
        name="voicebox.list_preset_voices",
        description=(
            "List built-in voices for a supported preset engine. "
            "Supported engines are kokoro and qwen_custom_voice."
        ),
    )
    async def voicebox_list_preset_voices(engine: str) -> dict[str, Any]:
        return profiles_service.list_preset_voices(engine)

    @mcp.tool(
        name="voicebox.create_profile",
        description=(
            "Create Voicebox profile metadata for one person. Use voice_type "
            "preset with preset_engine/preset_voice_id, or cloned and add a "
            "sample later with voicebox.add_profile_sample. This tool never "
            "generates audio."
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
            "including description, personality, sample counts, and readiness."
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
            "Attach a cloned-voice sample to a cloned profile. Pass exactly one "
            "of audio_base64 or capture_id. Base64 requires reference_text. A "
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

In `backend/mcp_server/tools.py`, add the import:

```python
from .profile_tools import register_profile_tools
```

At the end of `register_tools`, immediately before the top-level `# ─── Speak helper` section, add:

```python
    register_profile_tools(mcp)
```

Update `backend/mcp_server/server.py` instructions to:

```python
        instructions=(
            "Voicebox is a local voice I/O layer. Use `voicebox.list_profiles` "
            "and `voicebox.get_profile` to inspect voices, "
            "`voicebox.list_preset_voices` and `voicebox.create_profile` to "
            "create identities, `voicebox.add_profile_sample` for cloned "
            "voices, `voicebox.speak` to generate/play speech, and "
            "`voicebox.transcribe` for audio→text."
        ),
```

- [ ] **Step 4: Run registration and full regression tests**

Run:

```bash
python -m unittest backend.tests.test_mcp_profile_tools -v
python -m compileall -q backend/mcp_server backend/services backend/routes
```

Expected: all tests pass and `compileall` exits with status 0.

- [ ] **Step 5: Commit MCP registration**

```bash
git add \
  backend/mcp_server/profile_tools.py \
  backend/mcp_server/tools.py \
  backend/mcp_server/server.py \
  backend/tests/test_mcp_profile_tools.py
git commit -m "feat: expose profile management through MCP"
```

---

### Task 6: MCP Documentation and Docker End-to-End Verification

**Files:**
- Modify: `docs/content/docs/overview/mcp-server.mdx`
- Verify: `Dockerfile`
- Verify: `docker-compose.yml`

**Interfaces:**
- Consumes: the final registered MCP server and existing Docker build.
- Produces: user-facing workflow documentation and evidence that the branch image exposes all eight tools without TTS/GPU inference.

- [ ] **Step 1: Document the separated profile workflow**

Add this section after the existing MCP tools section in `docs/content/docs/overview/mcp-server.mdx`:

```markdown
## Create a voice profile from an MCP client

Profile creation and audio generation are deliberately separate:

1. Discover a preset voice with `voicebox.list_preset_voices`, or choose a cloning engine.
2. Create metadata with `voicebox.create_profile`.
3. For cloned profiles, attach one or more references with `voicebox.add_profile_sample`.
4. Generate audio with the existing `voicebox.speak` tool.

### Preset profile

```json
{
  "name": "Sara Podcast",
  "description": "Voce femminile italiana per podcast tecnici.",
  "personality": "Sara parla in modo chiaro, naturale e diretto.",
  "language": "it",
  "voice_type": "preset",
  "preset_engine": "kokoro",
  "preset_voice_id": "if_sara"
}
```

### Cloned profile and Base64 sample

```json
{
  "name": "Carlo",
  "description": "Voce italiana per spiegazioni tecniche.",
  "personality": "Carlo parla in modo concreto e usa esempi reali.",
  "language": "it",
  "voice_type": "cloned",
  "default_engine": "qwen"
}
```

Then call `voicebox.add_profile_sample` with exactly one source:

```json
{
  "profile": "Carlo",
  "audio_base64": "...",
  "filename": "carlo.wav",
  "reference_text": "Trascrizione esatta del campione."
}
```

For an existing Voicebox Capture, pass `capture_id` instead. Explicit `reference_text` wins; otherwise Voicebox uses `transcript_raw`. It never substitutes `transcript_refined` automatically.
```

- [ ] **Step 2: Run all non-GPU quality checks**

Run:

```bash
python -m unittest discover -s backend/tests -p "test_mcp_profile_tools.py" -v
python -m compileall -q backend
bun run typecheck
bun run build:web
```

Expected: unit tests pass, Python compilation succeeds, frontend typecheck succeeds, and web build completes.

- [ ] **Step 3: Build the existing Docker image from the feature branch checkout**

Run:

```bash
docker build -t voicebox-mcp-profile:test .
```

Expected: the existing three-stage Dockerfile completes successfully; no new Dockerfile is created.

- [ ] **Step 4: Start an isolated smoke-test container**

Run:

```bash
docker volume create voicebox-mcp-profile-test-data
docker run -d --rm \
  --name voicebox-mcp-profile-test \
  -p 127.0.0.1:17601:17493 \
  -v voicebox-mcp-profile-test-data:/app/data \
  voicebox-mcp-profile:test

for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:17601/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:17601/health
```

Expected: health endpoint returns success within 120 seconds.

- [ ] **Step 5: Verify tool discovery through the live `/mcp` endpoint**

Run:

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
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        missing = EXPECTED - names
        assert not missing, f"Missing MCP tools: {sorted(missing)}"
        print("MCP tools verified:", sorted(EXPECTED))

asyncio.run(main())
PY
```

Expected: output lists all eight expected tools.

- [ ] **Step 6: Run a live metadata-only MCP workflow**

Run:

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
                "description": "Temporary metadata-only smoke test.",
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
        print("create_profile:", created)
        print("get_profile:", fetched)

asyncio.run(main())
PY
```

Expected: both calls succeed and the fetched profile reports `ready_for_generation: false` with `sample_count: 0`.

- [ ] **Step 7: Stop and remove isolated test resources**

Run:

```bash
docker stop voicebox-mcp-profile-test
docker volume rm voicebox-mcp-profile-test-data
```

Expected: container and temporary data volume are removed; the production `voicebox-data` volume is untouched.

- [ ] **Step 8: Commit documentation**

```bash
git add docs/content/docs/overview/mcp-server.mdx
git commit -m "docs: document MCP profile creation workflow"
```

---

### Task 7: Final Review and Pull Request

**Files:**
- Review: all files changed by Tasks 1–6
- Compare: `feature/mcp-profile-management` against `main`

**Interfaces:**
- Consumes: the tested feature branch.
- Produces: a reviewable pull request that does not modify the production OMV deployment automatically.

- [ ] **Step 1: Verify the final diff is limited to the approved scope**

Run:

```bash
git status --short
git diff --stat main...HEAD
git diff --check main...HEAD
```

Expected: clean working tree, only the planned backend/tests/docs files changed, and no whitespace errors.

- [ ] **Step 2: Re-run the focused backend suite**

Run:

```bash
python -m unittest discover -s backend/tests -p "test_mcp_profile_tools.py" -v
```

Expected: all tests pass.

- [ ] **Step 3: Confirm no prohibited changes entered the branch**

Run:

```bash
git diff --name-only main...HEAD | grep -E \
  'backend/database/migrations|app/src|backend/services/export_import.py|docker-compose.yml|Dockerfile' \
  && exit 1 || true
```

Expected: no output. The implementation must not add migrations, frontend changes, export/import changes, or a new build definition.

- [ ] **Step 4: Open the pull request**

```bash
gh pr create \
  --base main \
  --head feature/mcp-profile-management \
  --title "feat: manage Voicebox profiles through MCP" \
  --body "Adds preset discovery, profile creation, complete profile lookup, and Base64/Capture sample attachment through MCP. Reuses existing Voicebox services and keeps voicebox.speak as the generation tool. Includes non-GPU tests and Docker MCP smoke verification."
```

Expected: a draft-free pull request is created for review; it is not merged automatically.
