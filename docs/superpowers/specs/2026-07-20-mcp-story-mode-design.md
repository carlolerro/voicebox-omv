# Asynchronous MCP Story Mode — Design

**Date:** 2026-07-20  
**Repository:** `carlolerro/voicebox-omv`  
**Branch:** `feature/mcp-story-mode`  
**Status:** Approved design, pending implementation plan

## 1. Goal

Expose Voicebox Story creation through MCP so an agent can submit an ordered script containing multiple voice profiles and receive one final mixed WAV file.

The first release must:

- accept an ordered list of segments containing only `profile` and `text`;
- resolve language, engine, and personality from each profile;
- start work asynchronously and return immediately with a `story_id`;
- generate one normal Voicebox `Generation` per segment using the existing serial TTS queue;
- stop at the first failed segment;
- preserve all previously completed generations and Story items;
- allow an explicit resume from the failed segment;
- render the final WAV automatically after the last segment completes;
- persist the final WAV and expose it through an HTTP download URL;
- remain compatible with the existing Story UI, REST endpoints, generation history, and audio-version model.

## 2. Non-goals for the first release

The first release will not include:

- existing `generation_id` values as segment input;
- imported or Base64 audio segments;
- per-segment overrides for language, engine, personality, seed, instruction, effects, track, trim, volume, or timing;
- parallel TTS inference;
- Story cancellation through MCP;
- Base64 delivery of the final WAV;
- automatic continuation past a failed segment;
- deletion of completed work on failure;
- a new frontend workflow;
- a separate worker container or external job system;
- GitHub Actions execution or workflow-trigger changes.

## 3. Existing backend capabilities

Voicebox already provides the core primitives:

- `Story` stores name, description, and timestamps.
- `StoryItem` links a completed `Generation` to a Story with timecode, track, trim, volume, and optional pinned version.
- `/generate` creates a persistent Generation and enqueues TTS asynchronously.
- the generation queue serializes inference to one job at a time.
- Story services support item insertion, reordering, editing, and audio mixing.
- `/stories/{story_id}/export-audio` exports a mixed WAV.

The missing component is a persistent orchestration layer that represents segments before they have a Generation, coordinates the existing queue, records progress, stops safely on failure, resumes idempotently, and persists the final mix.

## 4. Chosen architecture

Add a persistent Story orchestration service above the existing generation and Story services.

The architecture will have four layers:

1. **MCP tools** validate tool-level input and normalize output.
2. **Story orchestration service** owns workflow state and sequential progression.
3. **Existing generation queue** performs all TTS inference, still one job at a time.
4. **Existing Story mixer** assembles completed Story items and produces the final WAV.

The MCP layer must not call Voicebox over HTTP. It will call Python service functions directly, following the existing MCP implementation pattern.

### 4.1 Main components

Proposed focused modules:

- `backend/mcp_server/story_tools.py`
  - MCP schemas and tool registration;
  - profile/title/text validation;
  - normalized tool responses;
  - database-session lifecycle.

- `backend/services/story_orchestration.py`
  - Story workflow creation;
  - background task registration;
  - segment progression;
  - generation completion waiting;
  - stop-on-error and resume behavior;
  - restart recovery;
  - final render transition.

- existing `backend/services/stories.py`
  - remains responsible for Story timeline operations and mixing;
  - gains a reusable persistent-render function and render invalidation helpers.

- existing generation route/service boundary
  - the reusable logic currently embedded in `routes/generations.py::generate_speech` will be extracted into a service-level enqueue function;
  - REST `/generate`, MCP `voicebox.speak`, and Story orchestration will use that same function;
  - this avoids duplicating profile validation, engine resolution, personality rewriting, history creation, effects resolution, queue submission, and task-manager registration.

This is a targeted refactor only. TTS inference remains in `services/generation.py`, and queue mechanics remain in `services/task_queue.py`.

## 5. Data model

### 5.1 Story extensions

Extend the existing `stories` table with idempotent startup migrations:

| Column | Type | Default | Purpose |
|---|---|---:|---|
| `status` | VARCHAR | `draft` | Workflow state |
| `error` | TEXT nullable | null | Last workflow/render error |
| `render_audio_path` | VARCHAR nullable | null | Storage-relative path to the persistent final WAV |
| `rendered_at` | DATETIME nullable | null | Time of the last successful persistent render |
| `total_segments` | INTEGER | `0` | Number of orchestration segments |
| `completed_segments` | INTEGER | `0` | Number successfully attached to the timeline |
| `current_segment_index` | INTEGER nullable | null | One-based segment currently being processed |
| `failed_segment_index` | INTEGER nullable | null | One-based segment that stopped the workflow |

