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

# Profile services import cache helpers whose production implementation depends
# on torch. These tests exercise metadata only.
_cache_stub = ModuleType("backend.utils.cache")
_cache_stub._get_cache_dir = lambda: Path(tempfile.gettempdir()) / "voicebox-test-cache"
_cache_stub.clear_profile_cache = lambda _profile_id: None
sys.modules.setdefault("backend.utils.cache", _cache_stub)

from backend.database import Base
from backend.database import ProfileSample as DBProfileSample
from backend.database import VoiceProfile as DBVoiceProfile
from backend.services.story_orchestration import (
    resolve_story_engine,
    resolve_story_profile,
    validate_story_profile,
)


class StoryProfileResolutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
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

    def _profile(self, *, profile_id: str, name: str, **kwargs) -> DBVoiceProfile:
        profile = DBVoiceProfile(
            id=profile_id,
            name=name,
            language=kwargs.pop("language", "it"),
            voice_type=kwargs.pop("voice_type", "cloned"),
            default_engine=kwargs.pop("default_engine", "qwen"),
            **kwargs,
        )
        self.db.add(profile)
        self.db.commit()
        return profile

    def test_exact_profile_id_has_priority(self) -> None:
        profile = self._profile(profile_id="profile-id", name="Serena")
        self._profile(profile_id="another", name="profile-id")

        resolved = resolve_story_profile(profile.id, self.db)

        self.assertEqual(resolved.id, profile.id)

    def test_profile_name_lookup_is_case_insensitive(self) -> None:
        profile = self._profile(profile_id="profile-1", name="Serena")

        resolved = resolve_story_profile("sErEnA", self.db)

        self.assertEqual(resolved.id, profile.id)

    def test_legacy_case_insensitive_duplicates_are_rejected(self) -> None:
        self._profile(profile_id="profile-1", name="Serena")
        self._profile(profile_id="profile-2", name="SERENA")

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_story_profile("serena", self.db)

    def test_missing_and_empty_profile_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            resolve_story_profile("   ", self.db)
        with self.assertRaisesRegex(ValueError, "was not found"):
            resolve_story_profile("missing", self.db)

    def test_preset_engine_is_inherited_and_profile_is_validated(self) -> None:
        profile = self._profile(
            profile_id="preset-1",
            name="Preset Serena",
            voice_type="preset",
            default_engine=None,
            preset_engine="kokoro",
            preset_voice_id="if_sara",
        )
        kokoro = ModuleType("backend.backends.kokoro_backend")
        kokoro.KOKORO_VOICES = [("if_sara", "Sara", "female", "it")]

        with patch.dict(sys.modules, {"backend.backends.kokoro_backend": kokoro}):
            self.assertEqual(resolve_story_engine(profile), "kokoro")
            validate_story_profile(profile, self.db)

    def test_invalid_preset_voice_is_rejected(self) -> None:
        profile = self._profile(
            profile_id="preset-1",
            name="Broken Preset",
            voice_type="preset",
            default_engine=None,
            preset_engine="kokoro",
            preset_voice_id="missing",
        )
        kokoro = ModuleType("backend.backends.kokoro_backend")
        kokoro.KOKORO_VOICES = [("if_sara", "Sara", "female", "it")]

        with patch.dict(sys.modules, {"backend.backends.kokoro_backend": kokoro}):
            with self.assertRaisesRegex(ValueError, "not ready"):
                validate_story_profile(profile, self.db)

    def test_cloned_profile_requires_at_least_one_sample(self) -> None:
        profile = self._profile(profile_id="clone-1", name="Clone")

        with self.assertRaisesRegex(ValueError, "not ready"):
            validate_story_profile(profile, self.db)

        self.db.add(
            DBProfileSample(
                id="sample-1",
                profile_id=profile.id,
                audio_path="profiles/sample.wav",
                reference_text="Test sample",
            )
        )
        self.db.commit()
        validate_story_profile(profile, self.db)

    def test_unsupported_profile_type_is_rejected(self) -> None:
        profile = self._profile(
            profile_id="designed-1",
            name="Designed",
            voice_type="designed",
            default_engine="qwen",
            design_prompt="Calm voice",
        )

        with self.assertRaisesRegex(ValueError, "not supported"):
            validate_story_profile(profile, self.db)


if __name__ == "__main__":
    unittest.main()
