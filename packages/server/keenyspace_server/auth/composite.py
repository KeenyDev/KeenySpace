"""CompositeAuthBackend — resolver chain (cookie > api_key > oidc_bearer).

D-19: единственный production auth backend Phase 3+.
Wave 2: только api_key resolver активен; cookie + oidc_bearer заполняются в Wave 3.
"""

from __future__ import annotations

import structlog
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.requests import HTTPConnection

from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.oidc import OidcClient
from keenyspace_server.auth.user import User

log = structlog.get_logger(__name__)

PUBLIC_PREFIXES = (
    "/healthz",
    "/readyz",
    "/metrics",
    "/.well-known/oauth-protected-resource",
    "/v1/api/auth/discovery",
    "/v1/api/auth/login",
    "/v1/api/auth/callback",
)


class CompositeAuthBackend(AuthenticationBackend):
    def __init__(
        self,
        *,
        oidc_client: OidcClient | None,
        api_key_service: ApiKeyService,
        required_group: str = "",
    ) -> None:
        self._oidc: OidcClient | None = oidc_client
        self._keys = api_key_service
        self._required_group = required_group

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, User] | None:
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
        if self._required_group and user.source == "oidc" and self._required_group not in user.groups:
            log.warning("auth.group_gate.denied", sub=user.sub)
            raise AuthenticationError("forbidden")
        return (AuthCredentials(["authenticated"]), user)

    async def _try_cookie(self, conn: HTTPConnection) -> User | None:
        if self._oidc is None:
            return None
        token = conn.cookies.get("ks_at")
        if not token:
            return None
        return await self._oidc.validate_access_token(token, conn=conn)

    async def _try_api_key(self, conn: HTTPConnection) -> User | None:
        auth = conn.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :]
        if not token.startswith("ks_live_"):
            return None
        return await self._keys.verify(token)

    async def _try_oidc_bearer(self, conn: HTTPConnection) -> User | None:
        if self._oidc is None:
            return None
        auth = conn.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :]
        if token.startswith("ks_live_"):
            return None
        return await self._oidc.validate_access_token(token, conn=None)
