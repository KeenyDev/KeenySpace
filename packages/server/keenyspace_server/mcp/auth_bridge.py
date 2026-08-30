from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request

from keenyspace_server.auth.user import User


class McpAuthError(Exception):
    pass


def current_user_from_mcp() -> User:
    req = get_http_request()
    if not req.user.is_authenticated:
        raise McpAuthError("not authenticated")
    user = req.user
    if not isinstance(user, User):
        raise McpAuthError(f"unexpected user type: {type(user)}")
    return user


def resolve_workspace(workspace: str | None) -> str:
    """Resolve the target workspace for a tool call.

    An explicit ``workspace`` argument always wins. Otherwise fall back to a
    connection-level pin: the ``?workspace=<slug>`` query param on the MCP URL.
    This lets a client register one MCP server entry per workspace
    (connect-on-space) and omit the argument on every call. Authentik cannot
    scope tokens per workspace (no RFC 8707), so the pin lives in the URL, not
    the token; per-workspace ACL stays the server's concern.
    """
    if workspace:
        return workspace
    pinned = get_http_request().query_params.get("workspace")
    if pinned:
        return pinned
    raise ToolError(
        "no workspace specified: pass `workspace`, or pin one on the MCP "
        "connection URL as ?workspace=<slug>"
    )
