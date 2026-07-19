#!/usr/bin/env bash
set -Eeuo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-voicebox}"
VOICEBOX_HOST="${VOICEBOX_HOST:-192.168.1.112}"
VOICEBOX_PORT="${VOICEBOX_PORT:-17600}"
VOICEBOX_REPO="${VOICEBOX_REPO:-https://github.com/carlolerro/voicebox-omv.git}"
VOICEBOX_REF="${VOICEBOX_REF:-feature/mcp-profile-management}"
TUNNEL_ENV="${TUNNEL_ENV:-/etc/openai-tunnel.env}"
TUNNEL_SERVICE="${TUNNEL_SERVICE:-openai-tunnel.service}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ROLLBACK_IMAGE="voicebox-rollback:${TIMESTAMP}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this deployment script as root." >&2
  exit 1
fi

for command in docker curl systemctl; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Required command not found: ${command}" >&2
    exit 1
  }
done

docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 || {
  echo "Container '${CONTAINER_NAME}' was not found." >&2
  exit 1
}

compose_workdir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER_NAME")"
compose_files_label="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$CONTAINER_NAME")"
compose_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER_NAME")"
compose_service="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$CONTAINER_NAME")"

if [[ -z "$compose_workdir" || -z "$compose_files_label" || -z "$compose_project" || -z "$compose_service" ]]; then
  echo "The existing Voicebox container is not exposing the expected Docker Compose labels." >&2
  echo "No changes were made." >&2
  exit 1
fi

IFS=',' read -r -a compose_files <<< "$compose_files_label"
compose_args=(docker compose -p "$compose_project")
for compose_file in "${compose_files[@]}"; do
  if [[ "$compose_file" != /* ]]; then
    compose_file="${compose_workdir}/${compose_file}"
  fi
  [[ -f "$compose_file" ]] || {
    echo "Compose file not found: ${compose_file}" >&2
    exit 1
  }
  compose_args+=( -f "$compose_file" )
done

override_file="${compose_workdir}/compose.voicebox-mcp-profile.override.yml"
rollback_override="$(mktemp)"
old_image_id="$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME")"

cat > "$override_file" <<YAML
services:
  ${compose_service}:
    build:
      context: ${VOICEBOX_REPO}#${VOICEBOX_REF}
YAML

cat > "$rollback_override" <<YAML
services:
  ${compose_service}:
    image: ${ROLLBACK_IMAGE}
    build: null
YAML

cleanup() {
  rm -f "$rollback_override"
}
trap cleanup EXIT

rollback() {
  echo
  echo "Deployment verification failed. Restoring the previous image..." >&2
  docker image tag "$old_image_id" "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
  "${compose_args[@]}" -f "$rollback_override" up -d --no-build --no-deps "$compose_service" || true
  echo "Rollback image retained as ${ROLLBACK_IMAGE}." >&2
}
trap rollback ERR

echo "=== Existing deployment ==="
echo "Project:       ${compose_project}"
echo "Service:       ${compose_service}"
echo "Working dir:   ${compose_workdir}"
echo "Current image: ${old_image_id}"
echo "Target ref:    ${VOICEBOX_REF}"
echo "Override:      ${override_file}"
echo

docker image tag "$old_image_id" "$ROLLBACK_IMAGE"
docker inspect "$CONTAINER_NAME" > "/root/voicebox-container-before-${TIMESTAMP}.json"

# Validate the merged Compose model before touching the running container.
"${compose_args[@]}" -f "$override_file" config >/dev/null

echo "=== Building target image ==="
"${compose_args[@]}" -f "$override_file" build "$compose_service"

echo "=== Recreating Voicebox ==="
"${compose_args[@]}" -f "$override_file" up -d --no-build --no-deps "$compose_service"

health_url="http://${VOICEBOX_HOST}:${VOICEBOX_PORT}/health"
mcp_url="http://${VOICEBOX_HOST}:${VOICEBOX_PORT}/mcp"

healthy=0
for _attempt in $(seq 1 90); do
  if curl -fsS "$health_url" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done

if [[ "$healthy" != "1" ]]; then
  docker logs --tail 200 "$CONTAINER_NAME" >&2 || true
  echo "Voicebox did not become healthy at ${health_url}." >&2
  false
fi

echo "=== Health ==="
curl -fsS "$health_url"
echo

for method in GET HEAD; do
  for discovery_path in \
    '/.well-known/oauth-protected-resource' \
    '/.well-known/oauth-protected-resource/mcp'; do
    headers_file="$(mktemp)"
    body_file="$(mktemp)"
    status="$(curl -sS -X "$method" -D "$headers_file" -o "$body_file" -w '%{http_code}' \
      "http://${VOICEBOX_HOST}:${VOICEBOX_PORT}${discovery_path}" || true)"
    if [[ "$status" != "404" ]] || grep -qi '^content-type:.*text/html' "$headers_file"; then
      echo "Invalid OAuth discovery response: ${method} ${discovery_path} -> ${status}" >&2
      cat "$headers_file" >&2 || true
      cat "$body_file" >&2 || true
      rm -f "$headers_file" "$body_file"
      false
    fi
    rm -f "$headers_file" "$body_file"
  done
done

mcp_headers="$(mktemp)"
mcp_body="$(mktemp)"
mcp_status="$(curl -sS \
  -D "$mcp_headers" \
  -o "$mcp_body" \
  -w '%{http_code}' \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Voicebox-Client-Id: chatgpt' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"omv-production-probe","version":"1.0"}}}' \
  "$mcp_url" || true)"

if [[ ! "$mcp_status" =~ ^2[0-9][0-9]$ ]] || grep -qi '^content-type:.*text/html' "$mcp_headers"; then
  echo "Production POST /mcp verification failed with HTTP ${mcp_status}." >&2
  cat "$mcp_headers" >&2 || true
  cat "$mcp_body" >&2 || true
  rm -f "$mcp_headers" "$mcp_body"
  false
fi
rm -f "$mcp_headers" "$mcp_body"

echo "=== MCP tools ==="
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
        for name in sorted(EXPECTED):
            print(f"  - {name}")

asyncio.run(main())
PY

if [[ -f "$TUNNEL_ENV" ]]; then
  cp -a "$TUNNEL_ENV" "${TUNNEL_ENV}.bak-${TIMESTAMP}"

  set_env_line() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$TUNNEL_ENV"; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$TUNNEL_ENV"
    else
      printf '%s=%s\n' "$key" "$value" >> "$TUNNEL_ENV"
    fi
  }

  set_env_line MCP_SERVER_URL "$mcp_url"
  set_env_line MCP_EXTRA_HEADERS 'X-Voicebox-Client-Id: chatgpt'
  set_env_line MCP_DISCOVERY_EXTRA_HEADERS 'X-Voicebox-Client-Id: chatgpt'

  systemctl restart "$TUNNEL_SERVICE"
  systemctl is-active --quiet "$TUNNEL_SERVICE"
else
  echo "Tunnel environment file not found at ${TUNNEL_ENV}; Voicebox is deployed, but the tunnel was not changed." >&2
fi

trap - ERR

echo
echo "Voicebox production deployment completed successfully."
echo "MCP endpoint: ${mcp_url}"
echo "Persistent Compose override: ${override_file}"
echo "Rollback image retained as: ${ROLLBACK_IMAGE}"
echo "Container backup: /root/voicebox-container-before-${TIMESTAMP}.json"
