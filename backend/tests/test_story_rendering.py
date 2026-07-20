"""RED tests for strict persistent Story rendering.

The current Story mixer is intentionally permissive for the manual editor. MCP
Story completion needs a strict preflight and a persistent atomic WAV while
still delegating the actual mix to ``services.stories.export_story_audio``.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config
from backend.database import Base
from backend.database import Generation as DBGeneration
from backend.database import Story as DBStory
from backend.database import StoryItem as DBStoryItem
from backend.database import VoiceProfile as DBVoiceProfile
from backend.services import stories


def _wav_bytes(sample_rate: int = 24_000, duration_ms: int = 100) -> bytes:
    """Create a small valid mono PCM WAV without external test dependencies."""
    frame_count = sample_rate * duration_ms // 1000
    frames = bytearray()
    for index in range(frame_count):
        sample = 2_000 if (index // 40) % 2 == 0 else -2_000
        frames.extend(sample.to_bytes(2, "little", signed=True))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(frames))
    return buffer.getvalue()


class StoryRenderingRedTestCase(unittest.IsolatedAsyncioTestCase):
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

        self.profile = DBVoiceProfile(
            id="profile-1",
            name="Serena",
            language="it",
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="if_sara",
            default_engine="kokoro",
        )
        self.story = DBStory(id="story-1", name="Demo Story")
        self.db.add_all([self.profile, self.story])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    def _load_rendering_service(self):
        try:
            from backend.services import story_rendering
        except ImportError as exc:
            self.fail(f"Story rendering service is missing: {exc}")
        return story_rendering

    def _add_generation_item(
        self,
        *,
        generation_id: str,
        audio_path: str,
        status: str = "completed",
    ) -> DBGeneration:
        generation = DBGeneration(
            id=generation_id,
            profile_id=self.profile.id,
            text="Una battuta di prova",
            language="it",
            audio_path=audio_path,
            duration=0.1,
            engine="kokoro",
            status=status,
        )
        item = DBStoryItem(
            id=f"item-{generation_id}",
            story_id=self.story.id,
            generation_id=generation.id,
            start_time_ms=0,
            track=0,
        )
        self.db.add_all([generation, item])
        self.db.commit()
        return generation

    async def test_strict_render_rejects_a_missing_story_clip(self) -> None:
        """MCP completion must fail instead of silently omitting a segment."""
        rendering = self._load_rendering_service()
        self._add_generation_item(
            generation_id="generation-missing",
            audio_path="generations/missing.wav",
        )

        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            await rendering.render_story_persistent(self.story.id, self.db)

    async def test_persistent_render_reuses_existing_mixer_and_replaces_atomically(self) -> None:
        """The new service persists, but does not duplicate, Story mixing."""
        rendering = self._load_rendering_service()
        source_path = config.get_generations_dir() / "generation-1.wav"
        source_path.write_bytes(_wav_bytes())
        self._add_generation_item(
            generation_id="generation-1",
            audio_path=config.to_storage_path(source_path),
        )
        rendered_bytes = _wav_bytes(duration_ms=200)

        with patch.object(
            stories,
            "export_story_audio",
            new=AsyncMock(return_value=rendered_bytes),
        ) as mixer_mock, patch(
            "backend.services.story_rendering.os.replace",
            wraps=os.replace,
        ) as replace_mock:
            stored_path = await rendering.render_story_persistent(
                self.story.id,
                self.db,
            )

        self.db.refresh(self.story)
        final_path = config.resolve_storage_path(stored_path)
        self.assertIsNotNone(final_path)
        self.assertEqual(final_path, config.get_data_dir() / "stories" / self.story.id / "story.wav")
        self.assertEqual(final_path.read_bytes(), rendered_bytes)
        self.assertEqual(self.story.render_audio_path, stored_path)
        self.assertIsNotNone(self.story.rendered_at)
        self.assertFalse((final_path.parent / "story.wav.tmp").exists())
        mixer_mock.assert_awaited_once_with(self.story.id, self.db)
        replace_mock.assert_called_once()

    async def test_manual_story_export_remains_permissive_for_missing_audio(self) -> None:
        """The existing editor/export contract must remain backward compatible."""
        self._add_generation_item(
            generation_id="generation-legacy-missing",
            audio_path="generations/legacy-missing.wav",
        )

        result = await stories.export_story_audio(self.story.id, self.db)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
