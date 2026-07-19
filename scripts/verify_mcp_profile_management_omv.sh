#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-voicebox-mcp-profile:test}"
CONTAINER_NAME="${CONTAINER_NAME:-voicebox-mcp-profile-test}"
DATA_VOLUME="${DATA_VOLUME:-voicebox-mcp-profile-test-data}"
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

echo "[1/6] Building the existing Voicebox Docker image..."
docker build -t "$IMAGE_NAME" .

echo "[2/6] Starting an isolated Voicebox container..."
docker volume create "$DATA_VOLUME" >/dev/null
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "127.0.0.1:${HOST_PORT}:17493" \
  -v "${DATA_VOLUME}:/app/data" \
  -e LOG_LEVEL=info \
  -e NUMBA_CACHE_DIR=/tmp/numba_cache \
  "$IMAGE_NAME" >/dev/null

echo "[3/6] Waiting for /health..."
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

echo "[4/6] Running non-GPU unit tests and syntax compilation..."
docker exec "$CONTAINER_NAME" \
  python -m unittest discover -s backend/tests -p 'test_mcp_profile_tools*.py' -v
docker exec "$CONTAINER_NAME" python -m compileall -q backend

echo "[5/6] Discovering the live MCP tools..."
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

echo "[6/6] Running create -> sample -> get through the live MCP endpoint..."
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
echo "MCP profile-management verification completed successfully."
echo "The test image remains available as: ${IMAGE_NAME}"
