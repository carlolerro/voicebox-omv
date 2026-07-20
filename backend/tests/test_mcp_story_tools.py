from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

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
from backend.database import ProfileSample as DBProfileSample
from backend.database import Story as DBStory
from backend.database import StorySegment as DBStorySegment
from backend.database import VoiceProfile as DBVoiceProfile
from backend.mcp_server import story_tools


class FakeMCP:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def decorator(function):
            self.registered[name] = function
            return function

        return decorator


class MCPStoryToolsTestCase(unittest.IsolatedAsyncioTestCase):
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
        self.db.flush()
        self.db.add(
            DBProfileSample(
                id="sample-1",
                profile_id=self.profile.id,
                audio_path="profiles/sample.wav",
                reference_text="Reference",
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    def test_four_story_tools_are_registered(self) -> None:
        mcp = FakeMCP()

        story_tools.register_story_tools(mcp)

        self.assertEqual(
            set(mcp.registered),
            {
                "voicebox.create_story",
                "voicebox.get_story_status",
                "voicebox.get_story",
                "voicebox.resume_story",
            },
        )

    async def test_create_returns_immediate_queued_shape(self) -> None:
        with patch.object(
            story_tools.orchestration,
            "start_story_workflow",
        ) as start_mock:
            result = await story_tools.create_story(
                title="  Capitolo  ",
                description=" Demo ",
                segments=[{"profile": " Serena ", "text": " Benvenuti "}],
                db=self.db,
            )

        self.assertEqual(result["title"], "Capitolo")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["total_segments"], 1)
        self.assertEqual(result["completed_segments"], 0)
        self.assertEqual(result["status_tool"], "voicebox.get_story_status")
        self.assertIsNone(result["download_url"])
        start_mock.assert_called_once_with(result["story_id"])
        segment = self.db.query(DBStorySegment).one()
        self.assertEqual(segment.text, "Benvenuti")
        self.assertEqual(segment.profile_id, self.profile.id)

    async def test_create_enforces_script_limits(self) -> None:
        invalid_cases = [
            ("", [{"profile": "Serena", "text": "x"}], "title"),
            ("Title", [], "at least one"),
            (
                "Title",
                [{"profile": "Serena", "text": "x"}] * 101,
                "100",
            ),
            (
                "Title",
                [{"profile": "Serena", "text": "x" * 10_001}],
                "10000",
            ),
            (
                "Title",
                [
                    {"profile": "Serena", "text": "x" * 10_000}
                    for _ in range(11)
                ],
                "100000",
            ),
        ]
        for title, segments, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    await story_tools.create_story(
                        title=title,
                        description=None,
                        segments=segments,
                        db=self.db,
                    )

        self.assertEqual(self.db.query(DBStory).count(), 0)

    def test_status_exposes_http_url_only_when_persistent_render_exists(self) -> None:
        story = DBStory(
            id="story-1",
            name="Done",
            status="completed",
            total_segments=1,
            completed_segments=1,
            render_audio_path="stories/story-1/story.wav",
            rendered_at=datetime.utcnow(),
        )
        self.db.add(story)
        self.db.commit()
        path = config.get_stories_dir() / story.id / "story.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF....WAVE")

        result = story_tools.get_story_status(story.id, self.db)

        self.assertEqual(result["download_url"], f"/stories/{story.id}/export-audio")
        self.assertNotIn(str(config.get_data_dir()), str(result))
        self.assertFalse(result["resumable"])

    def test_get_story_returns_ordered_segments_without_audio_paths(self) -> None:
        story = DBStory(
            id="story-1",
            name="Details",
            status="failed",
            error="failure",
            total_segments=1,
            completed_segments=0,
        )
        generation = DBGeneration(
            id="generation-1",
            profile_id=self.profile.id,
            text="Hello",
            language="it",
            audio_path="/private/audio.wav",
            duration=1.25,
            engine="qwen",
            status="failed",
            error="failure",
            source="mcp_story",
            created_at=datetime.utcnow(),
        )
        segment = DBStorySegment(
            id="segment-1",
            story_id=story.id,
            position=1,
            profile_id=self.profile.id,
            text="Hello",
            generation_id=generation.id,
            status="failed",
            error="failure",
        )
        self.db.add_all([story, generation, segment])
        self.db.commit()

        result = story_tools.get_story(story.id, self.db)

        self.assertEqual(result["segments"][0]["profile_name"], "Serena")
        self.assertEqual(result["segments"][0]["generation_status"], "failed")
        self.assertEqual(result["segments"][0]["duration"], 1.25)
        self.assertNotIn("audio_path", result["segments"][0])
        self.assertNotIn("/private", str(result))

    async def test_resume_returns_normalized_status(self) -> None:
        story = DBStory(
            id="story-1",
            name="Resume",
            status="failed",
            total_segments=1,
            completed_segments=0,
        )
        self.db.add(story)
        self.db.commit()

        with patch.object(
            story_tools.orchestration,
            "resume_story_workflow",
            new=AsyncMock(return_value=story),
        ) as resume_mock:
            result = await story_tools.resume_story(story.id, self.db)

        resume_mock.assert_awaited_once_with(story.id, self.db)
        self.assertEqual(result["story_id"], story.id)
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
