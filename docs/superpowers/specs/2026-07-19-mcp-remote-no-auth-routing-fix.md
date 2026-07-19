# MCP Remote No-Auth Routing Fix

## Problem

Voicebox mounts a FastMCP Streamable HTTP application under `/mcp` and later
adds a GET-only catch-all route for the React SPA.

Two independent routing failures affected remote MCP discovery:

1. Starlette's mount handles `/mcp/` and descendants, but an exact `POST /mcp`
   can fall through to the SPA catch-all and become `405 Method Not Allowed`.
2. Voicebox intentionally has no OAuth metadata, but requests to
   `/.well-known/oauth-protected-resource` were falling through to the SPA and
   returning `index.html` with HTTP 200 instead of a real 404.

The tunnel header `X-Voicebox-Client-Id` identifies the MCP client for voice
bindings. It is not authentication and cannot correct either routing problem.

## Implemented behaviour

### Exact MCP path

`ClientIdMiddleware` normalizes only the exact ASGI path `/mcp` to `/mcp/`
before routing. This is an internal scope rewrite, not an HTTP redirect, so the
request method, body, headers, query string, and streaming semantics are
preserved.

The mounted FastMCP application therefore handles both:

- `/mcp`
- `/mcp/`

### No-auth OAuth discovery

Explicit API routes handle GET and HEAD for:

- `/.well-known/oauth-protected-resource`
- `/.well-known/oauth-protected-resource/{resource_path}`

They return HTTP 404 with a JSON response. The React SPA never handles these
paths.

### Mount order

The application order remains:

```text
API routers
    ↓
FastMCP mount at /mcp
    ↓
React SPA catch-all
```

## Tunnel configuration

The deployment keeps the LAN-reachable Voicebox address without a forced
trailing slash:

```text
MCP_SERVER_URL=http://192.168.1.112:17600/mcp
MCP_EXTRA_HEADERS=X-Voicebox-Client-Id: chatgpt
MCP_DISCOVERY_EXTRA_HEADERS=X-Voicebox-Client-Id: chatgpt
```

The concrete LAN address remains a deployment setting and is not committed to
Voicebox application code.

## Verification

The focused routing tests cover:

- GET and HEAD OAuth discovery returning 404 and never HTML;
- normal SPA routes still serving HTML;
- exact `POST /mcp` reaching a mounted ASGI MCP application;
- FastMCP mount registration preceding the SPA catch-all.

The OMV verification script additionally performs a real Streamable HTTP
initialize request against exact `/mcp`, discovers all expected tools, and
runs the profile lifecycle smoke test through the live container.
