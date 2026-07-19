from __future__ import annotations

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

# Profile services import cache helpers whose production implementation depends
# on torch. These tests exercise metadata and audio storage only, so a small
# stub keeps the test environment lightweight and GPU-independent.
_cache_stub = ModuleType("backend.utils.cache")
_cache_stub._get_cache_dir = (
    lambda: Path(tempfile.gettempdir()) / "voicebox-test-cache"
)
_cache_stub.clear_profile_cache = lambda _profile_id: None
sys.modules["backend.utils.cache"] = _cache_stub

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
    register_profile_tools,
)
from backend.services import preset_voices as preset_voices_service


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
            kokoro = preset_voices_service.list_preset_voices("kokoro")
            qwen = preset_voices_service.list_preset_voices(
                "qwen_custom_voice"
            )
        self.assertEqual(kokoro["engine"], "kokoro")
        self.assertEqual(
            set(kokoro["voices"][0]),
            {"voice_id", "name", "gender", "language"},
        )
        self.assertEqual(qwen["voices"][0]["voice_id"], "Ryan")

    def test_list_preset_voices_rejects_unknown_engine(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported preset engine"):
            preset_voices_service.list_preset_voices("unknown")

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
        self.assertEqual(
            result["description"],
            "Voce italiana per spiegazioni tecniche.",
        )
        self.assertEqual(
            result["personality"], "Carlo parla in modo concreto."
        )
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

    async def test_create_rejects_invalid_inputs(self) -> None:
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

    async def test_native_validation_and_uniqueness_are_preserved(self) -> None:
        with fake_preset_modules():
            with self.assertRaisesRegex(
                ValueError, "Preset profiles require"
            ):
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
        with self.assertRaisesRegex(
            ValueError, "Cloned profiles cannot use default engine"
        ):
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
        with patch(
            "backend.mcp_server.profile_tools.MAX_PROFILE_SAMPLE_BYTES", 8
        ):
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
                audio_base64=base64.b64encode(make_wav_bytes()).decode(
                    "ascii"
                ),
                capture_id="capture",
                filename="voice.wav",
                reference_text="Text",
                db=self.db,
            )
        with self.assertRaisesRegex(
            ValueError, "reference_text is required"
        ):
            await add_profile_sample(
                profile=clone["profile_id"],
                audio_base64=base64.b64encode(make_wav_bytes()).decode(
                    "ascii"
                ),
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
                audio_base64=base64.b64encode(make_wav_bytes()).decode(
                    "ascii"
                ),
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
        self.assertEqual(
            explicit_sample.reference_text, "Manual exact text."
        )

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
        with self.assertRaisesRegex(
            ValueError, "Capture 'missing' was not found"
        ):
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
        with self.assertRaisesRegex(
            ValueError, "no usable transcript_raw"
        ):
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

    def test_new_profile_tools_are_registered(self) -> None:
        fake = FakeMCP()
        register_profile_tools(fake)
        self.assertEqual(
            set(fake.registered),
            {
                "voicebox.list_preset_voices",
                "voicebox.create_profile",
                "voicebox.get_profile",
                "voicebox.add_profile_sample",
            },
        )


if __name__ == "__main__":
    unittest.main()
