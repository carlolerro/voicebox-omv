"""Explicit no-auth response for MCP OAuth discovery probes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{resource_path:path}")
async def oauth_protected_resource_not_configured(
    resource_path: str | None = None,
) -> None:
    """Return a real 404 instead of allowing the SPA to serve index.html.

    Voicebox's MCP server intentionally runs without OAuth metadata. Remote MCP
    discovery clients probe these standard paths; the absence of OAuth must be
    represented as 404 JSON, not a successful HTML SPA response.
    """
    raise HTTPException(
        status_code=404,
        detail="OAuth protected resource metadata is not configured.",
    )
