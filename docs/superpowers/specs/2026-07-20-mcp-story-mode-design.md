# Asynchronous MCP Story Mode — Design

**Date:** 2026-07-20  
**Repository:** `carlolerro/voicebox-omv`  
**Branch:** `feature/mcp-story-mode`  
**Status:** Approved design, pending user review and implementation plan

## 1. Goal

Expose Voicebox Story creation through MCP so an agent can submit an ordered script containing multiple voice profiles and receive one final mixed WAV file.

The first release must:

- accept ordered segments containing only `profile` and `text`;
- derive language, engine, and personality behavior from the selected profile;
- return immediately with a `story_id` and execute asynchronously;
- create one normal persistent Voicebox `Generation` per segment through the existing serial TTS queue;
- stop at the first failed segment;
- retain completed Generations, StoryItems, and audio;
- resume explicitly from the failed segment without regenerating completed work;
- render automatically after the final segment;
- persist the final WAV and expose a same-origin HTTP download URL;
- preserve existing Story UI, REST, history, versions, and single-clip MCP behavior.

## 2. Approved product decisions

The user approved these choices:

1. Story processing is asynchronous.
2. The workflow stops at the first segment error.
3. Completed work is retained and the Story is resumable.
4. V1 segment input is only `profile + text`.
5. Final rendering happens automatically.
6. Language, engine, and personality behavior are inherited from the profile.
7. The final WAV is delivered through an HTTP download URL, not Base64.

## 3. Non-goals

V1 does not include:

- existing `generation_id` input;
- uploaded, imported, local-path, or Base64 audio segments;
- segment-level overrides for language, engine, personality, seed, instruct, effects, track, trim, volume, or timing;
- parallel TTS inference;
- Story cancellation through MCP;
- automatic continuation after failure;
- deletion of completed work on failure;
- a new frontend workflow;
- an external job system or worker container;
- GitHub Actions execution or workflow changes.

## 4. Existing backend findings

Voicebox already has the required low-level primitives:

- `Story` stores Story metadata.
- `StoryItem` links a `Generation` to a Story with start time, track, trim, volume, and optional pinned version.
- `/generate` creates a persistent Generation and enqueues asynchronous TTS.
- `services.task_queue` serializes TTS inference to one job at a time.
- Story services support item insertion, timeline editing, and audio mixing.
- `/stories/{story_id}/export-audio` produces a mixed WAV.

The missing component is a persistent orchestration layer representing segments before Generation creation and coordinating progress, failure, resume, and persistent final rendering.

## 5. Chosen architecture

Add a persistent Story orchestration service above the existing generation and Story services.

Layers:

1. **MCP Story tools** validate input and normalize output.
2. **Story orchestration service** owns workflow state and sequential progression.
3. **Existing generation service and queue** perform all TTS work.
4. **Existing Story timeline and mixer** assemble completed clips.

MCP tools call Python services directly; they do not call the Voicebox HTTP API.

### 5.1 Components

Create:

- `backend/mcp_server/story_tools.py`
  - four MCP tools;
  - MCP input validation;
  - normalized responses;
  - short-lived database-session ownership.

- `backend/services/story_orchestration.py`
  - transactional workflow creation;
  - background-task registry;
  - ordered segment processing;
  - Generation terminal-state waiting;
  - stop-on-error;
  - resume and reconciliation;
  - restart recovery;
  - automatic final render.

Modify focused existing boundaries:

- `backend/services/stories.py`
  - reusable mixer;
  - persistent render;
  - render invalidation;
  - active-workflow mutation guard.

- generation request boundary
  - extract the reusable enqueue logic currently embedded in `routes/generations.py::generate_speech` into a service function;
  - REST `/generate`, `voicebox.speak`, and Story orchestration use the same function;
  - retain TTS inference in `services/generation.py` and queue mechanics in `services/task_queue.py`.

This refactor prevents duplication of engine resolution, profile validation, personality rewrite, history creation, effects resolution, task registration, and queue submission.

