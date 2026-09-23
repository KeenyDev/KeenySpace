"""Groups claim extraction in OidcClient.validate_access_token.

Four test cases exercise groups claim coercion:
  1. groups claim populated from token -> User.groups == ["keenyspace-users"],
     groups_seen_at set (the token is a source for the owner's group snapshot).
  2. groups claim absent -> User.groups == [], groups_seen_at None (no snapshot).
  3. groups claim is a non-list -> User.groups == [].
  4. groups claim has non-string members -> only strings kept.
"""

from __future__ import annotations

import time

import pytest

from tests.auth.conftest import _auth, _fetch_keyset, _make_client


def _valid_claims(
    *,
    iss: str | None = "http://localhost:9000/application/o/keenyspace/",
    sub: str = "u-groups-test",
    groups: object = None,
) -> dict:
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
    if groups is not None:
        claims["groups"] = groups
    return claims


@pytest.mark.asyncio
async def test_groups_claim_populated_from_token(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(groups=["keenyspace-users"]))
    result = await client.validate_access_token(token)
    assert result is not None
    assert result.groups == ["keenyspace-users"]
    assert result.groups_seen_at is not None


@pytest.mark.asyncio
async def test_groups_claim_absent_defaults_to_empty_list(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims())
    result = await client.validate_access_token(token)
    assert result is not None
    assert result.groups == []
    assert result.groups_seen_at is None


@pytest.mark.asyncio
async def test_groups_claim_non_list_coerced_to_empty(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(groups="not-a-list"))
    result = await client.validate_access_token(token)
    assert result is not None
    assert result.groups == []


@pytest.mark.asyncio
async def test_groups_claim_filters_non_strings(mock_authentik_provider) -> None:
    keyset = _fetch_keyset(mock_authentik_provider["jwks_uri"])
    sign_jwt = mock_authentik_provider["sign_jwt"]
    client = _make_client(_auth(), keyset)

    token = sign_jwt(_valid_claims(groups=[1, "ok", None]))
    result = await client.validate_access_token(token)
    assert result is not None
    assert result.groups == ["ok"]
