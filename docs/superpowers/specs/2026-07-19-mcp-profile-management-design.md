# MCP Profile Management Design

## Status

Approved for implementation on `feature/mcp-profile-management`.

## Goal

Expose Voicebox voice-profile creation and sample management through MCP while continuing to use the existing `voicebox.speak` tool for audio generation.

The first version must support:

- preset profiles;
- cloned profiles;
- profile identity fields (`name`, `description`, `language`);
- profile behaviour through the existing `personality` field;
- adding cloned-voice samples either from Base64 audio or an existing Voicebox Capture;
- reading a complete profile after creation.

The feature must reuse the existing Voicebox services, database schema, storage layout, generation history, Dockerfile, and `/mcp` endpoint.

## Scope

### New MCP tools

- `voicebox.list_preset_voices`
- `voicebox.create_profile`
- `voicebox.get_profile`
- `voicebox.add_profile_sample`

### Existing tool retained for generation

- `voicebox.speak`

### Explicitly out of scope for v1

- updating or deleting profiles through MCP;
- deleting samples through MCP;
- automatic test entities or comparison records;
- batch comparison workflows;
- automatic audio generation during profile creation;
- database migrations;
- frontend changes;
- changes to profile export/import;
- parallel TTS inference.

## Architecture

Add a focused module:

```text
backend/mcp_server/
├── server.py
├── tools.py
├── profile_tools.py
├── resolve.py
├── context.py
└── events.py
```

`profile_tools.py` owns registration and MCP-specific validation for the four new tools. `tools.py` calls `register_profile_tools(mcp)` in addition to registering the existing tools.

The MCP layer remains a thin adapter:

```text
MCP request
    ↓
profile_tools.py
    ↓
Pydantic/native Voicebox validation
    ↓
services/profiles.py and database/storage services
    ↓
normalized MCP response
```

No business logic that already exists in Voicebox will be duplicated.

## Profile model

A profile represents one person or voice identity.

### Identity fields

- `name`: required, unique, 1–100 characters;
- `description`: optional, maximum 500 characters, informational only;
- `language`: required, validated by the existing `VoiceProfileCreate` model;
- `personality`: optional, maximum 2,000 characters, used by Voicebox for in-character composition and rewriting.

`description` and `personality` remain separate. Description explains who or what the profile is. Personality controls how the profile formulates speech when `personality=true` is used during generation.

### Preset profile

Required fields:

- `voice_type="preset"`;
- `preset_engine`;
- `preset_voice_id`.

Rules:

- the preset voice must exist for the selected engine;
- `default_engine` is set automatically to `preset_engine`;
- the profile is ready for generation immediately after creation;
- preset profiles cannot receive cloned-voice samples.

Initial preset engines are the engines already exposed by Voicebox:

- `kokoro`;
- `qwen_custom_voice`.

### Cloned profile

Required fields:

- `voice_type="cloned"`;
- optional cloning-compatible `default_engine`.

Supported cloning engines remain those defined by Voicebox:

- `qwen`;
- `luxtts`;
- `chatterbox`;
- `chatterbox_turbo`;
- `tada`.

Rules:

- profile creation and sample addition are separate operations;
- a cloned profile may exist with zero samples;
- it is ready for generation only after at least one valid sample exists;
- it cannot contain `preset_engine`, `preset_voice_id`, or `design_prompt`.

## Tool contracts

### `voicebox.list_preset_voices`

Input:

```json
{
  "engine": "kokoro"
}
```

Output:

```json
{
  "engine": "kokoro",
  "voices": [
    {
      "voice_id": "if_sara",
      "name": "Sara",
      "gender": "female",
      "language": "it"
    }
  ]
}
```

Unknown engines return a validation error rather than silently returning an empty list.

### `voicebox.create_profile`

Preset example:

```json
{
  "name": "Sara Podcast",
  "description": "Voce femminile italiana per podcast tecnici.",
  "personality": "Sara parla in modo chiaro, naturale e diretto.",
  "language": "it",
  "voice_type": "preset",
  "preset_engine": "kokoro",
  "preset_voice_id": "if_sara"
}
```

Cloned example:

```json
{
  "name": "Carlo",
  "description": "Voce italiana maschile per spiegazioni tecniche.",
  "personality": "Carlo parla in modo concreto e usa esempi reali.",
  "language": "it",
  "voice_type": "cloned",
  "default_engine": "qwen"
}
```

The tool creates metadata only. It never generates audio and never accepts a sample.

Normalized response:

```json
{
  "profile_id": "uuid",
  "name": "Carlo",
  "description": "...",
  "personality": "...",
  "language": "it",
  "voice_type": "cloned",
  "preset_engine": null,
  "preset_voice_id": null,
  "default_engine": "qwen",
  "sample_count": 0,
  "generation_count": 0,
  "ready_for_generation": false
}
```

### `voicebox.get_profile`

Input accepts either a profile UUID or a case-insensitive profile name:

```json
{
  "profile": "Carlo"
}
```

The response contains the complete profile metadata plus `sample_count`, `generation_count`, and calculated `ready_for_generation`.

Readiness rules:

- valid preset profile: `true`;
- cloned profile with at least one sample: `true`;
- cloned profile with no samples: `false`.

### `voicebox.add_profile_sample`

The tool accepts exactly one audio source.

#### Base64 source

```json
{
  "profile": "Carlo",
  "audio_base64": "...",
  "filename": "carlo.m4a",
  "reference_text": "Trascrizione esatta del campione."
}
```

Rules:

- `reference_text` is required and non-empty;
- Base64 decoding is strict;
- the decoded payload must not exceed 50 MB;
- the filename is used only to derive a safe supported suffix;
- no client-supplied filesystem path is accepted;
- the temporary file is always removed.

#### Capture source

```json
{
  "profile": "Carlo",
  "capture_id": "capture-uuid",
  "reference_text": null
}
```

Reference-text precedence:

1. explicit non-empty `reference_text`;
2. non-empty Capture `transcript_raw`;
3. error.

`transcript_refined` is never selected automatically because cloning requires the closest possible correspondence between spoken audio and reference text.

Capture rules:

- the Capture must exist;
- its stored audio path must resolve through Voicebox storage configuration;
- the audio file must exist;
- no arbitrary local path is accepted.

Normalized response:

```json
{
  "profile_id": "uuid",
  "profile_name": "Carlo",
  "sample_id": "uuid",
  "source": "capture",
  "reference_text_source": "transcript_raw",
  "sample_count": 1,
  "ready_for_generation": true
}
```

`reference_text_source` is either `explicit` or `transcript_raw`.

## Data flow

### Profile creation

```text
MCP arguments
    ↓
models.VoiceProfileCreate
    ↓
profiles.create_profile
    ↓
SQLite profile row + profile directory
    ↓
normalized MCP response
```

### Base64 sample

```text
audio_base64
    ↓
strict decode and size check
    ↓
safe temporary file
    ↓
profiles.add_profile_sample
    ↓
Voicebox validation/conversion/storage
    ↓
temporary-file cleanup
```

### Capture sample

```text
capture_id
    ↓
Capture lookup
    ↓
storage-path resolution and file check
    ↓
explicit reference text or transcript_raw
    ↓
profiles.add_profile_sample
```

## Error handling

MCP failures use clear, stable human-readable messages. The first implementation may raise `ValueError` through FastMCP rather than introducing a new response envelope.

Required failure cases:

- duplicate profile name;
- profile not found;
- unsupported preset engine;
- invalid preset voice;
- invalid profile-field combination;
- cloned profile configured with a preset-only engine;
- sample addition attempted on a preset profile;
- neither or both audio sources supplied;
- invalid Base64;
- decoded audio over 50 MB;
- missing Base64 `reference_text`;
- Capture not found;
- Capture audio missing;
- Capture without usable `transcript_raw` and without explicit text;
- unsupported or invalid audio;
- temporary-file cleanup after success and failure.

The profile remains intact when sample addition fails. Existing samples are not changed.

## Security

- no arbitrary audio path parameter is exposed by the new sample tool;
- Base64 is decoded with validation enabled;
- decoded size is checked before Voicebox processing;
- Capture paths come only from the database and are resolved through Voicebox storage helpers;
- temporary files are created with supported suffixes and deleted in `finally` blocks;
- sample addition is rejected for non-cloned profiles.

## Testing strategy

Tests do not run real TTS inference or require a GPU.

### Tool registration

Confirm the four new tools are registered and the existing tools remain available:

- `voicebox.speak`;
- `voicebox.transcribe`;
- `voicebox.list_captures`;
- `voicebox.list_profiles`.

### Preset creation

Cover:

- valid Kokoro profile;
- valid Qwen CustomVoice profile;
- missing preset fields;
- unsupported engine;
- unknown voice;
- duplicate name;
- incompatible default engine.

### Cloned creation

Cover:

- successful creation without a sample;
- readiness false at zero samples;
- valid cloning engine;
- preset-only engine rejection;
- forbidden preset metadata;
- field-length validation.

### Base64 sample

Cover:

- valid small WAV;
- invalid Base64;
- over-size payload;
- missing or empty reference text;
- neither or both sources supplied;
- profile not found;
- preset profile rejection;
- temporary-file cleanup on success and failure;
- updated sample count and readiness.

### Capture sample

Cover:

- explicit reference text;
- fallback to `transcript_raw`;
- no automatic use of `transcript_refined`;
- Capture not found;
- stored audio missing;
- no usable text;
- updated sample count and readiness.

### Profile retrieval

Cover lookup by UUID, exact name, and case-insensitive name, plus readiness calculation and complete identity/behaviour fields.

### Regression

Run the existing backend tests and a web build smoke test. The Docker image continues to use the repository's existing three-stage Dockerfile and existing Compose configuration.

## Deployment

Implementation occurs on:

```text
feature/mcp-profile-management
```

Validation sequence:

1. run focused backend tests;
2. run the broader existing test suite available in the repository;
3. run the existing web build smoke test;
4. build the existing Docker image;
5. start the container locally or on OMV;
6. inspect `/mcp` and confirm all expected tools;
7. test preset creation, cloned creation, sample addition, and `voicebox.speak`;
8. open a pull request into `main`.

During OMV testing the Compose build context points to the feature branch. After merge it points to `main`. Existing named volumes remain unchanged, so database, profiles, samples, generations, and model cache persist across rebuilds.

## Acceptance criteria

The feature is complete when:

- an MCP client can list valid preset voices;
- an MCP client can create preset and cloned profiles with description and personality;
- a cloned profile is created independently from its samples;
- an MCP client can add a sample from Base64;
- an MCP client can add a sample from an existing Capture;
- Capture text precedence is explicit text, then `transcript_raw`, then error;
- preset profiles reject sample addition;
- `get_profile` exposes complete metadata and correct readiness;
- the existing `voicebox.speak` tool can generate audio with newly created ready profiles;
- no database migration, frontend change, or new Docker build system is introduced;
- focused tests, regression tests, and the existing Docker build pass.