## 6. Data model

### 6.1 Story extensions

Add these columns to `stories` through idempotent startup migrations:

| Column | Type | Default | Meaning |
|---|---|---:|---|
| `status` | VARCHAR | `draft` | Story workflow state |
| `error` | TEXT nullable | null | Last workflow or render error |
| `render_audio_path` | VARCHAR nullable | null | Storage-relative final WAV path |
| `rendered_at` | DATETIME nullable | null | Last successful persistent render |
| `total_segments` | INTEGER | `0` | Number of orchestration segments |
| `completed_segments` | INTEGER | `0` | Number of completed/attached segments |
| `current_segment_index` | INTEGER nullable | null | One-based active segment |
| `failed_segment_index` | INTEGER nullable | null | One-based segment that failed |

Allowed Story statuses:

- `draft`: normal UI/REST Story or manually edited Story;
- `queued`: accepted and waiting for orchestration;
- `generating`: a segment is being prepared, queued, generated, or attached;
- `rendering`: all segments are complete and final WAV creation is active;
- `completed`: persistent final WAV exists;
- `failed`: generation, orchestration, shutdown, or render failure.

String validation remains in services; no database enum is introduced.

### 6.2 New `story_segments` table

| Column | Type | Constraint |
|---|---|---|
| `id` | VARCHAR | primary-key UUID |
| `story_id` | VARCHAR | FK `stories.id`, not null |
| `position` | INTEGER | one-based, not null |
| `profile_id` | VARCHAR | FK `profiles.id`, not null |
| `text` | TEXT | original submitted text, not null |
| `generation_id` | VARCHAR nullable | FK `generations.id` |
| `status` | VARCHAR | default `pending` |
| `error` | TEXT nullable | segment error |
| `created_at` | DATETIME | not null |
| `updated_at` | DATETIME | not null |

Constraints/indexes:

- unique `(story_id, position)`;
- index `(story_id, status)`;
- segment status is one of `pending`, `generating`, `completed`, `failed`.

A completed segment must resolve to a completed Generation and an existing StoryItem. A failed segment may retain its failed Generation.

### 6.3 Generation source

Story-created Generations use:

```text
source = "mcp_story"
```

No Generation schema change is required.

### 6.4 Deletion semantics

Deleting a terminal/draft Story:

- deletes its `story_segments` and `story_items`;
- deletes its persisted final Story WAV and directory best-effort;
- preserves underlying Generation history, versions, and generation audio, matching current Story deletion semantics.

Deleting a Story in `queued`, `generating`, or `rendering` is rejected with HTTP `409`.

## 7. MCP contract

Register:

```text
voicebox.create_story
voicebox.get_story_status
voicebox.get_story
voicebox.resume_story
```

Story lookup is by ID only because titles are not unique.

### 7.1 `voicebox.create_story`

Input:

```json
{
  "title": "Capitolo 17 - Come lavora Spark",
  "description": "Dialogo introduttivo",
  "segments": [
    {"profile": "serena", "text": "Benvenuti..."},
    {"profile": "ryan", "text": "Partiamo dall'inizio..."}
  ]
}
```

`title` maps to the existing `Story.name` field.

Validation before persistence:

- title: 1–100 trimmed characters;
- description: optional, maximum 500 characters;
- segments: 1–100;
- each text: 1–10,000 trimmed characters;
- total submitted text: maximum 100,000 characters;
- profile: exact ID or case-insensitive unambiguous name;
- every profile exists and is generation-ready;
- accepted types: valid preset or ready cloned profile;
- rejected types: designed, legacy unsupported, invalid preset, or cloned without samples;
- duplicate profiles and duplicate text are permitted.

Profile-derived generation settings:

- language is `profile.language`;
- engine resolution is `profile.default_engine`, then `profile.preset_engine`, then the current backend fallback;
- personality rewrite is enabled exactly when `profile.personality` is non-empty;
- other parameters use current Voicebox generation defaults.

