"""Helpers shared by the OidcClient token-validation tests.

test_groups_claim, test_oidc_iss_validation and test_oidc_token_claims all build the
same OidcClient against the mock Authentik provider and differ only in the claims they
sign, so the settings/client/keyset construction lives here rather than in one of them.
"""

from __future__ import annotations

import httpx
from joserfc.jwk import KeySet
from keenyspace_server.auth.oidc import OidcClient
from keenyspace_server.config import AuthSettings


def _auth(**overrides: object) -> AuthSettings:
    base: dict[str, object] = {
        "oidc_issuer_url": "http://localhost:9000/application/o/keenyspace/",
        "oidc_client_id": "keenyspace-cli",
        "oidc_client_secret": "secret",
        "oidc_redirect_uri": "http://localhost:8000/v1/api/auth/callback",
        "oidc_post_logout_redirect_uri": "http://localhost:8000/",
        "session_secret_key": "session-secret-32chars-padded-here!",
        "api_key_pepper": "pepper-32chars-padded-here-xxxxx!",
    }
    base.update(overrides)
    return AuthSettings(**base)  # type: ignore[arg-type]


def _make_client(auth_settings: AuthSettings, keyset: KeySet) -> OidcClient:
    """Build OidcClient with JWKS cache mocked to return *keyset* directly."""
    from unittest.mock import AsyncMock

    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    client = OidcClient(oauth, auth_settings)
    client._jwks_cache.get = AsyncMock(return_value=keyset)  # type: ignore[method-assign]
    client._jwks_cache.force_refresh = AsyncMock(return_value=keyset)  # type: ignore[method-assign]
    return client


def _fetch_keyset(jwks_uri: str) -> KeySet:
    resp = httpx.get(jwks_uri)
    return KeySet.import_key_set(resp.json())
