"""RFC 9728 OAuth 2.0 Protected Resource Metadata for the MCP mount.

Claude Code (and any MCP client) bootstraps OAuth from a 401 carrying
``WWW-Authenticate: Bearer resource_metadata="<this-url>"`` (see
auth/middleware.py). It fetches this document, reads ``authorization_servers``,
then runs OIDC discovery against the Authentik issuer
(``<issuer>/.well-known/openid-configuration`` — Authentik 2026.2 does not
serve the RFC 8414 ``oauth-authorization-server`` document, so the OIDC
fallback is the only discovery path) and performs an authorization-code + PKCE
flow with a pre-registered static client. No Dynamic Client Registration:
Authentik exposes no ``registration_endpoint``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _metadata(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    public_url = settings.server.public_url.rstrip("/")
    issuer = settings.auth.oidc_issuer_url.rstrip("/")
    return {
        "resource": f"{public_url}/v1/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "profile", "email", "groups"],
    }


# Bare form + RFC 9728 path-suffix form (the metadata URL for a resource at
# ``/v1/mcp`` is ``/.well-known/oauth-protected-resource/v1/mcp``). Clients vary
# on which they request; serve both with the same document.
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/v1/mcp")
async def oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse(_metadata(request))
