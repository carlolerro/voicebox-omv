# Asynchronous MCP Story Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing Voicebox Story editor through four asynchronous MCP tools that generate ordered multi-profile segments, stop on first failure, resume safely, render one persistent WAV, and return an HTTP download URL.

**Architecture:** Add persistent orchestration metadata (`Story` workflow columns plus `StorySegment`) while retaining the existing `Story`, `StoryItem`, `Generation`, serial TTS queue, generation retry, Story mixer, and export route. A new orchestration service calls existing Python route/service functions directly, waits with short-lived database sessions, reconciles partial progress idempotently, and persists a strict final render. MCP tools are thin validated wrappers; current REST/UI contracts remain compatible.

**Tech Stack:** Python 3.11+, FastAPI, FastMCP, SQLAlchemy 2, Pydantic 2, SQLite, NumPy/audio utilities, pytest/unittest, Docker/OMV.

## Global Constraints

- V1 segment input is exactly `profile + text`; no existing generations, files, paths, Base64, or per-segment overrides.
- Processing is asynchronous and the MCP create/resume calls return immediately.
- Stop at the first segment failure; retain completed work; no automatic continuation or retry.
- Resume never regenerates completed segments and retries the same failed Generation ID when it still exists.
- All TTS inference continues through the existing global serial queue; no parallel inference or new worker/container.
- Language, engine, and personality behavior derive from the profile.
- Profile lookup is exact ID first, otherwise case-insensitive and unambiguous.
- Persistent MCP rendering is strict; legacy/manual on-demand Story export remains permissive.
- Final audio is an HTTP WAV download, not Base64.
- Existing Story REST paths and frontend payloads remain valid.
- Do not add, enable, or run GitHub Actions. Verification is local and on OMV.
- Implement with test-driven development: one failing behavior test before each production change.

---

## File map

### New production files

- `backend/services/story_orchestration.py` — workflow creation, profile validation, progress, polling, failure, resume, reconciliation, and restart recovery.
- `backend/services/story_rendering.py` — strict preflight, persistent atomic render, render-path validation/removal.
- `backend/mcp_server/story_tools.py` — four MCP tools and normalized response schemas.

### Modified production files

- `backend/database/models.py` — Story workflow columns and `StorySegment` ORM model.
- `backend/database/migrations.py` — idempotent Story column/table/index migrations.
- `backend/database/__init__.py` — export `StorySegment`.
- `backend/database/session.py` — invoke restart recovery after schema initialization.
- `backend/config.py` — `get_stories_dir()`.
- `backend/routes/stories.py` — active-workflow mutation guards, terminal deletion cleanup, persistent-render serving.
- `backend/mcp_server/server.py` — register Story tools and update instructions.
- `docs/content/docs/overview/mcp-server.mdx` — Story MCP contract and examples.
- `scripts/verify_mcp_profile_management_omv.sh` — expect 12 tools and run Story non-GPU tests.
- `scripts/deploy_mcp_profile_management_omv.sh` — verify the four Story tools after deployment without triggering Actions.

### New tests

- `backend/tests/test_story_orchestration_models.py`
- `backend/tests/test_story_profile_resolution.py`
- `backend/tests/test_story_rendering.py`
- `backend/tests/test_story_orchestration.py`
- `backend/tests/test_mcp_story_tools.py`
- `backend/tests/test_story_http_compatibility.py`
- `backend/tests/test_story_restart_recovery.py`

---

### Task 1: Persist Story workflow state and segments

**Files:**
- Modify: `backend/database/models.py`
- Modify: `backend/database/migrations.py`
- Modify: `backend/database/__init__.py`
- Test: `backend/tests/test_story_orchestration_models.py`

**Interfaces:**
- Produces ORM class `StorySegment` and Story fields `status`, `error`, `render_audio_path`, `rendered_at`, `total_segments`, `completed_segments`, `current_segment_index`, `failed_segment_index`.
- Produces idempotent `_migrate_stories()` and `_migrate_story_segments()` startup migrations.

