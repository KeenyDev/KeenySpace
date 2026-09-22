"""OIDC client wrapper — Authlib starlette_client + joserfc validation.

D-10..D-13. Lazy discovery; JWKS validation via JwksCache; PKCE S256.
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from authlib.integrations.httpx_client import AsyncOAuth2Client  # type: ignore[import-untyped]
from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from joserfc import jwt as joserfc_jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwt import JWTClaimsRegistry

from keenyspace_server.auth.jwks_cache import JwksCache
from keenyspace_server.auth.user import User
from keenyspace_server.config import AuthSettings, Settings

log = structlog.get_logger(__name__)

LEEWAY_SECONDS = 30


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=settings.auth.oidc_client_id,
        client_secret=settings.auth.oidc_client_secret.get_secret_value(),
        server_metadata_url=(
            f"{settings.auth.metadata_issuer_url.rstrip('/')}/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid profile email groups",
            "code_challenge_method": "S256",
        },
    )
    return oauth


class OidcClient:
    def __init__(self, oauth: OAuth, auth_settings: AuthSettings) -> None:
        self._oauth = oauth
        self._auth = auth_settings
        self._issuer = auth_settings.oidc_issuer_url.rstrip("/")
        self._client_id = auth_settings.oidc_client_id
        self._jwks_cache = JwksCache(
            jwks_uri_provider=self._get_jwks_uri,
            ttl_seconds=auth_settings.jwks_ttl_seconds,
            min_retry_interval_seconds=auth_settings.jwks_min_retry_interval_seconds,
        )

    async def _get_jwks_uri(self) -> str:
        metadata = await self._oauth.authentik.load_server_metadata()
        return str(metadata["jwks_uri"])

    async def _get_token_endpoint(self) -> str:
        metadata = await self._oauth.authentik.load_server_metadata()
        return str(metadata["token_endpoint"])

    @staticmethod
    def is_near_expiry(claims: dict[str, Any], threshold_seconds: int = 60) -> bool:
        exp = claims.get("exp")
        if exp is None:
            return False
        try:
            return (float(exp) - time.time()) < threshold_seconds
        except TypeError, ValueError:
            return False

    async def validate_access_token(self, token: str, *, conn: Any = None) -> User | None:
        keyset = await self._jwks_cache.get()
        if keyset is None:
            return None
        try:
            decoded = joserfc_jwt.decode(token, keyset, algorithms=["RS256", "ES256"])
        except InvalidKeyIdError:
            keyset = await self._jwks_cache.force_refresh()
            if keyset is None:
                return None
            try:
                decoded = joserfc_jwt.decode(token, keyset, algorithms=["RS256", "ES256"])
            except JoseError:
                return None
        except JoseError:
            return None
        except Exception:
            return None

        registry = JWTClaimsRegistry(
            leeway=LEEWAY_SECONDS,
            aud={"essential": True, "value": self._client_id},
            exp={"essential": True},
            iat={"essential": True},
        )
        try:
            registry.validate(decoded.claims)
        except JoseError:
            return None
        except Exception:
            return None

        # Authentik per_provider issuer_mode mints `iss` with a trailing slash
        # (http://host/application/o/<slug>/); JWTClaimsRegistry does an exact-value
        # match and would reject it. Normalize one side and compare manually so the
        # check is IdP-agnostic (Authentik trailing-slash, Keycloak/Auth0 no-slash).
        iss_claim = decoded.claims.get("iss")
        if not isinstance(iss_claim, str) or iss_claim.rstrip("/") != self._issuer:
            log.warning("auth.token.iss_mismatch", expected=self._issuer, got=iss_claim)
            return None

        # Authentik mints the access token as the ID token's claims plus
        # `scope`, `azp` and `uid` (IDToken.to_access_token); the ID token it
        # returns next to it carries no `scope`. Requiring the claim keeps an ID
        # token — same signer, issuer and audience — from being replayed as a
        # bearer credential.
        if not isinstance(decoded.claims.get("scope"), str):
            log.warning("auth.token.not_access_token", reason="missing_scope_claim")
            return None

        if conn is not None and self.is_near_expiry(
            decoded.claims, self._auth.refresh_threshold_seconds
        ):
            with contextlib.suppress(Exception):
                conn.state.ks_at_expiring_soon = True

        sub_value = decoded.claims.get("sub")
        if not isinstance(sub_value, str):
            return None
        display_name = (
            decoded.claims.get("preferred_username") or decoded.claims.get("name") or sub_value
        )
        issued_at = datetime.fromtimestamp(float(decoded.claims["iat"]), UTC)
        raw_groups = decoded.claims.get("groups")
        groups_seen_at: datetime | None = None
        groups: list[str] = []
        if isinstance(raw_groups, list):
            groups = [g for g in raw_groups if isinstance(g, str)]
            groups_seen_at = issued_at
        return User(
            sub=sub_value,
            _display_name=str(display_name),
            source="oidc",
            groups=groups,
            groups_seen_at=groups_seen_at,
            issued_at=issued_at,
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        try:
            token_endpoint = await self._get_token_endpoint()
        except Exception:
            log.warning("auth.token.refresh.metadata_failed")
            return None
        try:
            async with AsyncOAuth2Client(
                self._client_id, self._auth.oidc_client_secret.get_secret_value()
            ) as client:
                token = await client.refresh_token(token_endpoint, refresh_token=refresh_token)
                if isinstance(token, dict):
                    return token
                return dict(token)
        except Exception:
            log.warning("auth.token.refresh.failed")
            return None

    async def logout_url(self, id_token: str | None) -> str:
        try:
            if id_token:
                logout_info = await self._oauth.authentik.create_logout_url(
                    post_logout_redirect_uri=self._auth.oidc_post_logout_redirect_uri,
                    id_token_hint=id_token,
                )
                if isinstance(logout_info, dict):
                    url = logout_info.get("url")
                    if isinstance(url, str):
                        return url
                elif isinstance(logout_info, str):
                    return logout_info
        except Exception:
            log.warning("auth.logout.url_build_failed")
        return self._auth.oidc_post_logout_redirect_uri
