from __future__ import annotations

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
