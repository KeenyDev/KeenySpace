from __future__ import annotations

import hmac

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.requests import HTTPConnection

from .user import User

PUBLIC_PREFIXES = ("/healthz", "/readyz", "/metrics")


class DevTokenAuthBackend(AuthenticationBackend):
    def __init__(self, dev_token: str | None = None) -> None:
        self._dev_token = dev_token

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, User] | None:
        path = conn.url.path
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return None

        authorization = conn.headers.get("Authorization", "")
        if not authorization:
            raise AuthenticationError("Authorization header missing")

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            raise AuthenticationError("Invalid Authorization header format")

        token_value = parts[1]
        if not token_value.startswith("dev-"):
            raise AuthenticationError("Invalid token format")

        provided = token_value[4:]
        if not provided:
            raise AuthenticationError("Empty token value")

        if self._dev_token is None or not hmac.compare_digest(provided, self._dev_token):
            raise AuthenticationError("Invalid token")

        return (
            AuthCredentials(["authenticated"]),
            User(sub="dev", _display_name="Developer", source="dev_shim"),
        )