All profiles are validated first. Story and StorySegment rows are then created in one transaction. The background task starts only after commit.

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
  "error": null,
  "resumable": false,
  "download_url": null,
  "status_tool": "voicebox.get_story_status"
}
```

### 7.2 `voicebox.get_story_status`

Input:

```json
{"story_id": "uuid"}
```

Response shape:

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

Failure example:

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

Completion example:

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

`download_url` is a same-origin path and is present only when the persistent WAV exists.

`resumable` is true when the Story is failed and either an incomplete segment exists or all segments are completed but rendering failed. Current profile readiness is rechecked by `resume_story`.

### 7.3 `voicebox.get_story`

Input:

```json
{"story_id": "uuid"}
```

Return the normalized Story status plus ordered segments:

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

Do not expose local filesystem paths, stack traces, or internal exception representations.

### 7.4 `voicebox.resume_story`

Input:

```json
{"story_id": "uuid"}
```

Rules:

- only a failed, resumable Story is accepted;
- all remaining profiles are revalidated before changing state;
- concurrent orchestration/resume for the same Story is rejected;
- completed segments are never regenerated;
- when `generation_id` exists and that Generation is `failed`, reset and retry that same Generation ID through the shared retry service;
- when `generation_id` is absent or its row no longer exists, create one new Generation and store its ID;
- when the Generation is completed but StoryItem is absent, attach it idempotently;
- when StoryItem exists but segment state is not completed, reconcile the segment to completed;
- when all segments are completed and only rendering failed, retry rendering without TTS;
- set Story to `queued`, clear workflow error/current/failed fields as appropriate, commit, then start the background task;
- return immediately with the normalized status response.

A missing or no-longer-ready remaining profile rejects resume without changing completed work or Story state.

## 8. Workflow

### 8.1 Normal progression

```text
queued
  -> generating segment 1
  -> Generation completed
  -> StoryItem attached
  -> segment completed, progress incremented
  -> next segment
  -> rendering
  -> atomic persistent WAV write
  -> completed
