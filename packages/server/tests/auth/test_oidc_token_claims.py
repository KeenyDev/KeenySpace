"""OidcClient.validate_access_token: nbf enforcement and ID-token rejection.

Authentik issues the access token as the ID token's claims plus `scope`/`azp`/
`uid`; the ID token has no `scope`. Both share signer, issuer and audience, so
the missing `scope` claim is what keeps an ID token from being used as a bearer.
"""

from __future__ import annotations

import time

import pytest
import structlog.testing

from tests.auth.conftest import _auth, _fetch_keyset, _make_client

ISSUER = "http://localhost:9000/application/o/keenyspace/"


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "u-claims",
        "aud": "keenyspace-cli",
        "scope": "openid profile email groups",
        "exp": now + 3600,
        "iat": now,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.mark.asyncio
async def test_future_nbf_is_rejected(mock_authentik_provider) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    token = mock_authentik_provider["sign_jwt"](_claims(nbf=int(time.time()) + 600))

    assert await client.validate_access_token(token) is None


@pytest.mark.asyncio
async def test_nbf_within_leeway_is_accepted(mock_authentik_provider) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    token = mock_authentik_provider["sign_jwt"](_claims(nbf=int(time.time()) + 5))

    user = await client.validate_access_token(token)
    assert user is not None
    assert user.sub == "u-claims"


@pytest.mark.asyncio
async def test_past_nbf_is_accepted(mock_authentik_provider) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    token = mock_authentik_provider["sign_jwt"](_claims(nbf=int(time.time()) - 60))

    assert await client.validate_access_token(token) is not None


@pytest.mark.asyncio
async def test_id_token_shaped_jwt_is_rejected(mock_authentik_provider) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    id_token = mock_authentik_provider["sign_jwt"](
        _claims(scope=None, nonce="n-1", at_hash="abc", groups=["keenyspace-admins"])
    )

    with structlog.testing.capture_logs() as logs:
        result = await client.validate_access_token(id_token)

    assert result is None
    assert any(
        e["event"] == "auth.token.not_access_token" and e["reason"] == "missing_scope_claim"
        for e in logs
    ), logs


@pytest.mark.asyncio
async def test_non_string_scope_is_rejected(mock_authentik_provider) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    token = mock_authentik_provider["sign_jwt"](_claims(scope=["openid"]))

    assert await client.validate_access_token(token) is None


@pytest.mark.asyncio
async def test_client_credentials_access_token_without_requested_scopes_is_accepted(
    mock_authentik_provider,
) -> None:
    client = _make_client(_auth(), _fetch_keyset(mock_authentik_provider["jwks_uri"]))
    token = mock_authentik_provider["sign_jwt"](_claims(scope="", azp="keenyspace-cli"))

    assert await client.validate_access_token(token) is not None