Allowed `status` values:

- `draft`: ordinary UI/REST Story or a Story manually changed after rendering;
- `queued`: workflow accepted but not yet processing a segment;
- `generating`: one segment is queued/running or being attached;
- `rendering`: all segments completed and the final mix is being written;
- `completed`: final persistent WAV is available;
- `failed`: generation or render stopped the workflow and it may be resumable.

No database-level enum is required because the project uses SQLite and string statuses elsewhere. Service validation will enforce the values.

### 5.2 New StorySegment table

Add `story_segments`:

| Column | Type | Constraints | Purpose |
|---|---|---|---|
| `id` | VARCHAR | primary key UUID | Segment identity |
| `story_id` | VARCHAR | FK `stories.id`, not null | Parent Story |
| `position` | INTEGER | not null | One-based script order |
| `profile_id` | VARCHAR | FK `profiles.id`, not null | Voice profile chosen at creation |
| `text` | TEXT | not null | Submitted text before optional personality rewrite |
| `generation_id` | VARCHAR nullable | FK `generations.id` | Persistent Generation created for the segment |
| `status` | VARCHAR | default `pending` | Segment workflow state |
| `error` | TEXT nullable | null | Segment-specific error |
| `created_at` | DATETIME | not null | Creation time |
| `updated_at` | DATETIME | not null | Last transition time |

Add a unique constraint on `(story_id, position)`.

Allowed segment statuses:

- `pending`;
- `generating`;
- `completed`;
- `failed`.

A completed segment must have a completed Generation and an associated StoryItem. A failed segment may retain a failed Generation so resume can retry the same logical generation instead of creating duplicates.

### 5.3 Generation source

Story-created generations use:

```text
source = "mcp_story"
```

No schema change is needed because `Generation.source` is already a string.

## 6. MCP contract

Register four new tools:

```text
voicebox.create_story
voicebox.get_story_status
voicebox.get_story
voicebox.resume_story
```

The FastMCP server instructions will mention Story creation, status polling, resume, and the final download URL.

### 6.1 `voicebox.create_story`

Input:

```json
{
  "title": "Capitolo 17 - Come lavora Spark",
  "description": "Dialogo introduttivo",
  "segments": [
    {
      "profile": "serena",
      "text": "Benvenuti..."
    },
    {
      "profile": "ryan",
      "text": "Partiamo dall'inizio..."
    }
  ]
}
```

Validation:

- `title`: 1–100 characters after trimming;
- `description`: optional, maximum 500 characters;
- `segments`: 1–100 entries;
- each `text`: 1–10,000 characters after trimming;
- combined segment text: maximum 100,000 characters;
- `profile`: non-empty profile name or exact profile ID;
- profile-name lookup is case-insensitive and must resolve unambiguously;
- all profiles must exist and be ready for generation before any Story row is created;
- only `preset` and ready `cloned` profiles are accepted;
- legacy or unsupported profile types are rejected;
- duplicate profiles across different segments are allowed;
- duplicate text is allowed.

Profile-derived settings for every segment:

- `language = profile.language`;
- `engine = profile.default_engine`, then `profile.preset_engine`, then the existing backend fallback;
- personality rewriting is enabled when `profile.personality` is non-empty;
- all other generation parameters use existing Voicebox defaults.

Creation is transactional: validation completes first, then the Story and all StorySegment rows are committed together. A background orchestration task is started only after a successful commit.

Immediate response:

```json
{
  "story_id": "uuid",
  "title": "Capitolo 17 - Come lavora Spark",
  "status": "queued",
  "total_segments": 2,
  "completed_segments": 0,
  "current_segment": null,
  "failed_segment": null,
  "resumable": false,
  "download_url": null,
  "status_tool": "voicebox.get_story_status"
}
```

### 6.2 `voicebox.get_story_status`

Input:

```json
{
  "story_id": "uuid"
}
```

Story lookup is by ID only because Story titles are not unique.

Response while generating:

```json
{
  "story_id": "uuid",
  "title": "Capitolo 17 - Come lavora Spark",
  "status": "generating",
  "total_segments": 8,
  "completed_segments": 3,
  "current_segment": 4,
  "failed_segment": null,
  "error": null,
  "resumable": false,
  "download_url": null
}
```

Response after failure:

```json
{
  "story_id": "uuid",
  "title": "Capitolo 17 - Come lavora Spark",
  "status": "failed",
  "total_segments": 8,
  "completed_segments": 3,
  "current_segment": null,
  "failed_segment": 4,
  "error": "The selected model is not downloaded",
  "resumable": true,
  "download_url": null
}
```

