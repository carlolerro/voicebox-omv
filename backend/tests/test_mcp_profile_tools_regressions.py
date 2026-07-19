from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Keep this regression suite independent from torch/model loading.
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
    add_profile_sample,
    create_profile,
    get_profile,
)


class MCPProfileToolsRegressionTestCase(unittest.IsolatedAsyncioTestCase):
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
            language="it",
            voice_type="cloned",
            personality=None,
            default_engine="qwen",
            preset_engine=None,
            preset_voice_id=None,
            db=self.db,
        )

    async def test_case_insensitive_duplicate_name_is_rejected(self) -> None:
        await self._create_clone("Carlo")
        with self.assertRaisesRegex(ValueError, "already exists"):
            await self._create_clone("carlo")

    async def test_capture_sample_obeys_decoded_size_limit(self) -> None:
        profile = await self._create_clone("Capture Limit")
        audio_path = config.get_captures_dir() / "oversized.wav"
        audio_path.write_bytes(b"123456789")
        self.db.add(
            DBCapture(
                id="oversized",
                audio_path=config.to_storage_path(audio_path),
                source="file",
                language="it",
                transcript_raw="Testo esatto.",
            )
        )
        self.db.commit()

        with patch(
            "backend.mcp_server.profile_tools.MAX_PROFILE_SAMPLE_BYTES", 8
        ):
            with self.assertRaisesRegex(ValueError, "cannot exceed 50 MB"):
                await add_profile_sample(
                    profile=profile["profile_id"],
                    audio_base64=None,
                    capture_id="oversized",
                    filename=None,
                    reference_text=None,
                    db=self.db,
                )

    async def test_designed_profile_never_uses_clone_readiness(self) -> None:
        self.db.add(
            DBVoiceProfile(
                id="designed-profile",
                name="Designed Legacy",
                description=None,
                language="en",
                voice_type="designed",
                design_prompt="A calm synthetic narrator.",
            )
        )
        self.db.add(
            DBProfileSample(
                id="legacy-sample",
                profile_id="designed-profile",
                audio_path="profiles/legacy.wav",
                reference_text="Legacy sample.",
            )
        )
        self.db.commit()

        result = await get_profile("Designed Legacy", self.db)
        self.assertEqual(result["sample_count"], 1)
        self.assertFalse(result["ready_for_generation"])


if __name__ == "__main__":
    unittest.main()
