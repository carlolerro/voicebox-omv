"""RED tests for persistent MCP Story orchestration schema.

These tests intentionally describe schema that is not present before the
MCP Story implementation. Production code must not be added until this file
has been observed failing for the expected missing-schema reasons.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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


def _sqlite_engine(tmp_path: Path, name: str = "story-schema.db"):
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )


def test_story_model_exposes_orchestration_columns_and_segment_model() -> None:
    """The ORM must expose workflow state without replacing existing Story."""
    story_columns = set(database.Story.__table__.columns.keys())

    assert EXPECTED_STORY_COLUMNS <= story_columns
    assert hasattr(database, "StorySegment")

    segment_model = database.StorySegment
    assert segment_model.__tablename__ == "story_segments"
    assert {
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
    } <= set(segment_model.__table__.columns.keys())


def test_upgrade_migration_adds_story_workflow_schema_idempotently(tmp_path: Path) -> None:
    """An existing Voicebox database must upgrade without losing Story rows."""
    engine = _sqlite_engine(tmp_path, "legacy-story.db")
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
                    profile_id VARCHAR NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO stories (id, name) VALUES ('legacy-story', 'Legacy')"
            )
        )

    run_migrations(engine)
    run_migrations(engine)

    inspector = inspect(engine)
    assert EXPECTED_STORY_COLUMNS <= {
        column["name"] for column in inspector.get_columns("stories")
    }
    assert "story_segments" in set(inspector.get_table_names())

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

    assert tuple(migrated) == ("Legacy", "draft", 0, 0)


def test_story_segment_position_is_unique_per_story(tmp_path: Path) -> None:
    """Resume logic depends on one stable segment row per script position."""
    assert hasattr(database, "StorySegment")

    engine = _sqlite_engine(tmp_path, "unique-position.db")
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
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()
        engine.dispose()