Response after completion:

```json
{
  "story_id": "uuid",
  "title": "Capitolo 17 - Come lavora Spark",
  "status": "completed",
  "total_segments": 8,
  "completed_segments": 8,
  "current_segment": null,
  "failed_segment": null,
  "error": null,
  "resumable": false,
  "download_url": "/stories/uuid/export-audio"
}
```

`download_url` is a same-origin HTTP path. It is returned only when a valid persistent render exists.

### 6.3 `voicebox.get_story`

Input:

```json
{
  "story_id": "uuid"
}
```

Response includes the normalized Story status plus ordered segment details:

```json
{
  "story_id": "uuid",
  "title": "Capitolo 17 - Come lavora Spark",
  "description": "Dialogo introduttivo",
  "status": "failed",
  "total_segments": 2,
  "completed_segments": 1,
  "segments": [
    {
      "position": 1,
      "profile_id": "uuid",
      "profile": "serena",
      "text": "Benvenuti...",
      "status": "completed",
      "generation_id": "uuid",
      "duration": 2.4,
      "error": null
    },
    {
      "position": 2,
      "profile_id": "uuid",
      "profile": "ryan",
      "text": "Partiamo dall'inizio...",
      "status": "failed",
      "generation_id": "uuid",
      "duration": null,
      "error": "The selected model is not downloaded"
    }
  ],
  "download_url": null
}
```

The response will not expose local filesystem paths.

### 6.4 `voicebox.resume_story`

Input:

```json
{
  "story_id": "uuid"
}
```

Rules:

- only a Story in `failed` state with at least one incomplete segment can resume;
- all remaining profiles are revalidated before changing the Story state;
- a missing or no-longer-ready profile rejects the call without discarding completed work;
- concurrent resume attempts for the same Story are rejected;
- a failed existing Generation is retried using the same logical Generation when possible;
- a segment with no Generation receives a new Generation;
- a completed Generation missing its StoryItem is attached idempotently rather than regenerated;
- completed segments are never regenerated;
- the Story returns to `queued`, clears Story-level error fields, and starts a new background orchestration task;
- the tool returns immediately with the same normalized status shape as `create_story`.

## 7. Workflow and state transitions

### 7.1 Normal execution

```text
Story queued
  -> Story generating / segment 1 generating
  -> segment 1 completed + StoryItem attached
  -> Story completed_segments incremented
  -> next pending segment
  -> ...
  -> Story rendering
  -> persistent WAV written atomically
  -> Story completed
```

For each segment:

1. Open a short-lived database session.
2. Re-read Story and segment state.
3. Resolve the current profile by stored `profile_id`.
4. Enqueue a normal Generation through the shared generation service.
5. Persist `generation_id` and segment `generating` state.
6. Wait asynchronously for the Generation to reach `completed` or `failed`.
7. On completion, call existing Story insertion logic with track `0` and no explicit start time.
8. Existing insertion behavior places the clip after the previous clip with a fixed 200 ms gap.
9. Mark the segment completed and update Story progress in one transaction.
10. Continue to the next segment.

Database sessions must not remain open while TTS inference runs. The waiter re-reads status with short-lived sessions to avoid stale ORM state and long SQLite write locks.

### 7.2 Failure

At the first failed Generation or orchestration error:

- copy the error to the segment;
- set segment status to `failed`;
- set Story status to `failed`;
- set `failed_segment_index`;
- clear `current_segment_index`;
- preserve all completed Generations, versions, audio files, StoryItems, and segment records;
- do not process later segments;
- do not render a partial final file;
- leave `render_audio_path` null or remove any stale render reference.

No automatic retry is performed.

### 7.3 Rendering failure

If all segments complete but rendering fails:

- keep every segment completed;
- set Story status to `failed`;
- leave `failed_segment_index` null;
- record the render error;
- `resume_story` skips generation work and retries rendering only.

### 7.4 Restart recovery

At application startup, after the existing stale-Generation cleanup:

- Stories in `queued`, `generating`, or `rendering` are set to `failed`;
- the Story error becomes `Server was shut down during Story processing`;
- a currently active segment becomes `failed` unless its Generation is already completed;
- completed segments remain completed;
- `completed_segments` is recomputed from persisted segment state;
- no Story is resumed automatically;
- the user or MCP client must call `voicebox.resume_story`.

This makes recovery explicit and prevents duplicate generations after container restarts.

## 8. Concurrency and idempotency

Voicebox currently runs a single application process and serial TTS queue. Story orchestration will preserve that model.