- [ ] **Step 1: Write failing fresh-schema and upgrade tests**

```python
from sqlalchemy import inspect, text


def test_story_orchestration_schema_is_created(database_engine):
    tables = set(inspect(database_engine).get_table_names())
    assert "story_segments" in tables
    story_columns = {c["name"] for c in inspect(database_engine).get_columns("stories")}
    assert {
        "status", "error", "render_audio_path", "rendered_at",
        "total_segments", "completed_segments",
        "current_segment_index", "failed_segment_index",
    } <= story_columns


def test_existing_story_rows_receive_safe_defaults(upgraded_database_engine):
    with upgraded_database_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, total_segments, completed_segments FROM stories WHERE id='legacy'"
        )).one()
    assert row == ("draft", 0, 0)


def test_story_segment_positions_are_unique(db_session, story_row, profile_row):
    from backend.database import StorySegment
    db_session.add(StorySegment(story_id=story_row.id, position=1, profile_id=profile_row.id, text="a"))
    db_session.commit()
    db_session.add(StorySegment(story_id=story_row.id, position=1, profile_id=profile_row.id, text="b"))
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: Run RED**

Run:

```bash
python -m pytest backend/tests/test_story_orchestration_models.py -q
```

Expected: FAIL because the new columns/table/model do not exist.

- [ ] **Step 3: Add ORM fields and model**

Add to `Story`:

```python
status = Column(String, nullable=False, default="draft")
error = Column(Text, nullable=True)
render_audio_path = Column(String, nullable=True)
rendered_at = Column(DateTime, nullable=True)
total_segments = Column(Integer, nullable=False, default=0)
completed_segments = Column(Integer, nullable=False, default=0)
current_segment_index = Column(Integer, nullable=True)
failed_segment_index = Column(Integer, nullable=True)
```

Add:

```python
class StorySegment(Base):
    __tablename__ = "story_segments"
    __table_args__ = (
        UniqueConstraint("story_id", "position", name="uq_story_segments_story_position"),
        Index("ix_story_segments_story_status", "story_id", "status"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    story_id = Column(String, ForeignKey("stories.id"), nullable=False)
    position = Column(Integer, nullable=False)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    text = Column(Text, nullable=False)
    generation_id = Column(String, ForeignKey("generations.id"), nullable=True)
    status = Column(String, nullable=False, default="pending")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
```

Import `UniqueConstraint` and `Index`, export `StorySegment`, call migrations from `run_migrations()`.

Migration SQL must add each missing Story column independently and create `story_segments` plus its indexes when absent. It must be safe before `Base.metadata.create_all()` and on repeated startup.

- [ ] **Step 4: Run GREEN and migration idempotency**

```bash
python -m pytest backend/tests/test_story_orchestration_models.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/database backend/tests/test_story_orchestration_models.py
git commit -m "feat: persist Story orchestration state"
```

---

### Task 2: Add Story storage and strict persistent rendering

**Files:**
- Modify: `backend/config.py`
- Create: `backend/services/story_rendering.py`
- Test: `backend/tests/test_story_rendering.py`

**Interfaces:**
- Produces `validate_render_inputs(story_id: str, db: Session) -> None`.
- Produces `render_story_persistent(story_id: str, db: Session) -> str` returning a storage-relative WAV path.
- Produces `resolve_valid_render_path(story) -> Path | None` and `remove_persistent_render(story) -> None`.
- Consumes existing `services.stories.export_story_audio()`; does not add a second mixer.

- [ ] **Step 1: Write failing strict/permissive and atomic-write tests**

```python
@pytest.mark.asyncio
async def test_strict_render_rejects_missing_required_clip(story_with_missing_audio, db_session):
    with pytest.raises(ValueError, match="missing or unreadable"):
        await render_story_persistent(story_with_missing_audio.id, db_session)


@pytest.mark.asyncio
async def test_persistent_render_uses_existing_mixer_and_atomic_replace(
    complete_story, db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "get_stories_dir", lambda: tmp_path / "stories")
    monkeypatch.setattr(stories, "export_story_audio", AsyncMock(return_value=b"RIFF....WAVEdata"))
    stored = await render_story_persistent(complete_story.id, db_session)
    path = config.resolve_storage_path(stored)
    assert path.name == "story.wav"
    assert path.read_bytes().startswith(b"RIFF")
    stories.export_story_audio.assert_awaited_once_with(complete_story.id, db_session)
```

Also retain a regression test that direct `stories.export_story_audio()` remains permissive for a legacy/manual Story.

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_story_rendering.py -q
```

Expected: import/function failures.

- [ ] **Step 3: Add storage helper and rendering service**

Add:

```python
def get_stories_dir() -> Path:
    path = _data_dir / "stories"
    path.mkdir(parents=True, exist_ok=True)
    return path
```

Strict preflight must:

```python
items = db.query(DBStoryItem, DBGeneration).join(
    DBGeneration, DBStoryItem.generation_id == DBGeneration.id
).filter(DBStoryItem.story_id == story_id).all()

if not items:
    raise ValueError("Story has no audio items")
for item, generation in items:
    if (generation.status or "completed") != "completed":
        raise ValueError(f"Story segment generation is not completed: {generation.id}")
    stored = generation.audio_path
    if item.version_id:
        version = db.query(DBGenerationVersion).filter_by(
            id=item.version_id, generation_id=generation.id
        ).first()
        if version is None:
            raise ValueError("Story segment version is missing")
        stored = version.audio_path
    path = config.resolve_storage_path(stored)
    if path is None or not path.is_file():
        raise ValueError("Story segment audio is missing or unreadable")
    await asyncio.to_thread(load_audio, str(path), sample_rate=24000)
```

Then call existing `stories.export_story_audio`, write bytes to a temporary file inside `stories/{story_id}`, call `os.replace(temp_path, final_path)`, and update `Story.render_audio_path/rendered_at` only after replacement succeeds.

- [ ] **Step 4: Run GREEN**

```bash
python -m pytest backend/tests/test_story_rendering.py -q
```

Expected: all tests PASS, including permissive legacy regression.

- [ ] **Step 5: Commit**

```bash
git add backend/config.py backend/services/story_rendering.py backend/tests/test_story_rendering.py
git commit -m "feat: persist strict Story renders"
```

---

### Task 3: Resolve and validate Story profiles without ambiguity

**Files:**
- Create: `backend/tests/test_story_profile_resolution.py`
- Create initially inside: `backend/services/story_orchestration.py` focused resolution/readiness functions

**Interfaces:**
- Produces `resolve_story_profile(value: str, db: Session) -> DBVoiceProfile`.
- Produces `validate_story_profile(profile: DBVoiceProfile, db: Session) -> None`.
- Produces `resolve_story_engine(profile: DBVoiceProfile) -> str` by reusing `routes.generations._resolve_generation_engine` with an explicit `GenerationRequest(engine=None)`.

- [ ] **Step 1: Write failing resolution/readiness tests**

```python
def test_profile_id_has_priority(db_session, profile):
    assert resolve_story_profile(profile.id, db_session).id == profile.id


def test_profile_name_is_case_insensitive(db_session, profile):
    assert resolve_story_profile(profile.name.swapcase(), db_session).id == profile.id


def test_legacy_case_insensitive_duplicates_are_rejected(db_session):
    db_session.add_all([make_profile(name="Serena"), make_profile(name="SERENA")])
    db_session.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_story_profile("serena", db_session)


def test_preset_engine_is_derived_from_profile(db_session, ready_preset_profile):
    assert resolve_story_engine(ready_preset_profile) == ready_preset_profile.preset_engine


def test_cloned_profile_without_samples_is_rejected(db_session, cloned_profile):
    with pytest.raises(ValueError, match="not ready"):
        validate_story_profile(cloned_profile, db_session)
```

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_story_profile_resolution.py -q
```

Expected: functions missing.

- [ ] **Step 3: Implement minimal helpers**

```python
def resolve_story_profile(value: str, db: Session) -> DBVoiceProfile:
    candidate = value.strip()
    if not candidate:
        raise ValueError("profile must not be empty")
    exact = db.query(DBVoiceProfile).filter(DBVoiceProfile.id == candidate).first()
    if exact is not None:
        return exact
    matches = db.query(DBVoiceProfile).filter(
        func.lower(DBVoiceProfile.name) == candidate.lower()
    ).all()
    if not matches:
        raise ValueError(f"Voice profile '{candidate}' was not found.")
    if len(matches) != 1:
        raise ValueError(f"Voice profile name '{candidate}' is ambiguous.")
    return matches[0]
```

`validate_story_profile` accepts only valid preset profiles or cloned profiles with at least one sample and a cloning-compatible engine. Use existing `profiles_service.validate_profile_engine()` and preset-voice service validation; do not import MCP serialization code.

`resolve_story_engine` constructs `models.GenerationRequest(profile_id=profile.id, text="validation", language=profile.language, engine=None)` and calls the existing `_resolve_generation_engine` so the Pydantic `qwen` default cannot override preset/default engine metadata.

- [ ] **Step 4: Run GREEN**

```bash
python -m pytest backend/tests/test_story_profile_resolution.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/story_orchestration.py backend/tests/test_story_profile_resolution.py
git commit -m "feat: validate Story voice profiles"
```

---

### Task 4: Implement asynchronous ordered orchestration and stop-on-failure

**Files:**
- Modify: `backend/services/story_orchestration.py`
- Test: `backend/tests/test_story_orchestration.py`

**Interfaces:**
- Produces dataclass `StorySegmentSpec(profile: str, text: str)`.
- Produces `create_story_workflow(title, description, segments, db) -> DBStory`.
- Produces `start_story_workflow(story_id: str) -> None`.
- Produces private `_run_story_workflow(story_id: str) -> None`.
- Consumes existing `routes.generations.generate_speech`, `services.stories.add_item_to_story`, and global task queue.

- [ ] **Step 1: Write failing ordered-success test**

Use fake route functions that create Generation rows and complete them deterministically with tiny synthetic WAV files.

```python
@pytest.mark.asyncio
async def test_story_generates_segments_in_order_and_renders(
    db_session, two_ready_profiles, fake_generation_pipeline, monkeypatch
):
    story = await create_story_workflow(
        title="Demo",
        description=None,
        segments=[
            StorySegmentSpec(profile=two_ready_profiles[0].name, text="uno"),
            StorySegmentSpec(profile=two_ready_profiles[1].name, text="due"),
        ],
        db=db_session,
    )
    await _run_story_workflow(story.id)
    db_session.expire_all()
    refreshed = db_session.get(DBStory, story.id)
    segments = db_session.query(DBStorySegment).filter_by(story_id=story.id).order_by(DBStorySegment.position).all()
    assert refreshed.status == "completed"
    assert refreshed.completed_segments == 2
    assert [s.status for s in segments] == ["completed", "completed"]
    assert fake_generation_pipeline.texts == ["uno", "due"]
```

- [ ] **Step 2: Write failing first-error stop test**

```python
@pytest.mark.asyncio
async def test_first_failure_stops_later_segments_and_preserves_completed_work(...):
    fake_generation_pipeline.fail_on_position = 2
    await _run_story_workflow(story.id)
    assert story.status == "failed"
    assert story.completed_segments == 1
    assert story.failed_segment_index == 2
    assert segment_statuses == ["completed", "failed", "pending"]
    assert generation_count == 2
    assert story_item_count == 1
```

- [ ] **Step 3: Run RED**

```bash
python -m pytest backend/tests/test_story_orchestration.py -q
```

Expected: orchestration functions missing.

- [ ] **Step 4: Implement workflow creation and runner**

Creation validates every segment/profile before writing. Then create one `DBStory` and ordered `DBStorySegment` rows and commit once. Use an in-process `_active_story_ids: set[str]` and `create_background_task()`.

Per segment:

1. Open a DB session.
2. Reconcile an already completed generation/item.
3. Resolve/revalidate the stored profile.
4. Create `GenerationRequest(..., engine=None, personality=bool(profile.personality))`.
5. Call existing `generate_speech(req, db)`.
6. Override the new Generation row `source = "mcp_story"` and persist `segment.generation_id`.
7. Close the session.
8. Poll terminal state using short-lived sessions and `await asyncio.sleep(0.5)`.
9. On completed, call existing `add_item_to_story(story_id, StoryItemCreate(generation_id=id), db)`.
10. Mark segment completed and reconcile counters.

Sanitize runtime errors using a helper that returns a bounded user-safe message and strips absolute paths/newlines.

All registry cleanup must occur in `finally`.

- [ ] **Step 5: Run GREEN**

```bash
python -m pytest backend/tests/test_story_orchestration.py -q
```

Expected: ordered success and stop-on-failure tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/story_orchestration.py backend/tests/test_story_orchestration.py
git commit -m "feat: orchestrate asynchronous Story generation"
```

---

### Task 5: Implement idempotent resume and render-only recovery

**Files:**
- Modify: `backend/services/story_orchestration.py`
- Extend test: `backend/tests/test_story_orchestration.py`

**Interfaces:**
- Produces `resume_story_workflow(story_id: str, db: Session) -> DBStory`.
- Reuses existing `routes.generations.retry_generation` for an existing failed Generation ID.

- [ ] **Step 1: Write failing resume tests**

```python
@pytest.mark.asyncio
async def test_resume_retries_failed_generation_without_regenerating_completed(...):
    failed_generation_id = failed_segment.generation_id
    await resume_story_workflow(story.id, db_session)
    await wait_for_local_story_task(story.id)
    assert first_segment.generation_id == original_completed_generation_id
    assert second_segment.generation_id == failed_generation_id
    assert generation_count == 2
    assert story.status == "completed"


@pytest.mark.asyncio
async def test_resume_attaches_completed_generation_missing_story_item(...):
    await resume_story_workflow(story.id, db_session)
    assert story_item_for_generation_exists
    assert generation_was_not_requeued


@pytest.mark.asyncio
async def test_render_failure_resume_runs_render_only(...):
    await resume_story_workflow(story.id, db_session)
    assert fake_generation_pipeline.calls == []
    assert render_service.calls == [story.id]
```

Also test duplicate resume rejection and missing Generation row replacement exactly once.

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_story_orchestration.py -q
```

Expected: resume tests FAIL.

- [ ] **Step 3: Implement resume/reconciliation**

Before state change, require Story `failed`, not active, and either incomplete segments or a render-only failure. Revalidate all remaining profiles. For each segment:

- completed + item exists: leave untouched;
- completed Generation + no item: attach existing Generation;
- item exists + stale segment state: mark completed;
- failed Generation exists: call existing `retry_generation(id, db)`;
- Generation row missing: clear `generation_id`, create exactly one replacement;
- all segments complete: skip TTS and render.

Set Story to `queued`, clear current/error fields, preserve completed counters after reconciliation, commit, then start the task.

- [ ] **Step 4: Run GREEN**

```bash
python -m pytest backend/tests/test_story_orchestration.py -q
```

Expected: all orchestration and resume tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/story_orchestration.py backend/tests/test_story_orchestration.py
git commit -m "feat: resume failed Story workflows"
```

---

### Task 6: Expose four MCP Story tools

**Files:**
- Create: `backend/mcp_server/story_tools.py`
- Modify: `backend/mcp_server/server.py`
- Test: `backend/tests/test_mcp_story_tools.py`

**Interfaces:**
- Produces tools `voicebox.create_story`, `voicebox.get_story_status`, `voicebox.get_story`, `voicebox.resume_story`.
- Tool response never exposes local paths.

- [ ] **Step 1: Write failing registration and contract tests**

```python
def test_story_tools_are_registered():
    mcp = build_mcp_server()
    names = {tool.name for tool in list_tools(mcp)}
    assert {
        "voicebox.create_story", "voicebox.get_story_status",
        "voicebox.get_story", "voicebox.resume_story",
    } <= names


@pytest.mark.asyncio
async def test_create_story_returns_immediate_queued_shape(...):
    result = await create_story_tool(
        title="Capitolo",
        description=None,
        segments=[{"profile": "serena", "text": "Benvenuti"}],
    )
    assert result["status"] == "queued"
    assert result["status_tool"] == "voicebox.get_story_status"
    assert result["download_url"] is None
```

Add validation tests for 1–100 segments, 10,000 characters per segment, 100,000 total, trimmed title/text, and no local paths.

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_mcp_story_tools.py -q
```

Expected: tools missing.

- [ ] **Step 3: Implement tools and normalization**

Use Pydantic/FastMCP-compatible input annotations:

```python
class StoryToolSegment(BaseModel):
    profile: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)
```

Register wrappers with short-lived `get_db()` sessions. Normalize status through one function that checks `resolve_valid_render_path(story)` before returning:

```python
{
    "story_id": story.id,
    "title": story.name,
    "status": story.status,
    "total_segments": story.total_segments,
    "completed_segments": story.completed_segments,
    "current_segment": story.current_segment_index,
    "failed_segment": story.failed_segment_index,
    "error": story.error,
    "resumable": is_story_resumable(story, db),
    "download_url": f"/stories/{story.id}/export-audio" if render_exists else None,
}
```

`get_story` joins ordered `StorySegment`, profile name, and optional Generation duration/status but omits `audio_path`.

Update FastMCP instructions and register `register_story_tools(mcp)`.

- [ ] **Step 4: Run GREEN and MCP regression**

```bash
python -m pytest \
  backend/tests/test_mcp_story_tools.py \
  backend/tests/test_mcp_profile_tools.py \
  backend/tests/test_mcp_profile_tools_regressions.py \
  backend/tests/test_mcp_http_routing.py -q
```

Expected: all tests PASS; original eight tools unchanged plus four Story tools.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server backend/tests/test_mcp_story_tools.py
git commit -m "feat: expose asynchronous Story tools over MCP"
```

---

### Task 7: Preserve REST/UI compatibility and serve persistent renders

**Files:**
- Modify: `backend/routes/stories.py`
- Test: `backend/tests/test_story_http_compatibility.py`

**Interfaces:**
- Produces route guard `_reject_active_story_mutation(story_id, db)`.
- Existing URLs and successful response bodies remain valid.

- [ ] **Step 1: Write failing HTTP compatibility tests**

```python
@pytest.mark.parametrize("status", ["queued", "generating", "rendering"])
def test_active_story_mutations_return_409(client, active_story, status):
    active_story.status = status
    response = client.put(f"/stories/{active_story.id}", json={"name": "x", "description": None})
    assert response.status_code == 409


def test_persistent_render_is_served_without_rebuilding(client, completed_story, render_file, monkeypatch):
    mixer = AsyncMock(side_effect=AssertionError("mixer must not run"))
    monkeypatch.setattr(stories, "export_story_audio", mixer)
    response = client.get(f"/stories/{completed_story.id}/export-audio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content.startswith(b"RIFF")


def test_draft_story_export_keeps_legacy_fallback(client, draft_story, monkeypatch):
    monkeypatch.setattr(stories, "export_story_audio", AsyncMock(return_value=b"RIFF....WAVE"))
    assert client.get(f"/stories/{draft_story.id}/export-audio").status_code == 200
```

Also test terminal deletion removes segments/render but preserves Generation rows, and terminal timeline edits invalidate stale render metadata/file.

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_story_http_compatibility.py -q
```

Expected: active mutations are not guarded and persistent render is not selected.

- [ ] **Step 3: Add guards, serving, invalidation, and cleanup**

At the start of every Story mutation route, load Story and raise:

```python
if story.status in {"queued", "generating", "rendering"}:
    raise HTTPException(status_code=409, detail="Story is currently processing")
```

After a successful terminal timeline mutation call `story_rendering.invalidate_story_render(story, db)`; completed becomes draft, while failed remains failed.

Export route first calls `resolve_valid_render_path(story)` and returns `FileResponse`; otherwise it invokes existing permissive `stories.export_story_audio()` unchanged.

Deletion rejects active status, removes `StorySegment` rows and render artifacts, then calls existing `stories.delete_story()` so existing StoryItem behavior remains the source of truth. Do not delete Generations.

- [ ] **Step 4: Run GREEN and frontend API regression**

```bash
python -m pytest backend/tests/test_story_http_compatibility.py -q
bun run typecheck
```

Expected: pytest passes and TypeScript typecheck exits 0 without frontend changes.

- [ ] **Step 5: Commit**

```bash
git add backend/routes/stories.py backend/tests/test_story_http_compatibility.py
git commit -m "feat: integrate Story workflows with existing REST routes"
```

---

### Task 8: Recover interrupted Story workflows at startup

**Files:**
- Modify: `backend/services/story_orchestration.py`
- Modify: `backend/database/session.py`
- Test: `backend/tests/test_story_restart_recovery.py`

**Interfaces:**
- Produces synchronous `recover_interrupted_story_workflows(db: Session) -> int`.
- Called after migrations and `Base.metadata.create_all()`.

- [ ] **Step 1: Write failing recovery tests**

```python
def test_startup_recovery_marks_active_story_failed_and_recomputes_progress(db_session):
    story.status = "generating"
    segments[0].status = "completed"
    segments[1].status = "generating"
    db_session.commit()
    count = recover_interrupted_story_workflows(db_session)
    db_session.refresh(story)
    assert count == 1
    assert story.status == "failed"
    assert story.completed_segments == 1
    assert story.current_segment_index is None
    assert story.error == "Server was shut down during Story processing"
    assert segments[1].status == "failed"
```

Also test that a segment whose Generation already completed is reconciled instead of failed and that draft/completed/failed Stories remain unchanged.

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_story_restart_recovery.py -q
```

Expected: recovery function missing.

- [ ] **Step 3: Implement and call recovery**

Query Stories in active statuses. For every segment, reconcile Generation and StoryItem state; mark only unresolved active segments failed. Recompute `completed_segments`; clear current index; set Story error/status; commit once.

In `init_db()` after `Base.metadata.create_all(bind=engine)`:

```python
recovery_db = SessionLocal()
try:
    from ..services.story_orchestration import recover_interrupted_story_workflows
    recover_interrupted_story_workflows(recovery_db)
finally:
    recovery_db.close()
```

- [ ] **Step 4: Run GREEN**

```bash
python -m pytest backend/tests/test_story_restart_recovery.py -q
```

Expected: all recovery tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/database/session.py backend/services/story_orchestration.py backend/tests/test_story_restart_recovery.py
git commit -m "feat: recover interrupted Story workflows"
```

---

### Task 9: Documentation and OMV verification scripts

**Files:**
- Modify: `docs/content/docs/overview/mcp-server.mdx`
- Modify: `scripts/verify_mcp_profile_management_omv.sh`
- Modify: `scripts/deploy_mcp_profile_management_omv.sh`
- Test: `backend/tests/test_mcp_docker_build_context.py`

**Interfaces:**
- Verification discovers 12 tools.
- Optional real-model smoke test consumes `VERIFY_STORY_PROFILE_A` and `VERIFY_STORY_PROFILE_B`.

- [ ] **Step 1: Write failing script assertions**

Extend the Docker/script test to assert:

```python
assert "voicebox.create_story" in verify_script
assert "voicebox.get_story_status" in verify_script
assert "voicebox.get_story" in verify_script
assert "voicebox.resume_story" in verify_script
assert "VERIFY_STORY_PROFILE_A" in deploy_script
assert "VERIFY_STORY_PROFILE_B" in deploy_script
```

- [ ] **Step 2: Run RED**

```bash
python -m pytest backend/tests/test_mcp_docker_build_context.py -q
```

Expected: missing Story verification strings.

- [ ] **Step 3: Update docs and scripts**

Document create/poll/failure/resume/download examples.

Verification script runs all non-GPU Story tests and discovers all 12 tools. Deployment script lists the same 12 tools. When both environment variables are supplied, it creates a two-segment Italian Story, polls to terminal state with a bounded timeout, verifies ordered segments, downloads the final URL, and validates HTTP `200`, `audio/wav`, non-empty content, and `RIFF`/`WAVE` headers. It must not deliberately fail a production model.

Do not change `.github/workflows/ci.yml`.

- [ ] **Step 4: Run GREEN**

```bash
python -m pytest backend/tests/test_mcp_docker_build_context.py -q
bash -n scripts/verify_mcp_profile_management_omv.sh
bash -n scripts/deploy_mcp_profile_management_omv.sh
```

Expected: tests pass and both scripts parse successfully.

- [ ] **Step 5: Commit**

```bash
git add docs/content/docs/overview/mcp-server.mdx scripts backend/tests/test_mcp_docker_build_context.py
git commit -m "docs: add MCP Story verification workflow"
```

---

### Task 10: Full verification, review, and rollout gate

**Files:**
- No production changes unless verification or review finds a concrete defect.

- [ ] **Step 1: Run focused backend suite**

```bash
python -m pytest \
  backend/tests/test_story_orchestration_models.py \
  backend/tests/test_story_profile_resolution.py \
  backend/tests/test_story_rendering.py \
  backend/tests/test_story_orchestration.py \
  backend/tests/test_mcp_story_tools.py \
  backend/tests/test_story_http_compatibility.py \
  backend/tests/test_story_restart_recovery.py \
  backend/tests/test_mcp_profile_tools.py \
  backend/tests/test_mcp_profile_tools_regressions.py \
  backend/tests/test_mcp_http_routing.py \
  backend/tests/test_mcp_docker_build_context.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run compilation and frontend regression**

```bash
python -m compileall -q backend
bun run typecheck
bun run build:web
```

Expected: all commands exit 0.

- [ ] **Step 3: Run isolated OMV verification**

```bash
cd /tmp/voicebox-mcp-story
bash scripts/verify_mcp_profile_management_omv.sh
```

Expected: Docker build succeeds, focused tests pass, exact `/mcp` initialize returns non-HTML 2xx, OAuth discovery remains JSON 404, and all 12 tools are listed.

- [ ] **Step 4: Run real-model OMV smoke test**

```bash
VERIFY_STORY_PROFILE_A='<ready-profile-A>' \
VERIFY_STORY_PROFILE_B='<ready-profile-B>' \
bash scripts/deploy_mcp_profile_management_omv.sh
```

Expected: production remains healthy; two-profile Story completes; final WAV endpoint returns valid audio.

- [ ] **Step 5: Request code review**

Review against:

- `docs/superpowers/specs/2026-07-20-mcp-story-mode-design.md`
- `docs/superpowers/specs/2026-07-20-mcp-story-mode-compatibility-addendum.md`
- this plan

Fix Critical and Important findings, rerun affected tests, then rerun the focused full suite.

- [ ] **Step 6: Verification-before-completion evidence**

Record exact command output, test counts, Docker image ID, health result, tool list, created Story ID, segment statuses, and WAV response checks. Do not claim complete without this evidence.

- [ ] **Step 7: Keep rollout isolated**

Keep OMV pointed at:

```yaml
build:
  context: https://github.com/carlolerro/voicebox-omv.git#feature/mcp-story-mode
```

Do not move to `main` until explicit production verification and user approval. Do not run GitHub Actions.
