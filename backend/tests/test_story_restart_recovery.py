from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_cache_stub = ModuleType("backend.utils.cache")
_cache_stub._get_cache_dir = lambda: Path(tempfile.gettempdir()) / "voicebox-test-cache"
_cache_stub.clear_profile_cache = lambda _profile_id: None
sys.modules.setdefault("backend.utils.cache", _cache_stub)

from backend.database import Base
from backend.database import Generation as DBGeneration
from backend.database import Story as DBStory
from backend.database import StoryItem as DBStoryItem
from backend.database import StorySegment as DBStorySegment
from backend.database import VoiceProfile as DBVoiceProfile
from backend.services.story_orchestration import recover_interrupted_story_workflows


class StoryRestartRecoveryTestCase(unittest.TestCase):
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
        self.profile = DBVoiceProfile(
            id="profile-1",
            name="Serena",
            language="it",
            voice_type="cloned",
            default_engine="qwen",
        )
        self.db.add(self.profile)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _story(self, story_id: str, status: str, total: int = 2) -> DBStory:
        story = DBStory(
            id=story_id,
            name=story_id,
            status=status,
            total_segments=total,
            completed_segments=0,
            current_segment_index=2 if status in {"queued", "generating", "rendering"} else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.db.add(story)
        self.db.commit()
        return story

    def _generation(self, generation_id: str, status: str) -> DBGeneration:
        generation = DBGeneration(
            id=generation_id,
            profile_id=self.profile.id,
            text=generation_id,
            language="it",
            audio_path=(f"generations/{generation_id}.wav" if status == "completed" else ""),
            duration=1.0 if status == "completed" else 0.0,
            engine="qwen",
            status=status,
            source="mcp_story",
            created_at=datetime.utcnow(),
        )
        self.db.add(generation)
        self.db.commit()
        return generation

    def test_active_story_is_failed_and_progress_is_recomputed(self) -> None:
        story = self._story("active", "generating")
        completed_generation = self._generation("generation-1", "completed")
        active_generation = self._generation("generation-2", "generating")
        first = DBStorySegment(
            id="segment-1",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="one",
            generation_id=completed_generation.id,
            status="completed",
        )
        second = DBStorySegment(
            id="segment-2",
            story_id=story.id,
            position=2,
            profile_id=self.profile.id,
            text="two",
            generation_id=active_generation.id,
            status="generating",
        )
        item = DBStoryItem(
            id="item-1",
            story_id=story.id,
            generation_id=completed_generation.id,
            start_time_ms=0,
            track=0,
        )
        self.db.add_all([first, second, item])
        self.db.commit()

        count = recover_interrupted_story_workflows(self.db)

        self.db.expire_all()
        story = self.db.query(DBStory).filter_by(id=story.id).one()
        segments = (
            self.db.query(DBStorySegment)
            .filter_by(story_id=story.id)
            .order_by(DBStorySegment.position)
            .all()
        )
        self.assertEqual(count, 1)
        self.assertEqual(story.status, "failed")
        self.assertEqual(story.completed_segments, 1)
        self.assertIsNone(story.current_segment_index)
        self.assertEqual(story.failed_segment_index, 2)
        self.assertEqual(story.error, "Server was shut down during Story processing")
        self.assertEqual([segment.status for segment in segments], ["completed", "failed"])

    def test_completed_generation_without_item_remains_reconcilable(self) -> None:
        story = self._story("attach-later", "generating", total=1)
        generation = self._generation("generation-ready", "completed")
        segment = DBStorySegment(
            id="segment-ready",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="ready",
            generation_id=generation.id,
            status="generating",
        )
        self.db.add(segment)
        self.db.commit()

        recover_interrupted_story_workflows(self.db)

        self.db.expire_all()
        story = self.db.query(DBStory).filter_by(id=story.id).one()
        segment = self.db.query(DBStorySegment).filter_by(id=segment.id).one()
        self.assertEqual(story.status, "failed")
        self.assertEqual(story.completed_segments, 0)
        self.assertIsNone(story.failed_segment_index)
        self.assertEqual(segment.status, "pending")
        self.assertIsNone(segment.error)
        self.assertEqual(segment.generation_id, generation.id)

    def test_terminal_stories_are_unchanged(self) -> None:
        stories = [
            self._story("draft", "draft", total=0),
            self._story("completed", "completed", total=0),
            self._story("failed", "failed", total=0),
        ]

        count = recover_interrupted_story_workflows(self.db)

        self.assertEqual(count, 0)
        self.db.expire_all()
        self.assertEqual(
            [self.db.query(DBStory).filter_by(id=story.id).one().status for story in stories],
            ["draft", "completed", "failed"],
        )


if __name__ == "__main__":
    unittest.main()