Use an in-process registry of active `story_id` values to prevent duplicate orchestration tasks in the same process. Persistent status validation remains authoritative across restarts.

Idempotency requirements:

- Story/segment creation occurs once in a transaction;
- a segment may reference only one logical Generation;
- adding a Generation to a Story remains idempotent through existing `add_item_to_story` behavior;
- resuming after `Generation completed` but before StoryItem creation attaches the existing Generation;
- resuming after StoryItem creation but before segment completion detects the existing item and marks the segment completed;
- final render writes to a temporary file in the Story directory and uses atomic replacement for `story.wav`;
- `completed_segments` is derived/reconciled from segment states rather than trusted blindly after recovery.

No parallel segment generation will be added. Multiple Story workflows may be queued as background orchestrators, but every actual TTS Generation still passes through the global serial queue.

## 9. Persistent render and HTTP download

Create a Story storage directory through config helpers:

```text
/app/data/stories/{story_id}/story.wav
```

Persist `render_audio_path` using Voicebox storage-relative path helpers, never an absolute host path.

Refactor the current Story export implementation into reusable mixing and output functions:

- a mixer that resolves Generation versions, loads audio, applies trim and per-clip volume, places clips on the timeline, mixes overlaps, and normalizes clipping;
- a persistent renderer that writes the mixed audio to a temporary WAV and atomically replaces `story.wav`;
- the existing byte-export compatibility path for legacy/UI Stories.

`GET /stories/{story_id}/export-audio` behavior:

1. If `render_audio_path` points to an existing valid file, return it with `FileResponse`.
2. Otherwise preserve existing behavior by mixing the current timeline on demand and streaming a WAV.
3. Use a sanitized Story title for the download filename.
4. Return `404` when the Story does not exist.
5. Return `400` when the Story has no renderable audio items.

This preserves existing REST/UI behavior while making MCP-generated completed Stories efficient and downloadable.

## 10. Interaction with existing Story mutations

An active MCP workflow must not be manually changed underneath the orchestrator.

For Stories in `queued`, `generating`, or `rendering`:

- Story item add/remove/reorder/move/trim/split/duplicate/version/volume operations return HTTP `409`;
- Story deletion returns HTTP `409`;
- Story title/description updates may also return `409` for a consistent first release.

For terminal or draft Stories, existing operations remain available.

Any successful timeline mutation on a Story with a persistent render must:

- clear `render_audio_path` and `rendered_at`;
- remove the obsolete persisted file best-effort;
- set status to `draft` unless the Story is currently `failed` for an unresolved orchestration error.

The existing on-demand export route still allows the edited draft to be downloaded.

## 11. Profile and generation semantics

Before Story creation, all profiles are validated up front. Before each segment and resume, the profile is checked again because profiles can be edited or deleted after the initial request.

A profile is accepted when:

- preset profile: valid preset engine and voice ID, immediately ready;
- cloned profile: supported cloning engine and at least one valid sample;
- designed/legacy/unsupported profile: rejected for this release.

The Story orchestrator uses the same engine validation and model-cache errors as normal `/generate` and `voicebox.speak`.

Personality behavior is deterministic:

- when the profile has a non-empty personality prompt, Story generation requests personality rewriting;
- otherwise text is sent directly to TTS;
- the original submitted segment text remains in `story_segments.text`;
- the Generation row stores the actual text sent to TTS after rewriting, matching existing Generation behavior.

## 12. Error contract

Tool validation errors raise a clear MCP tool error before any Story is created.

Examples:

- ambiguous/missing profile;
- profile not ready;
- unsupported profile type;
- empty title or segment text;
- segment or total-size limit exceeded;
- Story not found;
- Story not resumable;
- another workflow already active for the Story.

Runtime TTS and render failures are persisted in Story/segment status and returned by status tools. They do not keep the original MCP call open.

Error messages must not expose host filesystem paths, stack traces, secrets, or raw internal exception representations containing sensitive data.

## 13. Security and resource limits

- No arbitrary local file paths are accepted by Story tools.
- No Base64 audio is accepted.
- No direct SQL or storage path is accepted.
- Text limits are enforced before database writes.
- The existing global serial queue prevents concurrent model inference and resource contention.
- Final audio is served only through a Story-ID route that verifies the Story and its stored path.
- Storage paths are resolved through existing safe config helpers.
- File writes use a Story UUID directory and fixed filename to avoid path traversal.

The deployment retains the current no-auth local/tunnel MCP model and `X-Voicebox-Client-Id` behavior. This feature does not redefine authentication.

