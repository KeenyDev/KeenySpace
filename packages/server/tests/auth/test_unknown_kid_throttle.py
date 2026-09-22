"""Random-kid tokens must not turn into one IdP JWKS fetch per request."""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from authlib.integrations.starlette_client import OAuth
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet
from keenyspace_server.auth.jwks_cache import JwksCache
from keenyspace_server.auth.oidc import OidcClient
from keenyspace_server.config import AuthSettings

ISSUER = "http://localhost:9000/application/o/keenyspace/"


def _auth() -> AuthSettings:
    return AuthSettings(
        oidc_issuer_url=ISSUER,
        oidc_client_id="keenyspace-cli",
        oidc_client_secret="secret",
        oidc_redirect_uri="http://localhost:8000/v1/api/auth/callback",
        oidc_post_logout_redirect_uri="http://localhost:8000/",
        session_secret_key="session-secret-32chars-padded-here!",
        api_key_pepper="pepper-32chars-padded-here-xxxxx!",
    )


def _claims() -> dict[str, object]:
    now = int(time.time())
    return {
        "sub": "u1",
        "aud": "keenyspace-cli",
        "iss": ISSUER,
        "exp": now + 3600,
        "iat": now,
        "scope": "openid",
    }


def _random_kid_token() -> str:
    kid = uuid.uuid4().hex
    key = ECKey.generate_key("P-256", parameters={"kid": kid})
    return jwt.encode({"alg": "ES256", "kid": kid}, _claims(), key)


@pytest.mark.asyncio
async def test_random_kid_tokens_fetch_jwks_at_most_once_per_interval() -> None:
    known = ECKey.generate_key("P-256", parameters={"kid": "known"})
    jwks = KeySet([known]).as_dict(private=False)

    client = OidcClient(OAuth(), _auth())
    client._jwks_cache = JwksCache(
        AsyncMock(return_value="https://idp/jwks"),
        ttl_seconds=3600,
        min_retry_interval_seconds=30,
    )

    fake_resp = MagicMock()
    fake_resp.json.return_value = jwks
    fake_resp.raise_for_status = MagicMock()
    with patch("keenyspace_server.auth.jwks_cache.httpx.AsyncClient") as mock_cli:
        mock_cli.return_value.__aenter__.return_value.get = AsyncMock(return_value=fake_resp)
        results = await asyncio.gather(
            *(client.validate_access_token(_random_kid_token()) for _ in range(25))
        )
        assert mock_cli.call_count == 1

        valid = jwt.encode({"alg": "ES256", "kid": "known"}, _claims(), known)
        user = await client.validate_access_token(valid)
        assert mock_cli.call_count == 1

    assert all(r is None for r in results)
    assert user is not None
