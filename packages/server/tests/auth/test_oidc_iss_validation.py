"""D-22 — custom iss validator in OidcClient.validate_access_token.

Three test cases exercise D-26 SC-2:
  1. Authentik per_provider trailing-slash iss -> User returned.
  2. Keycloak/Auth0 no-slash iss -> User returned.
  3. Wrong iss -> None returned + auth.token.iss_mismatch warn emitted.
  4. Missing iss claim -> None returned (isinstance guard).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog.testing
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
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    client = OidcClient(oauth, auth_settings)
    client._jwks_cache.get = AsyncMock(return_value=keyset)  # type: ignore[method-assign]
    client._jwks_cache.force_refresh = AsyncMock(return_value=keyset)  # type: ignore[method-assign]
    return client


def _valid_claims(*, iss: str | None, sub: str = "u-iss-test") -> dict:
    now = int(time.time())
    claims: dict = {
        "sub": sub,
        "aud": "keenyspace-cli",
        "scope": "openid profile email groups",
        "exp": now + 3600,
        "iat": now,
    }
    if iss is not None:
        claims["iss"] = iss
    return claims


def _fetch_keyset(jwks_uri: str) -> KeySet:
    resp = httpx.get(jwks_uri)
    return KeySet.import_key_set(resp.json())


@pytest.mark.asyncio
async def test_trailing_slash_iss_returns_user(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(iss="http://localhost:9000/application/o/keenyspace/"))
    result = await client.validate_access_token(token)
    assert result is not None, "trailing-slash iss should validate and return a User"
    assert result.source == "oidc"


@pytest.mark.asyncio
async def test_no_slash_iss_returns_user(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(iss="http://localhost:9000/application/o/keenyspace"))
    result = await client.validate_access_token(token)
    assert result is not None, "no-slash iss should validate and return a User"
    assert result.source == "oidc"


@pytest.mark.asyncio
async def test_wrong_iss_returns_none_and_warns(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(iss="http://wrong-idp.example.com/keenyspace"))
    with structlog.testing.capture_logs() as cap:
        result = await client.validate_access_token(token)
    assert result is None, "wrong iss should return None"
    events = [e["event"] for e in cap]
    assert any(e == "auth.token.iss_mismatch" for e in events), (
        f"Expected auth.token.iss_mismatch warn; captured events: {events}"
    )


@pytest.mark.asyncio
async def test_missing_iss_returns_none(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(iss=None))
    result = await client.validate_access_token(token)
    assert result is None, "missing iss claim should return None (isinstance guard)"
