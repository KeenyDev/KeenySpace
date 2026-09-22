"""CompositeAuthBackend — resolver chain (cookie > api_key > oidc_bearer).

D-19: единственный production auth backend Phase 3+.
Every OIDC principal whose token carries a groups claim refreshes the owner's
group snapshot, which is what API keys are authorized with; a token older than
the stored snapshot is authorized with the snapshot's groups instead of its own. `required_group`
applies to every principal: token groups for OIDC, the snapshot for API keys.
"""

from __future__ import annotations

from dataclasses import replace

import structlog
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.requests import HTTPConnection

from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.group_snapshot import GroupSnapshotStore
from keenyspace_server.auth.oidc import OidcClient
from keenyspace_server.auth.user import User

log = structlog.get_logger(__name__)

PUBLIC_PREFIXES = (
    "/healthz",
    "/readyz",
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
        group_snapshots: GroupSnapshotStore | None = None,
    ) -> None:
        self._oidc: OidcClient | None = oidc_client
        self._keys = api_key_service
        self._required_group = required_group
        self._snapshots = group_snapshots

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
        if self._snapshots is not None and user.source == "oidc" and user.groups_seen_at is not None:
            snapshot, wrote = await self._snapshots.observe(user)
            if wrote:
                self._keys.forget_user(user.sub)
            if snapshot.seen_at > user.groups_seen_at:
                # A newer IdP assertion (or an admin revoke-all) supersedes the
                # groups this still-valid older token carries.
                user = replace(user, groups=list(snapshot.groups), groups_seen_at=snapshot.seen_at)
        if self._required_group and self._required_group not in user.groups:
            log.warning(
                "auth.group_gate.denied",
                sub=user.sub,
                source=user.source,
                reason=_group_denial_reason(user),
            )
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


def _group_denial_reason(user: User) -> str:
    if user.groups_seen_at is not None:
        return "not_in_group"
    return "no_group_snapshot" if user.source == "api_key" else "no_groups_claim"
