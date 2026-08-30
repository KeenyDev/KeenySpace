from __future__ import annotations

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse


def on_auth_error(conn: HTTPConnection, exc: Exception) -> JSONResponse:
    headers: dict[str, str] = {}
    # RFC 9728 §5.1: an MCP client bootstraps OAuth from a 401 whose
    # WWW-Authenticate names the protected-resource metadata URL. Without this
    # header, Claude Code's /mcp "Authorize" button never appears. Emit it only
    # on the MCP mount — the cookie/API-key surfaces under /v1/api expect a
    # plain 401 and must not advertise an OAuth challenge.
    if conn.url.path.startswith("/v1/mcp"):
        try:
            public_url = conn.app.state.settings.server.public_url.rstrip("/")
        except AttributeError:
            public_url = ""
        if public_url:
            resource_metadata = f"{public_url}/.well-known/oauth-protected-resource"
            headers["WWW-Authenticate"] = (
                f'Bearer resource_metadata="{resource_metadata}"'
            )
    return JSONResponse({"error": str(exc)}, status_code=401, headers=headers)
