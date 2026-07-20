#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-voicebox-mcp-story:test}"
CONTAINER_NAME="${CONTAINER_NAME:-voicebox-mcp-story-test}"
DATA_VOLUME="${DATA_VOLUME:-voicebox-mcp-story-test-data}"
HOST_PORT="${HOST_PORT:-17601}"
REMOVE_IMAGE="${REMOVE_IMAGE:-0}"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker volume rm "$DATA_VOLUME" >/dev/null 2>&1 || true
  if [[ "$REMOVE_IMAGE" == "1" ]]; then
    docker image rm "$IMAGE_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$ROOT_DIR"

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker volume rm "$DATA_VOLUME" >/dev/null 2>&1 || true

echo "[1/7] Building the existing Voicebox Docker image..."
docker build -t "$IMAGE_NAME" .

echo "[2/7] Starting an isolated Voicebox container..."
docker volume create "$DATA_VOLUME" >/dev/null
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "127.0.0.1:${HOST_PORT}:17493" \
  -v "${DATA_VOLUME}:/app/data" \
  -e LOG_LEVEL=info \
  -e NUMBA_CACHE_DIR=/tmp/numba_cache \
  "$IMAGE_NAME" >/dev/null

echo "[3/7] Waiting for /health..."
healthy=0
for _attempt in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null; then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  docker logs "$CONTAINER_NAME" >&2 || true
  echo "Voicebox did not become healthy within 180 seconds." >&2
  exit 1
fi
curl -fsS "http://127.0.0.1:${HOST_PORT}/health"
echo

echo "[4/7] Running non-GPU MCP/Story tests and syntax compilation..."
docker exec "$CONTAINER_NAME" python -m unittest \
  backend.tests.test_story_orchestration_models \
  backend.tests.test_story_profile_resolution \
  backend.tests.test_story_rendering \
  backend.tests.test_story_orchestration \
  backend.tests.test_mcp_story_tools \
  backend.tests.test_story_http_compatibility \
  backend.tests.test_story_restart_recovery \
  -v
docker exec "$CONTAINER_NAME" \
  python -m unittest discover -s backend/tests -p 'test_mcp_*.py' -v
docker exec "$CONTAINER_NAME" python -m compileall -q backend

echo "[5/7] Verifying no-auth OAuth discovery and Streamable HTTP routing..."
for discovery_path in \
  '/.well-known/oauth-protected-resource' \
  '/.well-known/oauth-protected-resource/mcp'; do
  for discovery_method in GET HEAD; do
    headers_file="$(mktemp)"
    body_file="$(mktemp)"
    curl_method_args=()
    if [[ "$discovery_method" == "HEAD" ]]; then
      curl_method_args+=(--head)
    fi
    status="$({ curl -sS \
      --max-time 30 \
      "${curl_method_args[@]}" \
      -D "$headers_file" \
      -o "$body_file" \
      -w '%{http_code}' \
      "http://127.0.0.1:${HOST_PORT}${discovery_path}"; } || true)"
    if [[ "$status" != "404" ]]; then
      echo "Expected 404 for ${discovery_method} ${discovery_path}, got ${status}." >&2
      cat "$headers_file" >&2 || true
      cat "$body_file" >&2 || true
      rm -f "$headers_file" "$body_file"
      exit 1
    fi
    if grep -qi '^content-type:.*text/html' "$headers_file"; then
      echo "OAuth discovery ${discovery_method} ${discovery_path} incorrectly returned HTML." >&2
      cat "$headers_file" >&2 || true
      cat "$body_file" >&2 || true
      rm -f "$headers_file" "$body_file"
      exit 1
    fi
    rm -f "$headers_file" "$body_file"
  done
done

mcp_headers="$(mktemp)"
mcp_body="$(mktemp)"
mcp_status="$({ curl -sS \
  --max-time 30 \
  -D "$mcp_headers" \
  -o "$mcp_body" \
  -w '%{http_code}' \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Voicebox-Client-Id: chatgpt' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"omv-probe","version":"1.0"}}}' \
  "http://127.0.0.1:${HOST_PORT}/mcp"; } || true)"
if [[ ! "$mcp_status" =~ ^2[0-9][0-9]$ ]]; then
  echo "POST /mcp did not complete FastMCP initialization; HTTP status ${mcp_status}." >&2
  cat "$mcp_headers" >&2 || true
  cat "$mcp_body" >&2 || true
  rm -f "$mcp_headers" "$mcp_body"
  exit 1
fi
if grep -qi '^content-type:.*text/html' "$mcp_headers"; then
  echo "POST /mcp was intercepted by the SPA and returned HTML." >&2
  cat "$mcp_headers" >&2 || true
  cat "$mcp_body" >&2 || true
  rm -f "$mcp_headers" "$mcp_body"
  exit 1
fi
printf 'POST /mcp completed FastMCP initialization (HTTP %s).\n' "$mcp_status"
rm -f "$mcp_headers" "$mcp_body"

echo "[6/7] Discovering the live MCP tools..."
docker exec -i "$CONTAINER_NAME" python - <<'PY'
import asyncio
from fastmcp import Client

EXPECTED = {
    "voicebox.speak",
    "voicebox.transcribe",
    "voicebox.list_captures",
    "voicebox.list_profiles",
    "voicebox.list_preset_voices",
    "voicebox.create_profile",
    "voicebox.get_profile",
    "voicebox.add_profile_sample",
    "voicebox.create_story",
    "voicebox.get_story_status",
    "voicebox.get_story",
    "voicebox.resume_story",
}


async def main() -> None:
    async with Client("http://127.0.0.1:17493/mcp") as client:
        names = {tool.name for tool in await client.list_tools()}
        missing = EXPECTED - names
        if missing:
            raise RuntimeError(f"Missing MCP tools: {sorted(missing)}")
        print("Registered MCP tools:")
        for name in sorted(EXPECTED):
            print(f"  - {name}")


asyncio.run(main())
PY

echo "[7/7] Running create -> sample -> get through the live MCP endpoint..."
docker exec -i "$CONTAINER_NAME" python - <<'PY'
import asyncio
import base64
import io
import wave

from fastmcp import Client


def reference_wav_base64() -> str:
    frames = bytearray()
    for index in range(16_000 * 3):
        sample = 4_000 if (index // 80) % 2 == 0 else -4_000
        frames.extend(sample.to_bytes(2, "little", signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(bytes(frames))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def main() -> None:
    async with Client("http://127.0.0.1:17493/mcp") as client:
        await client.call_tool(
            "voicebox.list_preset_voices",
            {"engine": "kokoro"},
        )
        created = await client.call_tool(
            "voicebox.create_profile",
            {
                "name": "MCP OMV Smoke Person",
                "description": "Temporary local smoke-test profile.",
                "language": "it",
                "voice_type": "cloned",
                "default_engine": "qwen",
                "personality": "Parla in modo chiaro e diretto.",
            },
        )
        print("create_profile:", created)

        added = await client.call_tool(
            "voicebox.add_profile_sample",
            {
                "profile": "MCP OMV Smoke Person",
                "audio_base64": reference_wav_base64(),
                "filename": "reference.wav",
                "reference_text": "Questo è un campione vocale di verifica.",
            },
        )
        print("add_profile_sample:", added)

        loaded = await client.call_tool(
            "voicebox.get_profile",
            {"profile": "mcp omv smoke person"},
        )
        print("get_profile:", loaded)


asyncio.run(main())
PY

echo
echo "MCP profile-management, Story-mode, and tunnel-routing verification completed successfully."
echo "The test image remains available as: ${IMAGE_NAME}"
