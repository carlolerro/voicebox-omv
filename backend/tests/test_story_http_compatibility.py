from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_cache_stub = ModuleType("backend.utils.cache")
_cache_stub._get_cache_dir = lambda: Path(tempfile.gettempdir()) / "voicebox-test-cache"
_cache_stub.clear_profile_cache = lambda _profile_id: None
sys.modules.setdefault("backend.utils.cache", _cache_stub)

from backend import config
from backend.database import Base
from backend.database import Generation as DBGeneration
from backend.database import Story as DBStory
from backend.database import StoryItem as DBStoryItem
from backend.database import StorySegment as DBStorySegment
from backend.database import VoiceProfile as DBVoiceProfile

# Story routes historically import safe_content_disposition from backend.app.
# Import the app first, matching production startup, so direct test collection
# does not create a stories -> app -> register_routers -> stories cycle.
import backend.app as _backend_app  # noqa: F401,E402
from backend.routes import stories as story_routes  # noqa: E402


class StoryHTTPCompatibilityTestCase(unittest.TestCase):
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

        def override_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        app = FastAPI()
        app.include_router(story_routes.router)
        app.dependency_overrides[story_routes.get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    def _story(self, story_id: str, status: str = "draft") -> DBStory:
        story = DBStory(
            id=story_id,
            name="Story",
            status=status,
            total_segments=0,
            completed_segments=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.db.add(story)
        self.db.commit()
        return story

    def _generation_item(
        self,
        story: DBStory,
        suffix: str = "1",
    ) -> tuple[DBGeneration, DBStoryItem]:
        generation = DBGeneration(
            id=f"generation-{suffix}",
            profile_id=self.profile.id,
            text="Hello",
            language="it",
            audio_path=f"generations/{suffix}.wav",
            duration=1.0,
            engine="qwen",
            status="completed",
            source="mcp_story",
            created_at=datetime.utcnow(),
        )
        item = DBStoryItem(
            id=f"item-{suffix}",
            story_id=story.id,
            generation_id=generation.id,
            start_time_ms=0,
            track=0,
        )
        self.db.add_all([generation, item])
        self.db.commit()
        return generation, item

    def _render(self, story: DBStory) -> Path:
        path = config.get_stories_dir() / story.id / "story.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF....WAVE")
        story.render_audio_path = config.to_storage_path(path)
        story.rendered_at = datetime.utcnow()
        self.db.commit()
        return path

    def test_active_story_mutations_return_409(self) -> None:
        story = self._story("active")
        for status in ("queued", "generating", "rendering"):
            with self.subTest(status=status):
                story.status = status
                self.db.commit()
                response = self.client.put(
                    f"/stories/{story.id}",
                    json={"name": "Changed", "description": None},
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["detail"],
                    "Story is currently processing",
                )

    def test_persistent_render_is_served_without_rebuilding(self) -> None:
        story = self._story("completed", status="completed")
        path = self._render(story)

        with patch.object(
            story_routes.stories,
            "export_story_audio",
            new=AsyncMock(side_effect=AssertionError("mixer must not run")),
        ) as mixer:
            response = self.client.get(f"/stories/{story.id}/export-audio")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("audio/wav")
        )
        self.assertEqual(response.content, path.read_bytes())
        mixer.assert_not_awaited()

    def test_draft_story_export_keeps_legacy_fallback(self) -> None:
        story = self._story("draft")
        audio = b"RIFF....WAVE"
        with patch.object(
            story_routes.stories,
            "export_story_audio",
            new=AsyncMock(return_value=audio),
        ) as mixer:
            response = self.client.get(f"/stories/{story.id}/export-audio")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, audio)
        mixer.assert_awaited_once()

    def test_terminal_delete_removes_segments_render_and_items_but_keeps_generation(
        self,
    ) -> None:
        story = self._story("failed", status="failed")
        generation, _item = self._generation_item(story)
        segment = DBStorySegment(
            id="segment-1",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="Hello",
            generation_id=generation.id,
            status="completed",
        )
        self.db.add(segment)
        self.db.commit()
        render_path = self._render(story)

        response = self.client.delete(f"/stories/{story.id}")

        self.assertEqual(response.status_code, 200)
        self.db.expire_all()
        self.assertIsNone(
            self.db.query(DBStory).filter_by(id=story.id).first()
        )
        self.assertEqual(
            self.db.query(DBStorySegment).filter_by(story_id=story.id).count(),
            0,
        )
        self.assertEqual(
            self.db.query(DBStoryItem).filter_by(story_id=story.id).count(),
            0,
        )
        self.assertIsNotNone(
            self.db.query(DBGeneration).filter_by(id=generation.id).first()
        )
        self.assertFalse(render_path.exists())

    def test_terminal_timeline_edit_invalidates_stale_render(self) -> None:
        story = self._story("editable", status="completed")
        _generation, item = self._generation_item(story)
        render_path = self._render(story)

        response = self.client.put(
            f"/stories/{story.id}/items/{item.id}/volume",
            json={"volume": 0.5},
        )

        self.assertEqual(response.status_code, 200)
        self.db.expire_all()
        refreshed = self.db.query(DBStory).filter_by(id=story.id).one()
        self.assertEqual(refreshed.status, "draft")
        self.assertIsNone(refreshed.render_audio_path)
        self.assertIsNone(refreshed.rendered_at)
        self.assertFalse(render_path.exists())


if __name__ == "__main__":
    unittest.main()
