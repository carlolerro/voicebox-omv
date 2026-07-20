# MCP Story Mode — Existing Backend Compatibility Addendum

**Date:** 2026-07-20  
**Repository:** `carlolerro/voicebox-omv`  
**Branch:** `feature/mcp-story-mode`  
**Status:** Approved clarification to `2026-07-20-mcp-story-mode-design.md`

## Purpose

The existing Voicebox Story domain is the implementation foundation. MCP Story Mode must orchestrate existing Story, StoryItem, Generation, queue, retry, mixer, versions, and export behavior rather than create a parallel Story implementation.

## Required reuse

The implementation must call or extract reusable service boundaries around these existing capabilities:

- `backend/services/stories.py::create_story`
- `backend/services/stories.py::get_story`
- `backend/services/stories.py::add_item_to_story`
- `backend/services/stories.py::export_story_audio`
- generation creation/enqueue behavior currently used by `backend/routes/generations.py::generate_speech`
- failed-generation retry behavior currently used by `backend/routes/generations.py::retry_generation`
- `backend/services/task_queue.py` global serial generation queue
- existing `Generation`, `Story`, and `StoryItem` ORM records
- existing `/stories/{story_id}/export-audio` route

No second Story model, queue, mixer, generation history, or download route may be introduced.

## Compatibility rule 1: strict MCP render

The existing Story mixer is permissive: unreadable or missing clip files are skipped. This behavior must remain available for legacy/manual UI exports.

Persistent MCP Story rendering must use the same mixing implementation in strict mode:

- every expected StoryItem must resolve to a completed Generation;
- every selected generation/version audio path must exist and load successfully;
- missing or unreadable audio fails the render;
- a strict render failure sets the Story to `failed` and remains resumable;
- the workflow must never report `completed` with a silently omitted segment.

The mixer must be refactored into one shared implementation parameterized by strict/permissive behavior. Audio placement, trim, volume, pinned version, overlap mixing, sample-rate handling, and clipping normalization remain shared.

## Compatibility rule 2: shared engine resolution

`GenerationRequest.engine` currently has a Pydantic default of `qwen`. Constructing a request without explicitly neutralizing this default can override profile-derived engine selection.

Story orchestration must use the same engine-resolution policy as normal generation:

```text
explicit engine
or profile.default_engine
or profile.preset_engine
or qwen
```

Because V1 Story segments do not expose an engine override, the effective policy is:

```text
profile.default_engine
or profile.preset_engine
or qwen
```

The policy must exist in one reusable generation service function. REST `/generate`, MCP `voicebox.speak`, and MCP Story orchestration must not maintain separate copies.

## Compatibility rule 3: unambiguous profile resolution

Existing agent-facing profile management prevents new case-insensitive duplicate names, but legacy database rows may still contain names that differ only by case.

Story profile resolution must follow:

1. exact profile ID match;
2. otherwise case-insensitive name lookup;
3. zero matches: not-found error;
4. exactly one match: use it;
5. more than one match: ambiguity error listing no internal identifiers or sensitive data.

The resolver must not use `.first()` for case-insensitive Story lookup.

## REST and frontend compatibility

Existing REST route paths and current response fields remain valid. Additional Story workflow fields may be returned because the TypeScript client consumes JSON structurally and does not reject unknown fields.

The implementation must preserve:

- existing Story create/list/get/update/delete route paths;
- existing StoryItem route paths and request bodies;
- current Story export URL;
- existing UI-created Stories as `draft`;
- existing on-demand permissive export for draft/manual Stories;
- editing of terminal Stories;
- HTTP `409` only while a Story is `queued`, `generating`, or `rendering`.

## Acceptance implications

Tests must prove:

- Story MCP code calls shared Story/generation services rather than HTTP;
- no duplicate Story domain or generation queue is added;
- strict render fails when one required clip is missing;
- permissive legacy export still succeeds with remaining valid clips;
- preset profiles use their preset engine, not the accidental `GenerationRequest` default;
- cloned profiles use `default_engine` when set;
- legacy case-insensitive duplicate profile names produce an ambiguity error;
- existing Story API client contracts remain usable.
