from __future__ import annotations

import io
import sys
import tempfile
import unittest
import uuid
import wave
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
from backend.database import Generation as DBGeneration
from backend.database import ProfileSample as DBProfileSample
from backend.database import Story as DBStory
from backend.database import StoryItem as DBStoryItem
from backend.database import StorySegment as DBStorySegment
from backend.database import VoiceProfile as DBVoiceProfile
from backend.services import story_orchestration as orchestration


def _wav_bytes() -> bytes:
    frames = bytearray()
    for index in range(2_400):
        sample = 2_000 if (index // 40) % 2 == 0 else -2_000
        frames.extend(sample.to_bytes(2, "little", signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24_000)
        wav_file.writeframes(bytes(frames))
    return buffer.getvalue()


class StoryOrchestrationTestCase(unittest.IsolatedAsyncioTestCase):
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

        self.generate_calls: list[str] = []
        self.retry_calls: list[str] = []
        self.render_calls: list[str] = []
        self.fail_texts: set[str] = set()

        self.profile_a = self._ready_clone("profile-a", "Serena")
        self.profile_b = self._ready_clone("profile-b", "Ryan")

        def get_test_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        async def fake_generate(request, db):
            self.generate_calls.append(request.text)
            generation_id = str(uuid.uuid4())
            failed = request.text in self.fail_texts
            stored_path = ""
            duration = 0.0
            if not failed:
                audio_path = config.get_generations_dir() / f"{generation_id}.wav"
                audio_path.write_bytes(_wav_bytes())
                stored_path = config.to_storage_path(audio_path)
                duration = 0.1
            generation = DBGeneration(
                id=generation_id,
                profile_id=request.profile_id,
                text=request.text,
                language=request.language,
                audio_path=stored_path,
                duration=duration,
                engine=request.engine or "qwen",
                status="failed" if failed else "completed",
                error="synthetic failure" if failed else None,
                source="manual",
                created_at=datetime.utcnow(),
            )
            db.add(generation)
            db.commit()
            db.refresh(generation)
            return generation

        async def fake_retry(generation_id, db):
            self.retry_calls.append(generation_id)
            generation = db.query(DBGeneration).filter_by(id=generation_id).one()
            audio_path = config.get_generations_dir() / f"{generation_id}.wav"
            audio_path.write_bytes(_wav_bytes())
            generation.audio_path = config.to_storage_path(audio_path)
            generation.duration = 0.1
            generation.status = "completed"
            generation.error = None
            db.commit()
            db.refresh(generation)
            return generation

        async def fake_render(story_id, db):
            self.render_calls.append(story_id)
            path = config.get_stories_dir() / story_id / "story.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_wav_bytes())
            story = db.query(DBStory).filter_by(id=story_id).one()
            story.render_audio_path = config.to_storage_path(path)
            story.rendered_at = datetime.utcnow()
            db.commit()
            return story.render_audio_path

        self.patchers = [
            patch.object(orchestration, "get_db", get_test_db),
            patch.object(orchestration.generation_routes, "generate_speech", fake_generate),
            patch.object(orchestration.generation_routes, "retry_generation", fake_retry),
            patch.object(orchestration.story_rendering, "render_story_persistent", fake_render),
            patch.object(orchestration, "POLL_INTERVAL_SECONDS", 0),
        ]
        for patcher in self.patchers:
            patcher.start()
        orchestration._active_story_ids.clear()
        orchestration._story_tasks.clear()

    def tearDown(self) -> None:
        for task in list(orchestration._story_tasks.values()):
            task.cancel()
        orchestration._active_story_ids.clear()
        orchestration._story_tasks.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
        config.set_data_dir(self.original_data_dir)

    def _ready_clone(self, profile_id: str, name: str) -> DBVoiceProfile:
        profile = DBVoiceProfile(
            id=profile_id,
            name=name,
            language="it",
            voice_type="cloned",
            default_engine="qwen",
        )
        self.db.add(profile)
        self.db.flush()
        self.db.add(
            DBProfileSample(
                id=f"sample-{profile_id}",
                profile_id=profile_id,
                audio_path=f"profiles/{profile_id}.wav",
                reference_text="Reference",
            )
        )
        self.db.commit()
        return profile

    async def _create_three_segment_story(self) -> DBStory:
        return await orchestration.create_story_workflow(
            title="Demo",
            description=None,
            segments=[
                orchestration.StorySegmentSpec(profile="Serena", text="uno"),
                orchestration.StorySegmentSpec(profile="Ryan", text="due"),
                orchestration.StorySegmentSpec(profile="Serena", text="tre"),
            ],
            db=self.db,
        )

    def _segments(self, story_id: str) -> list[DBStorySegment]:
        self.db.expire_all()
        return (
            self.db.query(DBStorySegment)
            .filter_by(story_id=story_id)
            .order_by(DBStorySegment.position)
            .all()
        )

    async def test_creation_validates_all_profiles_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not found"):
            await orchestration.create_story_workflow(
                title="Invalid",
                description=None,
                segments=[
                    orchestration.StorySegmentSpec(profile="Serena", text="ok"),
                    orchestration.StorySegmentSpec(profile="Missing", text="bad"),
                ],
                db=self.db,
            )

        self.assertEqual(self.db.query(DBStory).count(), 0)
        self.assertEqual(self.db.query(DBStorySegment).count(), 0)

    async def test_segments_generate_in_order_attach_and_render(self) -> None:
        story = await self._create_three_segment_story()

        await orchestration._run_story_workflow(story.id)

        self.db.expire_all()
        refreshed = self.db.query(DBStory).filter_by(id=story.id).one()
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.completed_segments, 3)
        self.assertEqual(self.generate_calls, ["uno", "due", "tre"])
        self.assertEqual([row.status for row in self._segments(story.id)], ["completed"] * 3)
        self.assertEqual(self.db.query(DBStoryItem).filter_by(story_id=story.id).count(), 3)
        generations = self.db.query(DBGeneration).order_by(DBGeneration.created_at).all()
        self.assertTrue(all(row.source == "mcp_story" for row in generations))
        self.assertEqual(self.render_calls, [story.id])

    async def test_first_failure_stops_and_preserves_completed_work(self) -> None:
        story = await self._create_three_segment_story()
        self.fail_texts.add("due")

        await orchestration._run_story_workflow(story.id)

        self.db.expire_all()
        refreshed = self.db.query(DBStory).filter_by(id=story.id).one()
        self.assertEqual(refreshed.status, "failed")
        self.assertEqual(refreshed.completed_segments, 1)
        self.assertEqual(refreshed.failed_segment_index, 2)
        self.assertEqual([row.status for row in self._segments(story.id)], ["completed", "failed", "pending"])
        self.assertEqual(self.generate_calls, ["uno", "due"])
        self.assertEqual(self.db.query(DBGeneration).count(), 2)
        self.assertEqual(self.db.query(DBStoryItem).count(), 1)
        self.assertEqual(self.render_calls, [])

    async def test_resume_retries_same_failed_generation_without_regenerating_completed(self) -> None:
        story = await self._create_three_segment_story()
        self.fail_texts.add("due")
        await orchestration._run_story_workflow(story.id)
        failed_segment = self._segments(story.id)[1]
        failed_generation_id = failed_segment.generation_id
        first_generation_id = self._segments(story.id)[0].generation_id
        self.fail_texts.clear()

        await orchestration.resume_story_workflow(story.id, self.db)
        await orchestration.wait_for_story_workflow(story.id)

        segments = self._segments(story.id)
        refreshed = self.db.query(DBStory).filter_by(id=story.id).one()
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(segments[0].generation_id, first_generation_id)
        self.assertEqual(segments[1].generation_id, failed_generation_id)
        self.assertEqual(self.retry_calls, [failed_generation_id])
        self.assertEqual(self.db.query(DBGeneration).count(), 3)
        self.assertEqual(self.generate_calls, ["uno", "due", "tre"])

    async def test_resume_attaches_completed_generation_without_new_tts(self) -> None:
        story = await orchestration.create_story_workflow(
            title="Attach",
            description=None,
            segments=[orchestration.StorySegmentSpec(profile="Serena", text="ready")],
            db=self.db,
        )
        generation_id = "already-complete"
        audio_path = config.get_generations_dir() / f"{generation_id}.wav"
        audio_path.write_bytes(_wav_bytes())
        generation = DBGeneration(
            id=generation_id,
            profile_id=self.profile_a.id,
            text="ready",
            language="it",
            audio_path=config.to_storage_path(audio_path),
            duration=0.1,
            engine="qwen",
            status="completed",
            source="mcp_story",
            created_at=datetime.utcnow(),
        )
        segment = self.db.query(DBStorySegment).filter_by(story_id=story.id).one()
        segment.generation_id = generation_id
        segment.status = "failed"
        story.status = "failed"
        story.error = "interrupted"
        self.db.add(generation)
        self.db.commit()

        await orchestration.resume_story_workflow(story.id, self.db)
        await orchestration.wait_for_story_workflow(story.id)

        self.assertEqual(self.generate_calls, [])
        self.assertEqual(self.retry_calls, [])
        self.assertEqual(self.db.query(DBGeneration).count(), 1)
        self.assertEqual(self.db.query(DBStoryItem).filter_by(story_id=story.id).count(), 1)
        self.assertEqual(self._segments(story.id)[0].status, "completed")

    async def test_render_only_resume_skips_generation(self) -> None:
        story = await orchestration.create_story_workflow(
            title="Render only",
            description=None,
            segments=[orchestration.StorySegmentSpec(profile="Serena", text="done")],
            db=self.db,
        )
        generation_id = "render-only-generation"
        audio_path = config.get_generations_dir() / f"{generation_id}.wav"
        audio_path.write_bytes(_wav_bytes())
        generation = DBGeneration(
            id=generation_id,
            profile_id=self.profile_a.id,
            text="done",
            language="it",
            audio_path=config.to_storage_path(audio_path),
            duration=0.1,
            engine="qwen",
            status="completed",
            source="mcp_story",
            created_at=datetime.utcnow(),
        )
        segment = self.db.query(DBStorySegment).filter_by(story_id=story.id).one()
        segment.generation_id = generation_id
        segment.status = "completed"
        story.status = "failed"
        story.completed_segments = 1
        self.db.add_all(
            [
                generation,
                DBStoryItem(
                    id="render-only-item",
                    story_id=story.id,
                    generation_id=generation_id,
                    start_time_ms=0,
                    track=0,
                ),
            ]
        )
        self.db.commit()

        await orchestration.resume_story_workflow(story.id, self.db)
        await orchestration.wait_for_story_workflow(story.id)

        self.assertEqual(self.generate_calls, [])
        self.assertEqual(self.retry_calls, [])
        self.assertEqual(self.render_calls, [story.id])
        self.db.refresh(story)
        self.assertEqual(story.status, "completed")

    async def test_duplicate_resume_is_rejected(self) -> None:
        story = await self._create_three_segment_story()
        story.status = "failed"
        self.db.commit()
        orchestration._active_story_ids.add(story.id)

        with self.assertRaisesRegex(ValueError, "already processing"):
            await orchestration.resume_story_workflow(story.id, self.db)


if __name__ == "__main__":
    unittest.main()
