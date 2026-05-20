"""OIDC client wrapper — Authlib starlette_client + joserfc validation.

D-10..D-13. Lazy discovery; JWKS validation via JwksCache; PKCE S256.
"""

from __future__ import annotations

import time
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
        client_secret=settings.auth.oidc_client_secret,
        server_metadata_url=(
            f"{settings.auth.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration"
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
            iss={"essential": True, "value": self._issuer},
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

        if conn is not None and self.is_near_expiry(
            decoded.claims, self._auth.refresh_threshold_seconds
        ):
            try:
                conn.state.ks_at_expiring_soon = True
            except Exception:
                pass

        sub_value = decoded.claims.get("sub")
        if not isinstance(sub_value, str):
            return None
        display_name = (
            decoded.claims.get("preferred_username") or decoded.claims.get("name") or sub_value
        )
        return User(
            sub=sub_value,
            _display_name=str(display_name),
            source="oidc",
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        try:
            token_endpoint = await self._get_token_endpoint()
        except Exception:
            log.warning("auth.token.refresh.metadata_failed")
            return None
        try:
            async with AsyncOAuth2Client(self._client_id, self._auth.oidc_client_secret) as client:
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
