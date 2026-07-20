"""RED tests for persistent MCP Story orchestration schema.

These tests intentionally describe schema that is not present before the
MCP Story implementation. Production code must not be added until this file
has been observed failing for the expected missing-schema reasons.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.database.migrations import run_migrations


EXPECTED_STORY_COLUMNS = {
    "status",
    "error",
    "render_audio_path",
    "rendered_at",
    "total_segments",
    "completed_segments",
    "current_segment_index",
    "failed_segment_index",
}


def _sqlite_engine(directory: str, name: str):
    return create_engine(
        f"sqlite:///{Path(directory) / name}",
        connect_args={"check_same_thread": False},
    )


class StoryOrchestrationModelRedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_story_model_exposes_orchestration_columns_and_segment_model(self) -> None:
        """The ORM must extend the existing Story rather than replace it."""
        story_columns = set(database.Story.__table__.columns.keys())

        self.assertTrue(
            EXPECTED_STORY_COLUMNS <= story_columns,
            f"Missing Story columns: {sorted(EXPECTED_STORY_COLUMNS - story_columns)}",
        )
        self.assertTrue(hasattr(database, "StorySegment"))

        segment_model = database.StorySegment
        self.assertEqual(segment_model.__tablename__, "story_segments")
        expected_segment_columns = {
            "id",
            "story_id",
            "position",
            "profile_id",
            "text",
            "generation_id",
            "status",
            "error",
            "created_at",
            "updated_at",
        }
        actual_segment_columns = set(segment_model.__table__.columns.keys())
        self.assertTrue(
            expected_segment_columns <= actual_segment_columns,
            f"Missing StorySegment columns: {sorted(expected_segment_columns - actual_segment_columns)}",
        )

    def test_upgrade_migration_adds_story_workflow_schema_idempotently(self) -> None:
        """An existing Voicebox database upgrades without losing Story rows."""
        engine = _sqlite_engine(self.temp_dir.name, "legacy-story.db")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE stories (
                            id VARCHAR PRIMARY KEY,
                            name VARCHAR NOT NULL,
                            description TEXT,
                            created_at DATETIME,
                            updated_at DATETIME
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE profiles (
                            id VARCHAR PRIMARY KEY,
                            name VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE generations (
                            id VARCHAR PRIMARY KEY,
                            profile_id VARCHAR NOT NULL,
                            audio_path VARCHAR NOT NULL DEFAULT ''
                        )
                        """
                    )
                )
                connection.execute(
                    text("INSERT INTO stories (id, name) VALUES ('legacy-story', 'Legacy')")
                )

            run_migrations(engine)
            run_migrations(engine)

            inspector = inspect(engine)
            actual_story_columns = {
                column["name"] for column in inspector.get_columns("stories")
            }
            self.assertTrue(
                EXPECTED_STORY_COLUMNS <= actual_story_columns,
                f"Migration missed columns: {sorted(EXPECTED_STORY_COLUMNS - actual_story_columns)}",
            )
            self.assertIn("story_segments", set(inspector.get_table_names()))

            with engine.connect() as connection:
                migrated = connection.execute(
                    text(
                        """
                        SELECT name, status, total_segments, completed_segments
                        FROM stories
                        WHERE id = 'legacy-story'
                        """
                    )
                ).one()

            self.assertEqual(tuple(migrated), ("Legacy", "draft", 0, 0))
        finally:
            engine.dispose()

    def test_story_segment_position_is_unique_per_story(self) -> None:
        """Resume depends on one stable segment row per script position."""
        self.assertTrue(hasattr(database, "StorySegment"))

        engine = _sqlite_engine(self.temp_dir.name, "unique-position.db")
        database.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            profile = database.VoiceProfile(id="profile-1", name="Serena")
            story = database.Story(id="story-1", name="Demo")
            session.add_all([profile, story])
            session.commit()

            segment_model = database.StorySegment
            session.add(
                segment_model(
                    id="segment-1",
                    story_id=story.id,
                    position=1,
                    profile_id=profile.id,
                    text="Prima battuta",
                )
            )
            session.commit()

            session.add(
                segment_model(
                    id="segment-2",
                    story_id=story.id,
                    position=1,
                    profile_id=profile.id,
                    text="Posizione duplicata",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
