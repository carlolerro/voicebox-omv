from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_cache_stub = ModuleType("backend.utils.cache")
_cache_stub._get_cache_dir = lambda: Path(tempfile.gettempdir()) / "voicebox-test-cache"
_cache_stub.clear_profile_cache = lambda _profile_id: None
sys.modules.setdefault("backend.utils.cache", _cache_stub)

from backend import config
from backend.database import Base
from backend.database import Story as DBStory
from backend.database import StorySegment as DBStorySegment
from backend.database import VoiceProfile as DBVoiceProfile
from backend.services import story_orchestration as orchestration


class StoryOrchestrationGuardTestCase(unittest.IsolatedAsyncioTestCase):
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
            voice_type="cloned",
            default_engine="qwen",
        )
        self.db.add(self.profile)
        self.db.commit()
        orchestration._active_story_ids.clear()
        orchestration._story_tasks.clear()

    def tearDown(self) -> None:
        for task in list(orchestration._story_tasks.values()):
            task.cancel()
        orchestration._active_story_ids.clear()
        orchestration._story_tasks.clear()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    def test_background_task_creation_failure_does_not_leak_active_registry(self) -> None:
        with patch.object(
            orchestration,
            "create_background_task",
            side_effect=RuntimeError("no loop"),
        ):
            with self.assertRaisesRegex(RuntimeError, "no loop"):
                orchestration.start_story_workflow("story-1")

        self.assertNotIn("story-1", orchestration._active_story_ids)
        self.assertNotIn("story-1", orchestration._story_tasks)

    async def test_failed_story_with_valid_final_render_is_not_resumed(self) -> None:
        story = DBStory(
            id="story-1",
            name="Already rendered",
            status="failed",
            error="stale external status",
            total_segments=1,
            completed_segments=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        segment = DBStorySegment(
            id="segment-1",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="done",
            generation_id="generation-1",
            status="completed",
        )
        render_path = config.get_stories_dir() / story.id / "story.wav"
        render_path.parent.mkdir(parents=True, exist_ok=True)
        render_path.write_bytes(b"RIFF....WAVE")
        story.render_audio_path = config.to_storage_path(render_path)
        self.db.add_all([story, segment])
        self.db.commit()

        self.assertFalse(orchestration.is_story_resumable(story, self.db))
        with self.assertRaisesRegex(ValueError, "no incomplete work"):
            await orchestration.resume_story_workflow(story.id, self.db)

    def test_render_guard_rejects_missing_persisted_segment_rows(self) -> None:
        story = DBStory(
            id="story-1",
            name="Inconsistent",
            status="generating",
            total_segments=2,
            completed_segments=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        segment = DBStorySegment(
            id="segment-1",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="only one row",
            generation_id="generation-1",
            status="completed",
        )
        self.db.add_all([story, segment])
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "segment count"):
            orchestration._validate_story_ready_for_render(story, self.db)

    async def test_service_limits_apply_before_any_story_write(self) -> None:
        specs = [
            orchestration.StorySegmentSpec(profile="Serena", text="x")
            for _ in range(orchestration.MAX_STORY_SEGMENTS + 1)
        ]

        with self.assertRaisesRegex(ValueError, "100"):
            await orchestration.create_story_workflow(
                title="Too many",
                description=None,
                segments=specs,
                db=self.db,
            )

        self.assertEqual(self.db.query(DBStory).count(), 0)


if __name__ == "__main__":
    unittest.main()