## 14. Registration and documentation

`backend/mcp_server/server.py` will register `register_story_tools(mcp)` after existing base/profile tools.

Update:

- FastMCP server instructions;
- MCP documentation page;
- tool examples for create, poll, failure, resume, and download;
- tool count in OMV verification scripts;
- production verification expectations.

No GitHub Actions workflow will be added or automatically enabled. Verification remains local and on the OMV host.

## 15. Testing strategy

Implementation follows test-driven development.

### 15.1 Model and migration tests

- fresh database creates Story orchestration columns and `story_segments`;
- upgrade migration is idempotent;
- existing Story rows receive `draft` and zero progress defaults;
- unique `(story_id, position)` is enforced;
- migration preserves existing Story and StoryItem data.

### 15.2 MCP contract tests

- all four Story tools are registered;
- create accepts valid `profile + text` segments;
- all profiles are validated before persistence;
- profile names resolve case-insensitively and ambiguities fail;
- input limits are enforced;
- no local paths appear in responses;
- completed status returns the expected same-origin download URL;
- get and status require Story ID;
- resume rejects non-failed and already-active Stories.

### 15.3 Orchestration tests

Use a controlled fake generation runner; unit tests must not require GPU/model downloads.

- normal two-profile Story progresses in order;
- each segment creates one persistent Generation with source `mcp_story`;
- language, engine, and personality derive from the profile;
- StoryItems receive sequential timecodes with 200 ms gaps;
- first generation failure stops later segments;
- completed work survives failure;
- resume retries the failed segment and does not regenerate completed segments;
- completed Generation without StoryItem is attached on resume;
- existing StoryItem with incomplete segment state is reconciled;
- render failure leaves all segments completed and resume retries only render;
- duplicate resume is rejected;
- restart recovery marks active workflows failed and resumable;
- database sessions are closed on success and failure.

### 15.4 Mixer/download tests

- persistent render contains the ordered clips;
- trim, volume, pinned versions, overlaps, and normalization retain existing behavior;
- render uses atomic replacement;
- persisted render is served with `FileResponse`;
- legacy/draft Story falls back to on-demand export;
- missing Story returns `404`;
- Story with no audio returns `400`;
- manual timeline mutation invalidates a stale persistent render.

### 15.5 HTTP and lifecycle tests

- active Story mutations and deletion return `409`;
- startup recovery runs after stale Generation cleanup;
- exact `/mcp` routing remains functional;
- OAuth discovery behavior remains non-HTML JSON `404`;
- existing eight MCP tools remain unchanged.

### 15.6 OMV verification

Extend the existing OMV verification script to:

- build the current Dockerfile locally;
- run all non-GPU MCP Story tests;
- discover the original eight tools plus the four new Story tools;
- create a short Story using a controlled test path or available lightweight preset;
- poll until terminal state;
- verify ordered segment records;
- verify the final WAV route returns `200` and `audio/wav`;
- exercise a forced failure and resume scenario without deleting completed work.

Production deployment must retain the existing persistent `/app/data` volume and rollback process.

## 16. Compatibility and rollout

- Existing Stories become `draft`; no existing timeline data is changed.
- Existing Story REST routes continue to work.
- Existing `/stories/{id}/export-audio` remains compatible.
- Existing `voicebox.speak` remains the single-clip generation tool.
- Existing generation history and versions remain the source of segment audio.
- Existing global generation serialization remains unchanged.
- No frontend changes are required for MCP Story creation; generated Stories appear through existing Story queries and timeline data.
- The feature is developed and verified on `feature/mcp-story-mode` before any update to `main`.
- OMV production deployment uses the feature branch until explicit production verification and approval.

## 17. Acceptance criteria

The feature is accepted when all of the following are demonstrated with fresh evidence:

1. `voicebox.create_story` returns within the MCP request window with `status=queued`.
2. A Story with at least two different profiles generates segments serially in input order.
3. Each segment is visible as a normal Generation and StoryItem.
4. `voicebox.get_story_status` reports accurate progress throughout the workflow.
5. The first failed segment stops processing and leaves prior segments intact.
6. `voicebox.resume_story` continues from the failed segment without regenerating completed work.
7. Completion automatically creates a persistent mixed WAV.
8. Status returns `/stories/{story_id}/export-audio` only when that WAV exists.
9. The download route serves the final WAV successfully.
10. Restart recovery leaves interrupted work in an explicit failed/resumable state.
11. Existing Story UI/REST behavior and the original eight MCP tools pass regression tests.
12. The Docker image builds and the full non-GPU verification suite passes on OMV without GitHub Actions.
