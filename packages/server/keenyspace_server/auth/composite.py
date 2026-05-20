"""CompositeAuthBackend — resolver chain (cookie > api_key > oidc_bearer).

D-19: replaces DevTokenAuthBackend.
Wave 2: только api_key resolver активен; cookie + oidc_bearer заполняются в Wave 3.
"""

from __future__ import annotations

from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.requests import HTTPConnection

from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.user import User

PUBLIC_PREFIXES = (
    "/healthz",
    "/readyz",
    "/metrics",
    "/v1/api/auth/login",
    "/v1/api/auth/callback",
)


class CompositeAuthBackend(AuthenticationBackend):
    def __init__(
        self,
        *,
        oidc_client: object | None,
        api_key_service: ApiKeyService,
    ) -> None:
        self._oidc = oidc_client
        self._keys = api_key_service

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, User] | None:
        path = conn.url.path
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return None
        user = (
            await self._try_cookie(conn)
            or await self._try_api_key(conn)
            or await self._try_oidc_bearer(conn)
        )
        if user is None:
            raise AuthenticationError("no valid credentials")
        return (AuthCredentials(["authenticated"]), user)

    async def _try_cookie(self, conn: HTTPConnection) -> User | None:
        return None

    async def _try_api_key(self, conn: HTTPConnection) -> User | None:
        auth = conn.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :]
        if not token.startswith("ks_live_"):
            return None
        return await self._keys.verify(token)

    async def _try_oidc_bearer(self, conn: HTTPConnection) -> User | None:
        return None