```

Per segment:

1. Open a short-lived DB session and re-read Story/segment.
2. Revalidate stored profile ID.
3. Enqueue a normal Generation through the shared generation service.
4. Store `generation_id`; mark Story/segment generating.
5. Close the write session.
6. Wait asynchronously for Generation terminal state using repeated short-lived read sessions.
7. On completion, call existing Story insertion with track `0` and no explicit start time.
8. Existing insertion places the clip after prior audio with a fixed 200 ms gap.
9. Mark segment completed and update/reconcile Story progress in one transaction.
10. Continue.

No DB session remains open during inference or polling sleeps.

### 8.2 First-error stop

At the first Generation/orchestration failure:

- set the segment to `failed` and persist its sanitized error;
- set Story to `failed`;
- set `failed_segment_index`;
- clear `current_segment_index`;
- stop before any later segment;
- retain all completed Generation rows, versions, files, StoryItems, and segment rows;
- do not produce a partial final render;
- clear/remove stale render metadata if present;
- perform no automatic retry.

### 8.3 Render failure

When generation is complete but rendering fails:

- all segments stay completed;
- Story becomes failed;
- `failed_segment_index` remains null;
- Story error contains the sanitized render error;
- resume performs render only.

### 8.4 Restart recovery

During startup, after existing stale-Generation cleanup:

- Stories in `queued`, `generating`, or `rendering` become `failed`;
- error becomes `Server was shut down during Story processing`;
- an active segment becomes failed unless its Generation is completed;
- completed segments remain completed;
- `completed_segments` is recomputed from segment/StoryItem state;
- no automatic resume occurs.

Explicit resume prevents duplicate Generations after container restart.

## 9. Concurrency and idempotency

Use an in-process active-Story registry because the deployment is one Voicebox process. Persistent Story status remains authoritative after restart.

Required idempotency:

- Story and segments are created once transactionally;
- each segment owns at most one current logical Generation ID;
- existing `add_item_to_story` idempotency is preserved;
- completed Generation without StoryItem is attached, not regenerated;
- existing StoryItem with stale segment status is reconciled;
- `completed_segments` is recomputed when consistency is uncertain;
- persistent render writes a temporary file and atomically replaces `story.wav`;
- active-registry cleanup occurs in `finally` on success, failure, and cancellation.

Multiple Story orchestrators may wait concurrently, but every TTS job still enters the existing global serial queue. No parallel inference is introduced.

## 10. Persistent render and HTTP download

Use config helpers to store:

```text
/app/data/stories/{story_id}/story.wav
```

The database stores a storage-relative path.

Refactor current export into:

- reusable mix function: resolve pinned/default versions, load, trim, apply volume, place on timeline, sum overlaps, normalize clipping;
- persistent render function: write temporary WAV in the Story directory and atomically replace `story.wav`;
- existing on-demand byte export for legacy/draft Stories.

`GET /stories/{story_id}/export-audio`:

1. `404` when Story is absent.
2. Serve valid `render_audio_path` with `FileResponse` when present.
3. Otherwise preserve current on-demand mixing/streaming.
4. `400` when no renderable audio exists.
5. Use sanitized Story title as the download filename.

## 11. Existing Story mutation behavior

For `queued`, `generating`, or `rendering` Stories, every mutation returns HTTP `409`, including:

- title/description update;
- item add/remove/reorder/move/trim/split/duplicate/version/volume;
- Story deletion.

For draft/completed/failed Stories, existing operations remain available subject to normal validation.

A successful timeline mutation after a persistent render:

- clears `render_audio_path` and `rendered_at`;
- removes the stale rendered file best-effort;
- changes completed Story status to `draft`;
- retains failed status when an unresolved orchestration failure still exists.

On-demand export continues to work for the edited draft.

## 12. Profile semantics

Profiles are validated before Story creation, before each segment, and before resume.

Accepted:

- preset profile with valid preset engine and voice ID;
- cloned profile with supported cloning engine and at least one valid sample.

Rejected:

- designed profile;
- unsupported/legacy type;
- invalid preset;
- cloned profile without a sample.

Personality behavior:

- non-empty profile personality enables rewrite;
- empty personality sends submitted text directly to TTS;
- `story_segments.text` retains original text;
- Generation text stores the rewritten text actually sent to TTS, matching existing behavior.

## 13. Error and security contract

Tool validation errors occur before Story creation where applicable.

Runtime errors are persisted and observed through status tools; the original create/resume MCP call does not remain open.

Sanitized errors must not expose:

- host paths;
- stack traces;
- secrets;
- raw exception repr containing internal data.

Security/resource rules:

- no local paths, Base64, SQL, storage paths, or arbitrary files in Story input;
- limits enforced before writes;
- Story-ID download route verifies Story and safely resolves stored path;
- UUID directory plus fixed filename prevents path traversal;
- existing serial queue bounds inference concurrency;
- current local/tunnel no-auth and `X-Voicebox-Client-Id` behavior is unchanged.

## 14. Registration and documentation

`backend/mcp_server/server.py` registers `register_story_tools(mcp)` after existing tools.

Update:

- FastMCP instructions;
- MCP documentation;
- examples for create, polling, failure, resume, and download;
- OMV verification expected tool count from 8 to 12;
- production deployment verification documentation.

Do not add, enable, or modify GitHub Actions. Verification remains local/OMV.

## 15. Testing strategy

Implementation follows TDD.

### 15.1 Migration/model tests

- fresh DB contains new Story columns/table/indexes;
- migration is idempotent;
- existing Stories become draft with zero counters;
- existing Story/StoryItem data remains intact;
- unique `(story_id, position)` is enforced;
- terminal deletion removes segments/render but preserves Generations.

### 15.2 MCP tests

- four tools registered;
- valid create response is immediate and queued;
- all profiles validated before persistence;
- case-insensitive/ambiguous resolution;
- all limits;
- no local paths in output;
- exact status/get/resume shapes;
- URL only when persistent render exists;
- resume state/concurrency validation.

### 15.3 Orchestration tests

Use a deterministic fake generation runner that writes small synthetic WAVs; unit/integration tests require no GPU or model downloads.

- two-profile ordered success;
- one Generation per segment with source `mcp_story`;
- profile-derived language/engine/personality;
- 200 ms sequential gaps;
- first failure stops later segments;
- completed work survives;
- resume retries same failed Generation ID;
- missing Generation row creates exactly one replacement;
- completed Generation without item is attached;
- existing item/stale state is reconciled;
- render-only resume;
- duplicate resume rejected;
- startup recovery;
- active-registry cleanup;
- DB session closure.

### 15.4 Mixer/HTTP tests

- ordered mix;
- existing trim, volume, versions, overlap, and normalization behavior;
- atomic render replacement;
- persisted `FileResponse` path;
- legacy/draft fallback;
- `404`, `400`, and `409` behavior;
- mutation invalidates render.

### 15.5 Regression tests

- original eight MCP tools remain unchanged;
- exact `/mcp` POST remains `200`;
- OAuth discovery remains non-HTML JSON `404`;
- existing Story UI/REST tests pass;
- existing generation queue remains serial.

## 16. OMV verification

The OMV verification has two explicit layers.

### 16.1 Deterministic non-GPU verification

Inside the built isolated container:

- run all MCP Story unit/integration tests;
- integration test uses the fake generation runner and synthetic WAVs in the isolated data volume;
- verify create → poll → completed → HTTP WAV download;
- verify forced segment failure → stopped later work → resume → completion;
- discover all 12 MCP tools through the live FastMCP endpoint.

### 16.2 Real-model production smoke test

The production verification script accepts two required environment variables:

```text
VERIFY_STORY_PROFILE_A=<ready profile name or id>
VERIFY_STORY_PROFILE_B=<different ready profile name or id>
```

With both supplied, it:

1. creates a two-segment Story with short fixed Italian phrases;
2. polls `voicebox.get_story_status` until completed or failed;
3. fails verification on timeout or failed status;
4. verifies two completed segment records in input order;
5. verifies the two configured profile references;
6. downloads `/stories/{story_id}/export-audio`;
7. verifies HTTP `200`, `audio/wav`, and a non-empty valid WAV header.

Production acceptance requires this real-model smoke test. The deterministic forced-failure/resume test remains isolated and does not deliberately break production models.

Deployment retains `/app/data`, the transactional OMV deployment process, and rollback image.

## 17. Compatibility and rollout

- Existing Stories become draft without timeline changes.
- Existing REST Story routes and export remain compatible.
- Existing `voicebox.speak` remains the single-clip tool.
- Story segments remain normal Generation history entries.
- No frontend change is required; generated StoryItems are visible through current Story data.
- Development and verification occur on `feature/mcp-story-mode`.
- OMV uses the feature branch until production verification and explicit approval.
- Only after verification is the branch fast-forwarded/merged to `main`.

## 18. Acceptance criteria

Fresh evidence must demonstrate:

1. `create_story` returns immediately with queued status.
2. Two distinct ready profiles generate serially in input order.
3. Every segment becomes a normal Generation and StoryItem.
4. Status progress is accurate.
5. First failure stops later segments and preserves prior work.
6. Resume continues from failure without regenerating completed work.
7. Restart leaves an explicit failed/resumable state.
8. Completion automatically persists a mixed WAV.
9. Download URL appears only for an existing render.
10. HTTP download returns a valid WAV.
11. Active Story mutations return `409`.
12. Existing Story behavior and original eight MCP tools pass regression tests.
13. Docker build and deterministic non-GPU tests pass on OMV.
14. The real-model two-profile OMV smoke test passes.
15. No GitHub Actions are run or modified.
