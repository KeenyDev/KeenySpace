"""Admin authorization for /v1/admin/*: membership in `auth.admin_group`.

Groups come from the verified token claim for OIDC principals and from the
owner's group snapshot for API keys. The check sits in the route handler, ahead
of FastAPI's body parsing, so a non-admin restore upload is refused before it
is spooled to disk (endpoint dependencies only run after the multipart body
has been read).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

from keenyspace_server.auth.user import User

log = structlog.get_logger(__name__)


def is_admin(user: object, admin_group: str) -> bool:
    return bool(admin_group) and isinstance(user, User) and admin_group in user.groups


class AdminRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def _handler(request: Request) -> Response:
            admin_group: str = request.app.state.settings.auth.admin_group
            user = request.user
            if not is_admin(user, admin_group):
                log.warning(
                    "auth.admin_gate.denied",
                    sub=getattr(user, "sub", None),
                    source=getattr(user, "source", None),
                    reason="admin_api_disabled" if not admin_group else "not_in_admin_group",
                    path=request.url.path,
                )
                raise HTTPException(status_code=403, detail="forbidden")
            return await handler(request)

        return _handler
